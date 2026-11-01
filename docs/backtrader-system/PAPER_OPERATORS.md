# PAPER operator provisioning

Partner-facing click-through instructions: [IBKR PAPER 使用手册](PAPER_USER_GUIDE.html).

The local API key is a shared access credential and cannot authorize PAPER start/activate/stop actions. Provision one operator token per person on the trusted app host, then issue only the exact account/action grants they need.

Run from the `agent/` project. The token is displayed once; send it to its named operator through the team's normal secure channel and store it in the app as the Bearer credential.

```sh
uv run --project agent python -m src.execution.paper_operator_tokens issue --subject partner-alice
uv run --project agent python -m src.execution.paper_operator_tokens issue --subject workspace-admin
```

The CLI prompts for the issuer's operator token without echoing it, and verifies it matches `--issuer-subject`. The recipient must also have an active token. Grant one action at a time; `start`, `activate`, `cancel`, `stop`, and `recover` are separate grants. Use timezone-aware timestamps and the exact DU account reference:

```sh
uv run --project agent python -m src.execution.paper_operator_tokens grant \
  --subject partner-alice \
  --issuer-subject workspace-admin \
  --account-ref DU000000 \
  --action start \
  --valid-from 2026-11-02T14:20:00+00:00 \
  --valid-until 2026-11-02T21:10:00+00:00
```

Revoke a token with its non-secret `token_id`; revoke a grant with its non-secret `grant_id` and an active issuer token. Revoking a token blocks future API authentication; revoking a grant blocks the matching action. Neither operation silently stops an already-running runtime or cancels an order.

The commands write only the local operator-token and PAPER grant databases. The app must still install a trusted PAPER composition with `PaperControlGrantStore` as its authorizer; provisioning alone does not configure a runtime or enable an order.

## Partner workflow: freeze → backtest → select → PAPER

1. In Execution, create a Python upload strategy, keep instrument `AAPL`, quantity `1`, and `LIMIT · DAY` at `$400`, then fetch completed AAPL daily bars with the local IBKR PAPER profile. The host issues a short-lived token bound to the exact CSV and XNYS schedule; the upload preview validates and consumes it before adding the host-observed PAPER provenance to the frozen report. Hand-entered provider labels do not qualify. Review and confirm the frozen inputs; the successful report is bound to the immutable strategy version. These defaults match the current one-shot integration envelope; other values may backtest but are rejected by this fixed PAPER deployment.
2. In **Frozen Backtest Versions**, select **Use for this PAPER check** only when the report shows the exact current integration shape: Python upload; AAPL regular-session completed daily bars; long-only explicit one-share sizing; LMT/DAY/RTH; one single-session Backtrader evaluation; exactly one unfilled `BUY 1 AAPL LMT $400` order. The catalog explains rejected candidates. The present `sma-cross-10-30` MARKET/target-fraction template is not compatible with this fixed test.
3. Selection requires a named operator token, a stopped PAPER runtime, no in-flight integration action, and an empty one-shot order ledger. It pins the version, copies and attests the verified report, rebuilds the local PAPER composition, and records the operator, version, and report digest under `~/.vibe-trading/paper-integration-test/deployments/selection-audit.jsonl`. It does not connect to Gateway or submit an order. The selected version appears in PAPER status after the action.
4. The separate **Run the one-shot PAPER order** action is still explicit. It requires the selected version's current attestation, a fresh reconciled broker snapshot, the operator's `start` and `activate` grants, and the configured UTC window. The only allowed request is AAPL BUY 1, LMT `$400`, DAY, regular hours only, maximum order notional `$400`, at most one order. The current window is `2026-11-02 14:30–21:00 UTC`; outside it, the endpoint fails closed. After dispatch the app closes the gate and refreshes the account snapshot; the operator checks the persisted order callback, any fill, and reconciled position before declaring the check complete.

The runtime catalog includes the original integration-test strategy and can list newly frozen Python upload versions after successful backtests. A new candidate must independently produce a matching report before it can be selected. For PAPER candidate eligibility, the upload must carry the short-lived, single-use token returned by the host's IBKR PAPER history fetch; the server binds it to the exact CSV and pinned XNYS schedule, verifies the PAPER profile and qualified USD/STK contract, then records that evidence in the report and immutable version. Caller-provided provider labels alone never qualify. This host-issued provenance is not a cryptographic signature from IBKR and does not prove point-in-time publication or adjustment semantics. Synthetic/CSV-only/Yahoo reports remain usable for offline research but are not eligible for this PAPER flow. LIVE remains disabled.

For an authenticated API client, selection uses `POST /api/execution/paper/strategy-candidates/select` with `{"strategy_version_id":"<64-character frozen version id>"}`. Use the page when possible; it refreshes PAPER status and candidate eligibility after selection.

## Optional custom deployment composition

The current local one-shot flow loads its built-in composition when `~/.vibe-trading/paper-integration-test.json` exists; it does not need a custom factory setting. The hook below is only for a separate trusted deployment integration:

The API can load a trusted local factory during FastAPI startup:

```sh
export VIBE_PAPER_COMPOSITION_FACTORY=your_deployment.paper:build_composition
```

The referenced zero-argument function must return a validated `PaperExecutionComposition`. It must only construct local collaborators; the manager's authenticated start route remains the Gateway connection boundary. Without a local one-shot config or a custom factory, the app stays inert. An invalid configured factory stops startup.
