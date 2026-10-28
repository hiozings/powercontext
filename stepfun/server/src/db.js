import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import {
  SeekdbClient,
  Schema,
  FulltextIndexConfig,
  VectorIndexConfig,
} from "seekdb";
import mysql from "mysql2/promise";

export const DIM = 512;
const HERE = path.dirname(fileURLToPath(import.meta.url));
export const DATA_DIR =
  process.env.SEEKDB_DIR || path.resolve(HERE, "../data/seekdb");
export const SEEKDB_PORT = Number(process.env.SEEKDB_PORT || 2887);
const SESSION_FILE = path.resolve(HERE, "../data/session.json");
const NAME_POOL = ["知识库 1", "知识库 2", "知识库 3", "知识库 4", "知识库 5"];

let child = null;
let client = null;
let pool = null;
let endpoint = null;
let session = null;
let logTail = [];
const collectionCache = new Map();

function readSession() {
  if (session) return session;
  try {
    session = JSON.parse(fs.readFileSync(SESSION_FILE, "utf8"));
  } catch {
    session = { active: null, names: {} };
  }
  return session;
}

function writeSession() {
  fs.mkdirSync(path.dirname(SESSION_FILE), { recursive: true });
  fs.writeFileSync(SESSION_FILE, JSON.stringify(session, null, 2));
}

function displayNameFor(name) {
  const s = readSession();
  if (s.names[name]) return s.names[name];
  const idx = /^kb_(\d+)$/.exec(name);
  const label = NAME_POOL[(idx ? Number(idx[1]) : 1) - 1] || `知识库 ${name}`;
  return label;
}

function nativeRuntime() {
  const require = createRequire(import.meta.url);
  let dir;
  try {
    dir = path.join(
      path.dirname(
        require.resolve("@oceanbase/seekdb-node-linux-x64-gnu/package.json")
      ),
      "native"
    );
  } catch {
    throw new Error(
      "未找到 @oceanbase/seekdb-node-linux-x64-gnu，请先在 stepfun/ 执行 npm install。"
    );
  }
  const bin = path.join(dir, "seekdb");
  if (!fs.existsSync(bin)) throw new Error(`seekdb 可执行文件缺失：${bin}`);
  return { dir, bin };
}

async function canConnect(timeout = 800) {
  try {
    const conn = await mysql.createConnection({
      host: "127.0.0.1",
      port: SEEKDB_PORT,
      user: "root",
      password: "",
      database: "test",
      connectTimeout: timeout,
    });
    await conn.query("SELECT 1");
    await conn.end();
    return true;
  } catch {
    return false;
  }
}

async function waitReady(deadline) {
  let last = null;
  while (Date.now() < deadline) {
    if (child && child.exitCode !== null) {
      throw new Error(
        `seekdb 进程提前退出（code=${child.exitCode}）：${logTail.join(" ")}`
      );
    }
    try {
      const conn = await mysql.createConnection({
        host: "127.0.0.1",
        port: SEEKDB_PORT,
        user: "root",
        password: "",
        database: "test",
        connectTimeout: 1000,
      });
      await conn.query("SELECT 1");
      await conn.end();
      return;
    } catch (err) {
      last = err;
      await new Promise((r) => setTimeout(r, 400));
    }
  }
  throw new Error(
    `seekdb 未能在限定时间内就绪：${last?.message ?? "unknown"} ${logTail.join(" ")}`
  );
}

function clearStalePid() {
  const pidFile = path.join(DATA_DIR, "run", "seekdb.pid");
  try {
    const pid = Number(fs.readFileSync(pidFile, "utf8").trim());
    if (!Number.isInteger(pid) || pid <= 0) return;
    try {
      process.kill(pid, 0);
    } catch {
      fs.rmSync(pidFile, { force: true });
    }
  } catch {
    /* no pid file */
  }
}

async function startServer() {
  const { dir, bin } = nativeRuntime();
  fs.mkdirSync(DATA_DIR, { recursive: true });
  clearStalePid();
  child = spawn(
    bin,
    [
      "--nodaemon",
      "--port",
      String(SEEKDB_PORT),
      "--base-dir",
      DATA_DIR,
      "--data-dir",
      path.join(DATA_DIR, "store"),
      "--log-level",
      "WARN",
    ],
    {
      cwd: DATA_DIR,
      env: {
        ...process.env,
        LD_LIBRARY_PATH: [dir, process.env.LD_LIBRARY_PATH]
          .filter(Boolean)
          .join(":"),
      },
      stdio: ["ignore", "pipe", "pipe"],
    }
  );
  const keep = (buf) => {
    const text = buf.toString().trim();
    if (!text) return;
    logTail.push(...text.split("\n").slice(-4));
    logTail = logTail.slice(-8);
  };
  child.stdout.on("data", keep);
  child.stderr.on("data", keep);
  await waitReady(Date.now() + 60_000);
}

export async function connect() {
  if (client) return client;
  if (!(await canConnect())) await startServer();
  endpoint = { host: "127.0.0.1", port: SEEKDB_PORT, user: "root" };
  client = new SeekdbClient({
    host: endpoint.host,
    port: endpoint.port,
    user: endpoint.user,
    password: "",
    database: "test",
  });
  pool = mysql.createPool({
    host: endpoint.host,
    port: endpoint.port,
    user: endpoint.user,
    password: "",
    database: "test",
    connectionLimit: 2,
  });
  return client;
}

export async function close() {
  if (pool) await pool.end().catch(() => {});
  if (client) await client.close().catch(() => {});
  pool = null;
  client = null;
  if (child && child.exitCode === null) {
    const proc = child;
    proc.kill("SIGTERM");
    child = null;
    await new Promise((resolve) => {
      const timer = setTimeout(() => {
        proc.kill("SIGKILL");
        resolve();
      }, 5000);
      proc.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
}

export async function serverInfo() {
  if (!pool) return null;
  const [rows] = await pool.query("SELECT VERSION() AS version");
  return { version: rows[0].version, host: endpoint.host, port: endpoint.port };
}

function schema() {
  return new Schema()
    .createIndex(new FulltextIndexConfig("ngram", { ngram_token_size: 2 }))
    .createIndex(
      new VectorIndexConfig({ hnsw: { dimension: DIM, distance: "cosine" } })
    );
}

export async function listKbs() {
  const list = await client.listCollections({ withEmbeddingFunction: false });
  const s = readSession();
  const out = [];
  for (const col of list) {
    if (!col.name.startsWith("kb_")) continue;
    out.push({
      name: col.name,
      label: displayNameFor(col.name),
      chunks: await col.count(),
    });
  }
  out.sort((a, b) => a.name.localeCompare(b.name));
  return { collections: out, active: s.active };
}

export async function getCollect(name) {
  if (!/^kb_\d+$/.test(name)) throw new Error("非法的知识库名称");
  if (collectionCache.has(name)) return collectionCache.get(name);
  if (!(await client.hasCollection(name))) throw new Error("知识库不存在");
  const col = await client.getCollection({ name, embeddingFunction: null });
  collectionCache.set(name, col);
  return col;
}

export async function createKb() {
  let n = 1;
  while (await client.hasCollection(`kb_${n}`)) n += 1;
  const name = `kb_${n}`;
  const col = await client.createCollection({
    name,
    schema: schema(),
    embeddingFunction: null,
  });
  collectionCache.set(name, col);
  const s = readSession();
  s.names[name] = NAME_POOL[n - 1] || `知识库 ${n}`;
  s.active = name;
  writeSession();
  return { name, label: s.names[name], chunks: 0 };
}

export async function activate(name) {
  await getCollect(name);
  const s = readSession();
  s.active = name;
  writeSession();
  return { name, label: displayNameFor(name) };
}

export async function ensureDefaultKb() {
  const s = readSession();
  if (s.active) {
    try {
      await getCollect(s.active);
      return { name: s.active, label: displayNameFor(s.active) };
    } catch {
      /* fall through and create */
    }
  }
  const list = await client.listCollections({ withEmbeddingFunction: false });
  const existing = list.find((c) => c.name.startsWith("kb_"));
  if (existing) {
    s.active = existing.name;
    writeSession();
    return { name: existing.name, label: displayNameFor(existing.name) };
  }
  return createKb();
}

export async function ingest(name, doc, chunks, { deferRefresh = false } = {}) {
  const col = await getCollect(name);
  const ids = chunks.map((_, i) => `${doc.id}::${i}`);
  const old = await col.get({
    where: { doc_id: { $eq: doc.id } },
    include: ["metadatas"],
  });
  const oldIds = old.ids ?? [];
  if (oldIds.length) await col.delete({ ids: oldIds });

  const started = performance.now();
  const batchSize = 32;
  for (let i = 0; i < chunks.length; i += batchSize) {
    const slice = chunks.slice(i, i + batchSize);
    await col.add({
      ids: ids.slice(i, i + batchSize),
      embeddings: slice.map((c) => c.embedding),
      documents: slice.map((c) => c.text),
      metadatas: slice.map((c, j) => ({
        doc_id: doc.id,
        title: doc.title,
        source: doc.source || doc.title,
        chunk: i + j,
        chars: c.text.length,
      })),
    });
  }
  const addMs = performance.now() - started;

  let refreshMs = 0;
  if (!deferRefresh) {
    const refreshStart = performance.now();
    await col.refresh_index();
    refreshMs = Math.round(performance.now() - refreshStart);
  }

  return {
    chunks: chunks.length,
    replaced: oldIds.length,
    addMs: Math.round(addMs),
    refreshMs,
  };
}

export async function refreshIndex(name) {
  const col = await getCollect(name);
  const started = performance.now();
  await col.refresh_index();
  return { refreshMs: Math.round(performance.now() - started) };
}

const QUERY_SPECIAL = /[+\-&|!(){}[\]^"~*?:\\/]/g;

export async function search(name, query, embedding, limit = 6) {
  const col = await getCollect(name);
  const text = query.replace(QUERY_SPECIAL, " ").replace(/\s+/g, " ").trim();
  if (!text) throw new Error("查询不能为空");
  const started = performance.now();
  const res = await col.hybridSearch({
    query: { whereDocument: { $contains: text }, nResults: limit * 4 },
    knn: { queryEmbeddings: embedding, nResults: limit * 4 },
    rank: { rrf: {} },
    nResults: limit,
    include: ["documents", "metadatas", "distances"],
  });
  const tookMs = performance.now() - started;
  const ids = res.ids?.[0] ?? [];
  const rows = ids.map((id, i) => ({
    id,
    score: Number(res.distances?.[0]?.[i] ?? 0),
    content: res.documents?.[0]?.[i] ?? "",
    ...(res.metadatas?.[0]?.[i] ?? {}),
  }));
  return { rows, tookMs: Math.round(tookMs * 10) / 10 };
}

export function sessionSnapshot() {
  const s = readSession();
  return { active: s.active, label: s.active ? displayNameFor(s.active) : null };
}
