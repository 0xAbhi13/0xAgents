"""Durable long-running agent: kill the process mid-run, resume later."""
import os
from agentgraph import END, JsonFileCheckpointer, StateGraph

DB = os.path.join(os.path.dirname(__file__), "_checkpoints")

def make_app():
    g = StateGraph()
    g.add_node("fetch", lambda s: {"data": "raw"})
    g.add_node("analyze", lambda s: {"report": f"analysis-of-{s['data']}"})
    g.add_node("publish", lambda s: {"published": True})
    g.add_edge("fetch", "analyze")
    g.add_edge("analyze", "publish")
    g.add_edge("publish", END)
    g.set_entry_point("fetch")
    # pause after analyze so you can Ctrl+C / restart and still resume
    return g.compile(checkpointer=JsonFileCheckpointer(DB), interrupt_after=["analyze"])

if __name__ == "__main__":
    app = make_app()
    cfg = {"thread_id": "etl-job-1"}
    print("state before:", app.get_state(cfg).values)
    out = app.invoke({"job": "nightly-etl"}, config=cfg)
    if "__interrupt__" in out:
        print("PAUSED (safe to kill process). state:", {k: v for k, v in out.items() if not k.startswith("__")})
        print("Re-run this file to resume -> publish step.")
    else:
        print("DONE:", out)
