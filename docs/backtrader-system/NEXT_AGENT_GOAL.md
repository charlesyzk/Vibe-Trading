# Goal: Ship a partner-ready Windows PAPER trading app

## Goal prompt

Continue from the repository's current `main` HEAD. First read `AGENTS.md` (if present), `docs/backtrader-system/PROGRESS.md`, `BLOCKERS.md`, `ACCEPTANCE.md`, `DECISIONS.md`, and `PAPER_USER_GUIDE.html`. The current handoff commit is `95c87ad7` (`feat: complete paper strategy execution workflow`). Preserve the work already in the tree and update the evidence docs and HTML manual as you make changes.

Deliver a Windows installer that lets a partner install the app, log in to the existing IB Gateway PAPER account, and use the complete strategy-to-PAPER workflow without manually configuring API tokens, Python environments, Docker, or internal service ports. Keep LIVE trading disabled.

## User decisions and boundaries

- Do **not** send or validate a real broker order as part of this task. The user and partners will perform the real PAPER order validation later. Automated tests, fake broker callback tests, package builds, and non-order Gateway connectivity checks are in scope.
- Partners share one IB Gateway PAPER account. Expect a single active owner/session; reconnecting may replace the prior session only through the existing explicit lifecycle flow.
- Cancellation is required. Cancellation should remain available for an app-owned working order even after the new-order submission window closes; expiry of the entry window must never strand a live working PAPER order. Keep cancel authorization bound to the trusted PAPER account, locally owned broker order, order status, and operator/audit rules. Cancellation must never become a new-order capability. Define and document any bounded post-stop/reconnect recovery behavior.
- Remove the hard-coded AAPL/1-share integration-test restriction from the partner workflow. Let partners choose supported instruments and quantities, with transparent qualification and risk checks (for example max notional, order type, session/calendar, account facts, and market-data prerequisites). Keep sensible configurable PAPER guardrails and fail closed when required facts are missing. Do not imply every symbol or quantity is supported; show concrete reasons when a contract or risk check fails.
- The user has never traded live and wants a smooth partner-facing flow. The UI/manual should make PAPER mode unmistakable and explain what each action does in plain language.

## Work to complete

1. **Audit current state and finish the partner workflow.** Follow an uploaded strategy from preview/validation, freeze, Backtrader backtest, compatible candidate, selection/deployment, PAPER connection, start/reconciliation, controlled order submission, order/fill history and positions. Fix any actual breaks. Do not claim a real broker ACK or fill without evidence.
2. **Generalize order setup.** Add a clear UI for supported symbol, side, quantity, order type/price and allowed schedule/window. Bind selected strategy version and verified backtest/data provenance. Validate the full proposal server-side against account-scoped PAPER policy. Remove fixed integration-test strategy assumptions from production partner routes while retaining isolated fixtures where useful.
3. **Make cancellation dependable.** Ensure the UI exposes cancel for owned working orders, cancel authorization is not accidentally revoked just because the entry window elapsed, and the user sees pending/confirmed/rejected outcomes based on broker callbacks. Cover partial fills, duplicate clicks, reconnect, stop/restart, stale order IDs, and account isolation.
4. **Build the Windows install experience.** Inspect existing Electron packaging and prior IB Gateway interface/dependency work before changing architecture. Produce a distributable Windows installer from the current app. Bundle or install all required app/backend/runtime dependencies, including the independent `ibkr-runtime`; provide a reliable connection to the user's already-installed/logged-in IB Gateway PAPER API session. The partner-facing setup should be: install app → log in to IB Gateway → app detects/connects and displays PAPER account status. Do not ask partners to paste operator tokens, configure backend keys, start a terminal/server, install Python/Docker, or enter internal ports. Preserve the isolated runtime boundary and correct OAuth/API auth internally. Do not bundle proprietary IB Gateway binaries unless the repository/legal distribution mechanism explicitly permits it; prefer integrating with the user's installed Gateway and explain any unavoidable one-time Gateway API setting in the manual.
5. **Update operator manual and evidence.** Maintain `docs/backtrader-system/PAPER_USER_GUIDE.html` and its served copy. Document install, Gateway login, creating/freezing a strategy, backtest, choosing symbol/quantity/risk limits, start/stop, order and cancel status, reconnect/recovery, and troubleshooting. Update `PROGRESS.md`, `BLOCKERS.md`, and `ACCEPTANCE.md` with exact evidence and clearly mark real broker order validation as intentionally deferred to partners.

## Acceptance criteria

- A Windows installer artifact is produced and its install/launch path is exercised on Windows (native Windows runner or documented Windows VM), including clean install and upgrade/reinstall behavior. Report the exact artifact path, version, and hash.
- On a clean user profile, a partner can install the app, log into IB Gateway PAPER, and see the app establish the correct PAPER connection without API token/secret/port/CLI setup. The app visibly identifies the account as PAPER and will not connect to LIVE trading mode.
- The UI supports strategy creation/freezing/backtest and deploying a compatible frozen strategy for a partner-selected supported instrument and quantity, with transparent eligibility/risk failures and configurable server-enforced limits.
- A partner can start and stop a managed PAPER session, see current reconciliation state, inspect durable order/fill history, and cancel an app-owned working order after its submission window closes. Callback state, partial fill, cancel acknowledgement/rejection and restart recovery are represented accurately.
- Automated coverage exercises representative symbols/quantities, limits, account isolation, window expiry vs cancellation, restart/reconnect and auth/package integration. Run relevant tests and packaging checks; report exact commands/results and unresolved failures. Do not issue an actual broker order.
- The HTML manual matches the shipped Windows UI and install flow. No LIVE order path is enabled.

## Completion report

At completion, summarize: shipped installer and hash; exact install/Gateway setup steps; supported order policy and configurable limits; cancellation behavior outside the entry window; tests/build/install evidence; known constraints; and the manual path. Explicitly state that partners still need to perform the real PAPER ACK/fill/reconciliation validation.
