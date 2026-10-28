"""PowerContext 记忆层：把阅读画像与检索历史存进 scope，检索时读回参与排序。"""

import json
import os
import time
import urllib.error
import urllib.request

SCOPE_TITLE = "寻章 · 文献检索与阅读记忆助手"
SCOPE_SUMMARY = "阅读画像、关注点与检索历史"


class PowerContextMemory:
    def __init__(self):
        self.base_url = os.environ.get("POWERCONTEXT_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        self.token = os.environ.get("POWERCONTEXT_TOKEN", "dev-token")
        self.scope_key = os.environ.get("XUNZHANG_SCOPE_KEY", "xunzhang-reader-demo")
        self._scope_id = None
        self._reachable = None
        self._checked_at = 0.0

    # ---------- HTTP ----------

    def _call(self, method, path, payload=None, timeout=1.5):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}

    def reachable(self):
        now = time.monotonic()
        if self._reachable is not None and now - self._checked_at < 5:
            return self._reachable
        try:
            self._call("GET", "/health/live", timeout=1.0)
            self._reachable = True
        except (urllib.error.URLError, OSError, ValueError):
            self._reachable = False
            self._scope_id = None
        self._checked_at = now
        return self._reachable

    def ensure_scope(self):
        if self._scope_id:
            return self._scope_id
        if not self.reachable():
            return None
        try:
            data = self._call(
                "POST",
                "/v1/scopes",
                {
                    "title": SCOPE_TITLE,
                    "summary": SCOPE_SUMMARY,
                    "idempotency_key": self.scope_key,
                },
            )
            self._scope_id = data.get("scope_id")
        except (urllib.error.URLError, OSError, ValueError):
            self._reachable = False
            self._scope_id = None
        return self._scope_id

    def entries(self, limit=50):
        scope_id = self.ensure_scope()
        if not scope_id:
            return []
        try:
            data = self._call(
                "POST", "/v1/memory/entries/list", {"scope_id": scope_id, "limit": limit}
            )
        except (urllib.error.URLError, OSError, ValueError):
            self._reachable = False
            return []
        return data.get("entries", [])

    def search(self, query, limit=5):
        scope_id = self.ensure_scope()
        if not scope_id:
            return []
        try:
            data = self._call(
                "POST",
                "/v1/memory/search",
                # 未配置 embedding 模型时 auto 模式可能返回空，这里固定用全文检索
                {"scope_id": scope_id, "query": query, "limit": limit, "mode": "fts"},
            )
        except (urllib.error.URLError, OSError, ValueError):
            self._reachable = False
            return []
        texts = []
        for hit in data.get("hits", []):
            text = hit.get("text") or (hit.get("memory") or {}).get("text")
            if text:
                texts.append(text)
        return texts

    def remember(self, text, kind="search", reason="寻章检索"):
        scope_id = self.ensure_scope()
        if not scope_id:
            return False
        try:
            self._call(
                "POST",
                "/v1/memory/remember",
                {"scope_id": scope_id, "kind": kind, "text": text, "reason": reason},
            )
            return True
        except (urllib.error.URLError, OSError, ValueError):
            self._reachable = False
            return False

    # ---------- 画像 ----------

    def profile_entries(self, profile):
        return [
            f"研究方向：{profile['topic']}",
            f"关注点：{' / '.join(profile['focus'])}",
            f"已读 {profile['read']} / {profile['total']} 篇，常用方法：{' / '.join(profile['methods'])}",
        ]

    def ensure_profile(self, profile):
        """首次启动把阅读画像写进 PowerContext；已有条目则不重复写。"""
        if not self.entries(limit=1):
            for text in self.profile_entries(profile):
                self.remember(text, kind="profile", reason="阅读画像初始化")

    def status(self):
        if not self.reachable():
            return {"reachable": False, "scope_id": None, "entry_count": 0}
        scope_id = self.ensure_scope()
        return {"reachable": bool(scope_id), "scope_id": scope_id, "entry_count": len(self.entries(limit=100))}
