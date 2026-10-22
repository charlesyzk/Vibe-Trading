# macOS uploaded-Python isolation probe

Date: 2026-10-20 (Asia/Shanghai)<br>
Host: macOS 26.5, arm64; Python 3.12.13<br>
Scope: offline local-only checks. No account, Gateway, broker SDK, order, external host, dependency install, or system setting was used. The network test used an ephemeral listener bound to `127.0.0.1`; the file test used a temporary synthetic sentinel.

## Result

**BLOCKED for the current runner as a paper-deployment security boundary.** This macOS host can enforce Seatbelt network and path rules for a subprocess tree, and can enforce several POSIX resource limits. The current Vibe runner does not invoke Seatbelt, has no `vibe-sandbox` account on this host, and silently ignores the `RLIMIT_AS` error. Therefore it currently provides no demonstrated network/file isolation here and no working address-space cap.

`/usr/bin/sandbox-exec` is present and enforced both checks below, but Apple's local `sandbox-exec(1)` man page marks it **DEPRECATED**. This is evidence of a mechanism available on this host, not a supported long-term deployment design.

## Reproduction and observations

Run from the repository root:

```sh
python3 docs/backtrader-system/experiments/sandbox_probe/probe_macos.py
```

Observed on this host:

- The unconfined baseline's descendant Python process connected to the local TCP listener (`returncode=0`, listener accepted).
- Under `(version 1) (allow default) (deny network*)`, the descendant's `connect()` failed with `PermissionError: [Errno 1] Operation not permitted`; the listener accepted no connection.
- The unconfined baseline read the synthetic sentinel. Under a profile denying `file-read*` for that exact file, reading failed with the same `EPERM` error.
- Setting `RLIMIT_CPU` to 1 second stopped a CPU spin with signal 24 (`SIGXCPU`).
- Setting `RLIMIT_FSIZE` to 1 KiB capped the test output file at exactly 1,024 bytes.
- Setting `RLIMIT_NOFILE` to 16 caused the 17th open attempt to fail with `EMFILE`.
- Applying the runner's exact `RLIMIT_AS` setting (4 GiB soft and hard) failed on this macOS/Python combination with `ValueError: current limit exceeds maximum limit`. Lowering `RLIMIT_DATA` to 128 MiB failed the same way. `RLIMIT_AS` and `RLIMIT_RSS` share the same resource ID here. The existing runner catches the error and proceeds, so its address-space cap does not take effect on this host.
- `vibe-sandbox` is not an account on this host. Runner source explicitly falls back to running without a UID drop if that account is absent.

The probe source contains the ephemeral listener, temporary sentinel, and child-only resource-limit attempts; it creates no persistent outputs beyond this directory.

## Existing-runner evidence

In [`agent/src/core/runner.py`](../../../../agent/src/core/runner.py):

- Lines 34–39 describe the UID and rlimit controls as applying to a hardened Docker deployment, with fallback elsewhere.
- Lines 56–71 resolve `vibe-sandbox` only when present; this host has no such account.
- Lines 104–114 attempt `RLIMIT_AS` and `RLIMIT_NOFILE`, then swallow `ValueError`/`OSError`.
- Lines 628–638 assemble those controls and the wall-clock timeout. The execution path does not wrap the strategy with `sandbox-exec` or another OS network/filesystem sandbox.

Changing `HOME` and applying an AST scrubber are defense-in-depth; this experiment does not treat either as OS-enforced file or network isolation.

## Minimum next action

Before allowing uploaded Python to paper, choose and support a deployment isolation mechanism that provides network denial, an explicit filesystem allowlist, bounded CPU/memory/time, and descendant-process confinement. If macOS remains a target, the next small validation is to prototype a maintained host integration and repeat these same negative cases on the packaged app's supported macOS versions; `sandbox-exec` alone is deprecated, and the present `RLIMIT_AS` path is not usable here. Until that passes, keep uploaded Python offline-only. No production code or shared contract was changed in this probe.
