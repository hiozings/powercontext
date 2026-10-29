"""Persist and reload SDD task state through PowerContext's HTTP API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8010"
DEFAULT_SCOPE_KEY = "sdd-books-demo"


class PowerContextState:
    def __init__(self, base_url: str | None = None, token: str | None = None, scope_key: str | None = None) -> None:
        self.base_url = (base_url or os.environ.get("POWERCONTEXT_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.token = token or os.environ.get("POWERCONTEXT_TOKEN")
        self.scope_key = scope_key or os.environ.get("POWERCONTEXT_SCOPE_KEY", DEFAULT_SCOPE_KEY)
        self._scope_id: str | None = None

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"PowerContext {path} failed: HTTP {error.code} {detail}") from error

    def ensure_scope(self) -> str:
        if self._scope_id:
            return self._scope_id
        response = self._post(
            "/v1/scopes",
            {
                "title": "SpecLens demo",
                "summary": "SDD task state for the bookstore price-filter task",
                "idempotency_key": self.scope_key,
            },
        )
        self._scope_id = str(response["scope_id"])
        return self._scope_id

    def write_state(self, state: dict[str, Any]) -> dict[str, Any]:
        payload = dict(state)
        payload.setdefault("updated_at", datetime.now(UTC).isoformat())
        return self._post(
            "/v1/memory/remember",
            {
                "scope_id": self.ensure_scope(),
                "kind": "sdd-task-state",
                "text": json.dumps(payload, ensure_ascii=False),
                "reason": f"SDD task {payload.get('task_id', '')}",
            },
        )

    def entries(self) -> list[dict[str, Any]]:
        response = self._post("/v1/memory/entries/list", {"scope_id": self.ensure_scope()})
        return list(response.get("entries", []))

    def latest_state(self, task_id: str) -> dict[str, Any] | None:
        matches: list[tuple[int, dict[str, Any]]] = []
        for entry in self.entries():
            try:
                state = json.loads(entry.get("text", ""))
            except (TypeError, ValueError):
                continue
            if not isinstance(state, dict) or state.get("task_id") != task_id:
                continue
            revision = int((entry.get("citation") or {}).get("memory_ref", {}).get("revision", 0))
            matches.append((revision, state))
        if not matches:
            return None
        return max(matches, key=lambda item: (item[0], str(item[1].get("updated_at"))))[1]
