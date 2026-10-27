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

import asyncio
import json

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    RememberMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import ContextPack
from powercontext.builtin.scope import ScopeDraft


def test_runtime_selection_preserves_real_citations_and_incremental_content() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Evidence", summary="Evidence", idempotency_key="evidence")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            text = "General background notes. " * 70 + "The cobalt calibration signal occurs at 37 degrees."
            await memory.remember(RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=text),)))
            context = runtime.context.for_scope(scope.scope_id)
            ordinary = await context.prepare(PrepareContextRequest(query="cobalt calibration", max_bytes=900))
            assert "at 37 degrees" not in (ordinary.content or "")
            request = PrepareContextRequest(query="cobalt calibration", max_bytes=900, evidence_selection=True)
            selected = await context.prepare(request)
            assert selected.content is not None and "at 37 degrees" in selected.content
            assert selected.content_bytes == len(selected.content.encode()) <= 900
            for item in json.loads(selected.content.splitlines()[-2])["items"]:
                assert item["citation"]["scope_id"] == scope.scope_id
                citation = MemoryCitation.model_validate({
                    key: value for key, value in item["citation"].items() if key != "scope_id"
                })
                original = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text
                assert original[item["start"] : item["end"]] == item["content"]
            pack = await context.prepare_incremental(request)
            assert isinstance(pack, ContextPack)
            assert b"".join(ContextPack.from_bytes(pack.to_bytes()).items) == selected.content.encode()

    asyncio.run(scenario())
