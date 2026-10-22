from __future__ import annotations

import unittest
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from ibkr_runtime.ipc import IpcProtocolError, validate_request


class RuntimeIpcTests(unittest.TestCase):
    def test_rejects_unknown_fields_and_invalid_scope_parameters(self) -> None:
        base = {"protocol": 1, "type": "command", "request_id": "r" * 24,
                "command": "status", "payload": {}}
        self.assertEqual(validate_request(base)[1], "status")
        self.assertEqual(validate_request({**base, "command": "request_order_ids"})[1],
                         "request_order_ids")
        for bad in (
            {**base, "actor": "user"},
            {**base, "command": "execute_code"},
            {**base, "command": ["status"]},
            {**base, "command": "start", "payload": {"host": "localhost", "port": 4002,
                                                           "client_id": 0, "account": "DU123"}},
            {**base, "command": "start", "payload": {"host": "localhost", "port": True,
                                                           "client_id": 0}},
        ):
            with self.subTest(bad=bad), self.assertRaises(IpcProtocolError):
                validate_request(bad)

    def test_worker_returns_correlated_protocol_error_and_remains_available(self) -> None:
        runtime_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                [sys.executable, "-m", "ibkr_runtime.worker", "--spool", str(Path(directory) / "spool.sqlite")],
                cwd=runtime_root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            assert process.stdin is not None and process.stdout is not None

            def send(command: dict[str, object]) -> dict[str, object]:
                process.stdin.write(json.dumps(command, separators=(",", ":")).encode("ascii") + b"\n")
                process.stdin.flush()
                line = process.stdout.readline(16 * 1024 + 1)
                return json.loads(line)

            rejected = send({"protocol": 1, "type": "command", "request_id": "a" * 24,
                             "command": "status", "payload": {}, "actor": "user"})
            self.assertFalse(rejected["ok"])
            self.assertEqual(rejected["request_id"], "a" * 24)
            status = send({"protocol": 1, "type": "command", "request_id": "b" * 24,
                           "command": "status", "payload": {}})
            self.assertTrue(status["ok"])
            self.assertEqual(status["result"]["state"], "NEW")
            order_ids = send({"protocol": 1, "type": "command", "request_id": "d" * 24,
                              "command": "request_order_ids", "payload": {}})
            self.assertFalse(order_ids["ok"])
            self.assertEqual(order_ids["error"], "RuntimeError")
            closed = send({"protocol": 1, "type": "command", "request_id": "c" * 24,
                           "command": "shutdown", "payload": {}})
            self.assertTrue(closed["ok"])
            self.assertEqual(process.wait(timeout=3), 0)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()

if __name__ == "__main__":
    unittest.main()
