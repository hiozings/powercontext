(function () {
  "use strict";

  var CORPUS = window.XZ;
  if (!CORPUS) return;

  var els = {
    form: document.getElementById("search-form"),
    input: document.getElementById("query"),
    results: document.getElementById("results"),
    memoryToggle: document.getElementById("memory-toggle"),
    memoryStrip: document.getElementById("memory-strip"),
    pipeExpand: document.getElementById("pipe-expand"),
    pipeRecall: document.getElementById("pipe-recall"),
    pipeRank: document.getElementById("pipe-rank"),
    pipeOut: document.getElementById("pipe-out"),
    dialog: document.getElementById("detail"),
    dialogKicker: document.getElementById("detail-kicker"),
    dialogTitle: document.getElementById("detail-title"),
    dialogMeta: document.getElementById("detail-meta"),
    dialogText: document.getElementById("detail-text"),
    dialogSignals: document.getElementById("detail-signals"),
    dialogClose: document.getElementById("detail-close"),
    copy: document.getElementById("copy-citation"),
    toast: document.getElementById("toast"),
    engineBadge: document.getElementById("engine-badge")
  };

  var live = { enabled: false, info: null, controller: null, seq: 0 };

  var index = [];
  var idf = new Map();
  var avgdl = 0;
  var currentResults = [];
  var memoryWrites = 0;
  var searchTimer = null;
  var toastTimer = null;
  var lastQuery = "";

  var FOCUS_TAGS = ["切块", "边界", "评估", "召回"];

  function tokenize(text) {
    var s = String(text || "").toLowerCase();
    var out = [];
    var latin = s.match(/[a-z0-9][a-z0-9.+#_-]*/g) || [];
    latin.forEach(function (t) { out.push(t); });
    var runs = s.match(/[\u4e00-\u9fff]+/g) || [];
    runs.forEach(function (run) {
      if (run.length === 1) {
        out.push(run);
      } else {
        for (var i = 0; i < run.length - 1; i++) out.push(run.slice(i, i + 2));
      }
    });
    return out;
  }

  function buildIndex() {
    CORPUS.docs.forEach(function (doc) {
      doc.chunks.forEach(function (chunk, i) {
        var tokens = tokenize(doc.title + " " + doc.tags.join(" ") + " " + chunk.text);
        var tf = new Map();
        tokens.forEach(function (t) { tf.set(t, (tf.get(t) || 0) + 1); });
        index.push({
          key: doc.code + "-" + i,
          doc: doc,
          chunk: chunk,
          tokens: tokens,
          tf: tf,
          len: tokens.length,
          set: new Set(tokens)
        });
      });
    });

    index.forEach(function (item) {
      item.set.forEach(function (t) {
        idf.set(t, (idf.get(t) || 0) + 1);
      });
    });
    idf.forEach(function (df, t) {
      idf.set(t, Math.log(1 + (index.length - df + 0.5) / (df + 0.5)));
    });
    var total = 0;
    index.forEach(function (item) { total += item.len; });
    avgdl = total / index.length;
  }

  function bm25(tokens, item) {
    var k1 = 1.2;
    var b = 0.75;
    var score = 0;
    tokens.forEach(function (weight, t) {
      var tf = item.tf.get(t);
      if (!tf) return;
      var w = idf.get(t) || 0;
      score += weight * w * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * item.len / avgdl));
    });
    return score;
  }

  function cosine(a, b) {
    var dot = 0;
    var na = 0;
    var nb = 0;
    Object.keys(a).forEach(function (k) {
      var x = a[k] || 0;
      var y = b[k] || 0;
      dot += x * y;
      na += x * x;
    });
    Object.keys(b).forEach(function (k) { nb += b[k] * b[k]; });
    if (!na || !nb) return 0;
    return dot / (Math.sqrt(na) * Math.sqrt(nb));
  }

  function conceptFromText(text, weight, target) {
    var lower = text.toLowerCase();
    CORPUS.termConcepts.forEach(function (row) {
      var hit = row.terms.some(function (t) { return lower.indexOf(t) !== -1; });
      if (!hit) return;
      Object.keys(row.c).forEach(function (k) {
        target[k] = (target[k] || 0) + row.c[k] * weight;
      });
    });
  }

  function buildQuery(query, memoryOn) {
    var lower = query.toLowerCase();
    var concepts = {};
    conceptFromText(lower, 1, concepts);

    var expanded = [];
    if (memoryOn) {
      Object.keys(CORPUS.expansions).forEach(function (key) {
        if (lower.indexOf(key) === -1) return;
        CORPUS.expansions[key].forEach(function (term) {
          if (expanded.indexOf(term) === -1) expanded.push(term);
        });
      });
      expanded.forEach(function (term) { conceptFromText(term, 0.45, concepts); });
    }

    var tokens = new Map();
    tokenize(query).forEach(function (t) { tokens.set(t, 1); });
    if (memoryOn) {
      expanded.forEach(function (term) {
        tokenize(term).forEach(function (t) {
          tokens.set(t, Math.max(tokens.get(t) || 0, 0.45));
        });
      });
    }

    return { concepts: concepts, tokens: tokens, expanded: expanded };
  }

  function memoryBoost(doc, memoryOn) {
    if (!memoryOn) return 0;
    var hits = doc.tags.filter(function (tag) { return FOCUS_TAGS.indexOf(tag) !== -1; }).length;
    return Math.min(0.06, hits * 0.02);
  }

  function search(query, memoryOn) {
    if (live.enabled) {
      liveSearch(query, memoryOn);
      return;
    }
    localSearch(query, memoryOn);
  }

  function localSearch(query, memoryOn) {
    var q = query.trim();
    if (!q) {
      renderEmpty("输入一个问题，开始检索。");
      updatePipeline(null);
      return;
    }

    var parsed = buildQuery(q, memoryOn);
    var scored = index.map(function (item) {
      var sem = cosine(parsed.concepts, item.chunk.concepts);
      var kw = bm25(parsed.tokens, item);
      return {
        item: item,
        semRaw: sem,
        kwRaw: kw,
        boost: memoryBoost(item.doc, memoryOn)
      };
    });

    var maxSem = 0;
    var maxKw = 0;
    scored.forEach(function (s) {
      if (s.semRaw > maxSem) maxSem = s.semRaw;
      if (s.kwRaw > maxKw) maxKw = s.kwRaw;
    });

    var results = scored.map(function (s) {
      var sem = maxSem ? s.semRaw / maxSem : 0;
      var kw = maxKw ? s.kwRaw / maxKw : 0;
      return {
        item: s.item,
        sem: sem,
        kw: kw,
        boost: s.boost,
        score: Math.min(1, 0.55 * sem + 0.45 * kw + s.boost)
      };
    }).sort(function (a, b) { return b.score - a.score; });

    var candidates = results.filter(function (r) { return r.score > 0.08; });
    var top = candidates.slice(0, 4);
    currentResults = top;

    renderResults(top, q, parsed.tokens);
    renderMemoryStrip(parsed.expanded, memoryOn);
    updatePipeline({
      parsed: parsed,
      memoryOn: memoryOn,
      semCount: results.filter(function (r) { return r.sem > 0.02; }).length,
      kwCount: results.filter(function (r) { return r.kw > 0; }).length,
      count: top.length
    });
    lastQuery = q;
  }

  function renderResults(results, query, queryTokens) {
    els.results.innerHTML = "";
    els.results.setAttribute("aria-busy", "false");

    if (!results.length) {
      renderEmpty("没有找到足够相关的结果。试试更具体的说法，例如「切块边界」或「评估指标」。");
      return;
    }

    var highlightTerms = [];
    queryTokens.forEach(function (weight, token) {
      if (token.length >= 2) highlightTerms.push(token);
    });

    results.forEach(function (result, i) {
      var doc = docOf(result);
      var chunk = chunkOf(result);
      var key = keyOf(result);
      var article = document.createElement("article");
      article.className = "result";
      article.dataset.key = key;
      article.style.setProperty("--delay", i * 45 + "ms");

      var pct = Math.round(result.score * 100);
      article.innerHTML =
        '<div class="result-rank" aria-hidden="true">' + String(i + 1).padStart(2, "0") + "</div>" +
        '<div class="result-body">' +
          '<div class="result-meta">' +
            '<span class="kind">' + escapeHtml(doc.kind) + "</span>" +
            '<span class="doc">《' + escapeHtml(doc.title) + "》</span>" +
            "<span>" + doc.year + "</span>" +
            '<span class="page">p.' + chunk.page + "</span>" +
          "</div>" +
          '<p class="result-text">' + highlight(chunk.text, highlightTerms) + "</p>" +
          '<div class="result-foot">' +
            '<span class="signal"><span class="sig-label">语义</span><span class="sig-bar"><i style="width:' + Math.round(result.sem * 100) + '%"></i></span><span class="sig-num">' + result.sem.toFixed(2) + "</span></span>" +
            '<span class="signal"><span class="sig-label">关键词</span><span class="sig-bar"><i style="width:' + Math.round(result.kw * 100) + '%"></i></span><span class="sig-num">' + result.kw.toFixed(2) + "</span></span>" +
            '<span class="result-score" title="融合分数">融合 ' + (pct / 100).toFixed(2) + "</span>" +
            '<button type="button" class="result-open" data-key="' + key + '">查看原文 <span aria-hidden="true">→</span></button>' +
          "</div>" +
        "</div>";

      els.results.appendChild(article);
    });
  }

  function renderEmpty(message) {
    els.results.innerHTML = '<p class="results-empty">' + escapeHtml(message) + "</p>";
    els.results.setAttribute("aria-busy", "false");
    currentResults = [];
  }

  function renderMemoryStrip(expanded, memoryOn) {
    if (!memoryOn) {
      els.memoryStrip.innerHTML =
        '<span class="mem-badge is-off">记忆暂停</span>' +
        '<span class="mem-line">本次检索使用原始查询，不读取研究主题与阅读记录。</span>';
      return;
    }

    var chips = expanded.slice(0, 5).map(function (term) {
      return "<i>" + escapeHtml(term) + "</i>";
    }).join("");

    els.memoryStrip.innerHTML =
      '<span class="mem-badge">演示记忆</span>' +
      '<span class="mem-line">主题 <b>' + escapeHtml(CORPUS.profile.topic) + "</b> · 焦点 <b>" + escapeHtml(CORPUS.profile.focus[0]) + "</b> · 已读 <b>" + CORPUS.profile.read + " / " + CORPUS.profile.total + "</b></span>" +
      (chips ? '<span class="mem-expand">扩展 ' + chips + "</span>" : '<span class="mem-expand is-quiet">本次查询无需扩展</span>') +
      '<span class="mem-write" id="mem-write">已写回 ' + memoryWrites + " 条</span>";
  }

  function updatePipeline(state) {
    if (!state) {
      els.pipeExpand.textContent = "读取研究记忆，扩展同义表达";
      els.pipeRecall.textContent = "向量 · 全文 · 元数据";
      els.pipeRank.textContent = "0.55 语义 + 0.45 关键词";
      els.pipeOut.textContent = "文档 · 页码 · 原文片段";
      return;
    }

    if (state.memoryOn) {
      els.pipeExpand.textContent = state.parsed.expanded.length
        ? "记忆扩展：" + state.parsed.expanded.slice(0, 3).join(" · ")
        : "已读取记忆，本次无需扩展";
    } else {
      els.pipeExpand.textContent = "未使用记忆，按原样检索";
    }

    if (state.live) {
      els.pipeRecall.textContent = "seekdb 向量候选 " + state.stats.candidates + " · 全文命中 " + state.stats.keyword_hits;
      els.pipeRank.textContent = "0.55 语义 + 0.45 关键词" + (state.memoryOn ? " + 记忆加成" : "") + " · " + state.stats.elapsed_ms + " ms";
      els.pipeOut.textContent = state.count + " 条结果 · 均带页码";
      return;
    }

    els.pipeRecall.textContent = "语义 " + state.semCount + " · 关键词 " + state.kwCount + " · 过滤 0";
    els.pipeRank.textContent = "0.55 语义 + 0.45 关键词" + (state.memoryOn ? " + 记忆加成" : "");
    els.pipeOut.textContent = state.count + " 条结果 · 均带页码";
  }

  function openDetail(key) {
    var found = currentResults.filter(function (r) { return keyOf(r) === key; })[0];
    if (!found) return;
    var doc = docOf(found);
    var chunk = chunkOf(found);

    els.dialogKicker.textContent = "原文片段 · " + (live.enabled ? "seekdb 检索结果" : "内置样例") + " · " + doc.kind;
    els.dialogTitle.textContent = "《" + doc.title + "》";
    els.dialogMeta.textContent = doc.year + " · " + doc.venue + " · p." + chunk.page;
    els.dialogText.textContent = chunk.text;
    els.dialogSignals.innerHTML =
      '<span><b>' + found.sem.toFixed(2) + "</b>语义（" + (live.enabled ? "seekdb 向量" : "概念向量") + "）</span>" +
      '<span><b>' + found.kw.toFixed(2) + "</b>关键词（" + (live.enabled ? "ngram 全文" : "BM25") + "）</span>" +
      '<span><b>' + found.score.toFixed(2) + "</b>融合分数" + (found.boost ? "（含记忆加成）" : "") + "</span>";
    els.copy.dataset.citation = "《" + doc.title + "》，" + doc.year + "，" + doc.venue + "，第 " + chunk.page + " 页。";

    if (typeof els.dialog.showModal === "function") {
      els.dialog.showModal();
    } else {
      els.dialog.setAttribute("open", "");
    }
  }

  function closeDetail() {
    if (typeof els.dialog.close === "function") {
      els.dialog.close();
    } else {
      els.dialog.removeAttribute("open");
    }
  }

  function copyCitation() {
    var text = els.copy.dataset.citation || "";
    if (!text) return;
    var done = function () { showToast("引用已复制"); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else {
      fallbackCopy(text);
      done();
    }
  }

  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); } catch (e) { /* 忽略复制失败 */ }
    document.body.removeChild(area);
  }

  function showToast(message) {
    els.toast.textContent = message;
    els.toast.classList.add("is-on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { els.toast.classList.remove("is-on"); }, 1800);
  }

  function scheduleSearch() {
    clearTimeout(searchTimer);
    els.results.classList.add("is-loading");
    els.results.setAttribute("aria-busy", "true");
    searchTimer = setTimeout(function () {
      els.results.classList.remove("is-loading");
      search(searchTimerQuery, els.memoryToggle.checked);
      if (els.memoryToggle.checked) {
        memoryWrites += 1;
        var write = document.getElementById("mem-write");
        if (write) write.textContent = "已写回 " + memoryWrites + " 条";
      }
    }, 170);
  }

  var searchTimerQuery = "";

  function runSearch() {
    searchTimerQuery = els.input.value;
    scheduleSearch();
  }

  function docOf(result) { return result.doc || result.item.doc; }
  function chunkOf(result) { return result.chunk || result.item.chunk; }
  function keyOf(result) { return result.key || result.item.key; }

  function probeLive() {
    if (!window.fetch || !window.AbortController) return;
    // 只在本地检索服务（默认 8787）或显式 ?live=1 时探测，静态托管下不产生多余请求
    var onService = window.location.port === "8787" || /[?&]live=1\b/.test(window.location.search);
    if (!onService) return;
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, 1500);
    fetch("/api/health", { signal: controller.signal, cache: "no-store" })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (info) {
        clearTimeout(timer);
        if (!info || info.engine !== "seekdb") return;
        live.enabled = true;
        live.info = info;
        setEngineBadge("live");
        search(els.input.value, els.memoryToggle.checked);
      })
      .catch(function () { clearTimeout(timer); });
  }

  function setEngineBadge(mode) {
    if (!els.engineBadge) return;
    if (mode === "live" && live.info) {
      var version = String(live.info.version).replace(/^.*seekdb-/, "seekdb ");
      els.engineBadge.textContent = version + " · " + live.info.chunks + " 个片段 · " +
        (live.info.memory.reachable ? "PowerContext 已连接" : "PowerContext 未连接");
      els.engineBadge.classList.add("is-live");
      return;
    }
    if (mode === "fallback") {
      els.engineBadge.textContent = "内置样例语料 · 5 篇 / 12 个片段";
      els.engineBadge.classList.remove("is-live");
    }
  }

  function liveSearch(query, memoryOn) {
    var q = query.trim();
    if (!q) {
      renderEmpty("输入一个问题，开始检索。");
      updatePipeline(null);
      return;
    }

    if (live.controller) live.controller.abort();
    var controller = new AbortController();
    live.controller = controller;
    var seq = ++live.seq;
    var timer = setTimeout(function () { controller.abort(); }, 4000);
    els.results.setAttribute("aria-busy", "true");

    fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: q, memory: memoryOn, limit: 5 }),
      signal: controller.signal
    }).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    }).then(function (data) {
      clearTimeout(timer);
      if (seq !== live.seq) return;
      applyLive(data, q, memoryOn);
    }).catch(function (err) {
      clearTimeout(timer);
      if (seq !== live.seq || (err && err.name === "AbortError")) return;
      live.enabled = false;
      setEngineBadge("fallback");
      showToast("seekdb 服务不可用，已切回内置演示");
      localSearch(q, memoryOn);
    });
  }

  function applyLive(data, query, memoryOn) {
    var tokens = new Map();
    tokenize(query).forEach(function (t) { tokens.set(t, 1); });
    var results = (data.results || []).map(function (r) {
      return {
        key: r.chunk_id,
        doc: {
          code: r.doc_code, title: r.doc_title, year: r.doc_year,
          venue: r.doc_venue, kind: r.doc_kind, tags: r.tags || []
        },
        chunk: { page: r.page, text: r.content },
        sem: r.sem_norm,
        kw: r.kw_norm,
        score: r.score,
        boost: r.boost
      };
    });

    currentResults = results;
    els.results.setAttribute("aria-busy", "false");
    renderResults(results, query, tokens);
    renderLiveMemory(data.memory);
    updatePipeline({
      parsed: { expanded: (data.memory && data.memory.expansions) || [] },
      memoryOn: memoryOn,
      live: true,
      stats: data.stats,
      count: results.length
    });
    lastQuery = query;
  }

  function renderLiveMemory(mem) {
    if (!mem || !mem.enabled) {
      els.memoryStrip.innerHTML =
        '<span class="mem-badge is-off">记忆暂停</span>' +
        '<span class="mem-line">本次检索不读取 PowerContext，也不写回检索记录。</span>';
      return;
    }

    var profile = mem.profile || CORPUS.profile;
    var chips = (mem.expansions || []).slice(0, 5).map(function (term) {
      return "<i>" + escapeHtml(term) + "</i>";
    }).join("");
    var recalled = (mem.recalled || []).length;

    els.memoryStrip.innerHTML =
      '<span class="mem-badge">' + (mem.source === "powercontext" ? "PowerContext" : "本地画像") + "</span>" +
      '<span class="mem-line">主题 <b>' + escapeHtml(profile.topic) + "</b> · 焦点 <b>" + escapeHtml(profile.focus[0]) + "</b> · 已读 <b>" + profile.read + " / " + profile.total + "</b></span>" +
      (chips ? '<span class="mem-expand">扩展 ' + chips + "</span>" : '<span class="mem-expand is-quiet">本次查询无需扩展</span>') +
      '<span class="mem-write">' + (recalled ? "召回 " + recalled + " 条历史检索" : "记忆已写回") + "</span>";
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function highlight(text, terms) {
    var unique = [];
    terms.forEach(function (t) {
      if (t.length >= 2 && unique.indexOf(t) === -1) unique.push(t);
    });
    if (!unique.length) return escapeHtml(text);

    var ranges = [];
    unique.forEach(function (term) {
      var lowerText = text.toLowerCase();
      var lowerTerm = term.toLowerCase();
      var from = 0;
      while (from < lowerText.length) {
        var at = lowerText.indexOf(lowerTerm, from);
        if (at === -1) break;
        ranges.push([at, at + lowerTerm.length]);
        from = at + 1;
      }
    });
    if (!ranges.length) return escapeHtml(text);

    ranges.sort(function (a, b) { return a[0] - b[0] || b[1] - a[1]; });
    var merged = [];
    ranges.forEach(function (range) {
      var last = merged[merged.length - 1];
      if (last && range[0] <= last[1]) {
        last[1] = Math.max(last[1], range[1]);
      } else {
        merged.push(range.slice());
      }
    });

    var out = "";
    var pos = 0;
    merged.forEach(function (range) {
      out += escapeHtml(text.slice(pos, range[0]));
      out += "<mark>" + escapeHtml(text.slice(range[0], range[1])) + "</mark>";
      pos = range[1];
    });
    out += escapeHtml(text.slice(pos));
    return out;
  }

  function bindEvents() {
    els.form.addEventListener("submit", function (event) {
      event.preventDefault();
      runSearch();
    });

    els.input.addEventListener("input", runSearch);

    document.querySelectorAll(".chip[data-q]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        els.input.value = chip.dataset.q;
        runSearch();
        els.input.focus();
      });
    });

    els.memoryToggle.addEventListener("change", function () {
      runSearch();
    });

    els.results.addEventListener("click", function (event) {
      var button = event.target.closest("[data-key]");
      var card = event.target.closest(".result");
      var key = button ? button.dataset.key : card ? card.dataset.key : null;
      if (key) openDetail(key);
    });

    els.dialogClose.addEventListener("click", closeDetail);
    els.copy.addEventListener("click", copyCitation);
    els.dialog.addEventListener("click", function (event) {
      if (event.target === els.dialog) closeDetail();
    });
  }

  buildIndex();
  bindEvents();
  renderMemoryStrip([], true);
  search(els.input.value, true);
  els.results.classList.remove("is-loading");
  probeLive();
})();
