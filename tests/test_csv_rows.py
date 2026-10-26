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

"""CSV evidence preserves original records through the public Source API."""

import asyncio

import pytest

from powercontext.sources import SourceDefinitionRegistry, SourceMaterialization
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def test_csv_source_round_trip_preserves_unicode_column_order_and_whitespace() -> None:
    row = parse_csv_document(
        CSVDocumentInput(
            document_id="机场:表",
            primary_key=" id ",
            content='\ufeffnote, id ,empty,flag\r\n"中文\r\n第二行", 001 ,,false\r\n'.encode(),
        )
    )[0]
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(row))
    restored = CSVRowSource.model_validate_json(source.model_dump_json())
    assert restored.materialization == SourceMaterialization.CAPTURED
    assert restored.source_id == restored.name == "csv:%E6%9C%BA%E5%9C%BA%3A%E8%A1%A8:%20001%20"
    assert asyncio.run(registry.read(restored)) == row
    assert registry.project(restored, TEXT_EVIDENCE_PROJECTION_KEY) == {
        "source_type": "csv-row",
        "source_id": restored.source_id,
        "content": '{"note":"中文\\r\\n第二行"," id ":" 001 ","empty":"","flag":"false"}',
        "metadata": {
            "document_id": "机场:表",
            "primary_key": " id ",
            "primary_key_value": " 001 ",
            "columns": ["note", " id ", "empty", "flag"],
            "row": {"note": "中文\r\n第二行", " id ": " 001 ", "empty": "", "flag": "false"},
        },
    }


@pytest.mark.parametrize(
    "content",
    [
        b"\n",
        b'""\n',
        b'id\n1\n""\n',
        b"id\n1\n \t\n",
        b"id,name\n1,ok\n2,too,many\n",
        b'id,name\n1,ok\n2,"bad"tail\n',
        b'id,name\n1,ok\n2,"unterminated',
        b"id,name\n1,ok\n2,\xff\n",
    ],
)
def test_invalid_later_records_and_empty_headers_fail_atomically(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="table", primary_key="id", content=content))


@pytest.mark.parametrize(
    ("document_id", "primary_key"), [("", "id"), (" table", "id"), ("table", ""), ("table", " id ")]
)
def test_header_only_documents_still_validate_document_and_primary_key(document_id: str, primary_key: str) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id=document_id, primary_key=primary_key, content=b"id,name\n"))


def test_blank_physical_records_do_not_discard_empty_non_key_fields() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(document_id="table", primary_key="id", content=b'id,name\r\n\r\n1,""\r\n\r\n2,\r\n')
    )
    assert [row.values for row in rows] == [("1", ""), ("2", "")]


def test_identity_bounds_apply_after_percent_encoding() -> None:
    assert csv_row_source_id("d", "a" * 250) == "csv:d:" + "a" * 250
    with pytest.raises(ValueError):
        csv_row_source_id("d", "a" * 251)
    assert csv_row_source_id("d", "中" * 27 + "a" * 7) == "csv:d:" + "%E4%B8%AD" * 27 + "a" * 7
    with pytest.raises(ValueError):
        csv_row_source_id("d", "中" * 28)
    assert csv_row_source_id("a:b", "c") != csv_row_source_id("a", "b:c")
    assert csv_row_source_id("a%3Ab", "c") != csv_row_source_id("a:b", "c")


@pytest.mark.parametrize("primary_key_value", ["", " ", "\t\n"])
def test_blank_key_cannot_be_used_as_source_identity(primary_key_value: str) -> None:
    with pytest.raises(ValueError):
        csv_row_source_id("table", primary_key_value)


@pytest.mark.parametrize(
    ("columns", "values", "primary_key", "primary_key_value"),
    [
        (("id", "name"), ("1",), "id", "1"),
        (("id", "id"), ("1", "2"), "id", "1"),
        (("id", " "), ("1", "2"), "id", "1"),
        (("id",), ("1",), "missing", "1"),
        (("id",), ("1",), "id", "2"),
    ],
)
def test_direct_row_inputs_cannot_lose_fields_or_misidentify_the_key(
    columns: tuple[str, ...], values: tuple[str, ...], primary_key: str, primary_key_value: str
) -> None:
    with pytest.raises(ValueError):
        row = CSVRowInput(
            document_id="table",
            primary_key=primary_key,
            primary_key_value=primary_key_value,
            columns=columns,
            values=values,
        )
        asyncio.run(SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,)).resolve(row))
