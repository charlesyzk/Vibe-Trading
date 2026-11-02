import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { verifyPaperRuntime } from "./verify-paper-runtime.mjs";

const root = await mkdtemp(path.join(os.tmpdir(), "vibe-paper-package-"));
const files = [
  "python.exe", "ibkr-runtime/python.exe", "ibkr-runtime/python312.zip",
  "wasi-runtime/python.wasm", "wasi-runtime/stdlib/python312.zip",
  "wasi-runtime/CPYTHON-3.12.0-LICENSE.txt", "wasi-runtime/WASI-RUNTIMES-APACHE-2.0.txt",
  "Lib/site-packages/wasmtime/__init__.py", "Lib/site-packages/src/execution/wasi_worker_host.py",
  "Lib/site-packages/wasmtime/win32-x86_64/_wasmtime.dll",
  "Lib/site-packages/src/execution/strategy_worker_image/worker.py",
  "ibkr-runtime/python312._pth", "ibkr-runtime/IBAPI-LICENSE.txt",
  "ibkr-runtime/Lib/site-packages/ibapi/__init__.py",
  "ibkr-runtime/Lib/site-packages/ibapi/client.py",
  "ibkr-runtime/Lib/site-packages/ibkr_runtime/worker.py",
  "ibkr-runtime/Lib/site-packages/google/protobuf/__init__.py",
];
try {
  await assert.rejects(verifyPaperRuntime(root), /missing/u);
  for (const relative of files) {
    const file = path.join(root, ...relative.split("/"));
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, "fixture");
  }
  await verifyPaperRuntime(root);
  await writeFile(path.join(root, "ibkr-runtime", "IBAPI-LICENSE.txt"), "");
  await assert.rejects(verifyPaperRuntime(root), /nonempty regular file/u);
  console.log("PAPER runtime packaging layout checks: OK");
} finally {
  await rm(root, { recursive: true, force: true });
}
