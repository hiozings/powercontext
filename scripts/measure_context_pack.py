# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Measure 40 context transfers starting with eight supplied original meeting turns.

This supplemental schedule starts with eight turns, appends two more before rounds
10, 20 and 30, and cycles supplied queries once per stage. It is not the unpublished
competition workload. Input JSON contains fourteen original `turns` with `text`.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, PrepareContextRequest, open_builtin_runtime
from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta, apply_context_pack_delta
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource


class MeetingPipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(kind="meeting-turn", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


def receive(base: ContextPack | None, wire: bytes) -> ContextPack:
    """Reconstruct receiver state without access to any sender object."""

    if base is None:
        return ContextPack.from_bytes(wire)
    return apply_context_pack_delta(ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(wire))


async def measure(workload: dict[str, Any], queries: list[str], max_bytes: int) -> dict[str, Any]:
    turns = workload["turns"]
    if len(turns) != 14 or not queries or any(not query.strip() for query in queries):
        raise ValueError("measurement requires fourteen original turns and non-empty queries")  # noqa: TRY003
    totals = dict.fromkeys(("raw_full_bytes", "incremental_bytes", "full_pack_bytes", "gzip_bytes"), 0)
    contexts: set[bytes] = set()
    rounds = []
    received: ContextPack | None = None
    started = perf_counter()
    with TemporaryDirectory(prefix="context-pack-measure-") as directory:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{directory}/measurement.db"))
        async with open_builtin_runtime(config, candidate_pipeline=MeetingPipeline()) as runtime:
            if runtime.scopes is None:
                raise RuntimeError("scope service unavailable")  # noqa: TRY003
            scope = await runtime.scopes.create(
                ScopeDraft(title="Measurement", summary="Context bytes", idempotency_key="measurement")
            )
            sources = runtime.sources.for_scope(scope.scope_id)
            memory = runtime.memory.for_scope(scope.scope_id)
            context = runtime.context.for_scope(scope.scope_id)
            for stage, (start, stop) in enumerate(((0, 8), (8, 10), (10, 12), (12, 14))):
                for index in range(start, stop):
                    await sources.capture(
                        CaptureSource(source_id=f"turn-{index}", content=turns[index]["text"], metadata={})
                    )
                while (await memory.flush()).processed:
                    pass
                request = PrepareContextRequest(query=queries[stage % len(queries)], max_bytes=max_bytes)
                for _ in range(10):
                    prepared = await context.prepare(request)
                    raw = (prepared.content or "").encode("utf-8")
                    if not raw:
                        raise ValueError("measurement query produced an empty context")  # noqa: TRY003
                    sent = await context.prepare_incremental(request, base_pack=received)
                    wire = sent.to_bytes()
                    received = receive(received, wire)
                    if b"".join(received.items) != raw:
                        raise RuntimeError("receiver did not reconstruct the ordinary prepared content")  # noqa: TRY003
                    counts = {
                        "raw_full_bytes": len(raw),
                        "incremental_bytes": len(wire),
                        "full_pack_bytes": len(ContextPack.from_content(prepared.content).to_bytes()),
                        "gzip_bytes": len(gzip.compress(raw, compresslevel=9, mtime=0)),
                    }
                    for key, count in counts.items():
                        totals[key] += count
                    contexts.add(raw)
                    rounds.append({"round": len(rounds), "query": request.query, **counts})
    return {
        "measurement": "supplemental-runtime-40-rounds",
        "source_url": workload.get("source_url"),
        "source_sha256": workload.get("source_sha256"),
        "selection": workload.get("selection"),
        "turn_indices": [turn.get("turn_index") for turn in turns],
        "turn_count": len(turns),
        "initial_turn_count": 8,
        "append_rounds": [10, 20, 30],
        "queries": queries,
        "max_bytes": max_bytes,
        "round_count": len(rounds),
        "distinct_contexts": len(contexts),
        **totals,
        "saving_vs_raw": 1 - totals["incremental_bytes"] / totals["raw_full_bytes"],
        "saving_vs_full_pack": 1 - totals["incremental_bytes"] / totals["full_pack_bytes"],
        "saving_vs_gzip": 1 - totals["incremental_bytes"] / totals["gzip_bytes"],
        "seconds": perf_counter() - started,
        "rounds": rounds,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workload", type=Path)
    parser.add_argument("--queries", nargs="+", required=True)
    parser.add_argument("--max-bytes", type=int, default=4096)
    args = parser.parse_args()
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    print(json.dumps(asyncio.run(measure(workload, args.queries, args.max_bytes)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
