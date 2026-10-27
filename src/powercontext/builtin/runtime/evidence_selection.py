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

"""Offline query-weighted selection of literal Memory sentence windows."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from powercontext.builtin.runtime.prepared_context import MemoryEntryAddress, PreparedMemoryFragment

_WORDS = re.compile(r"[a-z]+|\d+|[\u3400-\u9fff]", re.IGNORECASE)
_BOUNDARIES = re.compile(r"(?<=[.!?。！？])\s+|\n+")  # noqa: RUF001 - include Chinese sentence punctuation
_STOP_WORDS = frozenset(
    "a an the and or of to in on at by for from with as is are was were be been being "  # noqa: SIM905
    "it its this that these those which what how when where who do does did can could "
    "would should has have had into than then their there them we our they you your".split()
)


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us")):
        word = word[:-1]
    for ending in ("ation", "atory", "ing", "ion", "ed"):
        if len(word) > len(ending) + 3 and word.endswith(ending):
            word = word[: -len(ending)] + ("ate" if ending in {"ation", "atory"} else "")
            break
    return word[:-1] if len(word) > 5 and word.endswith("e") else word


def _terms(text: str) -> list[str]:
    return [
        _stem(word)
        for word in _WORDS.findall(unicodedata.normalize("NFKC", text).casefold())
        if word not in _STOP_WORDS
    ]


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    start = 0
    for end in [match.end() for match in _BOUNDARIES.finditer(text)] + [len(text)]:
        left, right = start, end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            spans.append((left, right))
        start = end
    return spans


def _windows(text: str, query: set[str]) -> Iterator[tuple[int, int]]:
    spans = _sentence_spans(text)
    for index, (start, end) in enumerate(spans):
        for length in (1, 2, 3):
            if index + length <= len(spans):
                yield start, spans[index + length - 1][1]
        if len(text[start:end].encode("utf-8")) <= 600:
            continue
        words = list(_WORDS.finditer(text, start, end))
        for position, word in enumerate(words):
            if _stem(word.group().casefold()) not in query:
                continue
            for width in (12, 24, 40):
                left = max(0, position - width // 2)
                right = min(len(words), left + width)
                yield words[left].start(), words[right - 1].end()


@dataclass(frozen=True)
class _Candidate:
    fragment: PreparedMemoryFragment
    source: str
    score: float
    matched: frozenset[str]
    cost: int


def _candidates(
    entries: Sequence[tuple[MemoryEntryAddress, str]],
    query: set[str],
    render: Callable[[Sequence[PreparedMemoryFragment]], str],
    max_bytes: int,
) -> list[_Candidate]:
    from powercontext.builtin.runtime.prepared_context import PreparedMemoryFragment

    document_terms = [Counter(_terms(text)) for _, text in entries]
    frequencies = Counter(term for terms in document_terms for term in terms)
    weights = {term: math.log1p((len(entries) + 1) / (frequencies[term] + 0.5)) for term in query}
    overhead = len(render(()).encode("utf-8"))
    candidates = []
    for (address, text), document in zip(entries, document_terms, strict=True):
        seen: set[str] = set()
        local = []
        document_score = sum(weights[term] for term in query.intersection(document))
        for start, end in _windows(text, query):
            selected = text[start:end]
            if selected in seen:
                continue
            seen.add(selected)
            terms = Counter(_terms(selected))
            matched = query.intersection(terms)
            if not matched:
                continue
            fragment = PreparedMemoryFragment(address, start, end, selected)
            wire_bytes = len(render((fragment,)).encode("utf-8"))
            if wire_bytes > max_bytes:
                continue
            score = sum(weights[term] * (1 + math.log(terms[term])) for term in matched)
            score = score / (0.8 + 0.2 * sum(terms.values()) / 40) + 0.15 * document_score
            local.append(_Candidate(fragment, text, score, frozenset(matched), wire_bytes - overhead))
        # Bound fitting work per input entry, without discarding any source identity.
        candidates.extend(sorted(local, key=lambda item: -item.score / item.cost**0.5)[:32])
    return candidates


def _merge(
    selected: tuple[PreparedMemoryFragment, ...],
    candidate: _Candidate,
) -> tuple[PreparedMemoryFragment, ...]:
    from powercontext.builtin.runtime.prepared_context import PreparedMemoryFragment

    fragment = candidate.fragment
    start, end = fragment.start, fragment.end
    retained = []
    for old in selected:
        gap = candidate.source[min(old.end, end) : max(old.start, start)]
        if old.address == fragment.address and not gap.strip():
            start, end = min(old.start, start), max(old.end, end)
        else:
            retained.append(old)
    retained.append(PreparedMemoryFragment(fragment.address, start, end, candidate.source[start:end]))
    return tuple(retained)


def select_fragments(
    entries: Sequence[tuple[MemoryEntryAddress, str]],
    query: str,
    max_bytes: int,
    render: Callable[[Sequence[PreparedMemoryFragment]], str],
    max_fragments: int,
) -> tuple[PreparedMemoryFragment, ...]:
    """Choose literal windows by marginal relevance per complete serialized byte cost."""

    query_terms = set(_terms(query))
    candidates = _candidates(entries, query_terms, render, max_bytes)
    selected: tuple[PreparedMemoryFragment, ...] = ()
    covered: set[str] = set()
    used_bytes = 0
    while len(selected) < max_fragments:
        best = None
        best_value = 0.0
        for candidate in candidates:
            fragment = candidate.fragment
            overlap = sum(
                max(0, min(old.end, fragment.end) - max(old.start, fragment.start))
                for old in selected
                if old.address == fragment.address
            )
            added = len(fragment.text) - overlap
            if added <= 0:
                continue
            proposed = _merge(selected, candidate)
            size = len(render(proposed).encode("utf-8"))
            if size > max_bytes:
                continue
            novelty = 0.35 + 0.65 * len(candidate.matched - covered) / len(candidate.matched)
            gain = candidate.score * added / len(fragment.text) * novelty / max(64, size - used_bytes) ** 0.5
            if gain > best_value:
                best_value = gain
                best = proposed, size, candidate.matched
        if best is None:
            break
        selected, used_bytes, matched = best
        covered.update(matched)
    return selected
