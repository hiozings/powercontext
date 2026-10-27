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

"""Selection preserves exact cited substrings and charges the complete wire envelope."""

import json
from itertools import pairwise

import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import PreparedContextBuilder, PreparedMemoryCandidates
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY


def candidates(scope: str, text: str) -> PreparedMemoryCandidates:
    reference = ArtifactRef(family="memory", artifact_id="memory", revision=7)
    return PreparedMemoryCandidates(
        scope_id=scope,
        memory_ref=reference,
        hits=(
            MemoryHit(
                memory_ref=reference,
                entry_id="same-entry",
                entry_version_id="same-version",
                text=text,
                score=1.0,
                matched_by=("fts",),
            ),
        ),
    )


def build(query: str, groups: tuple[PreparedMemoryCandidates, ...], *, budget: int = 1400, enabled: bool = True):
    return PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query=query, max_bytes=budget, evidence_selection=enabled),
        current_scope_id="local",
        memory_candidates=groups,
    )


@pytest.mark.parametrize("budget", [512, 900, 1400, 1800])
def test_complete_unicode_envelope_and_fragment_offsets_fit_budget(budget: int) -> None:
    text = (
        "Administrative notes. " * 50 + '海洋观测🌊 station records "温度" at 37 degrees.\nAnother unrelated sentence.'
    )
    result = build("海洋观测 station temperature", (candidates("local", text),), budget=budget)
    content = result.context.content or ""
    assert result.context.content_bytes == len(content.encode()) <= budget
    assert len(result.fragments) == len(result.origins)
    if content:
        assert content.startswith(TRUST_POLICY)
        assert "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n" in content
        assert content.endswith("END_POWERCONTEXT_PREPARED_CONTEXT_V1")
        envelope = json.loads(content.splitlines()[-2])
        assert envelope["trust"] == "untrusted_history"
        for item, fragment, origin in zip(envelope["items"], result.fragments, result.origins, strict=True):
            assert type(fragment.start) is int and type(fragment.end) is int
            assert 0 <= fragment.start < fragment.end <= len(text)
            assert fragment.text == text[fragment.start : fragment.end] == item["content"]
            assert item["start"] == fragment.start and item["end"] == fragment.end
            assert origin == fragment.address
            assert item["citation"] == {
                "scope_id": "local",
                "memory_ref": {"family": "memory", "artifact_id": "memory", "revision": 7},
                "entry_id": "same-entry",
                "entry_version_id": "same-version",
            }
    if budget >= 900:
        assert any("37" in fragment.text for fragment in result.fragments)


def test_queries_choose_different_late_evidence_in_the_same_candidate() -> None:
    text = (
        "Background information. " * 45
        + "The cobalt sensor calibrates at 37 degrees. "
        + "Unrelated control information. " * 40
        + "The amber valve opens at 19 degrees."
    )
    groups = (candidates("local", text),)
    cobalt = build("cobalt sensor calibration", groups, budget=900)
    amber = build("amber valve opens", groups, budget=900)
    assert any("37" in fragment.text for fragment in cobalt.fragments)
    assert any("19" in fragment.text for fragment in amber.fragments)
    assert cobalt.fragments != amber.fragments


@pytest.mark.parametrize("identical_text", [False, True])
def test_scopes_with_identical_entry_ids_keep_distinct_origins(identical_text: bool) -> None:
    first = "The cobalt sensor calibrates at 37 degrees."
    second = first if identical_text else "The amber valve opens at 19 degrees."
    groups = (candidates("local", first), candidates("reference", second))
    result = build("cobalt sensor amber valve", groups, budget=1800)
    assert {fragment.address.memory.scope_id for fragment in result.fragments} == {"local", "reference"}
    for fragment in result.fragments:
        source = first if fragment.address.memory.scope_id == "local" else second
        assert fragment.text == source[fragment.start : fragment.end]


def test_nonadjacent_evidence_can_share_one_entry_without_inventing_text() -> None:
    text = (
        "Cobalt sensors require calibration. "
        + "Unrelated administrative information. " * 60
        + "Cobalt sensors need maintenance."
    )
    result = build("cobalt sensor calibration maintenance", (candidates("local", text),), budget=1400)
    assert any("calibration" in fragment.text for fragment in result.fragments)
    assert any("maintenance" in fragment.text for fragment in result.fragments)
    assert len(result.fragments) >= 2
    intervals = sorted((fragment.start, fragment.end) for fragment in result.fragments)
    assert all(left[1] <= right[0] for left, right in pairwise(intervals))


def test_long_unpunctuated_evidence_can_be_selected_away_from_prefix() -> None:
    text = "unrelated words " * 100 + "cobalt sensor calibration " + "other background " * 100
    result = build("cobalt sensor calibration", (candidates("local", text),), budget=1000)
    assert any("cobalt sensor calibration" in fragment.text for fragment in result.fragments)


def test_default_mode_is_unchanged_and_empty_selection_is_valid() -> None:
    groups = (candidates("local", "A station records temperature."),)
    ordinary = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="temperature", max_bytes=1200),
        current_scope_id="local",
        memory_candidates=groups,
    )
    assert ordinary == build("temperature", groups, budget=1200, enabled=False)
    assert ordinary.fragments == ()
    assert build("unknownxyz", ()).context.content is None


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_line_separators_preserve_one_json_envelope_line(separator: str) -> None:
    text = f"Cobalt{separator}sensor calibration evidence."
    result = build("cobalt sensor calibration", (candidates("local", text),))
    assert result.context.content is not None
    item = json.loads(result.context.content.splitlines()[-2])["items"][0]
    assert item["content"] == text
    assert result.fragments[0].text == text


def test_budget_probe_describes_the_same_selected_output() -> None:
    request = PrepareContextRequest(query="sensor", max_bytes=1200, evidence_selection=True)
    groups = (candidates("local", "Background notes. " * 50 + "The cobalt sensor is calibrated."),)
    builder = PreparedContextBuilder()
    result = builder.build_scopes_result(request=request, current_scope_id="local", memory_candidates=groups)
    probe = builder.probe_budget(request=request, current_scope_id="local", memory_candidates=groups)
    assert probe.delivered_items == len(result.fragments)
    assert probe.unused_bytes == request.max_bytes - result.context.content_bytes
