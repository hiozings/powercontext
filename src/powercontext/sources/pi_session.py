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

"""Capture raw Pi v3 branch history with durable, verified import progress."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class PiSessionInput(BaseModel):
    """Complete UTF-8 JSONL and the optional branch endpoint to import."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event, including its complete original JSON record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """Captured event evidence independent of the original session file."""

    event: PiEventInput


def _text(value: JsonValue, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Pi {field} must be a string")  # noqa: TRY003, TRY004
    return value


def _identifier(value: JsonValue, field: str) -> str:
    result = _text(value, field)
    if not result:
        raise ValueError(f"Pi {field} must not be empty")  # noqa: TRY003
    return result


def _compact_json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _event_identity(session_id: str, event_id: str) -> str:
    # Length-prefixing via a JSON array separates the two arbitrary identifiers.
    encoded = _compact_json([session_id, event_id]).encode("utf-8")
    return "pi:" + hashlib.sha256(encoded).hexdigest()


def _visible_content(value: JsonValue, *, tool_calls: bool = False) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if not isinstance(value, list):
        raise ValueError("Pi content must be text or a list of content blocks")  # noqa: TRY003, TRY004
    parts: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            raise ValueError("Pi content blocks must be JSON objects")  # noqa: TRY003, TRY004
        if block.get("type") == "text":
            parts.append(_text(block.get("text"), "text block"))
        elif tool_calls and block.get("type") == "toolCall":
            name = _identifier(block.get("name"), "tool name")
            call_id = _identifier(block.get("id"), "tool call ID")
            arguments = block.get("arguments")
            if not isinstance(arguments, dict):
                raise ValueError("Pi tool arguments must be a JSON object")  # noqa: TRY003
            parts.append(f"[tool_call {name} {call_id}] {_compact_json(arguments)}")
    return "\n".join(parts) if any(parts) else ""


def _event_body(record: dict[str, JsonValue]) -> tuple[str, str] | None:
    event_type = record["type"]
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError("Pi message must be a JSON object")  # noqa: TRY003
        role = _identifier(message.get("role"), "message role")
        if role == "bashExecution":
            command = _text(message.get("command"), "bash command")
            output = _text(message.get("output"), "bash output")
            body = f"$ {command}\n{output}" if command or output else ""
        else:
            body = _visible_content(message.get("content"), tool_calls=True)
            if role == "toolResult" and body:
                tool_name = _identifier(message.get("toolName"), "tool result name")
                call_id = _identifier(message.get("toolCallId"), "tool result call ID")
                body = f"[tool_result {tool_name} {call_id}] {body}"
    elif event_type in {"compaction", "branch_summary"}:
        role = _text(event_type, "summary type")
        body = _text(record.get("summary"), "summary")
    elif event_type == "custom_message":
        role = "custom"
        body = _visible_content(record.get("content"))
    else:
        return None
    return (role, body) if body else None


@dataclass(frozen=True)
class _ParsedSession:
    session_id: str
    records: dict[str, dict[str, JsonValue]]
    events: tuple[PiEventInput, ...]


def _reject_non_json_number(value: str) -> None:
    raise ValueError(f"Invalid JSON numeric constant: {value}")  # noqa: TRY003


def _read_records(content: bytes) -> list[dict[str, JsonValue]]:
    records: list[dict[str, JsonValue]] = []
    # JSONL separates on LF; Unicode line/paragraph separators inside strings are data.
    for line in content.decode("utf-8-sig").split("\n"):
        if not line.strip():
            continue
        record = json.loads(line, parse_constant=_reject_non_json_number)
        if not isinstance(record, dict):
            raise ValueError("Pi JSONL records must be objects")  # noqa: TRY003, TRY004
        records.append(record)
    if not records:
        raise ValueError("Pi history has no session header")  # noqa: TRY003
    return records


def _parse_session(value: PiSessionInput) -> _ParsedSession:
    header, *entries = _read_records(value.content)
    if header.get("type") != "session" or type(header.get("version")) is not int or header["version"] != 3:
        raise ValueError("Pi history must start with a version 3 session header")  # noqa: TRY003
    session_id = _identifier(header.get("id"), "session ID")
    records: dict[str, dict[str, JsonValue]] = {}
    events: dict[str, PiEventInput] = {}
    parents: dict[str, str | None] = {}
    last_id: str | None = None
    for record in entries:
        event_id = _identifier(record.get("id"), "event ID")
        event_type = _identifier(record.get("type"), "event type")
        if event_type == "session" or event_id in records:
            raise ValueError("Pi history contains a duplicate header or event ID")  # noqa: TRY003
        if "parentId" not in record:
            raise ValueError("Pi events must declare parentId")  # noqa: TRY003
        parent_id = record["parentId"]
        if parent_id is not None and (not isinstance(parent_id, str) or parent_id not in records):
            raise ValueError("Pi parentId must be null or refer to an earlier event")  # noqa: TRY003
        records[event_id] = record
        parents[event_id] = parent_id
        last_id = event_id
        rendered = _event_body(record)
        if rendered is not None:
            role, body = rendered
            events[event_id] = PiEventInput(
                session_id=session_id,
                event_id=event_id,
                parent_id=parent_id,
                event_type=event_type,
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
    leaf_id = value.leaf_id if value.leaf_id is not None else last_id
    if leaf_id is not None and leaf_id not in records:
        raise ValueError("Pi selected leaf does not exist")  # noqa: TRY003
    branch: list[PiEventInput] = []
    while leaf_id is not None:
        if leaf_id in events:
            branch.append(events[leaf_id])
        leaf_id = parents[leaf_id]
    return _ParsedSession(session_id, records, tuple(reversed(branch)))


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Validate the entire archive, then return visible raw events from root to leaf."""

    return _parse_session(value).events


class _PiEventAdapter:
    name = "pi-event"
    input_class = PiEventInput
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=_event_identity(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            event=value.model_copy(deep=True),
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return source.event.model_copy(deep=True)


class _PiTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        event = source.event
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=event.content,
            metadata={
                "session_id": event.session_id,
                "event_id": event.event_id,
                "parent_id": event.parent_id,
                "event_type": event.event_type,
                "role": event.role,
            },
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    _PiEventAdapter(),
    projections=(_PiTextEvidenceProjection(),),
)


class _PiCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    session_id: str
    records: dict[str, str]


class PiSessionConnector:
    """Import unseen events while verifying all previously committed records."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = await asyncio.to_thread(self.path.read_bytes)
        parsed = _parse_session(PiSessionInput(content=content, leaf_id=self.leaf_id))
        previous = (
            _PiCheckpoint(session_id=parsed.session_id, records={})
            if session.checkpoint is None
            else _PiCheckpoint.model_validate(session.checkpoint)
        )
        if previous.session_id != parsed.session_id:
            raise ValueError("Pi checkpoint belongs to a different session")  # noqa: TRY003
        selected_ids = {event.event_id for event in parsed.events}
        fingerprints = {
            event_id: hashlib.sha256(_compact_json(record).encode("utf-8")).hexdigest()
            for event_id, record in parsed.records.items()
            if event_id in previous.records or event_id in selected_ids
        }
        for event_id, fingerprint in previous.records.items():
            if fingerprints.get(event_id) != fingerprint:
                raise ValueError(f"Previously imported Pi event was changed or removed: {event_id}")  # noqa: TRY003
        imported = previous.records.copy()
        for event in parsed.events:
            if event.event_id in imported:
                continue
            outcome = await session.submit(_event_identity(event.session_id, event.event_id), "pi-event", event)
            if outcome.status != ConnectorSubmissionStatus.ACCEPTED:
                return ConnectorRunCompletion(status=ConnectorRunStatus.INCOMPLETE, checkpoint=session.checkpoint)
            imported[event.event_id] = fingerprints[event.event_id]
        checkpoint = _PiCheckpoint(session_id=parsed.session_id, records=imported)
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=checkpoint.model_dump(mode="json"))


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
]
