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

"""Full PDF pages and exact scoped Memory citations, using synthetic reports."""

import asyncio
import hashlib
import io
import zlib
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.protocols import MemoryCandidateRequest
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    SourceMaterialization,
    SourceObservation,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def pdf_bytes(texts: list[str], *, password: str | None = None, broken_last_page: bool = False) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
        })
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 20 750 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = stream
    if broken_last_page:
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf (unterminated")
        writer.pages[-1][NameObject("/Contents")] = stream
    if password is not None:
        writer.encrypt(password, owner_password="owner")  # noqa: S106
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_physical_pages_preserve_text_numbers_blank_pages_and_revision_identity() -> None:
    raw = pdf_bytes(["Revenue 2024 12,345.67", "", "Printed page 2: liabilities 950"])
    pages = parse_pdf_document(PDFDocumentInput(document_id="Report 2024", content=raw))
    assert [(page.page_number, page.text) for page in pages] == [
        (1, "Revenue 2024 12,345.67"),
        (2, ""),
        (3, "Printed page 2: liabilities 950"),
    ]
    assert {page.document_sha256 for page in pages} == {hashlib.sha256(raw).hexdigest()}
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(pages[2]))
    assert source.materialization == SourceMaterialization.CAPTURED
    restored = type(source).model_validate_json(source.model_dump_json())
    assert asyncio.run(registry.read(restored)) == pages[2]
    assert registry.project(restored, TEXT_EVIDENCE_PROJECTION_KEY) == {
        "source_type": "pdf-page",
        "source_id": source.name,
        "content": "Report 2024\nPrinted page 2: liabilities 950",
        "metadata": {
            "document_id": "Report 2024",
            "document_sha256": hashlib.sha256(raw).hexdigest(),
            "page_number": 3,
        },
    }
    revision = parse_pdf_document(PDFDocumentInput(document_id="Report 2024", content=pdf_bytes(["Changed content"])))[
        0
    ]
    assert asyncio.run(registry.resolve(revision)).name != asyncio.run(registry.resolve(pages[0])).name


@pytest.mark.parametrize("document_id", ["", " \t\n"])
def test_blank_document_ids_are_rejected(document_id: str) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id=document_id, content=pdf_bytes([""])))


def test_unreadable_later_page_does_not_return_partial_document() -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(
            PDFDocumentInput(document_id="report", content=pdf_bytes(["valid", "broken"], broken_last_page=True))
        )


@pytest.mark.parametrize("damage", ["number", "invalid-deflate", "truncated-deflate", "truncated-eof"])
def test_damaged_content_cannot_be_accepted_as_a_blank_page(damage: str) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    if damage == "number":
        page[NameObject("/Contents")] = NumberObject(123)
    elif damage in {"invalid-deflate", "truncated-deflate"}:
        stream = DecodedStreamObject()
        stream.set_data(
            b"not zlib content" if damage == "invalid-deflate" else zlib.compress(b"BT (lost text) Tj ET")[:-4]
        )
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
        page[NameObject("/Contents")] = stream
    buffer = io.BytesIO()
    writer.write(buffer)
    raw = buffer.getvalue()[:-4] if damage == "truncated-eof" else buffer.getvalue()
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="damaged", content=raw))


def test_password_permissions_and_protected_files_are_distinguished() -> None:
    pages = parse_pdf_document(PDFDocumentInput(document_id="report", content=pdf_bytes(["full text"], password="")))
    assert pages[0].text == "full text"
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="report", content=pdf_bytes(["secret"], password="required")))  # noqa: S106


def test_content_stream_array_is_parsed_as_one_logical_page() -> None:
    writer = PdfWriter(io.BytesIO(pdf_bytes(["original"])))
    first, second = DecodedStreamObject(), DecodedStreamObject()
    first.set_data(b"BT /F1 12 Tf 10 50 Td (hello")
    second.set_data(b" world) Tj ET")
    writer.pages[0][NameObject("/Contents")] = ArrayObject([writer._add_object(first), writer._add_object(second)])
    buffer = io.BytesIO()
    writer.write(buffer)
    page = parse_pdf_document(PDFDocumentInput(document_id="split", content=buffer.getvalue()))[0]
    assert page.text.replace("\n", "") == "hello world"


def test_identity_is_bounded_and_keeps_document_revision_and_physical_page_distinct() -> None:
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    identities = set()
    for document, digest, page in [
        ("文档" * 200, "a" * 64, 1),
        ("other", "a" * 64, 1),
        ("other", "b" * 64, 1),
        ("other", "b" * 64, 2),
    ]:
        value = PDFPageInput(document_id=document, document_sha256=digest, page_number=page, text="body")
        source = asyncio.run(registry.resolve(value))
        assert len(source.name) <= 256
        assert asyncio.run(registry.resolve(value)).name == source.name
        identities.add(source.name)
    assert len(identities) == 4


def test_search_returns_scoped_distinct_page_citations_after_restart(tmp_path: Path) -> None:
    class PagePipeline:
        async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
            entries = []
            for source in request.sources:
                assert isinstance(source, SourceObservation)
                evidence = TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
                # Multiple Memory entries for a page must still yield one page citation.
                entries.extend(
                    MemoryEntryInput(kind=kind, text=evidence.content, sources=(source,))
                    for kind in ("page", "duplicate")
                )
            return tuple(entries)

    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'reports.db'}"))
        texts = [
            "Consolidated statements of operations. Revenue 1000. Operating income 160. Net earnings 120.",
            "Consolidated statements of cash flows. Depreciation and amortization 40. Purchases of property plant and equipment 30.",
            "Legal proceedings. Pending litigation concerns a patent claim.",
            "Report on the audit. This annual report describes the company and its financial statements.",
        ]
        texts.extend(
            f"Restructuring note {number}. Restructuring costs and restructuring charges reduced operating income."
            for number in range(12)
        )
        pages = parse_pdf_document(PDFDocumentInput(document_id="ACME_2024_10K", content=pdf_bytes(texts)))
        other_pages = parse_pdf_document(PDFDocumentInput(document_id="OTHER_2024_10K", content=pdf_bytes(texts[:4])))
        async with open_builtin_runtime(config, candidate_pipeline=PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Reports", summary="Reports", idempotency_key="reports")
            )
            other = await runtime.scopes.create(ScopeDraft(title="Other", summary="Other", idempotency_key="other"))
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for page in (*pages, *other_pages):
                source = await registry.resolve(page)
                observation = project_source_for_transport(registry, source)
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            assert (await runtime.memory.for_scope(scope.scope_id).flush()).processed
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            citations = await search_pdf_pages(memory, "What is ACME's 2024 EBITDA less capex?", limit=3)
            entries = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry for citation in citations]
            names = [entry.sources[0].source_id for entry in entries]
            expected = {(await registry.resolve(page)).name for page in pages[:2]}
            assert expected.issubset(names)
            assert len(names) == len(set(names)) <= 3
            assert await search_pdf_pages(runtime.memory.for_scope(other.scope_id), "operating income", limit=3) == ()
            assert await search_pdf_pages(memory, "litigation", limit=0) == ()
            legal = await search_pdf_pages(memory, "What lawsuits are ongoing at ACME?", limit=1)
            assert len(legal) == 1
            entry = (await memory.get(GetMemoryEntryRequest(citation=legal[0]))).entry
            assert entry.sources[0].source_id == (await registry.resolve(pages[2])).name
            statements = await search_pdf_pages(
                memory, "What restructuring costs appear on ACME's income statement?", limit=2
            )
            statement_sources = [
                (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.sources[0].source_id
                for citation in statements
            ]
            assert (await registry.resolve(pages[0])).name in statement_sources

    asyncio.run(scenario())
