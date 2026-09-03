"""Interrupt primitives for human-in-the-loop control."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict


class NodeInterrupt(Exception):
    """Raise inside a node to pause the graph for human review.

    Example:
        def tool_node(state):
            if not state.get("approved"):
                raise NodeInterrupt("Need human approval before calling tool")
            return {"result": call_tool()}
    """

    def __init__(self, value: Any):
        super().__init__(str(value))
        self.value = value


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HumanInterrupt:
    """A pause request surfaced to the caller when the graph stops."""

    node: str
    reason: str
    thread_id: str
    state_snapshot: Dict[str, Any] = field(default_factory=dict)
    when: str = "before"  # "before" | "after" | "dynamic"
    created_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node": self.node,
            "reason": self.reason,
            "thread_id": self.thread_id,
            "when": self.when,
            "created_at": self.created_at,
            "state_snapshot": dict(self.state_snapshot),
        }
