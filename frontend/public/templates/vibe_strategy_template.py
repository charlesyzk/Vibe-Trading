"""Vibe-Trading isolated one-event Python strategy template.

The Docker worker calls this function once per completed daily bar. It receives
the current bar and the previous JSON state only. It does not receive a
Backtrader feed, broker, account, filesystem path, or future bar.
"""


def on_event(event, state):
    """Return a JSON checkpoint and zero or more Market/DAY proposals."""
    bar = event["bar"]
    state = {"events_seen": int(state.get("events_seen", 0)) + 1}
    order_proposals = []

    # Add deterministic rules using this completed bar and JSON state.
    # To request a whole-share order, append for example:
    # order_proposals.append({"side": "BUY", "integer_qty": 1, "reason": "entry rule"})

    return {"state": state, "order_proposals": order_proposals}
