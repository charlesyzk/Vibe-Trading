# BrokerBase no-bar probe

## Scope

This is an offline feasibility experiment against the installed Backtrader
1.9.78.123 source and real `Cerebro` loop. The feed emits one synthetic bar,
then returns `None` for repeated live polls. An in-memory fake callback queue
stands in for SDK order and cancellation events. The broker maps those events
to Backtrader `Order` instances and puts native order clones on Cerebro's
broker notification queue.

The experiment imports no IB API package, reads no credentials, opens no
socket, connects to no account or Gateway, and sends no real orders. It does
not change Backtrader source, shared interfaces, dependency manifests, or
files outside this experiment directory.

## Environment and reproduction

The Vibe virtual environment does not have Backtrader installed. The existing
Backtrader environment documented in `docs/backtrader-system/EVIDENCE.md` was
used without installing or changing dependencies:

```bash
/Users/yaozekun/strategy_verification/backtrader/.venv/bin/python \
  docs/backtrader-system/experiments/broker_probe/broker_probe.py
```

Recorded result: **exit code 0**. The script prints the installed version,
module path, and per-case callback trace; its assertions check that each
callback carries a native `bt.Order`, that cancel/completion statuses arrive,
and that callback bar counts match the configuration.

## Observed behavior

| Cerebro setting | Broker event pump during empty polls | `notify_order` timing | Result |
|---|---|---|---|
| `quicknotify=True` | Continued for 18 polls after the only bar | Accepted, Completed, and Canceled callbacks all ran with `len(data) == 1`; the second order was canceled from its Accepted callback | Feasible to deliver native order callbacks and issue cancel control without another bar |
| `quicknotify=False` (default) | Continued through 42 empty polls across two gaps | Accepted and Completed callbacks ran at `len(data) == 2`; Canceled ran at `len(data) == 3` | Broker still pumps and maps native orders, but strategy callbacks are deferred until a bar |

No strategy `next()` callback ran while the first feed was returning `None`.
The default setting therefore supports broker-side control processing during
empty polls, but it does not provide prompt strategy-level `notify_order`
callbacks there. A production adapter that needs those callbacks between bars
must enable `quicknotify=True` or deliver an equivalent event through a
separate runtime control path.

## Limits

- The callback transport is synthetic and in-process. It validates the
  Backtrader integration point, not official `ibapi` callback threading,
  Gateway behavior, IPC, reconnection, persistence, or order-ID recovery.
- The broker implements only the methods needed by this probe. It is not a
  production broker, accounting ledger, risk gate, or execution adapter.
- The feed uses synthetic timestamps and prices. This says nothing about live
  market-data finalization, trading calendars, fills, or paper-account
  readiness.
- The result supports the feasibility of the Cerebro/BrokerBase event path; it
  does not establish end-to-end trading safety or paper/live readiness.
