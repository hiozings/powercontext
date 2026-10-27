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

"""Batch HTTP scoring with stable global selection of original Memory candidates."""

from __future__ import annotations

import math

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage


class HTTPMemoryReranker:
    """Score candidates through a caller-owned client without rewriting their identities.

    Each call sends every global rank once, validates all batches, then selects
    globally by descending score and ascending original rank. HTTP failures are
    propagated without retries or fallback; the supplied client is never closed.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("rerank batch_size must be a positive integer")  # noqa: TRY003
        self.policy_id = policy_id
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Return exact global ranks and the number of HTTP scoring requests sent."""

        if not candidates:
            raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
        if type(limit) is not int or not 1 <= limit <= len(candidates):
            raise ValueError("rerank limit must be an integer between one and the candidate count")  # noqa: TRY003
        scores: dict[int, int | float] = {}
        requests = 0
        for offset in range(0, len(candidates), self._batch_size):
            batch = candidates[offset : offset + self._batch_size]
            requests += 1
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": rank, "text": candidate.text} for rank, candidate in enumerate(batch, start=offset + 1)
                    ],
                },
                follow_redirects=False,
            )
            response.raise_for_status()
            scores.update(_batch_scores(response, range(offset + 1, offset + len(batch) + 1)))
        ordered_ranks = sorted(scores, key=lambda rank: (-scores[rank], rank))
        return MemoryRerankDecision(
            selected_ranks=tuple(ordered_ranks[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("rerank response contains a duplicate JSON member")  # noqa: TRY003
        value[key] = item
    return value


def _batch_scores(response: httpx.Response, ranks: range) -> dict[int, int | float]:
    value = response.json(object_pairs_hook=_unique_json_object)
    if not isinstance(value, dict) or set(value) != {"scores"} or not isinstance(value["scores"], list):
        raise ValueError("rerank response must contain only a scores array")  # noqa: TRY003
    scores: dict[int, int | float] = {}
    for item in value["scores"]:
        if not isinstance(item, dict) or set(item) != {"rank", "score"}:
            raise ValueError("rerank scores must contain exactly rank and score")  # noqa: TRY003
        rank, score = item["rank"], item["score"]
        if type(rank) is not int or rank not in ranks or rank in scores:
            raise ValueError("rerank scores must cover each rank in this batch exactly once")  # noqa: TRY003
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("rerank score must be a finite number")  # noqa: TRY003, TRY004
        if isinstance(score, float) and not math.isfinite(score):
            raise ValueError("rerank score must be a finite number")  # noqa: TRY003
        scores[rank] = score
    if len(scores) != len(ranks):
        raise ValueError("rerank scores do not cover the complete batch")  # noqa: TRY003
    return scores


__all__ = ["HTTPMemoryReranker"]
