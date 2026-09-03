"""Graph engine: define agent logic as a flowchart of nodes + edges.

Key ideas (low-level, explicit, observable):
- Nodes are plain callables: ``fn(state: dict) -> dict`` (partial update).
- State is a dict merged after every node. Per-key reducers (channels)
  control merging, e.g. ``{"messages": operator.add}`` appends instead
  of replacing — that is how agents "remember".
- Edges are static (A -> B) or conditional (route function -> node).
- Every node execution checkpoints, so crashes/resumes are safe.
- Human-in-the-loop via ``interrupt_before`` / ``interrupt_after`` or by
  raising :class:`NodeInterrupt` inside a node.
"""

from __future__ import annotations

import copy
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .checkpoint import BaseCheckpointer, InMemoryCheckpointer
from .interrupts import HumanInterrupt, NodeInterrupt
from .observability import GraphEvent, NullTracer, Tracer

START = "__start__"
END = "__end__"
INTERRUPT_KEY = "__interrupt__"

StateUpdate = Dict[str, Any]
NodeFn = Callable[[Dict[str, Any]], Optional[StateUpdate]]
RouteFn = Callable[[Dict[str, Any]], str]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _replace(old: Any, new: Any) -> Any:
    return new


@dataclass
class _NodeSpec:
    fn: NodeFn
    retry: int = 0
    retry_delay: float = 0.0


@dataclass
class StateSnapshot:
    """Point-in-time view returned by get_state()."""

    values: Dict[str, Any]
    next_nodes: Tuple[str, ...]
    step: int
    thread_id: str
    checkpoint_id: Optional[str] = None
    pending_interrupt: Optional[Dict[str, Any]] = None
    history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def interrupted(self) -> bool:
        return self.pending_interrupt is not None


class StateGraph:
    """Builder for an agent flowchart. Compile it to run."""

    def __init__(self, channels: Optional[Dict[str, Callable[[Any, Any], Any]]] = None):
        # channels: state_key -> reducer(old, new). Default = replace.
        self.channels: Dict[str, Callable[[Any, Any], Any]] = dict(channels or {})
        self.nodes: Dict[str, _NodeSpec] = {}
        self.edges: Dict[str, str] = {}
        self.conditional_edges: Dict[str, Tuple[RouteFn, Optional[Dict[str, str]]]] = {}
        self.entry_point: Optional[str] = None

    # -- definition -----------------------------------------------------
    def add_node(self, name: str, fn: NodeFn, retry: int = 0, retry_delay: float = 0.0) -> "StateGraph":
        if not name or name in (START, END):
            raise ValueError(f"Invalid node name: {name!r}")
        if name in self.nodes:
            raise ValueError(f"Node already exists: {name!r}")
        if not callable(fn):
            raise TypeError("Node fn must be callable(state) -> dict|None")
        self.nodes[name] = _NodeSpec(fn=fn, retry=max(0, int(retry)), retry_delay=max(0.0, float(retry_delay)))
        return self

    def add_edge(self, frm: str, to: str) -> "StateGraph":
        self._check_endpoint(frm, allow_start=True)
        self._check_endpoint(to, allow_end=True)
        if frm in self.conditional_edges:
            raise ValueError(f"{frm!r} already has conditional edges")
        if frm in self.edges:
            raise ValueError(f"{frm!r} already has an edge; use conditional edges for branching")
        self.edges[frm] = to
        return self

    def add_conditional_edges(
        self, source: str, condition: RouteFn, mapping: Optional[Dict[str, str]] = None
    ) -> "StateGraph":
        if source not in self.nodes and source != START:
            raise ValueError(f"Unknown source node: {source!r}")
        if not callable(condition):
            raise TypeError("condition must be callable(state) -> str")
        if source in self.edges:
            raise ValueError(f"{source!r} already has a static edge")
        if mapping:
            for route, target in mapping.items():
                if target != END and target not in self.nodes:
                    raise ValueError(f"Mapping {route!r} -> unknown node {target!r}")
        self.conditional_edges[source] = (condition, dict(mapping) if mapping else None)
        return self

    def set_entry_point(self, name: str) -> "StateGraph":
        if name not in self.nodes:
            raise ValueError(f"Unknown entry node: {name!r}")
        self.entry_point = name
        return self

    def compile(
        self,
        checkpointer: Optional[BaseCheckpointer] = None,
        interrupt_before: Optional[List[str]] = None,
        interrupt_after: Optional[List[str]] = None,
        tracers: Optional[List[Tracer]] = None,
        recursion_limit: int = 100,
    ) -> "CompiledGraph":
        if not self.entry_point:
            raise ValueError("No entry point set. Call set_entry_point().")
        for src, dst in self.edges.items():
            if dst != END and dst not in self.nodes:
                raise ValueError(f"Edge {src!r} -> unknown node {dst!r}")
        before = set(interrupt_before or [])
        after = set(interrupt_after or [])
        for n in before | after:
            if n not in self.nodes:
                raise ValueError(f"Interrupt on unknown node: {n!r}")
        return CompiledGraph(
            graph=self,
            checkpointer=checkpointer or InMemoryCheckpointer(),
            interrupt_before=before,
            interrupt_after=after,
            tracers=tracers or [NullTracer()],
            recursion_limit=int(recursion_limit),
        )

    def _check_endpoint(self, name: str, allow_start: bool = False, allow_end: bool = False) -> None:
        if name in (START, END):
            if name == START and not allow_start:
                raise ValueError("START cannot be an edge target here")
            if name == END and not allow_end:
                raise ValueError("END cannot be an edge source here")
            return
        if name not in self.nodes:
            raise ValueError(f"Unknown node: {name!r}")


class CompiledGraph:
    """Executable, durable, stateful agent. Created via StateGraph.compile()."""

    def __init__(
        self,
        graph: StateGraph,
        checkpointer: BaseCheckpointer,
        interrupt_before: Set[str],
        interrupt_after: Set[str],
        tracers: List[Tracer],
        recursion_limit: int = 100,
    ):
        self.graph = graph
        self.checkpointer = checkpointer
        self.interrupt_before = set(interrupt_before)
        self.interrupt_after = set(interrupt_after)
        self.tracers = tracers
        self.recursion_limit = max(1, recursion_limit)

    # -- helpers --------------------------------------------------------
    def _emit(self, event: GraphEvent) -> None:
        for t in self.tracers:
            try:
                t.on_event(event)
            except Exception:
                pass  # tracers must never break execution

    @staticmethod
    def _thread_id(config: Optional[Dict[str, Any]]) -> str:
        if not config:
            return "default"
        if "thread_id" in config and isinstance(config["thread_id"], str):
            return config["thread_id"]
        conf = config.get("configurable")
        if isinstance(conf, dict) and isinstance(conf.get("thread_id"), str):
            return conf["thread_id"]
        return "default"

    def _merge(self, current: Dict[str, Any], update: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not update:
            return dict(current)
        merged = dict(current)
        for k, v in update.items():
            if k == INTERRUPT_KEY:
                continue  # reserved; never store interrupts as state
            reducer = self.graph.channels.get(k, _replace)
            if k in merged:
                try:
                    merged[k] = reducer(merged[k], v)
                except Exception:
                    merged[k] = v
            else:
                merged[k] = copy.deepcopy(v)
        return merged

    def _resolve_next(self, node: str, state: Dict[str, Any]) -> List[str]:
        if node in self.graph.conditional_edges:
            cond, mapping = self.graph.conditional_edges[node]
            route = cond(state)
            if not isinstance(route, str):
                raise TypeError(f"Condition from {node!r} must return str, got {type(route)}")
            target = mapping.get(route, route) if mapping else route
            if target == END:
                return [END]
            if target not in self.graph.nodes:
                raise ValueError(f"Condition from {node!r} routed to unknown node {target!r}")
            return [target]
        target = self.graph.edges.get(node)
        if target is None:
            return [END]  # leaf nodes finish the run
        return [target]

    def _blank_record(self, thread_id: str) -> Dict[str, Any]:
        return {
            "thread_id": thread_id,
            "values": {},
            "next_nodes": [],
            "step": 0,
            "checkpoint_id": uuid.uuid4().hex,
            "parent_id": None,
            "pending_interrupt": None,
            "history": [],
        }

    def _save(self, record: Dict[str, Any]) -> str:
        record["checkpoint_id"] = uuid.uuid4().hex
        cid = self.checkpointer.save(record["thread_id"], record)
        self._emit(GraphEvent(type="checkpoint", thread_id=record["thread_id"], step=record["step"]))
        return cid

    # -- public API -----------------------------------------------------
    def invoke(
        self, input: Optional[Dict[str, Any]] = None, config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Run until END or human interrupt. Returns final state (or state + __interrupt__)."""
        result: Dict[str, Any] = {}
        for event in self._run(input, config, yield_events=False):
            if event["type"] == "final":
                result = event["state"]
            elif event["type"] == INTERRUPT_KEY:
                result = event["state"]
        return result

    def stream(
        self, input: Optional[Dict[str, Any]] = None, config: Optional[Dict[str, Any]] = None
    ):
        """Yield per-step updates: {node, update, state, step} + final interrupt event."""
        yield from self._run(input, config, yield_events=True)

    def get_state(self, config: Optional[Dict[str, Any]] = None) -> StateSnapshot:
        thread_id = self._thread_id(config)
        rec = self.checkpointer.load(thread_id) or self._blank_record(thread_id)
        return StateSnapshot(
            values=copy.deepcopy(rec.get("values", {})),
            next_nodes=tuple(rec.get("next_nodes", [])),
            step=int(rec.get("step", 0)),
            thread_id=thread_id,
            checkpoint_id=rec.get("checkpoint_id"),
            pending_interrupt=copy.deepcopy(rec.get("pending_interrupt")),
            history=copy.deepcopy(rec.get("history", [])),
        )

    def update_state(
        self,
        config: Optional[Dict[str, Any]],
        values: Dict[str, Any],
        as_node: Optional[str] = None,
    ) -> StateSnapshot:
        """Human edit: merge values into checkpoint; optionally rewind to `as_node`.

        Use this to approve-with-edits before resuming:
            app.update_state(cfg, {"approved": True})
            app.invoke(None, cfg)
        """
        if not isinstance(values, dict):
            raise TypeError("values must be a dict")
        thread_id = self._thread_id(config)
        rec = self.checkpointer.load(thread_id) or self._blank_record(thread_id)
        rec["values"] = self._merge(rec.get("values", {}), values)
        if as_node is not None:
            if as_node != END and as_node not in self.graph.nodes:
                raise ValueError(f"Unknown node: {as_node!r}")
            rec["next_nodes"] = [as_node]
            rec["pending_interrupt"] = None  # rewinding clears a stale pause
        self._save(rec)
        return self.get_state(config)

    def get_state_history(self, config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        thread_id = self._thread_id(config)
        rec = self.checkpointer.load(thread_id)
        if not rec:
            return []
        return copy.deepcopy(rec.get("history", []))

    # -- engine ---------------------------------------------------------
    def _run(self, input, config, yield_events: bool):
        thread_id = self._thread_id(config)
        rec = self.checkpointer.load(thread_id)

        if rec is None:
            rec = self._blank_record(thread_id)
            rec["values"] = copy.deepcopy(input or {})
            rec["next_nodes"] = [self.graph.entry_point]  # type: ignore[list-item]
            self._save(rec)
        else:
            # Resume: fresh input (non-empty dict) merges into saved state.
            if isinstance(input, dict) and input:
                rec["values"] = self._merge(rec.get("values", {}), input)
                self._save(rec)
            # A fresh empty-thread (no history, at entry) + new input replaces state.
            elif input is not None and not rec.get("history") and rec.get("next_nodes") == [self.graph.entry_point]:
                if isinstance(input, dict):
                    rec["values"] = copy.deepcopy(input)
                    self._save(rec)

        self._emit(GraphEvent(type="graph_start", thread_id=thread_id, step=rec["step"]))
        queue: deque = deque(rec.get("next_nodes", []))
        steps = 0

        while queue:
            if steps >= self.recursion_limit:
                raise RecursionError(
                    f"Recursion limit ({self.recursion_limit}) exceeded — possible infinite loop. "
                    "Increase recursion_limit or fix graph routing."
                )
            current = queue.popleft()
            if current == END:
                continue

            pending = rec.get("pending_interrupt")
            bypass_before = bool(
                pending
                and pending.get("when") == "before"
                and pending.get("node") == current
            )
            if bypass_before:
                rec["pending_interrupt"] = None  # human resumed; run it now

            # --- human-in-the-loop: pause BEFORE the node -----------------
            if current in self.interrupt_before and not bypass_before:
                reason = f"interrupt_before {current!r}: awaiting human review"
                interrupt = HumanInterrupt(
                    node=current, reason=reason, thread_id=thread_id,
                    state_snapshot=copy.deepcopy(rec["values"]), when="before",
                )
                rec["next_nodes"] = [current] + list(queue)
                rec["pending_interrupt"] = {"node": current, "when": "before", "reason": reason}
                self._save(rec)
                self._emit(GraphEvent(type="interrupt", node=current, thread_id=thread_id,
                                      step=rec["step"], payload={"reason": reason}))
                if yield_events:
                    yield {"type": INTERRUPT_KEY, INTERRUPT_KEY: [interrupt.to_dict()],
                           "node": current, "state": copy.deepcopy(rec["values"]), "step": rec["step"]}
                else:
                    yield {"type": INTERRUPT_KEY,
                           "state": {**copy.deepcopy(rec["values"]), INTERRUPT_KEY: [interrupt.to_dict()]}}
                return

            # --- execute node (with retry + dynamic interrupt) ------------
            spec = self.graph.nodes.get(current)
            if spec is None:
                raise ValueError(f"Unknown node: {current!r}")
            self._emit(GraphEvent(type="node_start", node=current, thread_id=thread_id, step=rec["step"]))
            update: Optional[Dict[str, Any]] = None
            attempt = 0
            while True:
                try:
                    snapshot = copy.deepcopy(rec["values"])
                    update = spec.fn(snapshot) or {}
                    if not isinstance(update, dict):
                        raise TypeError(f"Node {current!r} must return dict|None, got {type(update)}")
                    break
                except NodeInterrupt as ni:
                    reason = str(ni.value)
                    interrupt = HumanInterrupt(
                        node=current, reason=reason, thread_id=thread_id,
                        state_snapshot=copy.deepcopy(rec["values"]), when="dynamic",
                    )
                    rec["next_nodes"] = [current] + list(queue)
                    rec["pending_interrupt"] = {"node": current, "when": "dynamic", "reason": reason}
                    self._save(rec)
                    self._emit(GraphEvent(type="interrupt", node=current, thread_id=thread_id,
                                          step=rec["step"], payload={"reason": reason}))
                    if yield_events:
                        yield {"type": INTERRUPT_KEY, INTERRUPT_KEY: [interrupt.to_dict()],
                               "node": current, "state": copy.deepcopy(rec["values"]), "step": rec["step"]}
                    else:
                        yield {"type": INTERRUPT_KEY,
                               "state": {**copy.deepcopy(rec["values"]), INTERRUPT_KEY: [interrupt.to_dict()]}}
                    return
                except Exception as exc:  # noqa: BLE001 — robustness: checkpoint then retry/raise
                    self._emit(GraphEvent(type="node_error", node=current, thread_id=thread_id,
                                          step=rec["step"], payload={"error": repr(exc)}))
                    attempt += 1
                    if attempt > spec.retry:
                        rec["next_nodes"] = [current] + list(queue)
                        self._save(rec)  # persist progress so resume doesn't lose prior steps
                        raise
                    if spec.retry_delay:
                        time.sleep(spec.retry_delay)

            parent = rec.get("checkpoint_id")
            rec["values"] = self._merge(rec["values"], update)
            rec["step"] = int(rec.get("step", 0)) + 1
            rec["history"].append({
                "step": rec["step"], "node": current,
                "update": copy.deepcopy(update), "checkpoint_id": parent,
                "ts": _utcnow(),
            })
            steps += 1
            nxt = self._resolve_next(current, rec["values"])
            rec["next_nodes"] = nxt + list(queue)
            self._save(rec)
            self._emit(GraphEvent(type="node_end", node=current, thread_id=thread_id,
                                  step=rec["step"], payload={"update": copy.deepcopy(update)}))
            if yield_events:
                yield {"type": "update", "node": current, "update": copy.deepcopy(update),
                       "state": copy.deepcopy(rec["values"]), "step": rec["step"]}

            # --- human-in-the-loop: pause AFTER the node ------------------
            if current in self.interrupt_after:
                reason = f"interrupt_after {current!r}: awaiting human approval"
                interrupt = HumanInterrupt(
                    node=current, reason=reason, thread_id=thread_id,
                    state_snapshot=copy.deepcopy(rec["values"]), when="after",
                )
                rec["pending_interrupt"] = {"node": current, "when": "after", "reason": reason}
                self._save(rec)
                self._emit(GraphEvent(type="interrupt", node=current, thread_id=thread_id,
                                      step=rec["step"], payload={"reason": reason}))
                if yield_events:
                    yield {"type": INTERRUPT_KEY, INTERRUPT_KEY: [interrupt.to_dict()],
                           "node": current, "state": copy.deepcopy(rec["values"]), "step": rec["step"]}
                else:
                    yield {"type": INTERRUPT_KEY,
                           "state": {**copy.deepcopy(rec["values"]), INTERRUPT_KEY: [interrupt.to_dict()]}}
                return

            queue = deque(rec["next_nodes"])
            rec["pending_interrupt"] = None

        rec["next_nodes"] = [END]
        self._save(rec)
        self._emit(GraphEvent(type="graph_end", thread_id=thread_id, step=rec["step"]))
        yield {"type": "final", "state": copy.deepcopy(rec["values"])}
