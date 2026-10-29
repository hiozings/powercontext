"""Build stable engineering chunks from specs, code and tests."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

_MARKER = re.compile(r"<!--\s*id:\s*([A-Za-z0-9._-]+)\s*-->")
_SPEC_VARIANT = re.compile(r"\.v\d+\.md$")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    source_type: str
    source_id: str
    path: str
    symbol: str | None
    title: str | None
    content: str
    metadata: dict[str, object]

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def row(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "path": self.path,
            "symbol": self.symbol,
            "title": self.title,
            "content": self.content,
            "metadata": json.dumps(self.metadata, ensure_ascii=False),
            "content_hash": self.content_hash,
        }


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _spec_chunks(project: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for spec_path in sorted((project / "specs").glob("*.md")):
        if _SPEC_VARIANT.search(spec_path.name):
            continue
        lines = _read(spec_path).splitlines()
        markers = [(index, match.group(1)) for index, line in enumerate(lines) if (match := _MARKER.search(line))]
        for position, (line_index, spec_id) in enumerate(markers):
            end = markers[position + 1][0] if position + 1 < len(markers) else len(lines)
            body = "\n".join(lines[line_index + 1 : end]).strip()
            title = next(
                (line.lstrip("# ").strip() for line in lines[line_index + 1 : end] if line.lstrip().startswith("#")),
                spec_id,
            )
            chunks.append(
                Chunk(
                    chunk_id=f"spec:{spec_id}",
                    source_type="spec",
                    source_id=spec_id,
                    path=spec_path.relative_to(project).as_posix(),
                    symbol=None,
                    title=title,
                    content=f"{title}\n\n{body}",
                    metadata={"line": line_index + 2, "end_line": end},
                )
            )
    return chunks


def _symbol_chunks(project: Path, path: Path, source: str) -> list[Chunk]:
    relative = path.relative_to(project).as_posix()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    chunks: list[Chunk] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segment = ast.get_source_segment(source, node)
            if segment:
                chunks.append(
                    Chunk(
                        chunk_id=f"code:{relative}#{node.name}",
                        source_type="code",
                        source_id=f"{relative}#{node.name}",
                        path=relative,
                        symbol=node.name,
                        title=node.name,
                        content=segment,
                        metadata={"line": node.lineno, "end_line": node.end_lineno},
                    )
                )
        elif isinstance(node, ast.ClassDef):
            segment = ast.get_source_segment(source, node)
            if segment:
                chunks.append(
                    Chunk(
                        chunk_id=f"code:{relative}#{node.name}",
                        source_type="code",
                        source_id=f"{relative}#{node.name}",
                        path=relative,
                        symbol=node.name,
                        title=node.name,
                        content=segment,
                        metadata={"line": node.lineno, "end_line": node.end_lineno},
                    )
                )
    return chunks


def _test_chunks(project: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for test_path in sorted((project / "tests").glob("test_*.py")):
        relative = test_path.relative_to(project).as_posix()
        source = _read(test_path)
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                segment = ast.get_source_segment(source, node)
                if segment:
                    chunks.append(
                        Chunk(
                            chunk_id=f"test:{relative}::{node.name}",
                            source_type="test",
                            source_id=f"{relative}::{node.name}",
                            path=relative,
                            symbol=node.name,
                            title=node.name,
                            content=segment,
                            metadata={"line": node.lineno, "end_line": node.end_lineno},
                        )
                    )
    return chunks


def ingest_project(project: Path) -> list[Chunk]:
    project = project.resolve()
    chunks = _spec_chunks(project)
    for code_path in sorted((project / "app").rglob("*.py")):
        if "__pycache__" in code_path.parts:
            continue
        chunks.extend(_symbol_chunks(project, code_path, _read(code_path)))
    chunks.extend(_test_chunks(project))
    return chunks
