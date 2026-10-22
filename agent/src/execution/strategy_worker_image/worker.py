"""One-shot isolated worker for a single completed strategy event.

Uploaded code implements ``on_event(event, state)`` and returns only
``{"state": object, "order_proposals": [...]}``. The host calls this worker
once per journal event, so no future bar or authoritative cursor is present in
the worker process. Docker supplies the OS isolation boundary.
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any


MAX_REQUEST_BYTES = 1_048_576
MAX_SOURCE_BYTES = 262_144


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise ValueError("request too large or unterminated")
    request = json.loads(raw)
    if not isinstance(request, dict) or set(request) != {
        "op", "protocol_version", "request_id", "source", "event_id", "event", "state"
    }:
        raise ValueError("strategy event request has an invalid shape")
    if request["op"] != "strategy_event":
        raise ValueError("unsupported worker operation")
    if type(request["protocol_version"]) is not int or request["protocol_version"] != 1:
        raise ValueError("unsupported strategy IPC protocol version")
    if (
        not isinstance(request["request_id"], str)
        or len(request["request_id"]) != 32
        or any(char not in "0123456789abcdef" for char in request["request_id"])
    ):
        raise ValueError("request_id must be a 32-character host correlation id")
    if not isinstance(request["source"], str) or not request["source"].strip():
        raise ValueError("strategy source must be non-empty UTF-8 text")
    if len(request["source"].encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("strategy source exceeds the byte limit")
    if not isinstance(request["event_id"], str) or not request["event_id"]:
        raise ValueError("event_id must be non-empty")
    if not isinstance(request["event"], dict) or set(request["event"]) != {"bar"}:
        raise ValueError("worker receives exactly one current bar")
    if not isinstance(request["event"]["bar"], dict):
        raise ValueError("bar must be an object")
    if not isinstance(request["state"], dict):
        raise ValueError("checkpoint state must be an object")
    return request


def _json_value(value: Any, name: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"strategy {name} must contain JSON values only") from exc


def main() -> int:
    source_line = None
    try:
        request = _read_request()
        namespace: dict[str, Any] = {"__name__": "__vibe_uploaded_strategy__"}
        # Strategy output must not corrupt the line-oriented JSON response.
        with contextlib.redirect_stdout(sys.stderr):
            try:
                exec(compile(request["source"], "<uploaded-strategy>", "exec"), namespace, namespace)
            except BaseException as exc:
                source_line = _uploaded_source_line(exc)
                raise
            callback = namespace.get("on_event")
            if not callable(callback):
                raise ValueError("strategy source must define on_event(event, state)")
            try:
                output = callback(request["event"], request["state"])
            except BaseException as exc:
                source_line = _uploaded_source_line(exc)
                raise
        if not isinstance(output, dict) or set(output) != {"state", "order_proposals"}:
            raise ValueError("on_event must return exactly state and order_proposals")
        state = _json_value(output["state"], "state")
        proposals = _json_value(output["order_proposals"], "order_proposals")
        if not isinstance(state, dict) or not isinstance(proposals, list):
            raise ValueError("state must be an object and order_proposals must be a list")
        response = {
            "protocol_version": request["protocol_version"],
            "request_id": request["request_id"],
            "event_id": request["event_id"],
            "state": state,
            "order_proposals": proposals,
        }
        print(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        return 0
    except BaseException as exc:
        error_type = type(exc).__name__
        if error_type not in {
            "AssertionError", "AttributeError", "IndexError", "KeyError", "NameError",
            "OverflowError", "TypeError", "ValueError", "ZeroDivisionError",
        }:
            error_type = "OtherError"
        category = "strategy_runtime_error" if source_line is not None else "strategy_contract_error"
        print(json.dumps({
            "worker_error": category,
            "error_type": error_type,
            "source_line": source_line,
        }, separators=(",", ":")))
        return 2


def _uploaded_source_line(exc: BaseException) -> int | None:
    """Return only a source line number, never traceback text or local values."""
    line = getattr(exc, "lineno", None)
    if type(line) is int and line > 0:
        return line
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == "<uploaded-strategy>":
            return tb.tb_lineno
        tb = tb.tb_next
    return None


if __name__ == "__main__":
    raise SystemExit(main())
