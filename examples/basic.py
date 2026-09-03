"""Basic flowchart agent: plan -> act -> summarize."""
import operator
from agentgraph import END, StateGraph
from agentgraph.observability import ConsoleTracer

g = StateGraph(channels={"messages": operator.add})

def plan(state):
    return {"messages": ["plan: break task down"], "plan": ["step1", "step2"]}

def act(state):
    return {"messages": ["act: executed tools"], "result": "42"}

def summarize(state):
    return {"messages": [f"summary: result={state.get('result')}"]}

g.add_node("plan", plan)
g.add_node("act", act)
g.add_node("summarize", summarize)
g.add_edge("plan", "act")
g.add_edge("act", "summarize")
g.add_edge("summarize", END)
g.set_entry_point("plan")

app = g.compile(tracers=[ConsoleTracer()])

if __name__ == "__main__":
    out = app.invoke({"messages": ["user: solve it"]}, config={"thread_id": "demo"})
    print("FINAL:", out)
    print("HISTORY:", [h["node"] for h in app.get_state_history({"thread_id": "demo"})])
