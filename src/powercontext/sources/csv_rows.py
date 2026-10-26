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

"""Capture complete CSV records as stable, independently readable evidence."""

from __future__ import annotations

import csv
import io
import json
from typing import Self
from urllib.parse import quote

from pydantic import BaseModel, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class CSVDocumentInput(BaseModel):
    """An offline UTF-8 CSV document and its exact primary-key column name."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """A complete row whose column order and field strings are preserved."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        csv_row_source_id(self.document_id, self.primary_key_value)
        key_index = _primary_key_index(self.columns, self.primary_key)
        if len(self.values) != len(self.columns):
            raise ValueError("CSV row width must match the header")  # noqa: TRY003
        if self.values[key_index] != self.primary_key_value:
            raise ValueError("CSV primary key value must match its row field")  # noqa: TRY003
        return self


class CSVRowSource(Source, CSVRowInput):
    """A captured record containing everything needed to read the original row."""

    source_id: str

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        identity = csv_row_source_id(self.document_id, self.primary_key_value)
        if self.source_id != identity or self.name != identity:
            raise ValueError("CSV Source identity must match its document and primary key")  # noqa: TRY003
        if self.materialization != SourceMaterialization.CAPTURED:
            raise ValueError("CSV row Sources must contain captured records")  # noqa: TRY003
        return self


def _validate_document_id(document_id: str) -> None:
    if not document_id or document_id != document_id.strip():
        raise ValueError("CSV document_id must be non-empty without surrounding whitespace")  # noqa: TRY003


def _primary_key_index(columns: tuple[str, ...], primary_key: str) -> int:
    if not columns or any(not column.strip() for column in columns):
        raise ValueError("CSV column names must not be empty or blank")  # noqa: TRY003
    if len(set(columns)) != len(columns):
        raise ValueError("CSV column names must be unique")  # noqa: TRY003
    if not primary_key or primary_key not in columns:
        raise ValueError("CSV primary_key must exactly match a column name")  # noqa: TRY003
    return columns.index(primary_key)


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build a bounded identity independent of row position and non-key fields."""

    _validate_document_id(document_id)
    if not primary_key_value.strip():
        raise ValueError("CSV primary key values must not be empty or blank")  # noqa: TRY003
    identity = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV Source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Validate a whole document before returning any rows; reject errors with ValueError."""

    _validate_document_id(value.document_id)
    # Preserve line endings inside quoted fields, while accepting an initial BOM.
    with io.StringIO(value.content.decode("utf-8-sig"), newline="") as stream:
        reader = csv.reader(stream, strict=True)
        try:
            columns = tuple(next(reader, ()))
            key_index = _primary_key_index(columns, value.primary_key)
            rows: list[CSVRowInput] = []
            seen_keys: set[str] = set()
            for values in reader:
                if not values:
                    continue
                if len(values) != len(columns):
                    raise ValueError(f"CSV row ending at line {reader.line_num} has the wrong width")  # noqa: TRY003
                key = values[key_index]
                if key in seen_keys:
                    raise ValueError(f"CSV primary key value is duplicated: {key!r}")  # noqa: TRY003
                rows.append(
                    CSVRowInput(
                        document_id=value.document_id,
                        primary_key=value.primary_key,
                        primary_key_value=key,
                        columns=columns,
                        values=tuple(values),
                    )
                )
                seen_keys.add(key)
        except csv.Error as error:
            raise ValueError(f"Invalid CSV at line {reader.line_num}: {error}") from error  # noqa: TRY003
    return tuple(rows)


class _CSVRowAdapter:
    input_class = CSVRowInput
    name = "csv-row"
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            **value.model_dump(),
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class _CSVRowTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type="csv-row",
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    _CSVRowAdapter(),
    projections=(_CSVRowTextEvidenceProjection(),),
)

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
