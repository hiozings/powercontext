"""SpecLens CLI: ingest engineering material, build context, run the SDD task."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
import shutil
from typing import Any

from ingest import Chunk, ingest_project
from minimax_agent import live, prepare_baseline, replay
from seekdb_store import SeekDBStore
from selection import build_context, render_markdown, task_query
from state import PowerContextState

MINIMAX_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT = MINIMAX_ROOT / "demo_project"
DEFAULT_DB = MINIMAX_ROOT / "data" / "engineering"
DEFAULT_TASK = (
    "Add min_price and max_price filters to GET /books. Reject min_price > max_price with HTTP 422. "
    "Keep genre filtering, pagination and the detail endpoint unchanged."
)
TASK_ID = "books-price-filter"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _store_path(value: str | None) -> Path:
    return Path(value).resolve() if value else DEFAULT_DB


def _project_path(value: str | None) -> Path:
    return Path(value).resolve() if value else DEFAULT_PROJECT


def _load_state() -> dict[str, Any] | None:
    try:
        return PowerContextState().latest_state(TASK_ID)
    except Exception as error:
        print(f"warning: PowerContext state unavailable: {error}", file=sys.stderr)
        return None


async def _ingest(project: Path, db: Path) -> list[Chunk]:
    chunks = ingest_project(project)
    async with SeekDBStore.open(db) as store:
        await store.replace_all([chunk.row() for chunk in chunks])
    return chunks


async def _context(project: Path, db: Path, task: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    async with SeekDBStore.open(db) as store:
        rows = await store.search(task_query(task), limit=80)
    state = _load_state()
    return build_context(project, task, rows, state=state), rows


def command_ingest(arguments: argparse.Namespace) -> int:
    project = _project_path(arguments.project)
    chunks = asyncio.run(_ingest(project, _store_path(arguments.db)))
    evidence = MINIMAX_ROOT / "evidence"
    evidence.mkdir(exist_ok=True)
    summary = {
        "project": str(project),
        "chunks": len(chunks),
        "by_type": {
            source_type: sum(1 for chunk in chunks if chunk.source_type == source_type)
            for source_type in ("spec", "code", "test")
        },
        "chunk_ids": [chunk.chunk_id for chunk in chunks],
    }
    (evidence / "seekdb-chunks.json").write_text(_json(summary), encoding="utf-8")
    print(_json(summary))
    return 0


def command_context(arguments: argparse.Namespace) -> int:
    project = _project_path(arguments.project)
    context, rows = asyncio.run(_context(project, _store_path(arguments.db), arguments.task))
    output = render_markdown(context) if arguments.format == "md" else _json(context)
    if arguments.out:
        Path(arguments.out).write_text(output, encoding="utf-8")
    else:
        print(output)
    evidence = MINIMAX_ROOT / "evidence"
    evidence.mkdir(exist_ok=True)
    record = {
        "task": arguments.task,
        "query": task_query(arguments.task),
        "chunk_ids": [row["chunk_id"] for row in rows],
        "selected": context["selection"]["chunk_ids"],
        "timestamp": datetime.now(UTC).isoformat(),
    }
    with (evidence / "seekdb-queries.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if arguments.label:
        (evidence / f"context-{arguments.label}.json").write_text(_json(context), encoding="utf-8")
    return 0


def command_status(arguments: argparse.Namespace) -> int:
    state = PowerContextState().latest_state(TASK_ID)
    print(_json({"task_id": TASK_ID, "state": state}))
    return 0


def command_run(arguments: argparse.Namespace) -> int:
    project = _project_path(arguments.project)
    if arguments.mode == "live":
        live_project = prepare_baseline(project, MINIMAX_ROOT / "snapshots")
        context, _rows = asyncio.run(_context(live_project, _store_path(arguments.db), arguments.task))
    else:
        context, _rows = asyncio.run(_context(project, _store_path(arguments.db), arguments.task))
    state = None if arguments.no_state else PowerContextState()
    record_dir = MINIMAX_ROOT / "evidence" / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{arguments.mode}"
    if arguments.mode == "replay":
        patch = Path(arguments.patch).resolve() if arguments.patch else MINIMAX_ROOT / "patches" / "0001-books-price-filter.patch"
        result = replay(project, MINIMAX_ROOT / "snapshots", patch, record_dir, state=state)
    else:
        api_key = os.environ.get("MINIMAX_API_KEY")
        if not api_key:
            print("MINIMAX_API_KEY is not set; use --replay or provide the key", file=sys.stderr)
            return 2
        protocol = os.environ.get("MINIMAX_PROTOCOL", "anthropic").strip().lower()
        default_base = (
            "https://api.minimax.cn/anthropic" if protocol == "anthropic" else "https://api.minimax.io/v1"
        )
        result = live(
            project,
            MINIMAX_ROOT / "snapshots",
            context,
            record_dir,
            api_key=api_key,
            base_url=os.environ.get("MINIMAX_BASE_URL", default_base),
            model=os.environ.get("MINIMAX_MODEL", "MiniMax-M3.1-Flash"),
            protocol=protocol,
            state=state,
        )
    (record_dir / "result.json").write_text(_json(result), encoding="utf-8")
    print(_json({key: value for key, value in result.items() if key not in {"before_output", "after_output"}}))
    return 0


def command_db_stats(arguments: argparse.Namespace) -> int:
    async def stats() -> dict[str, int]:
        async with SeekDBStore.open(_store_path(arguments.db)) as store:
            return await store.stats()

    print(_json(asyncio.run(stats())))
    return 0


def command_promote(arguments: argparse.Namespace) -> int:
    result_path = Path(arguments.run) / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    baseline = Path(result["baseline"])
    if not baseline.is_dir():
        print(f"baseline not found: {baseline}", file=sys.stderr)
        return 2
    destination = _project_path(arguments.project) / "app"
    shutil.copytree(baseline / "app", destination, dirs_exist_ok=True)
    from minimax_agent import diff_project, prepare_baseline

    baseline = prepare_baseline(_project_path(arguments.project), MINIMAX_ROOT / "snapshots")
    patch = diff_project(_project_path(arguments.project), baseline)
    patch_path = MINIMAX_ROOT / "patches" / "0001-books-price-filter.patch"
    patch_path.write_text(patch, encoding="utf-8")
    (Path(arguments.run) / "patch.diff").write_text(patch, encoding="utf-8")
    print(_json({"promoted": str(destination), "patch": str(patch_path), "bytes": len(patch)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SpecLens SDD context manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="rebuild the seekdb engineering index")
    ingest.add_argument("--project")
    ingest.add_argument("--db")
    ingest.set_defaults(func=command_ingest)

    context = subparsers.add_parser("context", help="select task context")
    context.add_argument("--task", default=DEFAULT_TASK)
    context.add_argument("--project")
    context.add_argument("--db")
    context.add_argument("--format", choices=("json", "md"), default="json")
    context.add_argument("--out")
    context.add_argument("--label")
    context.set_defaults(func=command_context)

    status = subparsers.add_parser("status", help="read the latest PowerContext task state")
    status.set_defaults(func=command_status)

    run = subparsers.add_parser("run", help="apply the recorded patch or ask MiniMax to implement the task")
    run.add_argument("--mode", choices=("replay", "live"), default="replay")
    run.add_argument("--task", default=DEFAULT_TASK)
    run.add_argument("--project")
    run.add_argument("--db")
    run.add_argument("--patch")
    run.add_argument("--no-state", action="store_true")
    run.set_defaults(func=command_run)

    promote = subparsers.add_parser("promote", help="copy a successful live run into demo_project")
    promote.add_argument("--run", required=True, help="evidence/<timestamp>-live directory")
    promote.add_argument("--project")
    promote.set_defaults(func=command_promote)

    db_stats = subparsers.add_parser("db-stats", help="show seekdb chunk counts")
    db_stats.add_argument("--db")
    db_stats.set_defaults(func=command_db_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    return int(arguments.func(arguments))


if __name__ == "__main__":
    sys.exit(main())
