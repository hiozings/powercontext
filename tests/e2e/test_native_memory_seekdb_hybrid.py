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

"""Real seekdb checks against the unchanged two-query index on the same stored heads."""

import asyncio
import math
import re
import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event

from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemoryEntryInput,
    MemorySearchRequest,
    MemoryService,
)
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.memory import RelationalMemoryBackend
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex
from powercontext.builtin.persistence.oceanbase.memory_index import OceanBaseMemoryFTSIndex, OceanBaseMemoryVectorIndex
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts
from powercontext.builtin.tags import MemoryEntryTagTarget, TagFilter

pytest.importorskip("pylibseekdb")


class GeometryEmbedding:
    profile = EmbeddingProfile(profile_id="hybrid-native-test", model="offline-geometry", dimension=384)

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        vectors = []
        for value in texts:
            match = re.search(r"\bneedle (\d+)\b", value)
            angle = int(match[1]) / 100 if match else 0
            vectors.append((math.cos(angle), math.sin(angle), *((0.0,) * 382)))
        return EmbeddingResult(vectors=tuple(vectors))


@contextmanager
def candidate_statements() -> Iterator[list[tuple[str, Any]]]:
    statements: list[tuple[str, Any]] = []

    def record(_connection, _cursor, statement, parameters, _context, _executemany):
        if "pc_memory_" in statement and ("AGAINST" in statement.upper() or "l2_distance" in statement.lower()):
            statements.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def test_native_hybrid_filters_bounds_and_matches_frozen_path(short_tmp_path: Path) -> None:
    async def scenario() -> None:
        embedding = GeometryEmbedding()
        config = BuiltinConfig(database=SeekDBConfig(path=short_tmp_path / "seekdb"))
        elapsed = []
        equal_results = 0
        async with open_builtin_contexts(config, embedding_model=embedding) as contexts:
            baseline_index = CompositeMemoryIndex(
                OceanBaseMemoryFTSIndex(), OceanBaseMemoryVectorIndex(embedding.profile)
            )
            stored = []
            for scope in ("left", "middle", "right"):
                service = (await contexts.get(scope)).artifacts.memory
                head = await service.remember(
                    memory=None,
                    entries=tuple(
                        MemoryEntryInput(kind="fact", text=f"needle {index} evidence from {scope}.")
                        for index in range(40)
                    ),
                    mode="append",
                )
                assert head is not None
                latest = await service.remember(
                    memory=head,
                    entries=(MemoryEntryInput(kind="fact", text=f"needle 40 added to {scope}."),),
                    mode="append",
                )
                assert latest is not None
                baseline = MemoryService(
                    backend=RelationalMemoryBackend(
                        database=contexts.database,
                        scope_id=scope,
                        artifacts=contexts.repositories.artifacts,
                        index=baseline_index,
                    ),
                    embedding_model=embedding,
                )
                stored.append((scope, service, baseline, head, latest))

            for scope, service, baseline, old, head in stored:
                await service.search("needle 5", memories=(head,), mode="hybrid", limit=10)
                await baseline.search("needle 5", memories=(head,), mode="hybrid", limit=10)
                for number in (5, 13, 27, 39):
                    query = f"needle {number}"
                    with candidate_statements() as statements:
                        started = time.perf_counter()
                        current = await service.search(query, memories=(head,), mode="hybrid", limit=10)
                        elapsed.append((time.perf_counter() - started) * 1000)
                    assert len(statements) == 1
                    with candidate_statements() as original_statements:
                        original = await baseline.search(query, memories=(head,), mode="hybrid", limit=10)
                    assert len(original_statements) == 2
                    relevant = f"needle {number} evidence from {scope}."
                    original_rank = next(
                        (rank for rank, hit in enumerate(original.hits, 1) if hit.text == relevant), None
                    )
                    current_rank = next(
                        (rank for rank, hit in enumerate(current.hits, 1) if hit.text == relevant), None
                    )
                    if original_rank is not None:
                        assert current_rank is not None and current_rank <= original_rank, (
                            query,
                            current_rank,
                            original_rank,
                        )
                    equal_results += current.hits == original.hits
                    assert len({(hit.entry_id, hit.entry_version_id) for hit in current.hits}) == len(current.hits)
                    exact = await service.expand(current.hits)
                    assert [entry.text for entry in exact] == [hit.text for hit in current.hits]
                    assert all(hit.memory_ref == head.as_ref() and hit.matched_by for hit in current.hits)
                    repeated = await service.search(query, memories=(head,), mode="hybrid", limit=10)
                    assert repeated.hits == current.hits
                with pytest.raises(CapabilityNotSupportedError):
                    await service.search("needle 5", memories=(old,), mode="hybrid", limit=1)

            scope, service, baseline, _, head = stored[0]
            entries = await service.entries(head)
            target = next(entry for entry in entries if entry.text.startswith("needle 39 "))
            tag_target = MemoryEntryTagTarget(artifact_id=head.artifact_id, entry_id=target.entry_id)
            tags = await contexts.records.get_tags(scope, tag_target)
            await contexts.records.replace_tags(scope, tag_target, ("selected", "reviewed"), expected_etag=tags.etag)
            for tag_filter in (TagFilter(tags=("selected",)), TagFilter(tags=("selected", "reviewed"), match="all")):
                request = MemorySearchRequest(
                    query="needle",
                    analyzed_query="needle",
                    memories=(head.as_ref(),),
                    mode="hybrid",
                    candidate_limit=1,
                    query_vector=(1.0, 0.0, *((0.0,) * 382)),
                    embedding_profile=embedding.profile,
                    tag_filter=tag_filter,
                )
                async with contexts.database.transaction() as connection:
                    with candidate_statements() as statements:
                        channels = await contexts.index.search(connection, scope, request)
                    assert len(statements) == 1
                    assert [hit.entry_id for hit in channels.fts] == [target.entry_id]
                    assert [hit.entry_id for hit in channels.vector] == [target.entry_id]
                    assert channels.vector[0].distance == pytest.approx(math.sqrt(2 - 2 * math.cos(0.39)), rel=1e-5)
                    statement, parameters = statements[0]
                    plan = "\n".join(
                        str(row[0])
                        for row in (await connection.exec_driver_sql("EXPLAIN " + statement, parameters)).all()
                    )
                    assert "TEXT RETRIEVAL SCAN" in plan and "VECTOR INDEX" in plan
                tagged = await service.search("needle", memories=(head,), mode="hybrid", limit=1, tag_filter=tag_filter)
                assert tagged.hits[0].entry_id == target.entry_id
                assert tagged.hits[0].matched_by == ("fts", "vector")

            for entry in entries:
                target_tags = MemoryEntryTagTarget(artifact_id=head.artifact_id, entry_id=entry.entry_id)
                old_tags = await contexts.records.get_tags(scope, target_tags)
                await contexts.records.replace_tags(scope, target_tags, ("dense",), expected_etag=old_tags.etag)
            started = time.perf_counter()
            dense = await service.search(
                "needle 5", memories=(head,), mode="hybrid", limit=10, tag_filter=TagFilter(tags=("dense",))
            )
            dense_ms = (time.perf_counter() - started) * 1000
            assert dense.hits

            # Another Memory artifact in this scope must not enter the requested head.
            other_service = MemoryService(
                backend=RelationalMemoryBackend(
                    database=contexts.database,
                    scope_id=scope,
                    artifacts=contexts.repositories.artifacts,
                    index=contexts.index,
                ),
                embedding_model=embedding,
            )
            other = await other_service.remember(
                memory=None,
                entries=(MemoryEntryInput(kind="fact", text="needle 5 decoy in another artifact."),),
                mode="append",
            )
            assert other is not None and other.artifact_id != head.artifact_id
            selected = await service.search("needle 5", memories=(head,), mode="hybrid", limit=10)
            assert all(hit.memory_ref == head.as_ref() and "decoy" not in hit.text for hit in selected.hits)
            print({
                "workload": "synthetic 384-dimensional geometry; not formal SciFact quality",
                "scopes": 3,
                "documents_per_scope": 41,
                "queries": len(elapsed),
                "p50_memory_search_ms": statistics.median(elapsed),
                "identical_frozen_results": equal_results,
                "dense_tag_eligible_entries": len(entries),
                "dense_tag_memory_search_ms": dense_ms,
            })

    asyncio.run(scenario())
