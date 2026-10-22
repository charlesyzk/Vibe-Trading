# Isolated Python event worker image

The image executes one uploaded Python event callback inside the existing
fail-closed `PythonSandbox`. It uses a digest-pinned Python 3.12 base, has no
strategy dependencies, and the launcher runs it with no network, no host mounts,
a read-only root filesystem, a non-root UID, and configured CPU/memory/PID/time
limits.

Build offline from the cached pinned base and inspect the immutable image ID:

```sh
docker --context colima-vibe-execution build \
  --network=none --pull=false \
  --file agent/src/execution/strategy_worker_image/Dockerfile \
  --tag vibe-strategy-worker:local \
  agent/src/execution/strategy_worker_image

docker --context colima-vibe-execution image inspect --format '{{.Id}}' vibe-strategy-worker:local
```

Pass the inspected `sha256:<image-id>` to `PythonSandbox`. Production setup must
build and pin the resulting image as deployment configuration; the source tree
does not silently build or pull an image at runtime.

Uploaded source defines:

```python
def on_event(event, state):
    # event has exactly one current completed bar; state is the prior JSON checkpoint.
    return {"state": state, "order_proposals": []}
```

The callback receives no account identity, Backtrader objects, data file,
DataFrame, future bar, or event cursor. It may return only JSON state and
Market/DAY whole-share proposals. The offline Python upload report path calls
this worker once per completed bar, then applies validated proposals to the
host's native BackBroker and verifies the report by replay. This does not
connect the worker to the paper broker.
