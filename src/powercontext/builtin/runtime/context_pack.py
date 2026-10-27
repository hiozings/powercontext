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

"""Canonical v1 context packs and independently verifiable prefix deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """The context pack, delta, or requested chunking violates the wire contract."""


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_items(items: tuple[bytes, ...]) -> None:
    if not isinstance(items, tuple) or any(not isinstance(item, bytes) for item in items):
        raise ContextPackError("context pack items must be a tuple of bytes")  # noqa: TRY003


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("context pack wire items must be an array")  # noqa: TRY003
    items: list[bytes] = []
    for encoded in value:
        if not isinstance(encoded, str):
            raise ContextPackError("context pack wire items must be Base64 strings")  # noqa: TRY003
        try:
            item = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeError, binascii.Error) as error:
            raise ContextPackError("invalid Base64 context pack item") from error  # noqa: TRY003
        if base64.b64encode(item).decode("ascii") != encoded:
            raise ContextPackError("context pack items must use standard padded Base64")  # noqa: TRY003
        items.append(item)
    return tuple(items)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContextPackError("duplicate context pack JSON field")  # noqa: TRY003
        result[key] = value
    return result


def _read_wire(data: bytes, schema: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise ContextPackError("context pack wire data must be bytes")  # noqa: TRY003
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as error:
        raise ContextPackError("context pack wire data must be valid UTF-8 JSON") from error  # noqa: TRY003
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != schema:
        raise ContextPackError("incorrect context pack schema or fields")  # noqa: TRY003
    return value


def _validate_digest(digest: str) -> None:
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ContextPackError("context pack digest must be sha256 followed by lowercase hexadecimal")  # noqa: TRY003


@dataclass(frozen=True, slots=True)
class ContextPack:
    """An immutable ordered byte sequence with derived digest and complete wire size."""

    items: tuple[bytes, ...]
    digest: str = field(init=False)
    byte_count: int = field(init=False)

    def __post_init__(self) -> None:
        _validate_items(self.items)
        unsigned: dict[str, object] = {"schema": _PACK_SCHEMA, "items": _encode_items(self.items)}
        digest = "sha256:" + hashlib.sha256(_json_bytes(unsigned)).hexdigest()
        object.__setattr__(self, "digest", digest)
        payload = {**unsigned, "digest": digest, "byte_count": 0}
        size = len(_json_bytes(payload))
        while payload["byte_count"] != size:
            payload["byte_count"] = size
            size = len(_json_bytes(payload))
        object.__setattr__(self, "byte_count", size)

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        """Build a pack without changing its bytes or block boundaries."""

        return cls(items)

    @classmethod
    def from_content(cls, content: str | bytes | None, *, chunk_bytes: int = 512) -> ContextPack:
        """Chunk UTF-8 deterministically, preferring complete lines within the byte bound."""

        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        try:
            raw = b"" if content is None else content.encode("utf-8") if isinstance(content, str) else content
            if not isinstance(raw, bytes):
                raise ContextPackError("context content must be text, UTF-8 bytes, or None")  # noqa: TRY003
            raw.decode("utf-8")
        except UnicodeError as error:
            raise ContextPackError("context content must be valid UTF-8") from error  # noqa: TRY003
        items = []
        start = 0
        while start < len(raw):
            end = min(start + chunk_bytes, len(raw))
            while end < len(raw) and raw[end] & 0xC0 == 0x80:
                end -= 1
            if end == start:
                raise ContextPackError("chunk_bytes cannot contain the next UTF-8 character")  # noqa: TRY003
            if end < len(raw):
                newline = raw.rfind(b"\n", start, end)
                if newline >= start:
                    end = newline + 1
            items.append(raw[start:end])
            start = end
        return cls.build(tuple(items))

    def to_bytes(self) -> bytes:
        """Encode the canonical complete v1 pack, including its own wire byte count."""

        return _json_bytes({
            "schema": _PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
            "byte_count": self.byte_count,
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Decode and verify the digest and canonical serialized byte count."""

        value = _read_wire(data, _PACK_SCHEMA, {"schema", "items", "digest", "byte_count"})
        pack = cls.build(_decode_items(value["items"]))
        if value["digest"] != pack.digest:
            raise ContextPackError("context pack digest mismatch")  # noqa: TRY003
        # At a decimal boundary, two self-consistent sizes can exist (999/1000).
        # Validate the sender's encoding, then normalize to our deterministic size.
        if type(value["byte_count"]) is not int or value["byte_count"] != len(_json_bytes(value)):
            raise ContextPackError("context pack byte count mismatch")  # noqa: TRY003
        return pack


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """Reuse an exact base prefix, then append independently encoded target blocks."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_digest(self.base_digest)
        _validate_digest(self.target_digest)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("delta prefix_items must be a non-negative integer")  # noqa: TRY003
        _validate_items(self.appended)

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Reuse the longest exact common block prefix."""

        prefix = 0
        for old, new in zip(base.items, target.items, strict=False):
            if old != new:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _json_bytes({
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        value = _read_wire(data, _DELTA_SCHEMA, {"schema", "base_digest", "target_digest", "prefix_items", "appended"})
        return cls(
            base_digest=value["base_digest"],
            target_digest=value["target_digest"],
            prefix_items=value["prefix_items"],
            appended=_decode_items(value["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild a complete target using only the supplied base and delta."""

    if base.digest != delta.base_digest:
        raise ContextPackError("delta base digest mismatch")  # noqa: TRY003
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base pack")  # noqa: TRY003
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest mismatch")  # noqa: TRY003
    return target


__all__ = ["ContextPack", "ContextPackDelta", "ContextPackError", "apply_context_pack_delta"]
