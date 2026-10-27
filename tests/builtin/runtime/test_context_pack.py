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

"""Independent wire checks and UTF-8 boundary behavior for context packs."""

import base64
import hashlib
import json
import subprocess
import sys

import pytest

from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)


@pytest.mark.parametrize("size", [0, 1, 600, 7000])
def test_pack_byte_count_includes_its_own_decimal_digits(size: int) -> None:
    items = (b"a" * size, "海洋🌊".encode())
    pack = ContextPack.build(items)
    payload = json.loads(pack.to_bytes())
    assert pack.byte_count == payload["byte_count"] == len(pack.to_bytes())
    unsigned = {"schema": "powercontext.context-pack.v1", "items": [base64.b64encode(item).decode() for item in items]}
    assert (
        pack.digest
        == "sha256:" + hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    assert ContextPack.from_bytes(pack.to_bytes()).items == items


@pytest.mark.parametrize("bound", range(4, 18))
def test_chunking_keeps_every_utf8_byte_and_is_deterministic(bound: int) -> None:
    content = "a\r\n海洋🌊\né\x00最后一行"
    pack = ContextPack.from_content(content, chunk_bytes=bound)
    assert pack == ContextPack.from_content(content.encode(), chunk_bytes=bound)
    assert b"".join(pack.items) == content.encode()
    assert all(0 < len(item) <= bound and item.decode().encode() == item for item in pack.items)
    assert ContextPack.from_content(None, chunk_bytes=bound).items == ()


@pytest.mark.parametrize(
    ("content", "bound"),
    [("🌊", 3), ("海", 2), ("text", 0), ("text", True), ("text", 2.5), (b"\xff", 4), ("\ud800", 4)],
)
def test_impossible_or_invalid_chunking_fails(content: str | bytes, bound: object) -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content(content, chunk_bytes=bound)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("wire_item", ["YQ", "YQ===", "Y Q==", "YR==", "_w==", "海", 1, None])
def test_nonstandard_base64_is_rejected(wire_item: object) -> None:
    base = ContextPack.build(())
    delta = ContextPackDelta.between(base, ContextPack.build((b"a",)))
    payload = {**json.loads(delta.to_bytes()), "appended": [wire_item]}
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps(payload).encode())


@pytest.mark.parametrize(
    "change",
    [
        {"byte_count": True},
        {"byte_count": 1.0},
        {"byte_count": 0},
        {"schema": "unknown"},
        {"digest": "sha256:" + "0" * 64},
        {"extra": 1},
    ],
)
def test_pack_rejects_tampered_metadata(change: dict[str, object]) -> None:
    payload = {**json.loads(ContextPack.build((b"valid",)).to_bytes()), **change}
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps(payload).encode())


@pytest.mark.parametrize("decoder", [ContextPack.from_bytes, ContextPackDelta.from_bytes])
@pytest.mark.parametrize("wire", [b"null", b"[]", b"{", b"\xff", b'{"schema":"x","schema":"y"}'])
def test_malformed_wire_is_a_protocol_error(decoder, wire: bytes) -> None:
    with pytest.raises(ContextPackError):
        decoder(wire)


def test_delta_supports_every_valid_prefix_and_rejects_false_target() -> None:
    base = ContextPack.build((b"head", b"body", b"tail"))
    target = ContextPack.build((b"head", b"body", b"changed"))
    for prefix in range(3):
        sent = ContextPackDelta(base.digest, target.digest, prefix, target.items[prefix:])
        received = ContextPackDelta.from_bytes(sent.to_bytes())
        assert apply_context_pack_delta(ContextPack.from_bytes(base.to_bytes()), received) == target
        assert sent.byte_count == len(sent.to_bytes())
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, target.digest, 3, ()))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, target.digest, 4, ()))


def test_digest_commits_to_block_boundaries_and_order() -> None:
    first = ContextPack.build((b"ab", b"c"))
    second = ContextPack.build((b"a", b"bc"))
    assert first.digest != second.digest
    assert first.digest != ContextPack.build(tuple(reversed(first.items))).digest
    assert ContextPack.build((b"",)).digest != ContextPack.build(()).digest


def test_decoder_accepts_a_valid_byte_count_at_a_decimal_boundary() -> None:
    pack = ContextPack.build((b"", b"", b"", b"a" * 625))
    assert pack.byte_count == 999
    payload = {**json.loads(pack.to_bytes()), "byte_count": 1000}
    wire = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert len(wire) == 1000
    decoded = ContextPack.from_bytes(wire)
    assert decoded.items == pack.items and decoded.digest == pack.digest
    assert decoded.byte_count == len(decoded.to_bytes())


def test_receiver_reconstructs_using_only_wire_bytes_in_a_fresh_process() -> None:
    base = ContextPack.from_content("海洋\n" * 100)
    target = ContextPack.from_content("海洋\n" * 100 + "new evidence")
    delta = ContextPackDelta.between(base, target)
    receiver = """
import json, sys
from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta, apply_context_pack_delta
base, delta = json.load(sys.stdin)
result = apply_context_pack_delta(ContextPack.from_bytes(base.encode()), ContextPackDelta.from_bytes(delta.encode()))
sys.stdout.buffer.write(result.to_bytes())
"""
    result = subprocess.run(
        [sys.executable, "-c", receiver],
        input=json.dumps([base.to_bytes().decode(), delta.to_bytes().decode()]).encode(),
        capture_output=True,
        check=True,
    )
    assert result.stdout == target.to_bytes()
