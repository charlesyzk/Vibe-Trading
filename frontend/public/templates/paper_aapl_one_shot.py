"""PAPER integration example. Set the date to the one-day evaluation session.

Use only with the configured AAPL BUY 1, LIMIT $400, DAY PAPER check.
The evaluation session must be the latest completed IBKR session when run.
"""


def on_event(event, state):
    if event["bar"]["session_date"] != "2026-10-30" or state.get("submitted", False):
        return {"state": state, "order_proposals": []}
    return {
        "state": {"submitted": True},
        "order_proposals": [{
            "side": "BUY",
            "integer_qty": 1,
            "order_type": "limit",
            "time_in_force": "DAY",
            "limit_price": "400.00",
            "reason": "one-shot PAPER integration example",
        }],
    }
