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

"""Shared embedding work preserves cancellation boundaries and per-call provider usage."""

import asyncio

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


class Metered:
    profile = EmbeddingProfile(profile_id="coalescing-tests", model="offline", dimension=1)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        self.calls.append(texts)
        return EmbeddingResult(
            vectors=tuple((float(len(text)),) for text in texts),
            usage=InferenceUsage(requests=2, input_tokens=11, output_tokens=3),
        )


def test_late_waiter_shares_inflight_work_without_cross_call_cache() -> None:
    class Blocked(Metered):
        def __init__(self) -> None:
            super().__init__()
            self.started, self.release = asyncio.Event(), asyncio.Event()

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.started.set()
            await self.release.wait()
            return await super().embed(texts)

    async def scenario() -> None:
        base = Blocked()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
        first = asyncio.create_task(model.embed(("shared", "shared")))
        await base.started.wait()
        late = asyncio.create_task(model.embed(("shared",)))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        base.release.set()
        result = await late
        assert result.vectors == ((6.0,),)
        assert result.usage == InferenceUsage(requests=2, input_tokens=11, output_tokens=3)
        assert base.calls == [("shared",)]
        await model.embed(("shared",))
        assert base.calls == [("shared",), ("shared",)]
        assert model.profile == base.profile

    asyncio.run(scenario())


def test_usage_counts_each_waited_batch_once_and_excludes_cached_text() -> None:
    async def scenario() -> None:
        base = Metered()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0)
        first, second = await asyncio.gather(model.embed(("one", "two", "three", "one")), model.embed(("two", "four")))
        assert sorted(text for batch in base.calls for text in batch) == ["four", "one", "three", "two"]
        assert all(len(batch) <= 2 for batch in base.calls)
        for texts, result in (({"one", "two", "three"}, first), ({"two", "four"}, second)):
            batches = sum(bool(texts.intersection(batch)) for batch in base.calls)
            assert result.usage == InferenceUsage(
                requests=2 * batches, input_tokens=11 * batches, output_tokens=3 * batches
            )
        assert first.vectors == ((3.0,), (3.0,), (5.0,), (3.0,))
        mixed = await model.embed(("one", "fresh", "fresh", "four"))
        assert mixed.vectors == ((3.0,), (5.0,), (5.0,), (4.0,))
        assert mixed.usage == InferenceUsage(requests=2, input_tokens=11, output_tokens=3)
        cached = await model.embed(("one", "fresh", "four"))
        assert cached.usage.requests == 0
        assert len(base.calls) == 3

    asyncio.run(scenario())


def test_failed_batch_does_not_block_independent_work_or_poison_retry() -> None:
    class Failure(RuntimeError):
        pass

    class Partial(Metered):
        fail = True

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            if "bad" in texts and self.fail:
                self.calls.append(texts)
                raise Failure
            return await super().embed(texts)

    async def scenario() -> None:
        base = Partial()
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        failed, succeeded = await asyncio.gather(
            model.embed(("bad", "good")), model.embed(("good",)), return_exceptions=True
        )
        assert isinstance(failed, Failure)
        assert isinstance(succeeded, EmbeddingResult) and succeeded.vectors == ((4.0,),)
        assert succeeded.usage.requests == 2
        base.fail = False
        recovered = await model.embed(("bad", "good"))
        assert recovered.vectors == ((3.0,), (4.0,))
        assert recovered.usage.requests == 2
        assert base.calls.count(("good",)) == 1 and base.calls.count(("bad",)) == 2

    asyncio.run(scenario())


def test_malformed_vector_count_is_not_partially_cached() -> None:
    class Truncated(Metered):
        broken = True

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            result = await super().embed(texts)
            return result.model_copy(update={"vectors": result.vectors[:-1]}) if self.broken else result

    async def scenario() -> None:
        base = Truncated()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        with pytest.raises(ValueError):
            await model.embed(("one", "two"))
        base.broken = False
        assert (await model.embed(("one", "two"))).vectors == ((3.0,), (3.0,))
        assert base.calls == [("one", "two"), ("one", "two")]

    asyncio.run(scenario())


def test_lru_hit_refreshes_recency_without_charging_or_mutating_cache() -> None:
    async def scenario() -> None:
        base = Metered()
        model = CoalescingEmbeddingModel(base, cache_size=2, coalesce_ms=0)
        first = await model.embed(("alpha", "beta"))
        first.vectors = ((999.0,),)
        assert (await model.embed(("beta", "alpha", "alpha"))).vectors == ((4.0,), (5.0,), (5.0,))
        await model.embed(("gamma",))
        assert (await model.embed(("alpha",))).usage.requests == 0
        assert (await model.embed(("beta",))).usage.requests == 2
        assert base.calls == [("alpha", "beta"), ("gamma",), ("beta",)]

    asyncio.run(scenario())


def test_cancelled_callers_do_not_leave_unhandled_failures_and_can_retry() -> None:
    class Abandoned(Metered):
        def __init__(self) -> None:
            super().__init__()
            self.started, self.release, self.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
            self.fail = True

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.started.set()
            await self.release.wait()
            try:
                if self.fail:
                    raise RuntimeError
                return await super().embed(texts)
            finally:
                self.finished.set()

    async def scenario() -> None:
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
        base = Abandoned()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        caller = asyncio.create_task(model.embed(("one", "two")))
        await base.started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        base.release.set()
        await base.finished.wait()
        await asyncio.sleep(0)
        base.fail = False
        assert (await model.embed(("one", "two"))).vectors == ((3.0,), (3.0,))
        assert errors == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_batch_size": 0},
        {"max_batch_size": True},
        {"max_batch_size": 1.5},
        {"cache_size": -1},
        {"cache_size": False},
        {"cache_size": "1"},
        {"coalesce_ms": -1},
        {"coalesce_ms": True},
        {"coalesce_ms": float("nan")},
        {"coalesce_ms": float("inf")},
    ],
)
def test_invalid_limits_are_rejected(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(Metered(), **arguments)  # ty: ignore[invalid-argument-type]
