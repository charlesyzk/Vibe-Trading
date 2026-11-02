"""Save and revalidate one non-executable PAPER order draft per account."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from src.execution.paper_order_preparation import PaperOrderSetup, prepare_verified_paper_order
from src.execution.risk import ProposedOrder


def _path(root: Path, account_ref: str) -> Path:
    if not isinstance(account_ref, str) or not account_ref.startswith("DU"):
        raise ValueError("trusted PAPER account is required")
    key = hashlib.sha256(account_ref.encode("utf-8")).hexdigest()
    return root / "paper-order-drafts" / f"{key}.json"


def save_paper_order_draft(*, runtime_root: str | Path, account_ref: str, run_id: str,
                           version_id: str, order: ProposedOrder, setup: PaperOrderSetup) -> dict:
    root = Path(runtime_root)
    prepared = prepare_verified_paper_order(runtime_root=root, run_id=run_id, version_id=version_id,
                                            account_ref=account_ref, order=order)
    setup.validate_order(order)
    record = {"schema_version": 1, "account_ref": account_ref, "run_id": run_id,
              "strategy_version_id": version_id, "order": order.model_dump(mode="json"),
              "setup": setup.model_dump(mode="json"), "report_sha256": prepared.report_sha256,
              "data_manifest_hash": prepared.data_manifest_hash,
              "contract_id": prepared.contract.con_id}
    path = _path(root, account_ref)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("draft directory is unavailable")
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".draft-", delete=False) as stream:
            temp = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return _public(record)


def load_paper_order_draft(*, runtime_root: str | Path, account_ref: str) -> dict | None:
    root = Path(runtime_root)
    path = _path(root, account_ref)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if path.parent.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
        raise ValueError("PAPER order draft is invalid")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("account_ref") != account_ref:
        raise ValueError("PAPER order draft has an invalid account or format")
    order = ProposedOrder.model_validate(record["order"])
    setup = PaperOrderSetup.model_validate(record["setup"])
    prepared = prepare_verified_paper_order(runtime_root=root, run_id=record["run_id"],
                                            version_id=record["strategy_version_id"], account_ref=account_ref, order=order)
    setup.validate_order(order)
    if (prepared.report_sha256 != record.get("report_sha256")
            or prepared.data_manifest_hash != record.get("data_manifest_hash")
            or prepared.contract.con_id != record.get("contract_id")):
        raise ValueError("PAPER order draft evidence has changed; review it again")
    return _public(record)


def _public(record: dict) -> dict:
    return {key: value for key, value in record.items() if key != "account_ref"} | {
        "ready_for_submission": False, "live_risk_checked": False,
    }
