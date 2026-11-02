"""Run the trusted event-worker shell inside a no-network WASI instance.

This process is a host for Wasmtime, not the strategy interpreter. Only the
guest CPython executes uploaded source. It gets stdin/stdout/stderr and a
read-only standard-library directory; no host environment or user directory
is exposed. Its parent enforces wall time and bounded output.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        return 125
    import wasmtime

    wasm_path, stdlib_dir = (Path(value) for value in sys.argv[1:3])
    memory_bytes = int(sys.argv[3])
    if not 16 * 1024 * 1024 <= memory_bytes <= 8 * 1024 * 1024 * 1024:
        return 125
    worker = Path(__file__).resolve().parent / "strategy_worker_image" / "worker.py"
    config = wasmtime.Config()
    config.consume_fuel = True
    engine = wasmtime.Engine(config)
    module = wasmtime.Module.from_file(engine, str(wasm_path))
    store = wasmtime.Store(engine)
    store.set_limits(memory_size=memory_bytes, instances=1, memories=1, tables=4)
    store.set_fuel(5_000_000_000)
    wasi = wasmtime.WasiConfig()
    wasi.argv = ["python", "-I", "-c", worker.read_text(encoding="utf-8")]
    wasi.env = []
    wasi.inherit_stdin()
    wasi.inherit_stdout()
    wasi.inherit_stderr()
    wasi.preopen_dir(str(stdlib_dir), "/usr/local/lib", wasmtime.DirPerms.READ_ONLY, wasmtime.FilePerms.READ_ONLY)
    store.set_wasi(wasi)
    linker = wasmtime.Linker(engine)
    linker.define_wasi()
    try:
        linker.instantiate(store, module).exports(store)["_start"](store)
        return 0
    except wasmtime.ExitTrap as exc:
        return int(exc.code)
    except wasmtime.Trap:
        print("isolated WASI worker exceeded its execution limits or trapped", file=sys.stderr)
        return 124


if __name__ == "__main__":
    raise SystemExit(main())
