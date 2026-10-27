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

"""Source-count admission follows journal identities and successful Family cursors."""

import asyncio

import pytest
from pydantic import ValidationError

from powercontext.builtin.artifacts.topic_memory import TOPIC_MEMORY_SOURCE_WINDOW_BINDING
from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.runtime.config import RuntimeConfig
from powercontext.builtin.runtime.processing_discovery import SourceProcessingPendingProvider
from powercontext.builtin.sources import SourceCursor
from powercontext.builtin.triggers import SOURCE_WINDOW_TRIGGER_NAME
from powercontext.sources import SourceMaterialization
from tests.builtin.persistence.contract import NoteSource, repository_profile


@pytest.mark.parametrize("field", ["memory_source_count_threshold", "topic_memory_source_count_threshold"])
def test_count_thresholds_default_off_and_round_trip_without_coercion(field: str) -> None:
    assert getattr(RuntimeConfig(), field) is None
    configured = RuntimeConfig.model_validate({field: 3})
    assert getattr(RuntimeConfig.model_validate_json(configured.model_dump_json()), field) == 3
    for value in (True, False, 0, -1, 1.0, "3"):
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({field: value})


def test_pending_count_uses_distinct_journal_rows_after_each_familys_cursor() -> None:
    async def scenario() -> None:
        async with repository_profile() as (profile, repositories):
            memory = SourceProcessingPendingProvider(profile.database, SOURCE_WINDOW_TRIGGER_NAME, "memory")
            topic = SourceProcessingPendingProvider(
                profile.database, TOPIC_MEMORY_SOURCE_WINDOW_BINDING, "topic-memory"
            )
            first = NoteSource(name="first", materialization=SourceMaterialization.CAPTURED, body="same text")
            second = NoteSource(name="second", materialization=SourceMaterialization.CAPTURED, body="same text")
            third = NoteSource(name="third", materialization=SourceMaterialization.CAPTURED, body="same text")
            async with profile.database.transaction() as connection:
                saved = await repositories.sources.add(connection, "left", first)
                await repositories.sources.add(connection, "left", second)
                await repositories.sources.add(connection, "right", first)
                await repositories.sources.add(connection, "left", first)
                assert await memory.count_ready_scopes(connection, ("left", "right"), threshold=2) == {"left"}
                assert await memory.count_ready_scopes(connection, ("left", "right"), threshold=3) == set()
                await SourceCursorRepository().save(
                    connection,
                    "left",
                    SOURCE_WINDOW_TRIGGER_NAME,
                    SourceCursor(sequence=saved.journal_position),
                    expected_generation=None,
                )
                assert await memory.count_ready_scopes(connection, ("left", "right"), threshold=2) == set()
                assert await topic.count_ready_scopes(connection, ("left", "right"), threshold=2) == {"left"}
                await repositories.sources.add(connection, "left", third)
            # A new transaction and provider read the durable cursor, not a local counter.
            async with profile.database.transaction() as connection:
                reopened = SourceProcessingPendingProvider(profile.database, SOURCE_WINDOW_TRIGGER_NAME, "memory")
                assert await reopened.count_ready_scopes(connection, ("left", "right"), threshold=2) == {"left"}
                assert await reopened.count_ready_scopes(connection, (), threshold=2) == set()
                assert await reopened.count_ready_scopes(connection, ("left", "right"), threshold=2**70) == set()

    asyncio.run(scenario())
