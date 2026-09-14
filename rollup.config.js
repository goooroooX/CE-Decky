import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import deckyPlugin from "@decky/rollup";

const ROOT = dirname(fileURLToPath(import.meta.url));
const STAMP = resolve(ROOT, "dist", ".ce-decky-source.sha256");
const CONTROL_FILES = ["package.json", "plugin.json", "pnpm-lock.yaml", "rollup.config.js", "tsconfig.json"];

function sourceFiles() {
  const root = resolve(ROOT, "src");
  const found = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = resolve(directory, entry.name);
      if (entry.isDirectory()) visit(path);
      else if (entry.isFile()) found.push(path);
    }
  };
  visit(root);
  return found.sort((left, right) => {
    const leftName = Buffer.from(relative(ROOT, left).replaceAll("\\", "/"), "utf8");
    const rightName = Buffer.from(relative(ROOT, right).replaceAll("\\", "/"), "utf8");
    return Buffer.compare(leftName, rightName);
  });
}

function frontendDigest() {
  const hash = createHash("sha256");
  const files = [...CONTROL_FILES.map((path) => resolve(ROOT, path)), ...sourceFiles()];
  for (const path of files) {
    const name = Buffer.from(relative(ROOT, path).replaceAll("\\", "/"), "utf8");
    const nameLength = Buffer.alloc(4);
    nameLength.writeUInt32BE(name.length);
    const data = readFileSync(path);
    const dataLength = Buffer.alloc(8);
    dataLength.writeBigUInt64BE(BigInt(data.length));
    hash.update(nameLength);
    hash.update(name);
    hash.update(dataLength);
    hash.update(data);
  }
  return hash.digest("hex");
}

const sourceStampPlugin = {
  name: "ce-decky-source-stamp",
  writeBundle() {
    mkdirSync(dirname(STAMP), { recursive: true });
    writeFileSync(STAMP, `${frontendDigest()}\n`, "ascii");
  },
};

const config = deckyPlugin({});
config.plugins = [...(config.plugins ?? []), sourceStampPlugin];

export default config;
