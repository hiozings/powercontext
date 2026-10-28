import express from "express";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import {
  DIM,
  connect,
  close,
  serverInfo,
  listKbs,
  createKb,
  activate,
  ensureDefaultKb,
  ingest,
  refreshIndex,
  search,
  sessionSnapshot,
} from "./db.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIST = path.resolve(HERE, "../../web/dist");
const SAMPLES = path.resolve(HERE, "../../samples");
const PORT = Number(process.env.PORT || 8788);

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "32mb" }));
app.use((req, res, next) => {
  res.setHeader("Cross-Origin-Opener-Policy", "same-origin");
  res.setHeader("Cross-Origin-Embedder-Policy", "require-corp");
  next();
});

const ok = (res, data) => res.json({ ok: true, ...data });
const fail = (res, code, message) =>
  res.status(code).json({ ok: false, error: message });

function embeddingOf(value, count) {
  if (!Array.isArray(value) || value.length !== count)
    throw new Error(`embedding 数量应为 ${count}`);
  for (const vec of value) {
    if (!Array.isArray(vec) || vec.length !== DIM)
      throw new Error(`每个向量必须是 ${DIM} 维`);
    if (vec.some((x) => typeof x !== "number" || !Number.isFinite(x)))
      throw new Error("向量包含非法数值");
  }
  return value;
}

app.get("/api/health", async (_req, res) => {
  try {
    const info = await serverInfo();
    ok(res, {
      engine: "seekdb-js 1.4.0",
      mode: "embedded local server",
      dim: DIM,
      seekdb: info,
      session: sessionSnapshot(),
    });
  } catch (e) {
    fail(res, 500, e.message);
  }
});

app.get("/api/kbs", async (_req, res) => {
  try {
    ok(res, await listKbs());
  } catch (e) {
    fail(res, 500, e.message);
  }
});

app.post("/api/kbs", async (_req, res) => {
  try {
    ok(res, { collection: await createKb() });
  } catch (e) {
    fail(res, 500, e.message);
  }
});

app.post("/api/session", async (req, res) => {
  try {
    const { name } = req.body ?? {};
    if (typeof name !== "string") throw new Error("缺少 name");
    ok(res, { collection: await activate(name) });
  } catch (e) {
    fail(res, 400, e.message);
  }
});

app.post("/api/ingest", async (req, res) => {
  try {
    const { kb, doc, chunks, deferRefresh } = req.body ?? {};
    if (typeof kb !== "string") throw new Error("缺少 kb");
    if (!doc || typeof doc.id !== "string" || typeof doc.text !== "string")
      throw new Error("doc 需要 id 与 text");
    if (!Array.isArray(chunks) || chunks.length === 0)
      throw new Error("chunks 不能为空");
    if (chunks.some((c) => typeof c.text !== "string" || !c.text.trim()))
      throw new Error("chunk 文本不能为空");
    embeddingOf(
      chunks.map((c) => c.embedding),
      chunks.length
    );
    const result = await ingest(kb, doc, chunks, {
      deferRefresh: Boolean(deferRefresh),
    });
    ok(res, result);
  } catch (e) {
    fail(res, 400, e.message);
  }
});

app.post("/api/refresh", async (req, res) => {
  try {
    const { kb } = req.body ?? {};
    if (typeof kb !== "string") throw new Error("缺少 kb");
    ok(res, await refreshIndex(kb));
  } catch (e) {
    fail(res, 400, e.message);
  }
});

app.post("/api/search", async (req, res) => {
  try {
    const { kb, query, embedding, limit } = req.body ?? {};
    if (typeof kb !== "string") throw new Error("缺少 kb");
    if (typeof query !== "string" || !query.trim())
      throw new Error("查询不能为空");
    embeddingOf([embedding], 1);
    const n = Math.min(Math.max(Number(limit) || 6, 1), 20);
    ok(res, await search(kb, query, embedding, n));
  } catch (e) {
    fail(res, 400, e.message);
  }
});

app.use(
  "/samples",
  express.static(SAMPLES, {
    setHeaders: (res) => res.setHeader("Cache-Control", "no-store"),
  })
);

app.use("/models", (req, res, next) => {
  res.setHeader("Cache-Control", "public, max-age=31536000, immutable");
  next();
});

if (fs.existsSync(WEB_DIST)) {
  app.use(express.static(WEB_DIST, { index: "index.html" }));
  app.get(/^(?!\/api\/|\/samples\/).*/, (_req, res) =>
    res.sendFile(path.join(WEB_DIST, "index.html"))
  );
} else {
  app.get("/", (_req, res) =>
    res
      .status(503)
      .type("text/plain")
      .send("web/dist 不存在：先运行 npm run build，或使用 npm run dev。")
  );
}

app.use((err, _req, res, _next) => {
  console.error(err);
  fail(res, 500, err.message);
});

console.log(`正在启动 seekdb（首次启动需初始化数据目录）…`);
await connect();
await ensureDefaultKb();
const info = await serverInfo();
console.log(`seekdb ${info.version} @ ${info.host}:${info.port}`);
app.listen(PORT, "127.0.0.1", () => {
  console.log(`端阅已启动：http://127.0.0.1:${PORT}`);
});

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, async () => {
    console.log("正在关闭 seekdb…");
    await close();
    process.exit(0);
  });
}
