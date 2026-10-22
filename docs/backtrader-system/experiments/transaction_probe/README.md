# Offline SQLite transaction probe

This isolated experiment checks the P0 event-result transaction boundary from
`CONTRACTS.md` §5.2 and delivery checks T19/T42. It uses Python's standard
library and temporary SQLite files. It does not import Backtrader or IB API,
open a network connection, contact a broker, or dispatch an order. A stored
intent is only a durable local row.

## Model exercised

One trusted input event has sequence 41 and a result containing the next
strategy state, a command list, and the prior checkpoint revision. In one
`BEGIN IMMEDIATE` transaction the probe records the accepted result, advances
the checkpoint, records a decision and intent, and appends an event. The result
hash is SHA-256 over canonical JSON, including state, commands, and the prior
checkpoint revision.

On resubmission, the same event sequence and same hash returns the existing
acceptance. The same sequence with a different hash marks the deployment
paused and rejects the result. The experiment deliberately stops before any
broker dispatch stage.

## Reproduction

From the repository root:

```sh
python3 docs/backtrader-system/experiments/transaction_probe/probe.py scenario
```

Recorded execution on 2026-10-20, Python 3.12.13:

```text
$ python3 docs/backtrader-system/experiments/transaction_probe/probe.py scenario
duplicate_same_result
accepted
conflict_paused
before_commit_exit=73 state={"checkpoint_revision":0,"checkpoints":1,"decisions":0,"event_results":0,"events":0,"intents":0,"paused":false}
after_commit_exit=74 state={"checkpoint_revision":1,"checkpoints":1,"decisions":1,"event_results":1,"events":1,"intents":1,"paused":false}
same_result_retry_exit=0 state={"checkpoint_revision":1,"checkpoints":1,"decisions":1,"event_results":1,"events":1,"intents":1,"paused":false}
conflict_first_exit=0 state={"checkpoint_revision":1,"checkpoints":1,"decisions":1,"event_results":1,"events":1,"intents":1,"paused":false}
different_result_retry_exit=0 state={"checkpoint_revision":1,"checkpoints":1,"decisions":1,"event_results":1,"events":1,"intents":1,"paused":true}
scenario_result=PASS
$ echo $?
0
```

The child exits 73 before commit and 74 after commit. The scenario process
expects those injected exit codes, checks recovered database contents, and
itself exits 0 on success. The experiment's actual command was run and
returned exit code 0.

## Result and limits

- Before commit crash: SQLite recovery leaves no event result, decision, intent,
  event, or checkpoint advance. This demonstrates atomic rollback for the
  modeled local transaction.
- After commit crash: all rows and the checkpoint advance survive even though
  the caller receives no response. Replaying the same result returns the
  existing acceptance and does not add rows.
- Same sequence with changed state: the probe keeps the original accepted
  result and marks the deployment paused. It does not overwrite the first
  result or advance the checkpoint again.
- The hashes are deterministic for this probe's canonical JSON encoding. A
  production schema still needs versioned normalization rules, strict payload
  validation, persisted schema migrations, account/deployment binding from a
  trusted IPC channel, and an audited conflict/pause event.
- This is process-crash injection on a local filesystem, not power-loss,
  storage corruption, multi-process contention, network-filesystem, or
  broker-ambiguity testing. SQLite durability settings, filesystem guarantees,
  backup/restore, and a second writer need separate verification.
- The experiment says nothing about the broker side of exactly-once delivery.
  The production dispatch path must persist `SEND_STARTED` before calling the
  SDK and hold uncertain sends in `SUBMISSION_UNKNOWN` until reconciliation;
  a local SQLite transaction cannot make an external broker call atomic.

No account was connected and no paper or live order was submitted.
