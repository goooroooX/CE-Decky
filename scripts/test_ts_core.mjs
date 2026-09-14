import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const output = mkdtempSync(join(tmpdir(), "ce-decky-ts-core-"));
const tsc = join(root, "node_modules", "typescript", "bin", "tsc");

function run(command, args, env = process.env) {
  const result = spawnSync(command, args, {
    cwd: root,
    env,
    encoding: "utf8",
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

try {
  run(process.execPath, [
    tsc,
    "--target", "ES2020",
    "--module", "commonjs",
    "--strict",
    "--skipLibCheck",
    // Keep the emitted layout stable: these modules import shared `src/*` code,
    // so an inferred root would move them whenever that import set changes.
    "--rootDir", "src",
    "--outDir", output,
    "src/steam/proton.ts",
    "src/steam/client.ts",
  ]);
  run(process.execPath, [
    tsc,
    "--target", "ES2020",
    "--module", "commonjs",
    "--strict",
    "--skipLibCheck",
    "--rootDir", "src",
    "--outDir", output,
    "src/tableImport.ts",
  ]);
  run(process.execPath, ["tests/ts_core_test.cjs"], {
    ...process.env,
    CE_DECKY_TS_OUT: output,
  });
} finally {
  rmSync(output, { recursive: true, force: true });
}
