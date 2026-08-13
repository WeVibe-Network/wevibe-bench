import { promises as fs } from "node:fs";
import { join } from "node:path";

const HOLD_FILE = "hold-ui.json";
const MAX_DEPTH = 6;

async function statOrNull(path) {
  try {
    return await fs.stat(path);
  } catch {
    return null;
  }
}

async function listDir(path) {
  try {
    return await fs.readdir(path, { withFileTypes: true });
  } catch {
    return [];
  }
}

async function findHoldFiles(root, depth = 0, out = []) {
  if (depth > MAX_DEPTH) return out;
  for (const ent of await listDir(root)) {
    const path = join(root, ent.name);
    if (ent.isFile() && ent.name === HOLD_FILE) {
      const st = await statOrNull(path);
      if (st?.isFile()) out.push({ path, mtime: st.mtimeMs });
      continue;
    }
    if (ent.isDirectory()) await findHoldFiles(path, depth + 1, out);
  }
  return out;
}

export async function findActiveHold(runsRoot) {
  const files = await findHoldFiles(runsRoot);
  files.sort((a, b) => b.mtime - a.mtime);
  return files[0] ?? null;
}

export async function readHold({ runsRoot }) {
  const hit = await findActiveHold(runsRoot);
  if (!hit) return null;

  let raw;
  try {
    raw = await fs.readFile(hit.path, "utf8");
  } catch (err) {
    if (err?.code === "ENOENT") return { released: true };
    throw err;
  }

  const data = JSON.parse(raw);
  if (data?.schema_version !== 1) {
    return {
      invalid_schema: true,
      schema_version: data?.schema_version ?? null,
      hold_path: hit.path,
      reason: "unknown hold-ui.json schema_version — refusing to render guessed fields",
    };
  }

  let heldSeconds = null;
  if (typeof data.started_at === "string") {
    const t = Date.parse(data.started_at);
    if (Number.isFinite(t)) heldSeconds = Math.max(0, Math.round((Date.now() - t) / 1000));
  }

  return {
    ...data,
    hold_path: hit.path,
    held_seconds: heldSeconds,
  };
}

export async function releaseHold({ runsRoot }) {
  const hold = await readHold({ runsRoot });
  if (!hold) return { ok: true, released: true, path: null, note: "no active hold" };
  if (hold.released) return { ok: true, released: true, path: null, note: "hold file vanished" };
  if (hold.invalid_schema) {
    return { ok: false, code: "hold_invalid_schema", reason: hold.reason, schema_version: hold.schema_version };
  }

  const releasePath = typeof hold.release?.path === "string" && hold.release.path.trim()
    ? hold.release.path
    : null;
  if (!releasePath) {
    return { ok: false, code: "hold_release_unavailable", reason: "active hold did not publish release.path" };
  }

  await fs.writeFile(releasePath, "", { flag: "a" });
  return { ok: true, path: releasePath };
}
