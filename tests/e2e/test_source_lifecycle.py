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

"""Native lifecycle updates are atomic, scoped, and preserve exact historical evidence."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.application import BuiltinRuntime
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceRef


async def seed(runtime: BuiltinRuntime, name: str):
    assert runtime.scopes is not None
    scope = await runtime.scopes.create(ScopeDraft(title=name, summary=name, idempotency_key=name))
    sid = scope.scope_id
    lifecycle = runtime.lifecycle.for_scope(sid)
    receipt = await runtime.sources.for_scope(sid).capture(CaptureSource(source_id="root", content=name, metadata={}))
    await lifecycle.register_source("root", receipt.source_ref, revision=3)
    created = await runtime.memory.for_scope(sid).remember(
        RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=f"{name} evidence"),))
    )
    assert created.entry is not None
    citation = created.entry.citation
    await lifecycle.register_memory("memory", citation, parents=("root",), policy="all")
    topic = await runtime.records.for_scope(sid).create_artifact(
        "topic-memory",
        ArtifactWrite(
            content={
                "title": f"{name} topic",
                "summary": f"{name} supporting summary",
                "detail": f"{name} evidence",
            }
        ),
    )
    ref = ArtifactRef(family="topic-memory", artifact_id=topic.artifact_id, revision=topic.revision)
    await lifecycle.register_topic("topic", ref, parents=("memory",), policy="all")
    return sid, receipt.source_ref, citation, ref


@pytest.mark.parametrize(
    "fields",
    [
        {"node_id": " "},
        {"node_id": 1},
        {"reason": "\t"},
        {"reason": False},
        {"revision": True},
        {"revision": "3"},
        {"revision": 3.0},
        {"sequence": False},
        {"sequence": 0},
        {"sequence": -1},
        {"available": 1},
        {"available": "false"},
        {"extra": 1},
    ],
)
def test_event_fields_are_strict(fields: dict[str, object]) -> None:
    values = {"node_id": "root", "revision": 3, "sequence": 1, "available": False, "reason": "withdrawal"}
    with pytest.raises(ValidationError):
        SourceLifecycleEvent.model_validate({**values, **fields})


@pytest.mark.parametrize("failure_boundary", ["memory", "graph"])
def test_storage_failure_rolls_back_graph_and_native_states_then_replays_after_restart(
    tmp_path: Path, failure_boundary: str
) -> None:
    async def scenario() -> None:
        path = tmp_path / "lifecycle.db"
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{path}"))
        event = SourceLifecycleEvent(node_id="root", revision=3, sequence=1, available=False, reason="撤回说明" * 300)
        async with open_builtin_runtime(config) as runtime:
            sid, source, citation, topic = await seed(runtime, "atomic")
            lifecycle = runtime.lifecycle.for_scope(sid)
            before = (await runtime.memory.for_scope(sid).list()).memory_ref
            with sqlite3.connect(path) as connection:
                statement = (
                    "CREATE TRIGGER fail_lifecycle BEFORE INSERT ON pc_artifacts WHEN NEW.family='memory' "
                    if failure_boundary == "memory"
                    else "CREATE TRIGGER fail_lifecycle BEFORE UPDATE ON pc_source_lifecycle_nodes WHEN NEW.kind='topic' "
                )
                connection.execute(statement + "BEGIN SELECT RAISE(ABORT, 'controlled failure'); END")
            with pytest.raises(SQLAlchemyError):
                await lifecycle.apply(event)
            assert (await runtime.memory.for_scope(sid).list()).memory_ref == before
            assert await lifecycle.resolve("root", purpose="generation") == source
            assert await lifecycle.resolve("memory", purpose="generation") == citation
            assert (await runtime.topic_memory.for_scope(sid).search(SearchTopicMemoryRequest(query="atomic"))).hits
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TRIGGER fail_lifecycle")
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(sid)
            assert await lifecycle.apply(event) == []
            memory = runtime.memory.for_scope(sid)
            retired = await memory.list(include_inactive=True)
            assert [record.state for record in retired.entries] == ["inactive"]
            assert retired.entries[0].entry.entry_version_id == citation.entry_version_id
            assert not (await runtime.topic_memory.for_scope(sid).search(SearchTopicMemoryRequest(query="atomic"))).hits
            assert await lifecycle.resolve("memory", purpose="audit") == citation
            assert await lifecycle.resolve("topic", purpose="audit") == topic
            assert (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text == "atomic evidence"
            await lifecycle.apply(event)
            assert (await memory.list()).memory_ref == retired.memory_ref
            restored = event.model_copy(update={"sequence": 2, "available": True})
            assert await lifecycle.apply(restored) == ["memory", "root", "topic"]
            assert await lifecycle.resolve("memory", purpose="generation") == citation
            assert (await runtime.topic_memory.for_scope(sid).search(SearchTopicMemoryRequest(query="atomic"))).hits

    asyncio.run(scenario())


def test_scope_isolation_registration_idempotence_and_invalid_references() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            left, source, citation, _ = await seed(runtime, "left")
            right, _, other_citation, _ = await seed(runtime, "right")
            service = runtime.lifecycle.for_scope(left)
            before = (await runtime.memory.for_scope(left).list()).memory_ref
            await service.register_source("root", source, revision=3)
            await service.register_memory("memory", citation, parents=("root",), policy="all")
            assert (await runtime.memory.for_scope(left).list()).memory_ref == before
            with pytest.raises(SourceLifecycleError):
                await service.register_source(
                    "missing", SourceRef(source_type="content", source_id="absent"), revision=1
                )
            with pytest.raises(SourceLifecycleError):
                await service.register_memory("foreign", other_citation, parents=("root",))
            with pytest.raises(SourceLifecycleError):
                await service.register_memory("missing-parent", citation, parents=("unknown",))
            with pytest.raises(SourceLifecycleError):
                await service.register_memory("duplicates", citation, parents=("root", "root"))
            await service.apply(
                SourceLifecycleEvent(node_id="root", revision=3, sequence=1, available=False, reason="withdraw")
            )
            assert await runtime.lifecycle.for_scope(right).resolve("memory", purpose="generation") == other_citation
            with pytest.raises(SourceLifecycleError):
                await service.resolve("memory", purpose="generation")
            with pytest.raises(SourceLifecycleError):
                await service.resolve("memory", purpose="unknown")
            with pytest.raises(SourceLifecycleError):
                await service.resolve("unknown", purpose="audit")

    asyncio.run(scenario())
