"""Observability: tiny tracer interface + built-in tracers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class GraphEvent:
    type: str  # graph_start | node_start | node_end | node_error | checkpoint | graph_end | interrupt
    node: Optional[str] = None
    thread_id: str = "default"
    step: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=_utcnow)


class Tracer:
    """Implement on_event() to ship traces to LangSmith/logging/etc."""

    def on_event(self, event: GraphEvent) -> None:
        raise NotImplementedError


class NullTracer(Tracer):
    def on_event(self, event: GraphEvent) -> None:
        return None


class ConsoleTracer(Tracer):
    """Print human-readable progress lines. Great default for dev."""

    def __init__(self, verbose_state: bool = False):
        self.verbose_state = verbose_state

    def on_event(self, event: GraphEvent) -> None:
        if event.type == "graph_start":
            print(f"[graph] start thread={event.thread_id}")
        elif event.type == "node_start":
            print(f"[graph] → {event.node} (step {event.step})")
        elif event.type == "node_end":
            if self.verbose_state:
                print(f"[graph] ✓ {event.node} update={event.payload.get('update')}")
            else:
                print(f"[graph] ✓ {event.node}")
        elif event.type == "node_error":
            print(f"[graph] ✗ {event.node} error={event.payload.get('error')}")
        elif event.type == "interrupt":
            print(f"[graph] ⏸ paused at {event.node}: {event.payload.get('reason')}")
        elif event.type == "graph_end":
            print(f"[graph] done thread={event.thread_id} steps={event.step}")
        elif event.type == "checkpoint":
            pass  # too noisy by default


class CallbackTracer(Tracer):
    def __init__(self, fn: Callable[[GraphEvent], None]):
        self._fn = fn

    def on_event(self, event: GraphEvent) -> None:
        self._fn(event)
