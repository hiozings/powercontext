import { api } from "./api.js";
import { createEmbedder, QUERY_PREFIX } from "./embedder.js";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const DTYPE =
  new URLSearchParams(location.search).get("dtype") === "fp32" ? "fp32" : "q8";

const state = {
  dtype: DTYPE,
  embedder: null,
  device: null,
  reason: "",
  loadMs: 0,
  warmMs: 0,
  kb: null,
  kbs: [],
  queue: [],
  busy: false,
  lastIngest: null,
  lastSearch: null,
  bench: null,
};

const els = {
  modelPill: $("#pill-model"),
  devicePill: $("#pill-device"),
  dbPill: $("#pill-db"),
  kbSelect: $("#kb-select"),
  kbStat: $("#kb-stat"),
  kbNew: $("#kb-new"),
  dropzone: $("#dropzone"),
  fileInput: $("#file-input"),
  pasteTitle: $("#paste-title"),
  pasteText: $("#paste-text"),
  pasteAdd: $("#paste-add"),
  queue: $("#queue"),
  importBtn: $("#import-btn"),
  sampleBtn: $("#sample-btn"),
  progress: $("#progress"),
  progressLabel: $("#progress-label"),
  progressCount: $("#progress-count"),
  progressBar: $("#progress-bar"),
  progressDetail: $("#progress-detail"),
  mLoad: $("#m-load"),
  mEmbed: $("#m-embed"),
  mAdd: $("#m-add"),
  mRefresh: $("#m-refresh"),
  batch: $("#batch-size"),
  searchForm: $("#search-form"),
  query: $("#query"),
  searchBtn: $("#search-btn"),
  suggestions: $("#suggestions"),
  searchMeta: $("#search-meta"),
  qEmbed: $("#q-embed"),
  qDb: $("#q-db"),
  qTotal: $("#q-total"),
  results: $("#results"),
  searchEmpty: $("#search-empty"),
  evalBtn: $("#eval-btn"),
  evalRows: $("#eval-rows"),
  runDevice: $("#run-device"),
  runLoad: $("#run-load"),
  runThroughput: $("#run-throughput"),
  runIngest: $("#run-ingest"),
  runSearch: $("#run-search"),
  runE2e: $("#run-e2e"),
  evalRecall: $("#eval-recall"),
  evalNdcg: $("#eval-ndcg"),
  evalCount: $("#eval-count"),
  toast: $("#toast"),
  sizeInt8: $("#size-int8"),
  ratio: $("#ratio"),
};

function setPill(el, text, kind = "") {
  el.classList.remove("is-ok", "is-warn", "is-err", "is-busy");
  if (kind) el.classList.add(kind);
  el.innerHTML = "<i></i>";
  el.append(document.createTextNode(text));
}

let toastTimer = null;
function toast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle("is-error", isError);
  els.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.remove("show"), 3200);
}

const fmtMs = (ms) =>
  ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;
const fmtMb = (bytes) => `${(bytes / 1024 / 1024).toFixed(1)} MB`;
const escapeHtml = (s) =>
  s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function chunkText(text, size = 420, overlap = 60) {
  const clean = text.replace(/\r\n/g, "\n").replace(/[ \t]+\n/g, "\n").trim();
  if (!clean) return [];
  if (clean.length <= size) return [clean];
  const parts = [];
  let start = 0;
  while (start < clean.length) {
    let end = Math.min(start + size, clean.length);
    if (end < clean.length) {
      const slice = clean.slice(start, end);
      const candidates = ["。", "\n", "；", "！", "？", ". "].map((p) =>
        slice.lastIndexOf(p)
      );
      const cut = Math.max(...candidates);
      if (cut > size * 0.55) end = start + cut + 1;
    }
    const piece = clean.slice(start, end).trim();
    if (piece) parts.push(piece);
    if (end >= clean.length) break;
    start = Math.max(end - overlap, start + 1);
  }
  return parts;
}

function slugify(name) {
  return (
    name
      .toLowerCase()
      .replace(/\.[^.]+$/, "")
      .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40) || "doc"
  );
}

function setBusy(busy) {
  state.busy = busy;
  els.importBtn.disabled = busy || !state.queue.length || !state.embedder;
  els.sampleBtn.disabled = busy || !state.embedder;
  els.searchBtn.disabled = busy || !state.embedder;
  els.evalBtn.disabled = busy || !state.embedder;
  els.kbNew.disabled = busy;
}

function showProgress(label, count, pct, detail = "") {
  els.progress.hidden = false;
  els.progressLabel.textContent = label;
  els.progressCount.textContent = count;
  els.progressBar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  els.progressDetail.textContent = detail;
}

function hideProgress() {
  els.progress.hidden = true;
}

async function loadModel() {
  setPill(els.modelPill, "模型加载中 0%", "is-busy");
  try {
    const embedder = await createEmbedder({
      dtype: DTYPE,
      onProgress: ({ file, progress }) => {
        setPill(
          els.modelPill,
          `模型加载中 ${Math.round(progress)}%`,
          "is-busy"
        );
        if (file) els.modelPill.title = file;
      },
    });
    state.embedder = embedder;
    state.device = embedder.device;
    state.reason = embedder.reason;
    state.loadMs = embedder.loadMs;
    state.warmMs = embedder.warmMs;
    setPill(
      els.modelPill,
      `BGE-small-zh-v1.5 · ${DTYPE === "fp32" ? "FP32" : "INT8"}`,
      "is-ok"
    );
    els.modelPill.title = "模型与分词器均从本站静态资源加载，推理在浏览器内执行";
    setPill(
      els.devicePill,
      embedder.device === "webgpu" ? "WebGPU 推理" : "WASM 推理（已降级）",
      embedder.device === "webgpu" ? "is-ok" : "is-warn"
    );
    els.devicePill.title =
      embedder.reason ||
      (embedder.device === "webgpu"
        ? "已启用 WebGPU 执行后端"
        : "未获得 WebGPU，自动降级到 WASM");
    els.runDevice.textContent =
      embedder.device === "webgpu"
        ? `WebGPU（${DTYPE === "fp32" ? "FP32" : "INT8"}）`
        : `WASM（${DTYPE === "fp32" ? "FP32" : "INT8"}，自动降级）`;
    els.runLoad.textContent = fmtMs(embedder.loadMs + embedder.warmMs);
    els.mLoad.textContent = fmtMs(embedder.loadMs + embedder.warmMs);
    setBusy(false);
    if (embedder.device === "wasm" && embedder.reason) {
      console.info(`[端阅] ${embedder.reason}，使用 WASM 后端`);
    }
  } catch (err) {
    setPill(els.modelPill, "模型加载失败", "is-err");
    toast(`模型加载失败：${err.message}`, true);
    console.warn("[端阅] 模型加载失败", err);
  }
}

async function refreshKbs(preferred) {
  const data = await api.kbs();
  state.kbs = data.collections;
  const active = preferred || data.active;
  state.kb = active;
  els.kbSelect.innerHTML = "";
  for (const kb of data.collections) {
    const opt = document.createElement("option");
    opt.value = kb.name;
    opt.textContent = `${kb.label} · ${kb.chunks} 片段`;
    if (kb.name === active) opt.selected = true;
    els.kbSelect.append(opt);
  }
  const current = data.collections.find((k) => k.name === active);
  els.kbStat.textContent = `${current ? current.chunks : 0} 个片段`;
  return current;
}

async function connect() {
  try {
    const health = await api.health();
    setPill(
      els.dbPill,
      `seekdb ${health.seekdb.version.split("seekdb-")[1] || "1.4.0"}`,
      "is-ok"
    );
    els.dbPill.title = `${health.mode} · ${health.seekdb.host}:${health.seekdb.port} · ${health.engine}`;
    await refreshKbs();
  } catch (err) {
    setPill(els.dbPill, "seekdb 未连接", "is-err");
    toast(`无法连接本地服务：${err.message}`, true);
  }
}

function renderQueue() {
  els.queue.innerHTML = "";
  for (const [i, doc] of state.queue.entries()) {
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="doc-kind">${doc.kind || "TXT"}</span>
      <span class="doc-title">${escapeHtml(doc.title)}</span>
      <span class="doc-size">${doc.text.length} 字</span>
      <button type="button" class="remove" aria-label="移除 ${escapeHtml(doc.title)}">×</button>
    `;
    li.querySelector(".remove").addEventListener("click", () => {
      state.queue.splice(i, 1);
      renderQueue();
      setBusy(state.busy);
    });
    els.queue.append(li);
  }
  setBusy(state.busy);
}

function addDoc(doc) {
  const existing = state.queue.findIndex((d) => d.id === doc.id);
  if (existing >= 0) state.queue.splice(existing, 1);
  state.queue.push(doc);
  renderQueue();
}

async function readFiles(files) {
  for (const file of files) {
    if (!/\.(txt|md|markdown)$/i.test(file.name)) {
      toast(`跳过不支持的文件：${file.name}`, true);
      continue;
    }
    const text = await file.text();
    if (!text.trim()) continue;
    addDoc({
      id: slugify(file.name),
      title: file.name,
      source: file.name,
      text,
      kind: /\.md$/i.test(file.name) ? "MD" : "TXT",
    });
  }
}

async function ingestDocs(docs, { reportLabel = "导入" } = {}) {
  const groups = docs.map((doc) => ({ doc, chunks: chunkText(doc.text) }));
  const totalChunks = groups.reduce((n, g) => n + g.chunks.length, 0);
  if (!totalChunks) {
    toast("没有可导入的文本");
    return null;
  }

  const batchSize = Number(els.batch.value) || 8;
  const stats = { chunks: 0, embedMs: 0, addMs: 0, refreshMs: 0, docs: 0 };
  const flat = groups.flatMap((g) => g.chunks);

  showProgress(
    `${reportLabel} · 向量化`,
    `0 / ${totalChunks}`,
    0,
    `批大小 ${batchSize}`
  );
  const embedStart = performance.now();
  const vectors = [];
  for (let i = 0; i < flat.length; i += batchSize) {
    const slice = flat.slice(i, i + batchSize);
    const out = await state.embedder.embed(slice);
    vectors.push(...out);
    showProgress(
      `${reportLabel} · 向量化`,
      `${i + slice.length} / ${totalChunks}`,
      ((i + slice.length) / totalChunks) * 70,
      `批大小 ${batchSize}`
    );
  }
  stats.embedMs = performance.now() - embedStart;

  let cursor = 0;
  for (const { doc, chunks } of groups) {
    if (!chunks.length) continue;
    const vecs = vectors.slice(cursor, cursor + chunks.length);
    cursor += chunks.length;
    showProgress(
      `${reportLabel} · 写入 seekdb`,
      `${cursor} / ${totalChunks}`,
      72 + (cursor / totalChunks) * 20,
      `写入 ${chunks.length} 个片段与向量`
    );
    const res = await api.ingest(
      state.kb,
      { id: doc.id, title: doc.title, source: doc.source, text: doc.text },
      chunks.map((text, i) => ({ text, embedding: vecs[i] })),
      { deferRefresh: groups.length > 1 }
    );
    stats.addMs += res.addMs;
    stats.refreshMs += res.refreshMs;
    stats.chunks += chunks.length;
    stats.docs += 1;
  }

  if (groups.length > 1) {
    showProgress(
      `${reportLabel} · 刷新索引`,
      `${totalChunks} / ${totalChunks}`,
      95,
      "合并刷新一次"
    );
    const res = await api.refresh(state.kb);
    stats.refreshMs += res.refreshMs;
  }

  showProgress(`${reportLabel} · 完成`, `${totalChunks} / ${totalChunks}`, 100);
  setTimeout(hideProgress, 1200);

  const throughput = stats.chunks / (stats.embedMs / 1000);
  els.mEmbed.textContent = `${fmtMs(stats.embedMs)} · ${throughput.toFixed(
    1
  )} 片段/秒`;
  els.mAdd.textContent = fmtMs(stats.addMs);
  els.mRefresh.textContent = fmtMs(stats.refreshMs);
  els.runIngest.textContent = fmtMs(stats.addMs + stats.refreshMs);
  els.runThroughput.textContent = `${throughput.toFixed(1)} 片段/秒（批 ${batchSize}）`;
  state.lastIngest = { ...stats, batchSize, throughput };
  await refreshKbs(state.kb);
  return stats;
}

async function importQueue() {
  if (state.busy || !state.queue.length) return;
  setBusy(true);
  try {
    const docs = [...state.queue];
    const stats = await ingestDocs(docs);
    if (stats) {
      state.queue = [];
      renderQueue();
      toast(`已写入 ${stats.docs} 篇文档 / ${stats.chunks} 个片段`);
    }
  } catch (err) {
    hideProgress();
    toast(`导入失败：${err.message}`, true);
  } finally {
    setBusy(false);
  }
}

async function importSamples() {
  if (state.busy) return;
  setBusy(true);
  try {
    const corpus = await fetch("/samples/corpus.json").then((r) => r.json());
    const docs = corpus.map((d) => ({ ...d, kind: "SAMPLE" }));
    const stats = await ingestDocs(docs, { reportLabel: "示例语料" });
    if (stats) toast(`示例语料已入库：${stats.chunks} 个片段`);
  } catch (err) {
    hideProgress();
    console.error("[端阅] 示例导入失败", err);
    toast(`示例导入失败：${err.message}`, true);
  } finally {
    setBusy(false);
  }
}

function highlight(text, terms) {
  let html = escapeHtml(text);
  for (const term of terms) {
    if (!term) continue;
    html = html.replace(
      new RegExp(escapeRe(escapeHtml(term)), "gi"),
      (m) => `<mark>${m}</mark>`
    );
  }
  return html;
}

function termsOf(query) {
  return query
    .split(/[\s，。？！、；：,?.!;:（）()]+/)
    .filter((t) => t.length >= 2)
    .slice(0, 6);
}

function snippetOf(text, terms, max = 180) {
  const lower = text.toLowerCase();
  let at = -1;
  for (const term of terms) {
    at = lower.indexOf(term.toLowerCase());
    if (at >= 0) break;
  }
  if (at < 0) {
    return text.length > max ? `${text.slice(0, max)}…` : text;
  }
  const start = Math.max(0, at - 70);
  const end = Math.min(text.length, start + max);
  return `${start > 0 ? "…" : ""}${text.slice(start, end)}${
    end < text.length ? "…" : ""
  }`;
}

function renderResults(rows, query) {
  els.results.innerHTML = "";
  const terms = termsOf(query);
  if (!rows.length) {
    els.searchEmpty.hidden = false;
    els.searchEmpty.querySelector("p").textContent =
      "没有命中。试试更具体的关键词，或先导入相关文档。";
    return;
  }
  els.searchEmpty.hidden = true;
  rows.forEach((row, i) => {
    const li = document.createElement("li");
    li.className = "result";
    li.innerHTML = `
      <span class="rank">${String(i + 1).padStart(2, "0")}</span>
      <div class="result-body">
        <div class="result-head">
          <h3>${escapeHtml(row.title || "未命名文档")}</h3>
          <span class="score">RRF ${Number(row.score).toFixed(4)}</span>
        </div>
        <p class="snippet">${highlight(snippetOf(row.content || "", terms), terms)}</p>
        <div class="result-foot">
          <span>${escapeHtml(row.source || row.title || "")}</span>
          <span>片段 ${(row.chunk ?? 0) + 1}</span>
          <span>${row.chars || row.content?.length || 0} 字</span>
          <button type="button" class="expand">展开</button>
        </div>
      </div>
    `;
    const snippet = li.querySelector(".snippet");
    li.querySelector(".expand").addEventListener("click", (e) => {
      const expanded = li.classList.toggle("is-expanded");
      e.currentTarget.textContent = expanded ? "收起" : "展开";
      if (expanded) {
        snippet.innerHTML = highlight(row.content || "", terms);
      } else {
        snippet.innerHTML = highlight(
          snippetOf(row.content || "", terms),
          terms
        );
      }
    });
    els.results.append(li);
  });
}

async function runSearch(query) {
  if (!state.embedder || state.busy) return;
  const q = query.trim();
  if (!q) return;
  const t0 = performance.now();
  els.searchBtn.disabled = true;
  try {
    const embedStart = performance.now();
    const [vec] = await state.embedder.embed([QUERY_PREFIX + q]);
    const embedMs = performance.now() - embedStart;
    const res = await api.search(state.kb, q, vec, 6);
    const total = performance.now() - t0;
    state.lastSearch = { embedMs, dbMs: res.tookMs, total };
    renderResults(res.rows, q);
    els.searchMeta.hidden = false;
    els.qEmbed.textContent = fmtMs(embedMs);
    els.qDb.textContent = fmtMs(res.tookMs);
    els.qTotal.textContent = fmtMs(total);
    els.runSearch.textContent = fmtMs(res.tookMs);
    els.runE2e.textContent = fmtMs(total);
  } catch (err) {
    toast(`检索失败：${err.message}`, true);
  } finally {
    els.searchBtn.disabled = state.busy || !state.embedder;
  }
}

function median(values) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[mid]
    : (sorted[mid - 1] + sorted[mid]) / 2;
}

async function runEval() {
  if (state.busy) return;
  setBusy(true);
  try {
    const corpus = await fetch("/samples/corpus.json").then((r) => r.json());
    const evalSet = await fetch("/samples/eval.json").then((r) => r.json());
    const docs = corpus.map((d) => ({ ...d, kind: "SAMPLE" }));
    const stats = await ingestDocs(docs, { reportLabel: "评测语料" });

    const perQuery = [];
    for (const [i, item] of evalSet.entries()) {
      showProgress(
        "检索评测",
        `${i + 1} / ${evalSet.length}`,
        (i / evalSet.length) * 100,
        item.query
      );
      const t0 = performance.now();
      const [vec] = await state.embedder.embed([QUERY_PREFIX + item.query]);
      const res = await api.search(state.kb, item.query, vec, 5);
      const total = performance.now() - t0;
      const docIds = [];
      for (const row of res.rows) {
        if (!docIds.includes(row.doc_id)) docIds.push(row.doc_id);
      }
      const relevant = new Set(item.relevant);
      const hits = docIds.filter((id) => relevant.has(id));
      const recall = relevant.size ? hits.length / relevant.size : 0;
      let dcg = 0;
      docIds.slice(0, 5).forEach((id, rank) => {
        if (relevant.has(id)) dcg += 1 / Math.log2(rank + 2);
      });
      const ideal = Math.min(relevant.size, 5);
      let idcg = 0;
      for (let j = 0; j < ideal; j += 1) idcg += 1 / Math.log2(j + 2);
      const ndcg = idcg ? dcg / idcg : 0;
      perQuery.push({
        query: item.query,
        docs: docIds.slice(0, 5),
        recall,
        ndcg,
        ms: total,
      });
    }
    hideProgress();

    const recall = perQuery.reduce((s, q) => s + q.recall, 0) / perQuery.length;
    const ndcg = perQuery.reduce((s, q) => s + q.ndcg, 0) / perQuery.length;
    const searchMs = median(perQuery.map((q) => q.ms));
    state.bench = {
      timestamp: new Date().toISOString(),
      dtype: DTYPE,
      device: state.device,
      deviceReason: state.reason,
      model: `BGE-small-zh-v1.5 ${DTYPE === "fp32" ? "FP32" : "INT8"}`,
      modelBytes: state.modelBytes || null,
      loadMs: Math.round(state.loadMs + state.warmMs),
      ingest: stats,
      recall5: recall,
      ndcg5: ndcg,
      medianQueryMs: Math.round(searchMs),
      queries: perQuery,
    };
    window.__bench = state.bench;

    els.evalRecall.textContent = recall.toFixed(3);
    els.evalNdcg.textContent = ndcg.toFixed(3);
    els.evalCount.textContent = String(perQuery.length);
    els.evalRows.innerHTML = "";
    const table = document.createElement("table");
    table.className = "data-table eval-table";
    table.innerHTML = `<thead><tr><th>查询</th><th>前 5 文档</th><th>Recall</th><th>nDCG</th><th>耗时</th></tr></thead><tbody></tbody>`;
    const tbody = table.querySelector("tbody");
    perQuery.forEach((q) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(q.query)}</td>
        <td class="mono">${q.docs.map(escapeHtml).join(" · ")}</td>
        <td>${q.recall.toFixed(2)}</td>
        <td>${q.ndcg.toFixed(2)}</td>
        <td>${fmtMs(q.ms)}</td>
      `;
      tbody.append(tr);
    });
    els.evalRows.append(table);
    const oldLink = $("#eval-download");
    if (oldLink) oldLink.remove();
    const blob = new Blob([JSON.stringify(state.bench, null, 2)], {
      type: "application/json",
    });
    const link = document.createElement("a");
    link.id = "eval-download";
    link.href = URL.createObjectURL(blob);
    link.download = `duanyue-eval-${DTYPE}.json`;
    link.className = "text-btn download";
    link.textContent = "下载本次评测 JSON";
    els.evalRows.prepend(link);
    toast(`评测完成：Recall@5 ${recall.toFixed(3)} · nDCG@5 ${ndcg.toFixed(3)}`);
  } catch (err) {
    hideProgress();
    toast(`评测失败：${err.message}`, true);
  } finally {
    setBusy(false);
  }
}

function wire() {
  els.dropzone.addEventListener("click", () => els.fileInput.click());
  els.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      els.fileInput.click();
    }
  });
  els.fileInput.addEventListener("change", () => {
    readFiles(Array.from(els.fileInput.files || []));
    els.fileInput.value = "";
  });
  ["dragenter", "dragover"].forEach((evt) =>
    els.dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      els.dropzone.classList.add("is-over");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    els.dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      els.dropzone.classList.remove("is-over");
    })
  );
  els.dropzone.addEventListener("drop", (e) => {
    readFiles(Array.from(e.dataTransfer?.files || []));
  });

  els.pasteAdd.addEventListener("click", () => {
    const text = els.pasteText.value.trim();
    if (!text) {
      toast("先粘贴正文", true);
      return;
    }
    const title = els.pasteTitle.value.trim() || "粘贴片段";
    addDoc({
      id: `paste-${Date.now()}`,
      title,
      source: "粘贴文本",
      text,
      kind: "PASTE",
    });
    els.pasteTitle.value = "";
    els.pasteText.value = "";
    toast("已加入待导入");
  });

  els.importBtn.addEventListener("click", importQueue);
  els.sampleBtn.addEventListener("click", importSamples);
  els.evalBtn.addEventListener("click", runEval);

  els.kbNew.addEventListener("click", async () => {
    try {
      const { collection } = await api.createKb();
      await refreshKbs(collection.name);
      toast(`已新建 ${collection.label}`);
    } catch (err) {
      toast(`新建失败：${err.message}`, true);
    }
  });

  els.kbSelect.addEventListener("change", async () => {
    try {
      const { collection } = await api.activate(els.kbSelect.value);
      state.kb = collection.name;
      await refreshKbs(collection.name);
      toast(`切换到 ${collection.label}`);
    } catch (err) {
      toast(`切换失败：${err.message}`, true);
    }
  });

  els.searchForm.addEventListener("submit", (e) => {
    e.preventDefault();
    runSearch(els.query.value);
  });

  els.suggestions.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    els.query.value = btn.textContent;
    runSearch(btn.textContent);
  });

  els.batch.addEventListener("change", () => {
    if (state.lastIngest) {
      els.runThroughput.textContent = `${state.lastIngest.throughput.toFixed(
        1
      )} 片段/秒（批 ${state.lastIngest.batchSize}）`;
    }
  });
}

async function boot() {
  wire();
  setBusy(true);
  await connect();
  loadModel();
  fetch("/models/bge-small-zh-v1.5/manifest.json")
    .then((r) => r.json())
    .then((manifest) => {
      const q8 = manifest.files?.q8;
      const fp32 = manifest.files?.fp32;
      if (q8?.bytes) {
        els.sizeInt8.textContent = fmtMb(q8.bytes);
        if (DTYPE === "q8") state.modelBytes = q8.bytes;
      }
      if (fp32?.bytes) {
        $("#size-fp32").textContent = fmtMb(fp32.bytes);
        if (DTYPE === "fp32") state.modelBytes = fp32.bytes;
      }
      if (q8?.bytes && fp32?.bytes) {
        els.ratio.textContent = (fp32.bytes / q8.bytes).toFixed(2);
      }
    })
    .catch(() => {});
}

window.__app = state;
boot();
