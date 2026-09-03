"""Human-in-the-loop: review + approve/edit before a risky tool runs."""
from agentgraph import END, StateGraph

g = StateGraph()

def draft(state):
    return {"draft": f"email to {state.get('to')}: hello!"}

def send_email(state):
    assert state.get("approved"), "must be approved"
    return {"sent": True, "log": f"sent: {state['draft']}"}

g.add_node("draft", draft)
g.add_node("send_email", send_email)
g.add_edge("draft", "send_email")
g.add_edge("send_email", END)
g.set_entry_point("draft")

app = g.compile(interrupt_before=["send_email"])
cfg = {"thread_id": "email-1"}

if __name__ == "__main__":
    paused = app.invoke({"to": "boss@example.com"}, config=cfg)
    print("PAUSED:", paused.get("__interrupt__"))
    print("draft awaiting review:", app.get_state(cfg).values)

    # human reviews, edits, approves:
    app.update_state(cfg, {"draft": "email to boss: polished version", "approved": True})
    final = app.invoke(None, config=cfg)
    print("FINAL:", final)
