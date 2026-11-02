import { lstat } from "node:fs/promises";
import path from "node:path";

// Check both the staging tree and electron-builder's unpacked resources. This
// is a packaging check, not a runtime, licensing, or trading readiness test.
export async function verifyPaperRuntime(backendRoot) {
  const files = [
    "python.exe",
    "wasi-runtime/python.wasm",
    "wasi-runtime/stdlib/python312.zip",
    "wasi-runtime/CPYTHON-3.12.0-LICENSE.txt",
    "wasi-runtime/WASI-RUNTIMES-APACHE-2.0.txt",
    "Lib/site-packages/wasmtime/__init__.py",
    "Lib/site-packages/wasmtime/win32-x86_64/_wasmtime.dll",
    "Lib/site-packages/src/execution/wasi_worker_host.py",
    "Lib/site-packages/src/execution/strategy_worker_image/worker.py",
    "ibkr-runtime/python.exe",
    "ibkr-runtime/python312.zip",
    "ibkr-runtime/python312._pth",
    "ibkr-runtime/IBAPI-LICENSE.txt",
    "ibkr-runtime/Lib/site-packages/ibapi/__init__.py",
    "ibkr-runtime/Lib/site-packages/ibapi/client.py",
    "ibkr-runtime/Lib/site-packages/ibkr_runtime/worker.py",
    "ibkr-runtime/Lib/site-packages/google/protobuf/__init__.py",
  ];
  for (const relative of files) {
    const file = path.join(backendRoot, ...relative.split("/"));
    let info;
    try {
      info = await lstat(file);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      throw new Error(`PAPER runtime file is missing: ${relative}`);
    }
    if (!info.isFile() || info.isSymbolicLink() || info.size === 0) {
      throw new Error(`PAPER runtime file is not a nonempty regular file: ${relative}`);
    }
  }
}
