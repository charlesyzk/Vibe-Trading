# Isolated IBKR Python API runtime

This project keeps the official IBKR Python API dependency set separate from the main Vibe-Trading environment. IBKR release 1050.02 pins `protobuf==5.29.5`, which conflicts with the main project's optional `channels` dependencies in some supported Python/platform resolutions.

## Install

1. Download TWS API release 1050.02 from the [official IBKR API download page](https://download2.interactivebrokers.com/installers/tws-api/index.html), accept its license, and extract `source/pythonclient`.
2. Lock and install this isolated dependency environment using the same Python version as the Vibe runtime:

   ```sh
   uv lock --project ibkr-runtime --check
   uv sync --project ibkr-runtime --python .venv/bin/python
   ```

3. Verify and install only the pinned official source tree:

   ```sh
   ibkr-runtime/.venv/bin/python scripts/install_ibapi_from_official_source.py \
     --source-dir "/path/to/TWS API 1050.02/source/pythonclient" \
     --verify-only
   ibkr-runtime/.venv/bin/python scripts/install_ibapi_from_official_source.py \
     --source-dir "/path/to/TWS API 1050.02/source/pythonclient" \
     --python ibkr-runtime/.venv/bin/python
   ```

4. Run the isolated SDK callback mapping checks:

   ```sh
   ibkr-runtime/.venv/bin/python -m unittest discover -s ibkr-runtime/tests -v
   ```

The lock at `docs/backtrader-system/ibapi-source.lock.json` records upstream release, package version, license, the scope `setup.py + ibapi/**`, file count, and a deterministic SHA-256 over paths and bytes. This is a source-tree hash, not an archive hash. The source must be obtained from IBKR's official download page before verification; the installer does not download it.

This package contains explicit client and subprocess lifecycle primitives, but installation does not configure or start a broker connection. `IbapiClientOwner` constructs the official `EClient` without connecting; only an explicit `start(host, port, client_id)` opens a socket. The subprocess endpoint is not wired to `PaperExecutionManager` or FastAPI; callback spool consumption, broker/account recovery, and trusted account profile validation are still required before app integration. No Gateway connection, account read, order, or live configuration is part of installation.

`ibkr_runtime.callbacks.IbapiCallbackAdapter` converts order, execution, commission, position, account-summary, and error callbacks into primitive immutable records on a bounded queue. Each callback is committed to SQLite before queue handoff. `SQLiteCallbackSpool.pending()` reads ordered bounded batches, and `mark_processed(sequence)` is the consumer ACK after its ledger transaction commits. Overflow latches a fail-closed flag; the service loop must pause and reconcile before doing more work. The adapter does not own Backtrader state, the ledger, risk authorization, or an SDK network connection.

The main application's `src.execution.ibkr_runtime_process.IbkrRuntimeProcess` launches `ibkr_runtime.worker` with this project's explicit interpreter path and a reduced environment. The private stdio management channel accepts bounded protocol-V1 `status`, `start`, `stop`, `shutdown`, `place_order`, `cancel_order`, and `confirm_reconciled_stop` frames with exact schemas and correlated request IDs. Order commands accept only constrained PAPER SMART/USD/STK whole-share BUY/SELL MKT/DAY orders; cancel commands carry one exact positive broker order ID. The child constructs an `EClient` and opens its SQLite callback spool while disconnected; only an explicit `start` command invokes `connect`, on a dedicated SDK thread. The stdio protocol is not mounted into the strategy Docker worker and is not an HTTP API. A timeout, broken pipe, or invalid response leaves process lifecycle uncertain and blocks further commands.

`ibkr_runtime.order_factory.IbapiOrderFactory` builds official SDK `Contract` and `Order` values from an injected, operator-reviewed SMART/USD/STK contract map and a frozen PAPER intent. `paper_transport.IbapiPaperTransport` checks the Backtrader command against that intent before invoking the precommitted submit seam. `IbapiOrderSubmitter` reruns a host-provided pre-dispatch check; its `submit` path waits for durable `start_send`, while `submit_precommitted` is intended for a host that atomically committed `SEND_STARTED` and broker mapping before transport entry. Both paths invoke `placeOrder` only after their required checks and mark uncertain SDK exceptions UNKNOWN. Neither path connects to Gateway.

`IbkrOrderIdAllocator` persists a monotone cursor in SQLite, requires a fresh `nextValidId` callback after each process start, and takes an exclusive local advisory lock for one canonical database path. The callback only records a candidate; the host must reconcile broker open orders and durable local mappings, then explicitly call `confirm_reconciled()` before allocation. The app host does not yet perform that composition. `IbapiClientOwner` owns the SDK run loop on a dedicated thread and requires explicit disconnect plus bounded join; timeout or disconnect failure is not reported as clean stop. A run-loop failure remains latched until host reconciliation is confirmed. Status separates socket connection, SDK API handshake, and `ready_for_dispatch`; the latter remains false because app-level account/order reconciliation is not implemented. The local lock is not a distributed/account-wide owner guarantee. Tests have not sent a subprocess start command or connected to Gateway.
