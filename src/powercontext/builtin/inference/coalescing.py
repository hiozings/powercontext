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

"""Single-event-loop embedding coalescing with bounded successful-vector reuse."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from dataclasses import dataclass

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass(frozen=True, slots=True)
class _TextResult:
    vector: EmbeddingVector
    batch_id: int
    usage: InferenceUsage


class CoalescingEmbeddingModel:
    """Share pending work and cache successful vectors for one event loop.

    The wrapped model and its deployment-fixed profile remain caller-owned.
    Caller cancellation does not cancel provider batches. Usage is charged once
    per awaited batch to each caller; all-cache calls report zero requests.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size <= 0:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, (int, float)) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a finite non-negative number")  # noqa: TRY003
        try:
            delay = float(coalesce_ms) / 1000
        except OverflowError as error:
            raise ValueError("coalesce_ms must be a finite non-negative number") from error  # noqa: TRY003
        if not math.isfinite(delay):
            raise ValueError("coalesce_ms must be a finite non-negative number")  # noqa: TRY003
        self.profile = model.profile
        self._model = model
        self._max_batch_size = max_batch_size
        self._delay = delay
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[_TextResult]] = {}
        self._pending: dict[str, asyncio.Future[_TextResult]] = {}
        self._timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._next_batch_id = 0

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        requested = tuple(texts)
        if any(not isinstance(text, str) for text in requested):
            raise ValueError("embedding inputs must be strings")  # noqa: TRY003
        if not requested:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        loop = asyncio.get_running_loop()
        vectors: dict[str, EmbeddingVector] = {}
        waiting: dict[str, asyncio.Future[_TextResult]] = {}
        # No awaits until registration is complete: concurrent callers see one
        # shared future per exact text, even while the provider is running.
        for text in requested:
            cached = self._cache.get(text)
            if cached is not None:
                self._cache.move_to_end(text)
                vectors[text] = cached
                continue
            future = self._inflight.get(text)
            if future is None:
                future = loop.create_future()
                future.add_done_callback(_observe_completion)
                self._inflight[text] = future
                self._pending[text] = future
            waiting[text] = future
        if self._pending and self._timer is None:
            self._timer = loop.call_later(self._delay, self._dispatch)
        # asyncio.wait never propagates cancellation to the shared futures.
        # Observe failures promptly without cancelling unrelated provider batches.
        pending = set(waiting.values())
        while pending:
            completed, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for future in completed:
                future.result()
        batches: dict[int, InferenceUsage] = {}
        for text, future in waiting.items():
            result = future.result()
            vectors[text] = result.vector
            batches[result.batch_id] = result.usage
        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in requested),
            usage=_total_usage(tuple(batches.values())),
        )

    def _dispatch(self) -> None:
        self._timer = None
        pending = tuple(self._pending.items())
        self._pending.clear()
        for offset in range(0, len(pending), self._max_batch_size):
            self._next_batch_id += 1
            task = asyncio.create_task(
                self._run_batch(pending[offset : offset + self._max_batch_size], self._next_batch_id)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_batch(self, batch: tuple[tuple[str, asyncio.Future[_TextResult]], ...], batch_id: int) -> None:
        try:
            result = await self._model.embed(tuple(text for text, _ in batch))
            if len(result.vectors) != len(batch):
                raise ValueError("embedding vector count does not match the submitted batch")  # noqa: TRY003, TRY301
            usage = result.usage.model_copy(deep=True)
            for (text, future), vector in zip(batch, result.vectors, strict=True):
                self._cache_vector(text, vector)
                future.set_result(_TextResult(vector, batch_id, usage))
        except asyncio.CancelledError:
            for _text, future in batch:
                future.cancel()
            raise
        except Exception as error:
            for _text, future in batch:
                if not future.done():
                    future.set_exception(error)
        finally:
            for text, _future in batch:
                self._inflight.pop(text, None)

    def _cache_vector(self, text: str, vector: EmbeddingVector) -> None:
        if not self._cache_size:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _observe_completion(future: asyncio.Future[_TextResult]) -> None:
    # A provider can fail after every caller has cancelled its wait.
    # Retrieving the exception prevents an orphan warning without changing awaits.
    if not future.cancelled():
        future.exception()


def _total_usage(batches: tuple[InferenceUsage, ...]) -> InferenceUsage:
    input_tokens = [batch.input_tokens for batch in batches if batch.input_tokens is not None]
    output_tokens = [batch.output_tokens for batch in batches if batch.output_tokens is not None]
    return InferenceUsage(
        requests=sum(batch.requests for batch in batches),
        input_tokens=sum(input_tokens) if input_tokens else None,
        output_tokens=sum(output_tokens) if output_tokens else None,
    )


__all__ = ["CoalescingEmbeddingModel"]
