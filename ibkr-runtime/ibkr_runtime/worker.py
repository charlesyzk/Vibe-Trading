"""Trusted child entry point for the isolated official IBKR SDK runtime."""

from __future__ import annotations

import argparse
import sys
from threading import Event, Thread

from .ipc import encode_frame, read_frame, response, validate_request
from .lifecycle import IbapiClientOwner
from .commands import execute as execute_sdk_command


def _status(owner: IbapiClientOwner) -> dict[str, object]:
    is_connected = getattr(owner.client, "isConnected", None)
    return {
        "state": owner.state.value,
        "socket_connected": bool(is_connected()) if callable(is_connected) else owner.state.value == "RUNNING",
        "sdk_api_ready": owner.adapter.api_ready,
        "ready_for_dispatch": False,
        "callback_overflow": owner.overflowed.is_set(),
        "spool_path": owner.spool.path,
        "run_error": type(owner.run_error).__name__ if owner.run_error is not None else None,
    }


def serve(spool_path: str) -> int:
    """Serve private parent commands. Construction does not connect to a broker."""
    owner = IbapiClientOwner(spool_path)
    draining = Event()

    def drain_callback_queue() -> None:
        # The durable spool is the parent/host handoff. Drain this transient
        # queue so a healthy spool consumer can run independently of SDK pace.
        while not draining.is_set():
            try:
                owner.events.get(timeout=0.2)
            except Exception:
                continue

    Thread(target=drain_callback_queue, name="ibkr-callback-queue-drain", daemon=True).start()
    stopped = False
    while True:
        request_id: str | None = None
        try:
            frame = read_frame(sys.stdin.buffer)
            if frame is None:
                # Parent death must close a possibly-open SDK socket.
                try:
                    owner.stop(timeout=2.0)
                except Exception:
                    draining.set()
                    return 2
                draining.set()
                return 0
            candidate_id = frame.get("request_id")
            if isinstance(candidate_id, str) and 16 <= len(candidate_id) <= 128:
                request_id = candidate_id
            request_id, command, payload = validate_request(frame)
            if command == "status":
                result = _status(owner)
            elif command == "start":
                if stopped:
                    raise RuntimeError("runtime is shutting down")
                owner.start(payload["host"], payload["port"], payload["client_id"])
                result = _status(owner)
            elif command == "stop":
                owner.stop(timeout=payload["timeout"])
                result = _status(owner)
            elif command in {"place_order", "cancel_order", "request_order_ids"}:
                result = execute_sdk_command(owner, command, payload)
            elif command == "confirm_reconciled_stop":
                owner.confirm_reconciled_stop()
                result = _status(owner)
            elif command == "request_reconciliation":
                result = {"snapshot_id": owner.request_reconciliation_snapshot(payload["snapshot_id"])}
            elif command == "shutdown":
                if owner.state.value not in ("NEW", "STOPPED"):
                    raise RuntimeError("runtime must be safely stopped before process shutdown")
                owner.stop(timeout=2.0)
                stopped = True
                result = _status(owner)
            else:  # validate_request already rejects this
                raise RuntimeError("unsupported command")
            out = response(request_id, ok=True, result=result)
            sys.stdout.buffer.write(encode_frame(out))
            sys.stdout.buffer.flush()
            if command == "shutdown":
                draining.set()
                return 0
        except Exception as exc:
            # Keep diagnostics terse and free of host/port or account values.
            if request_id is None:
                return 3
            try:
                sys.stdout.buffer.write(encode_frame(response(request_id, ok=False, error=type(exc).__name__)))
                sys.stdout.buffer.flush()
            except Exception:
                return 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spool", required=True)
    args = parser.parse_args()
    return serve(args.spool)


if __name__ == "__main__":
    raise SystemExit(main())
