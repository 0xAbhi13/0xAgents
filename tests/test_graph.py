"""Tests for 0xAgents (agentgraph). Run with: pytest -q"""

import operator
import os
import tempfile

from agentgraph import (
    END,
    ConsoleTracer,
    InMemoryCheckpointer,
    JsonFileCheckpointer,
    NodeInterrupt,
    StateGraph,
)
from agentgraph.observability import CallbackTracer


def make_linear():
    g = StateGraph()

    def a(state):
        return {"x": state.get("x", 0) + 1}

    def b(state):
        return {"x": state["x"] * 10}

    g.add_node("a", a)
    g.add_node("b", b)
    g.add_edge("a", "b")
    g.add_edge("b", END)
    g.set_entry_point("a")
    return g


def test_linear_graph():
    app = make_linear().compile()
    out = app.invoke({"x": 0}, config={"thread_id": "t1"})
    assert out["x"] == 10, out


def test_stateful_reducer_appends():
    g = StateGraph(channels={"messages": operator.add})

    def n1(state):
        return {"messages": ["hi"]}

    def n2(state):
        return {"messages": ["there"]}

    g.add_node("n1", n1)
    g.add_node("n2", n2)
    g.add_edge("n1", "n2")
    g.add_edge("n2", END)
    g.set_entry_point("n1")
    app = g.compile()
    out = app.invoke({"messages": ["start"]}, config={"thread_id": "mem"})
    assert out["messages"] == ["start", "hi", "there"], out


def test_conditional_branching():
    g = StateGraph()

    g.add_node("start", lambda s: {})
    g.add_node("pos", lambda s: {"label": "positive"})
    g.add_node("neg", lambda s: {"label": "negative"})
    g.add_conditional_edges(
        "start", lambda s: "pos" if s.get("v", 0) > 0 else "neg",
    )
    g.add_edge("pos", END)
    g.add_edge("neg", END)
    g.set_entry_point("start")
    app = g.compile()
    assert app.invoke({"v": 5}, config={"thread_id": "b1"})["label"] == "positive"
    assert app.invoke({"v": -2}, config={"thread_id": "b2"})["label"] == "negative"


def test_interrupt_before_and_edit_resume():
    g = make_linear()
    app = g.compile(checkpointer=InMemoryCheckpointer(), interrupt_before=["b"])
    cfg = {"thread_id": "hitl-1"}

    paused = app.invoke({"x": 1}, config=cfg)
    assert "__interrupt__" in paused, paused
    assert paused["x"] == 2  # 'a' ran, 'b' paused
    snap = app.get_state(cfg)
    assert list(snap.next_nodes) == ["b"]

    # human edits state before approving
    app.update_state(cfg, {"x": 5})
    final = app.invoke(None, config=cfg)
    assert "__interrupt__" not in final
    assert final["x"] == 50, final  # 5 * 10


def test_interrupt_after():
    g = make_linear()
    app = g.compile(checkpointer=InMemoryCheckpointer(), interrupt_after=["a"])
    cfg = {"thread_id": "hitl-2"}
    paused = app.invoke({"x": 0}, config=cfg)
    assert "__interrupt__" in paused
    assert paused["x"] == 1
    final = app.invoke(None, config=cfg)
    assert final["x"] == 10


def test_dynamic_node_interrupt():
    g = StateGraph()

    def gate(state):
        if not state.get("approved"):
            raise NodeInterrupt("need approval")
        return {"done": True}

    g.add_node("gate", gate)
    g.add_edge("gate", END)
    g.set_entry_point("gate")
    app = g.compile(checkpointer=InMemoryCheckpointer())
    cfg = {"thread_id": "dyn"}
    paused = app.invoke({}, config=cfg)
    assert "__interrupt__" in paused
    app.update_state(cfg, {"approved": True})
    final = app.invoke(None, config=cfg)
    assert final.get("done") is True


def test_durable_resume_across_restart():
    with tempfile.TemporaryDirectory() as d:
        calls = []

        def step1(state):
            calls.append("s1")
            return {"n": 1}

        def step2(state):
            calls.append("s2")
            return {"n": 2}

        def build(checkpointer):
            g = StateGraph()
            g.add_node("s1", step1)
            g.add_node("s2", step2)
            g.add_edge("s1", "s2")
            g.add_edge("s2", END)
            g.set_entry_point("s1")
            return g.compile(checkpointer=checkpointer, interrupt_after=["s1"])

        # run 1: pauses after s1, persisted to disk
        app1 = build(JsonFileCheckpointer(d))
        cfg = {"thread_id": "long-run"}
        paused = app1.invoke({}, config=cfg)
        assert "__interrupt__" in paused
        assert os.path.exists(os.path.join(d, "long-run.json"))
        del app1  # simulate crash

        # run 2: brand-new process resumes same thread, s1 must NOT re-run
        app2 = build(JsonFileCheckpointer(d))
        snap = app2.get_state(cfg)
        assert snap.values["n"] == 1
        final = app2.invoke(None, config=cfg)
        assert final["n"] == 2
        assert calls == ["s1", "s2"], calls


def test_retry_and_observability():
    attempts = []
    events = []

    def flaky(state):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("boom")
        return {"ok": True}

    g = StateGraph()
    g.add_node("flaky", flaky, retry=3)
    g.add_edge("flaky", END)
    g.set_entry_point("flaky")
    tracer = CallbackTracer(lambda e: events.append(e.type))
    app = g.compile(tracers=[tracer, ConsoleTracer()])
    out = app.invoke({}, config={"thread_id": "retry"})
    assert out["ok"] is True
    assert len(attempts) == 3
    assert "node_end" in events


def test_stream_and_history():
    app = make_linear().compile()
    cfg = {"thread_id": "stream"}
    seen = [e for e in app.stream({"x": 1}, config=cfg) if e["type"] == "update"]
    assert [e["node"] for e in seen] == ["a", "b"]
    hist = app.get_state_history(cfg)
    assert len(hist) == 2
    assert hist[0]["node"] == "a"
