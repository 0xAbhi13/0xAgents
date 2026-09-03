"""Checkpointers: durability layer for long-running agents.

A checkpoint is the full resumable snapshot of a thread after every step:

    {
        "thread_id": str,
        "checkpoint_id": str (uuid),
        "parent_id": str | None,
        "step": int,
        "values": dict (current merged state),
        "next_nodes": [str] (what runs next),
        "pending_interrupt": dict | None,
        "history": [ {step, node, update, checkpoint_id, ts} ],
    }

`InMemoryCheckpointer` is for dev/tests.
`JsonFileCheckpointer` persists one JSON file per thread so a killed
process can resume by recompiling the same graph with the same directory.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _new_id() -> str:
    return uuid.uuid4().hex


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CheckpointRecord:
    thread_id: str
    values: Dict[str, Any] = field(default_factory=dict)
    next_nodes: List[str] = field(default_factory=list)
    step: int = 0
    checkpoint_id: str = field(default_factory=_new_id)
    parent_id: Optional[str] = None
    pending_interrupt: Optional[Dict[str, Any]] = None
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "values": copy.deepcopy(self.values),
            "next_nodes": list(self.next_nodes),
            "step": self.step,
            "checkpoint_id": self.checkpoint_id,
            "parent_id": self.parent_id,
            "pending_interrupt": copy.deepcopy(self.pending_interrupt),
            "history": copy.deepcopy(self.history),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckpointRecord":
        return cls(
            thread_id=data["thread_id"],
            values=data.get("values", {}),
            next_nodes=list(data.get("next_nodes", [])),
            step=int(data.get("step", 0)),
            checkpoint_id=data.get("checkpoint_id") or _new_id(),
            parent_id=data.get("parent_id"),
            pending_interrupt=data.get("pending_interrupt"),
            history=list(data.get("history", [])),
        )


class BaseCheckpointer:
    """Storage interface. Implement load/save/delete to add a backend."""

    def load(self, thread_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def save(self, thread_id: str, record: Dict[str, Any]) -> str:
        raise NotImplementedError

    def delete(self, thread_id: str) -> None:
        raise NotImplementedError

    def list_threads(self) -> List[str]:
        raise NotImplementedError


class InMemoryCheckpointer(BaseCheckpointer):
    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}

    def load(self, thread_id: str) -> Optional[Dict[str, Any]]:
        rec = self._store.get(thread_id)
        return copy.deepcopy(rec) if rec is not None else None

    def save(self, thread_id: str, record: Dict[str, Any]) -> str:
        record = copy.deepcopy(record)
        if not record.get("checkpoint_id"):
            record["checkpoint_id"] = _new_id()
        self._store[thread_id] = record
        return record["checkpoint_id"]

    def delete(self, thread_id: str) -> None:
        self._store.pop(thread_id, None)

    def list_threads(self) -> List[str]:
        return sorted(self._store.keys())


class JsonFileCheckpointer(BaseCheckpointer):
    """File-backed durability. Safe for process restarts/crashes.

    Layout: <directory>/<thread_id>.json (thread ids are sanitized).
    Writes are atomic (temp file + os.replace).
    """

    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)
        os.makedirs(self.directory, exist_ok=True)

    def _path(self, thread_id: str) -> str:
        safe = "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in thread_id)
        if not safe:
            safe = "default"
        return os.path.join(self.directory, f"{safe}.json")

    def load(self, thread_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(thread_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, thread_id: str, record: Dict[str, Any]) -> str:
        record = copy.deepcopy(record)
        if not record.get("checkpoint_id"):
            record["checkpoint_id"] = _new_id()
        # JSON-safety: fall back to repr for exotic values so we never crash persistence.
        try:
            json.dumps(record)
        except TypeError:
            record["values"] = {k: _json_safe(v) for k, v in record.get("values", {}).items()}
            record["history"] = _json_safe(record.get("history", []))
        path = self._path(thread_id)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2, default=str)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return record["checkpoint_id"]

    def delete(self, thread_id: str) -> None:
        path = self._path(thread_id)
        if os.path.exists(path):
            os.remove(path)

    def list_threads(self) -> List[str]:
        out = []
        for name in os.listdir(self.directory):
            if name.endswith(".json"):
                out.append(name[: -len(".json")])
        return sorted(out)


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(v) for v in value]
        return repr(value)
