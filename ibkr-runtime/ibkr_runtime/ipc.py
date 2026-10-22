"""Bounded versioned stdio protocol for the trusted IBKR runtime subprocess.

The pipes are private to a parent-created child. They are not exposed to
strategy workers or HTTP clients. One command is in flight at a time; every
frame is bounded and correlated by a host-generated request id.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, BinaryIO

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_COMMAND_FIELDS: dict[str, set[str]] = {
    "status": set(),
    "start": {"host", "port", "client_id"},
    "stop": {"timeout"},
    "shutdown": set(),
    "confirm_reconciled_stop": set(),
    "request_order_ids": set(),
    "request_reconciliation": {"snapshot_id"},
    "place_order": {"broker_order_id", "environment", "contract", "order"},
    "cancel_order": {"broker_order_id", "environment"},
}


class IpcProtocolError(ValueError):
    """Malformed, oversized, or unsupported runtime IPC frame."""


def encode_frame(value: dict[str, Any]) -> bytes:
    try:
        frame = json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IpcProtocolError("frame is not finite JSON") from exc
    if len(frame) > MAX_FRAME_BYTES:
        raise IpcProtocolError("frame exceeds byte limit")
    return frame + b"\n"


def read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    raw = stream.readline(MAX_FRAME_BYTES + 1)
    if raw == b"":
        return None
    if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise IpcProtocolError("frame exceeds byte limit or is unterminated")
    try:
        value = json.loads(raw[:-1].decode("ascii"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite number")))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise IpcProtocolError("frame is not valid finite JSON") from exc
    if not isinstance(value, dict):
        raise IpcProtocolError("frame must be an object")
    return value


def validate_request(value: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if set(value) != {"protocol", "type", "request_id", "command", "payload"}:
        raise IpcProtocolError("request has unknown or missing fields")
    if type(value["protocol"]) is not int or value["protocol"] != PROTOCOL_VERSION:
        raise IpcProtocolError("unsupported protocol version")
    if value["type"] != "command":
        raise IpcProtocolError("expected command frame")
    request_id = value["request_id"]
    if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
        raise IpcProtocolError("invalid request id")
    command = value["command"]
    if not isinstance(command, str) or command not in _COMMAND_FIELDS:
        raise IpcProtocolError("unsupported command")
    payload = value["payload"]
    if not isinstance(payload, dict) or set(payload) != _COMMAND_FIELDS[command]:
        raise IpcProtocolError("command payload has unknown or missing fields")
    if command == "start":
        host, port, client_id = payload["host"], payload["port"], payload["client_id"]
        if not isinstance(host, str) or not host.strip() or len(host) > 255:
            raise IpcProtocolError("invalid host")
        if type(port) is not int or not 1 <= port <= 65535:
            raise IpcProtocolError("invalid port")
        if type(client_id) is not int or client_id < 0:
            raise IpcProtocolError("invalid client id")
    if command == "stop":
        timeout = payload["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise IpcProtocolError("invalid stop timeout")
    if command == "request_reconciliation":
        if type(payload["snapshot_id"]) is not int or payload["snapshot_id"] <= 0:
            raise IpcProtocolError("snapshot_id must be a positive integer")
    if command in {"place_order", "cancel_order"}:
        if type(payload["broker_order_id"]) is not int or payload["broker_order_id"] <= 0:
            raise IpcProtocolError("broker order id must be a positive integer")
        if payload["environment"] != "paper":
            raise IpcProtocolError("runtime accepts PAPER commands only")
    if command == "place_order":
        contract, order = payload["contract"], payload["order"]
        contract_fields = {"con_id", "symbol", "security_type", "exchange", "primary_exchange", "currency"}
        order_fields = {"action", "quantity", "order_type", "time_in_force", "account_ref", "client_order_id", "outside_rth", "transmit"}
        if not isinstance(contract, dict) or set(contract) != contract_fields:
            raise IpcProtocolError("contract payload has unknown or missing fields")
        if not isinstance(order, dict) or set(order) != order_fields:
            raise IpcProtocolError("order payload has unknown or missing fields")
        if type(contract["con_id"]) is not int or contract["con_id"] <= 0:
            raise IpcProtocolError("contract conId must be a positive integer")
        for key in ("symbol", "primary_exchange"):
            if not isinstance(contract[key], str) or not contract[key].strip() or len(contract[key]) > 32:
                raise IpcProtocolError(f"invalid contract {key}")
        if (
            contract["security_type"] != "STK"
            or contract["exchange"] != "SMART"
            or contract["currency"] != "USD"
        ):
            raise IpcProtocolError("only SMART/USD/STK contracts are accepted")
        if (
            not isinstance(order["action"], str)
            or order["action"] not in {"BUY", "SELL"}
            or type(order["quantity"]) is not int
            or order["quantity"] <= 0
        ):
            raise IpcProtocolError("order must have BUY/SELL and positive whole-share quantity")
        if order["order_type"] != "MKT" or order["time_in_force"] != "DAY":
            raise IpcProtocolError("only MKT/DAY orders are accepted")
        if not isinstance(order["account_ref"], str) or not order["account_ref"].strip() or len(order["account_ref"]) > 128:
            raise IpcProtocolError("configured account reference is required")
        if not isinstance(order["client_order_id"], str) or not order["client_order_id"].strip() or len(order["client_order_id"]) > 64:
            raise IpcProtocolError("client order reference is required")
        if type(order["outside_rth"]) is not bool or order["outside_rth"] is not False:
            raise IpcProtocolError("outside regular trading hours is not supported")
        if type(order["transmit"]) is not bool or order["transmit"] is not True:
            raise IpcProtocolError("transmit must be enabled for an authorized submission")
    return request_id, command, payload


def response(request_id: str, *, ok: bool, result: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
    if ok and error is not None or not ok and (not isinstance(error, str) or not error):
        raise IpcProtocolError("invalid response status")
    return {"protocol": PROTOCOL_VERSION, "type": "response", "request_id": request_id, "ok": ok,
            "result": result or {}, "error": error}


def validate_response(value: dict[str, Any], request_id: str) -> dict[str, Any]:
    if set(value) != {"protocol", "type", "request_id", "ok", "result", "error"}:
        raise IpcProtocolError("response has unknown or missing fields")
    if type(value["protocol"]) is not int or value["protocol"] != PROTOCOL_VERSION or value["type"] != "response":
        raise IpcProtocolError("unsupported response protocol")
    if value["request_id"] != request_id or type(value["ok"]) is not bool:
        raise IpcProtocolError("response identity mismatch")
    if not isinstance(value["result"], dict):
        raise IpcProtocolError("response result must be an object")
    if value["ok"]:
        if value["error"] is not None:
            raise IpcProtocolError("successful response has error")
    elif not isinstance(value["error"], str) or not value["error"]:
        raise IpcProtocolError("failed response has no error")
    return value
