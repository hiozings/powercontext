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

"""HTTP scoring preserves global candidate ranks and exact Memory versions."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryCitation, MemoryEntryInput, MemoryHit
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft


def hits(count: int) -> tuple[MemoryHit, ...]:
    return tuple(
        MemoryHit(
            memory_ref=ArtifactRef(family="memory", artifact_id="memory", revision=2),
            entry_id=f"entry-{rank}",
            entry_version_id=f"entry-{rank}-v3",
            text=f"原始候选 {rank}",
            score=1 / rank,
            matched_by=("fts",),
        )
        for rank in range(1, count + 1)
    )


def unexpected_request(request: httpx.Request) -> httpx.Response:
    pytest.fail("invalid rerank arguments must fail before sending any HTTP request")


def test_global_ties_and_repeated_calls_keep_identity_request_count_and_client() -> None:
    candidates = hits(5)
    snapshot = tuple(hit.model_copy(deep=True) for hit in candidates)
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.method == "POST"
        assert request.url.path == "/score"
        assert set(body) == {"query", "candidates"}
        assert 1 <= len(body["candidates"]) <= 2
        for item in body["candidates"]:
            assert set(item) == {"rank", "text"}
            assert item["text"] == candidates[item["rank"] - 1].text
        requests.append(body)
        await asyncio.sleep(0)
        scores = {1: -1, 2: 0.9, 3: 0.95, 4: 0.9, 5: -2}
        return httpx.Response(
            200,
            json={
                "scores": [
                    {"rank": item["rank"], "score": scores[item["rank"]]} for item in reversed(body["candidates"])
                ]
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://scores.invalid/score", client=client, batch_size=2)
            first, second = await asyncio.gather(
                reranker.rerank("first query", candidates, 3), reranker.rerank("second query", candidates, 5)
            )
            assert first.selected_ranks == (3, 2, 4)
            assert second.selected_ranks == (3, 2, 4, 1, 5)
            assert first.usage.requests == second.usage.requests == 3
            assert not client.is_closed
            assert candidates == snapshot
            for query in ("first query", "second query"):
                assert sorted(
                    item["rank"] for body in requests if body["query"] == query for item in body["candidates"]
                ) == [1, 2, 3, 4, 5]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw",
    [
        b"not JSON",
        b"null",
        b"[]",
        b'{"scores":{}}',
        b'{"scores":[]}',
        b'{"scores":[{"rank":true,"score":1}]}',
        b'{"scores":[{"rank":1.0,"score":1}]}',
        b'{"scores":[{"rank":"1","score":1}]}',
        b'{"scores":[{"rank":1,"score":true}]}',
        b'{"scores":[{"rank":1,"score":"0.5"}]}',
        b'{"scores":[{"rank":1}]}',
        b'{"scores":[{"rank":1,"score":-Infinity}]}',
        b'{"scores":[{"rank":1,"rank":2,"score":1}]}',
        b'{"scores":[],"scores":[{"rank":1,"score":1}]}',
    ],
)
def test_invalid_response_is_value_error_without_coercion(raw: bytes) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw))
        ) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://scores.invalid", client=client).rerank("q", hits(1), 1)

    asyncio.run(scenario())


def test_second_batch_must_cover_its_own_global_ranks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scores": [{"rank": 1, "score": 0.5}, {"rank": 2, "score": 0.9}]})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://scores.invalid", client=client, batch_size=2).rerank("q", hits(4), 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [302, 400, 503])
def test_non_success_status_preserves_httpx_error_even_with_redirects_enabled(status: int) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(status, headers={"location": "/other"}))
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            with pytest.raises(httpx.HTTPStatusError) as raised:
                await HTTPMemoryReranker("https://scores.invalid", client=client).rerank("q", hits(1), 1)
            assert raised.value.response.status_code == status
            assert raised.value.request.method == "POST"

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_invalid_batch_size_is_rejected_before_requests(value: object) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
            with pytest.raises(ValueError):
                HTTPMemoryReranker("https://scores.invalid", client=client, batch_size=value)  # ty: ignore[invalid-argument-type]

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1", 3])
def test_invalid_limit_is_rejected_before_requests(value: object) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://scores.invalid", client=client).rerank("q", hits(2), value)  # ty: ignore[invalid-argument-type]

    asyncio.run(scenario())


def test_runtime_reranking_preserves_every_selected_entry_and_usage(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"scores": [{"rank": item["rank"], "score": item["rank"]} for item in body["candidates"]]}
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(
                "https://scores.invalid", client=client, batch_size=2, policy_id="offline-test"
            )
            config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}"))
            async with open_builtin_runtime(config, memory_reranker=reranker) as runtime:
                assert runtime.scopes is not None
                scope = await runtime.scopes.create(
                    ScopeDraft(title="Rerank", summary="Rerank", idempotency_key="rerank")
                )
                memory = runtime.memory.for_scope(scope.scope_id)
                await memory.remember(
                    RememberMemoryRequest(
                        entries=tuple(
                            MemoryEntryInput(kind="fact", text=f"Project finding {index}.") for index in range(5)
                        )
                    )
                )
                result = await memory.search(SearchMemoryRequest(query="project", limit=3, mode="fts"))
                assert result.rerank is not None
                assert result.rerank.policy_id == "offline-test"
                assert result.rerank.usage.requests == 3
                assert result.rerank.selected_ranks == (5, 4, 3)
                assert result.hits == tuple(reversed(result.rerank.candidate_hits))[:3]
                for hit in result.hits:
                    citation = MemoryCitation(
                        memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                    )
                    saved = await memory.get(GetMemoryEntryRequest(citation=citation))
                    assert saved.entry.text == hit.text
                    assert saved.entry.entry_id == hit.entry_id
                    assert saved.entry.entry_version_id == hit.entry_version_id
            assert not client.is_closed

    asyncio.run(scenario())
