"""Discover a single local PAPER Gateway or TWS using read-only requests."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Callable

from src.trading.connectors.ibkr.local import IBKRLocalConfig, get_account_snapshot, tcp_port_open


class PaperGatewayDiscoveryError(RuntimeError):
    """No unambiguous local PAPER account could be confirmed."""


def discover_local_paper_gateway(*, port_open: Callable = tcp_port_open,
                                 snapshot: Callable = get_account_snapshot) -> IBKRLocalConfig:
    """Return an exact read-only PAPER profile; never probe LIVE ports or send orders."""
    found = []
    saw_error = False
    for port in (4002, 7497):
        if not port_open("127.0.0.1", port):
            continue
        cfg = IBKRLocalConfig(profile="paper", host="127.0.0.1", port=port, account=None, readonly=True)
        try:
            result = snapshot(config=cfg)
            accounts = result.get("accounts") if isinstance(result, dict) and result.get("status") == "ok" else None
            if (not isinstance(accounts, list) or len(accounts) != 1 or not isinstance(accounts[0], str)
                    or re.fullmatch(r"DU[A-Z0-9]+", accounts[0]) is None):
                raise ValueError("expected one PAPER account")
            found.append(replace(cfg, account=accounts[0]))
        except Exception:
            saw_error = True
    if saw_error:
        raise PaperGatewayDiscoveryError("PAPER connection could not be verified; check Gateway login and API settings")
    if len(found) != 1:
        raise PaperGatewayDiscoveryError("expected exactly one local PAPER Gateway or TWS session")
    return found[0]
