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

"""Raw Pi evidence and checkpoint recovery through the public connector contract."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import JsonValue

from powercontext.errors import ConnectorSubmissionRejectedError
from powercontext.sources import ConnectorBinding, SourceDefinitionRegistry, SourceMaterialization, SourceRef
from powercontext.sources.connectors import ConnectorLifecycle, ConnectorRunStatus
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def archive(records: list[dict[str, JsonValue]], session_id: str = "session") -> bytes:
    header = {"type": "session", "version": 3, "id": session_id}
    return ("\n".join(json.dumps(row, ensure_ascii=False) for row in [header, *records]) + "\n").encode()


def message(event_id: str, parent_id: str | None, content: str) -> dict[str, JsonValue]:
    return {"type": "message", "id": event_id, "parentId": parent_id, "message": {"role": "user", "content": content}}


def test_original_chain_keeps_summaries_tools_unicode_and_control_ancestors() -> None:
    records: list[dict[str, JsonValue]] = [
        message("root", None, "原文\u2028不拆行"),
        {"type": "model_change", "id": "control", "parentId": "root"},
        {
            "type": "message",
            "id": "call",
            "parentId": "control",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "  before  "},
                    {"type": "thinking", "thinking": "not visible"},
                    {"type": "image", "data": "not visible"},
                    {"type": "toolCall", "name": "read", "id": "call-1", "arguments": {"z": [1, True], "a": "中文"}},
                    {"type": "text", "text": "after"},
                ],
            },
        },
        {"type": "compaction", "id": "summary", "parentId": "call", "summary": "摘要", "firstKeptEntryId": "call"},
        {"type": "context_edit", "id": "edit", "parentId": "summary", "targetId": "root", "replacement": None},
        message("other", "root", "other branch"),
    ]
    events = parse_pi_session(PiSessionInput(content=archive(records), leaf_id="edit"))
    assert [event.event_id for event in events] == ["root", "call", "summary"]
    assert [event.content for event in events] == [
        "user: 原文\u2028不拆行",
        'assistant:   before  \n[tool_call read call-1] {"a":"中文","z":[1,true]}\nafter',
        "compaction: 摘要",
    ]
    assert events[1].parent_id == "control"
    assert events[1].record == records[2]
    assert parse_pi_session(PiSessionInput(content=archive(records)))[-1].content == "user: other branch"


def test_source_preserves_large_tool_result_and_full_record_after_serialization() -> None:
    output = "完整工具结果\n" * 10000
    record: dict[str, JsonValue] = {
        "type": "message",
        "id": "result",
        "parentId": None,
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {
            "role": "toolResult",
            "toolName": "read",
            "toolCallId": "one",
            "content": [{"type": "text", "text": output}],
            "details": {"exitCode": 0},
        },
    }
    event = parse_pi_session(PiSessionInput(content=archive([record])))[0]
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(event))
    restored = type(source).model_validate_json(source.model_dump_json())
    assert restored.materialization == SourceMaterialization.CAPTURED
    assert asyncio.run(registry.read(restored)) == event
    assert event.record == record
    projection = registry.project(restored, TEXT_EVIDENCE_PROJECTION_KEY)
    assert isinstance(projection, dict)
    assert projection["content"] == "toolResult: [tool_result read one] " + output
    assert projection["metadata"] == {
        "session_id": "session",
        "event_id": "result",
        "parent_id": None,
        "event_type": "message",
        "role": "toolResult",
    }


@pytest.mark.parametrize(
    "record",
    [
        {"type": "message", "id": "missing-parent", "message": {"role": "user", "content": "x"}},
        {"type": "label", "id": "bad-parent", "parentId": 0},
        {"type": "label", "id": "forward-parent", "parentId": "later"},
        {"type": "label", "id": "root", "parentId": None},
        {"type": "label", "id": "", "parentId": None},
        {
            "type": "message",
            "id": "invalid-text",
            "parentId": None,
            "message": {"role": "user", "content": [{"type": "text", "text": 42}]},
        },
    ],
)
def test_whole_archive_is_validated_even_outside_selected_branch(record: dict[str, JsonValue]) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=archive([message("root", None, "good"), record]), leaf_id="root"))


def test_header_only_and_invisible_branch_have_no_text_events() -> None:
    assert parse_pi_session(PiSessionInput(content=archive([]))) == ()
    records: list[dict[str, JsonValue]] = [
        {"type": "custom", "id": "a", "parentId": None, "data": {"text": "private"}},
        {
            "type": "message",
            "id": "b",
            "parentId": "a",
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "private"}, {"type": "image", "data": "private"}],
            },
        },
    ]
    assert parse_pi_session(PiSessionInput(content=archive(records))) == ()
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=archive([]), leaf_id="missing"))


def test_identity_is_bounded_and_separates_session_and_event_components() -> None:
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    identities = set()
    for session_id, event_id in [("a:b", "c"), ("a", "b:c"), ("a%3Ab", "c"), ("会话" * 300, "事件" * 300)]:
        event = parse_pi_session(PiSessionInput(content=archive([message(event_id, None, "original")], session_id)))[0]
        source = asyncio.run(registry.resolve(event))
        assert 0 < len(source.name) <= 256
        identities.add(source.name)
        changed = parse_pi_session(PiSessionInput(content=archive([message(event_id, None, "changed")], session_id)))[0]
        assert asyncio.run(registry.resolve(changed)).name == source.name
    assert len(identities) == 4


class Checkpoints:
    def __init__(self) -> None:
        self.value: JsonValue = None

    async def load(self, binding: ConnectorBinding, /) -> JsonValue:
        return deepcopy(self.value)

    async def save(self, binding: ConnectorBinding, checkpoint: JsonValue, /, *, expected: JsonValue) -> None:
        assert self.value == expected
        self.value = json.loads(json.dumps(checkpoint))


class EventSink:
    def __init__(self) -> None:
        self.events: dict[str, PiEventInput] = {}
        self.reject_id: str | None = None

    async def submit(
        self, binding: ConnectorBinding, item_id: str, definition_name: str, value: object, /
    ) -> SourceRef:
        assert isinstance(value, PiEventInput)
        if value.event_id == self.reject_id:
            raise ConnectorSubmissionRejectedError("temporarily rejected")  # noqa: TRY003
        if item_id in self.events:
            assert self.events[item_id] == value
        self.events[item_id] = value.model_copy(deep=True)
        return SourceRef(source_type=definition_name, source_id=item_id)


@pytest.mark.parametrize("mutation", ["content", "metadata", "removed", "session"])
def test_connector_checks_imported_off_branch_records_before_any_new_submission(tmp_path: Path, mutation: str) -> None:
    async def scenario() -> None:
        path = tmp_path / "session.jsonl"
        records = [message("root", None, "shared"), message("left", "root", "left"), message("right", "root", "right")]
        path.write_bytes(archive(records))
        checkpoints, sink = Checkpoints(), EventSink()
        binding = ConnectorBinding(
            scope_id="scope", binding_id="pi", connector_name="pi-session-history", connector_version="1"
        )
        lifecycle = ConnectorLifecycle(sink=sink, checkpoints=checkpoints)
        first = await lifecycle.run(PiSessionConnector(path, "left"), binding)
        assert len(first.items) == 2
        previous = deepcopy(checkpoints.value)
        changed = deepcopy(records)
        if mutation == "content":
            changed[1] = message("left", "root", "rewritten off branch")
        elif mutation == "metadata":
            changed[1]["timestamp"] = "2026-01-01T00:00:00Z"
        elif mutation == "removed":
            changed.pop(1)
        path.write_bytes(archive(changed, "different-session" if mutation == "session" else "session"))
        with pytest.raises(ValueError):
            await lifecycle.run(PiSessionConnector(path, "right"), binding)
        assert len(sink.events) == 2 and checkpoints.value == previous
        path.write_bytes(archive(records))
        recovered = await lifecycle.run(PiSessionConnector(path, "right"), binding)
        assert len(recovered.items) == 1 and len(sink.events) == 3

    asyncio.run(scenario())


def test_rejected_write_keeps_checkpoint_and_retry_reuses_persisted_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "session.jsonl"
        path.write_bytes(archive([message("root", None, "root")]))
        checkpoints, sink = Checkpoints(), EventSink()
        binding = ConnectorBinding(
            scope_id="scope", binding_id="pi", connector_name="pi-session-history", connector_version="1"
        )
        lifecycle = ConnectorLifecycle(sink=sink, checkpoints=checkpoints)
        await lifecycle.run(PiSessionConnector(path), binding)
        previous = deepcopy(checkpoints.value)
        records = [
            message("root", None, "root"),
            message("second", "root", "second"),
            message("third", "second", "third"),
        ]
        path.write_bytes(archive(records))
        sink.reject_id = "third"
        failed = await lifecycle.run(PiSessionConnector(path), binding)
        assert failed.status == ConnectorRunStatus.INCOMPLETE
        assert checkpoints.value == previous and len(sink.events) == 2
        sink.reject_id = None
        recovered = await ConnectorLifecycle(sink=sink, checkpoints=checkpoints).run(PiSessionConnector(path), binding)
        assert recovered.status == ConnectorRunStatus.COMPLETE and len(sink.events) == 3
        assert (await lifecycle.run(PiSessionConnector(path), binding)).items == ()

    asyncio.run(scenario())
