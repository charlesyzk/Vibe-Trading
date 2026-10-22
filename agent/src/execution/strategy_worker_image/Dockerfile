FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY worker.py /opt/vibe-strategy-worker.py
# Debian's coreutils timeout is PID 1 and enforces a final in-container bound
# even if the host-side Docker CLI or supervisor disappears. PythonSandbox
# overrides this duration with its per-invocation limit.
RUN test -x /usr/bin/timeout
ENTRYPOINT ["/usr/bin/timeout", "--kill-after=2s", "--signal=TERM", "300s", "python", "-I", "/opt/vibe-strategy-worker.py"]
