async function call(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `请求失败（HTTP ${res.status}）`);
  }
  return data;
}

export const api = {
  health: () => call("/api/health"),
  kbs: () => call("/api/kbs"),
  createKb: () => call("/api/kbs", { method: "POST", body: "{}" }),
  activate: (name) =>
    call("/api/session", { method: "POST", body: JSON.stringify({ name }) }),
  ingest: (kb, doc, chunks, { deferRefresh = false } = {}) =>
    call("/api/ingest", {
      method: "POST",
      body: JSON.stringify({ kb, doc, chunks, deferRefresh }),
    }),
  refresh: (kb) =>
    call("/api/refresh", { method: "POST", body: JSON.stringify({ kb }) }),
  search: (kb, query, embedding, limit) =>
    call("/api/search", {
      method: "POST",
      body: JSON.stringify({ kb, query, embedding, limit }),
    }),
};
