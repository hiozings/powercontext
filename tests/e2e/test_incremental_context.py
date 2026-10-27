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

"""Incremental context follows ordinary preparation through persistence and empty results."""

import asyncio
from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    PrepareContextRequest,
    RememberMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta, apply_context_pack_delta
from powercontext.builtin.scope import ScopeDraft


def test_incremental_prepare_survives_restart_and_matches_every_ordinary_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'context.db'}"))
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Context", summary="Context", idempotency_key="context")
            )
            context = runtime.context.for_scope(scope.scope_id)
            empty = await context.prepare_incremental(PrepareContextRequest(query="alpha"))
            assert isinstance(empty, ContextPack) and empty.items == ()
            await runtime.memory.for_scope(scope.scope_id).remember(
                RememberMemoryRequest(
                    entries=(MemoryEntryInput(kind="fact", text="Alpha project uses ocean observations."),)
                )
            )
            update = await context.prepare_incremental(PrepareContextRequest(query="alpha"), empty)
            assert isinstance(update, ContextPackDelta)
            base_wire = apply_context_pack_delta(empty, ContextPackDelta.from_bytes(update.to_bytes())).to_bytes()
        async with open_builtin_runtime(config) as runtime:
            context = runtime.context.for_scope(scope.scope_id)
            await runtime.memory.for_scope(scope.scope_id).remember(
                RememberMemoryRequest(
                    entries=(MemoryEntryInput(kind="fact", text="Beta project uses temperature measurements."),)
                )
            )
            for query in ("alpha", "beta", "alpha", "unmatchedxyz"):
                request = PrepareContextRequest(query=query)
                ordinary = await context.prepare(request)
                delta = await context.prepare_incremental(request, ContextPack.from_bytes(base_wire))
                assert isinstance(delta, ContextPackDelta)
                restored = apply_context_pack_delta(
                    ContextPack.from_bytes(base_wire), ContextPackDelta.from_bytes(delta.to_bytes())
                )
                assert b"".join(restored.items) == (ordinary.content or "").encode()
                base_wire = restored.to_bytes()
            assert ContextPack.from_bytes(base_wire).items == ()

            async def deny(scopes: tuple[str, ...]) -> None:
                raise PermissionError

            with pytest.raises(PermissionError):
                await context.prepare_incremental(PrepareContextRequest(query="alpha"), authorize_scopes=deny)

    asyncio.run(scenario())
