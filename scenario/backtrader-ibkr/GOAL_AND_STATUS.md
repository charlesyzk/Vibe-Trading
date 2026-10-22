# Backtrader + IBKR integration: goal and handoff

Updated: 2026-10-22

## Goal

Integrate the existing Vibe-Trading application with Backtrader as its backtest engine and IBKR as a read-only market data source. Support both a natural-language strategy entry and manual Python strategy upload. Connect to a user-logged-in IBKR Gateway/TWS session and complete a PAPER data-to-backtest workflow. Defer live trading until a separate authorization and acceptance phase.

## Completed and verified

- The existing Execution page exposes NL2 and Python-upload backtest paths, API calls, IBKR local profile/port selection, review previews, and Backtrader run results.
- The local PAPER IB Gateway at `127.0.0.1:4002` completed an IB API handshake. The account list returned and passed the PAPER account-format check. The account identifiers were masked during the check.
- The IBKR daily provider fetched 23 AAPL `TRADES` bars for 2026-09-21 through 2026-10-21. Those bars passed the existing completed-daily importer and drove a real `Cerebro + BackBroker` NL2 API backtest. The resulting report had `execution integrity=verified`.
- The Python-upload market-data path generated a CSV and pinned XNYS schedule from IBKR bars. A digest-pinned isolated worker then ran a Python upload preview-to-run flow with three real IBKR bars. The run completed with `status=success`, `backend=backtrader`, `provider=ibkr`, and `execution integrity=verified`.
- The local worker image is configured in `~/.vibe-trading/.env` for `scripts/dev`. The development backend was restarted and confirmed to load the configured image digest and `DOCKER_CONTEXT=colima-vibe-execution`. Docker seccomp/image preflight and a worker JSON IPC round-trip passed.
- The focused backend suite passed 46 tests; Execution and Portfolio UI tests passed 23 tests; the frontend production build succeeded. The worker setup smoke check and `git diff --check` also passed.

## Not completed or not re-verified

- The returned Gateway account list was not compared to the user-designated account. Do not record or publish account IDs in this handoff. Account balances and positions were not queried in this round.
- The current PAPER endpoint was `4002`. The separate `4001` endpoint was not checked in this round. Standard IB Gateway installations commonly use `4001` for LIVE and `4002` for PAPER; confirm the environment shown by the installed Gateway before connecting.
- Live quote delivery was not part of the latest validation. Existing prior evidence reported EUR/USD ticks received and AAPL rejected with IBKR error `10089` because the relevant API market-data subscription was missing. Treat AAPL streaming as unavailable until retested with the required subscription.
- A prior what-if result exists in the user's earlier acceptance notes, but it was not repeated during this validation. No what-if request or order was sent in the latest round.
- IBKR market-data licensing, point-in-time availability, corporate-action adjustments, and contract/calendar semantics have not been independently certified.
- The worker image configuration is local to this machine and Docker daemon. Other hosts, packaged installs, and deployment environments need their own trusted image build and digest configuration.
- This validates the sample/template and API path. The user's own strategy file still needs a separate upload and review run.
- PAPER order submission, acknowledgement, fills, positions, restart recovery, and reconciliation remain outside the completed backtest workflow. Live trading remains deferred.

## Next continuation steps

1. Recheck the PAPER Gateway handshake and compare the returned account identity locally against the intended account without writing the identifier to logs or committed files.
2. If account and position display is a required acceptance target, perform a read-only account/position query and record only masked identifiers and aggregate evidence.
3. Have the user select one representative Python strategy file; run it through upload preview and confirm using a small, closed historical window. Verify the frozen data snapshot, Backtrader report, and integrity sidecar.
4. Decide whether streaming data is in scope for the backtest MVP. If so, add the required IBKR subscriptions and retest the intended instruments; preserve the `10089` limitation until that succeeds.
5. Keep live trading deferred. Before any PAPER order test, separately specify the exact account, order type, size bounds, what-if behavior, cancellation, callback/fill handling, and reconciliation evidence. Do not treat the backtest connection as trading authorization.

## Completion line for the current MVP

The backtest MVP is complete when an operator can log into PAPER Gateway, select the PAPER IBKR profile, fetch a bounded historical window, review the frozen data and strategy, run NL2 or an uploaded Python strategy on Backtrader, and inspect a verified report. Current evidence meets this line for sample strategies and the local development app. Account identity matching and a user-provided strategy run remain the next acceptance checks.
