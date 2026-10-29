"""Select task context from seekdb query rows and real project files."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_TERM = re.compile(r"[A-Za-z0-9_]+")


def task_query(task: str) -> str:
    terms = [term.lower() for term in _TERM.findall(task) if len(term) >= 3]
    seen: list[str] = []
    for term in terms:
        if term not in seen:
            seen.append(term)
    return " ".join(seen)


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata") or {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _keyword_hits(task: str, content: str) -> int:
    lowered = content.lower()
    return sum(1 for term in set(_TERM.findall(task.lower())) if term in lowered)


def _ranked(rows: list[dict[str, Any]], task: str, source_type: str) -> list[dict[str, Any]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        if row.get("source_type") != source_type:
            continue
        content = str(row.get("content", ""))
        score = float(row.get("score") or 0.0) + 0.25 * _keyword_hits(task, content)
        if source_type == "spec" and any(term in content.lower() for term in ("min_price", "max_price", "422")):
            score += 1.0
        if source_type == "code" and any(term in str(row.get("path", "")) for term in ("books", "repository")):
            score += 0.5
        if source_type == "test" and any(term in content.lower() for term in ("min_price", "max_price")):
            score += 1.0
        scored.append((score, row))
    return [row for _score, row in sorted(scored, key=lambda item: (-item[0], str(item[1].get("chunk_id"))))]


def _unique_by(rows: list[dict[str, Any]], key: str, limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        value = str(row.get(key, ""))
        if not value or value in seen:
            continue
        seen.add(value)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def build_context(
    project: Path,
    task: str,
    rows: list[dict[str, Any]],
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project = project.resolve()
    specs = _ranked(rows, task, "spec")[:5]
    code_chunks = _unique_by(_ranked(rows, task, "code"), "path", 3)
    tests = _unique_by(_ranked(rows, task, "test"), "chunk_id", 8)
    code: list[dict[str, Any]] = []
    for chunk in code_chunks:
        path = project / str(chunk["path"])
        if not path.is_file():
            continue
        symbols = [
            str(row["symbol"])
            for row in rows
            if row.get("source_type") == "code"
            and row.get("path") == chunk.get("path")
            and row.get("symbol")
        ]
        code.append(
            {
                "path": chunk["path"],
                "symbols": sorted(set(symbols)),
                "content": path.read_text(encoding="utf-8"),
                "reason": f"symbol/path matched task; seekdb chunk {chunk['chunk_id']}",
            }
        )
    sources = []
    for row in [*specs, *code_chunks, *tests]:
        path = project / str(row["path"])
        sources.append(
            {
                "chunk_id": row["chunk_id"],
                "path": row["path"],
                "exists": path.is_file(),
                "source_id": row["source_id"],
            }
        )
    return {
        "task": task,
        "project": str(project),
        "specs": [
            {
                "id": row["source_id"],
                "path": row["path"],
                "line": _metadata(row).get("line"),
                "title": row.get("title"),
                "content": row["content"],
                "reason": "spec clause retrieved from seekdb",
            }
            for row in specs
        ],
        "code": code,
        "tests": [
            {
                "path": row["path"],
                "nodeid": row["source_id"],
                "name": row.get("symbol"),
                "content": row["content"],
                "reason": "test retrieved from seekdb",
            }
            for row in tests
        ],
        "state": state,
        "selection": {
            "query": task_query(task),
            "chunk_ids": [row["chunk_id"] for row in [*specs, *code_chunks, *tests]],
            "sources": sources,
        },
    }


def render_markdown(context: dict[str, Any]) -> str:
    lines = [f"# SDD context: {context['task']}", ""]
    lines.append("## Specifications")
    for spec in context["specs"]:
        lines.append(f"- `{spec['id']}` ({spec['path']}:{spec['line']}): {spec['title']}")
    lines.append("")
    lines.append("## Code")
    for code in context["code"]:
        lines.append(f"- `{code['path']}`: {', '.join(code['symbols']) or '(module)'}")
    lines.append("")
    lines.append("## Tests")
    for test in context["tests"]:
        lines.append(f"- `{test['nodeid']}`")
    if context.get("state"):
        state = context["state"]
        lines.extend(["", "## Task state"])
        lines.append(f"- stage: {state.get('stage')}")
        lines.append(f"- done: {', '.join(state.get('done', []))}")
        lines.append(f"- todo: {', '.join(state.get('todo', []))}")
        lines.append(f"- verification: {state.get('verification')}")
    return "\n".join(lines)
