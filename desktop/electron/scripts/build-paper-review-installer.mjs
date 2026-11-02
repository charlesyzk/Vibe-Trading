import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { verifyPaperRuntime } from "./verify-paper-runtime.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
await verifyPaperRuntime(path.join(root, "runtime", "backend"));
const result = spawnSync(process.execPath, [path.join(root, "scripts", "build-review-installer.mjs")], {
  cwd: root,
  stdio: "inherit",
  env: process.env,
});
if (result.error) throw result.error;
if (result.status !== 0) throw new Error(`Review installer build failed (${result.status})`);
await verifyPaperRuntime(path.join(root, "release", "win-unpacked", "resources", "backend"));
console.log("PAPER runtime files are present in staging and unpacked installer output.");
console.log("This unsigned review build is not a partner-ready release.");
