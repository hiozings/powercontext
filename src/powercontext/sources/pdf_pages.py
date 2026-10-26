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

"""Complete physical PDF pages and offline retrieval of their exact Memory citations."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import unicodedata
import zlib
from collections import Counter
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field, field_validator

from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from pypdf import PageObject, PdfReader
    from pypdf.generic import DictionaryObject, StreamObject

    from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
    from powercontext.builtin.runtime.application import ScopedMemoryApplication


class PDFDocumentInput(BaseModel):
    """Original PDF bytes and a caller-owned document identifier."""

    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("PDF document_id must not be blank")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    """The complete extractable text on one physical page of one PDF revision."""

    document_id: str
    document_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    page_number: Annotated[int, Field(strict=True, ge=1)]
    text: str

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("PDF document_id must not be blank")  # noqa: TRY003
        return value


class PDFPageSource(Source):
    """Captured page text that remains readable after the PDF changes."""

    page: PDFPageInput


def _validate_stream_encoding(stream: StreamObject) -> None:
    from pypdf.generic import ArrayObject, EncodedStreamObject, NameObject

    filters = stream.get("/Filter", ArrayObject())
    if isinstance(filters, str):
        filters = ArrayObject([NameObject(filters)])
    parameters = stream.get("/DecodeParms")
    for index, filter_name in enumerate(filters):
        if filter_name not in {"/FlateDecode", "/Fl"}:
            continue
        # pypdf's Flate recovery can silently return partial or empty bytes even
        # in strict mode. Verify the encoded stream before trusting extracted text.
        prefix = EncodedStreamObject()
        prefix._data = stream._data
        prefix[NameObject("/Filter")] = ArrayObject(filters[:index])
        if isinstance(parameters, ArrayObject):
            prefix[NameObject("/DecodeParms")] = ArrayObject(parameters[:index])
        elif parameters is not None and index:
            prefix[NameObject("/DecodeParms")] = parameters
        zlib.decompress(prefix.get_data())


def _form_streams(owner: DictionaryObject) -> tuple[StreamObject, ...]:
    from pypdf.generic import DictionaryObject, StreamObject

    resources = owner["/Resources"].get_object() if "/Resources" in owner else DictionaryObject()
    if not isinstance(resources, DictionaryObject):
        raise ValueError("PDF resources must be a dictionary")  # noqa: TRY003, TRY004
    xobjects = resources["/XObject"].get_object() if "/XObject" in resources else DictionaryObject()
    if not isinstance(xobjects, DictionaryObject):
        raise ValueError("PDF XObject resources must be a dictionary")  # noqa: TRY003, TRY004
    forms = []
    for reference in xobjects.values():
        obj = reference.get_object()
        if not isinstance(obj, StreamObject):
            raise ValueError("PDF XObjects must be streams")  # noqa: TRY003, TRY004
        if obj.get("/Subtype") == "/Form":
            forms.append(obj)
    return tuple(forms)


def _validate_page_streams(page: PageObject, reader: PdfReader, seen_forms: set[int]) -> None:
    from pypdf.generic import ArrayObject, ContentStream, NullObject, StreamObject

    objects: list[DictionaryObject] = [page]
    while objects:
        current = objects.pop()
        contents = current.get("/Contents") if current is page else current
        if contents is not None:
            contents = contents.get_object()
            streams = contents if isinstance(contents, ArrayObject) else (contents,)
            for stream in streams:
                if stream is None:
                    continue
                stream = stream.get_object()
                if isinstance(stream, NullObject):
                    continue
                if not isinstance(stream, StreamObject):
                    raise ValueError("PDF page contents must be streams")  # noqa: TRY003, TRY004
                _validate_stream_encoding(stream)
            if not isinstance(contents, NullObject):
                # Array members can split operands; syntax belongs to their concatenation.
                _ = ContentStream(contents, reader).operations
        for form in _form_streams(current):
            if id(form) not in seen_forms:
                seen_forms.add(id(form))
                objects.append(form)


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page atomically, including pages without text.

    Requires the optional ``powercontext[pdf]`` dependencies. Files with an empty
    user password are readable; no password is requested or guessed.
    """

    from pypdf import PdfReader

    if not value.content.rstrip().endswith(b"%%EOF"):
        raise ValueError("PDF has no complete end-of-file marker")  # noqa: TRY003
    try:
        with io.BytesIO(value.content) as stream:
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("PDF requires a user password")  # noqa: TRY003, TRY301
            texts: list[str] = []
            seen_forms: set[int] = set()
            for page in reader.pages:
                _validate_page_streams(page, reader, seen_forms)
                texts.append(page.extract_text())
    except Exception as error:
        # pypdf documents that malformed objects can also raise builtin exceptions.
        # Normalize only the parser boundary; never return the pages read so far.
        raise ValueError("PDF could not be completely parsed") from error  # noqa: TRY003
    digest = hashlib.sha256(value.content).hexdigest()
    return tuple(
        PDFPageInput(document_id=value.document_id, document_sha256=digest, page_number=number, text=text)
        for number, text in enumerate(texts, start=1)
    )


class _PDFPageAdapter:
    name = "pdf-page"
    input_class = PDFPageInput
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = json.dumps(
            [value.document_id, value.document_sha256, value.page_number], ensure_ascii=False
        ).encode()
        return PDFPageSource(
            name="pdf:" + hashlib.sha256(identity).hexdigest(),
            materialization=SourceMaterialization.CAPTURED,
            page=value.model_copy(deep=True),
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return source.page.model_copy(deep=True)


class _PDFTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        page = source.page
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{page.document_id}\n{page.text}",
            metadata={
                "document_id": page.document_id,
                "document_sha256": page.document_sha256,
                "page_number": page.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    _PDFPageAdapter(),
    projections=(_PDFTextEvidenceProjection(),),
)


_STOP_WORDS = frozenset(
    """
    a an the and or for of to in on at by from with as is are was were be been being
    what which who how why when where do does did can could should would i we you it
    its our your this that these those please has have had any all than then if not
    using use used only within shown details based according give answer respond
    question assume assuming perspective analyst investment calculate calculated
    state explicitly outlined directly reported important materially primarily
    fiscal fy year years usd million millions billion billions s
""".split()  # noqa: SIM905
)

# General accounting vocabulary bridges abbreviations and financial-statement labels.
# These groups contain neither company names nor document/question identifiers.
_FINANCIAL_TERMS = (
    (
        "geography geographical geographic geographies region regions country countries international",
        "geographic segment international markets",
    ),
    ("legal lawsuit lawsuits litigation proceedings battles", "legal proceedings litigation contingencies"),
    ("ebitda", "operating income depreciation amortization"),
    ("capex", "capital expenditures property plant equipment"),
    ("income earnings profit pnl", "income earnings operations"),
    ("cash cashflow", "cash flows operating investing financing"),
    ("restructuring", "restructuring charges costs"),
    ("revenue revenues sales", "revenue sales net"),
    ("debt borrowing borrowings leverage", "debt borrowings obligations"),
    ("liquidity current ratio", "current assets liabilities"),
)


def _tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = re.sub(r"\bd\s*&\s*a\b", "depreciation amortization", normalized)
    normalized = re.sub(r"\bp\s*[&/]\s*l\b", "income statement", normalized)
    return re.findall(r"[a-z]+|\d+", normalized)


def _stem(term: str) -> str:
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 3 and term.endswith("s") and not term.endswith(("ss", "us")):
        return term[:-1]
    return term


def _search_queries(question: str) -> tuple[tuple[str, ...], dict[str, float]]:
    original = tuple(dict.fromkeys(term for term in _tokens(question) if len(term) > 1 and term not in _STOP_WORDS))
    if not original:
        return (), {}
    weights = {_stem(term): 0.15 if term.isdigit() else 1.0 for term in original}
    queries = [" ".join(original)]
    for triggers, expansion in _FINANCIAL_TERMS:
        if set(original).intersection(triggers.split()):
            queries.append(expansion)
            for term in expansion.split():
                weights.setdefault(_stem(term), 0.6)
    # Single-term channels recover candidates that fail the long-query coverage gate.
    queries.extend(sorted((term for term in original if not term.isdigit()), key=lambda term: (-len(term), term)))
    return tuple(dict.fromkeys(queries))[:16], weights


_STATEMENT_HEADINGS = (
    (frozenset({"income", "statement"}), frozenset({"operation", "statement"}), frozenset({"earning", "statement"})),
    (frozenset({"cash", "flow"}),),
    (frozenset({"balance", "sheet"}),),
)


def _rank_pages(hits: list[MemoryHit], weights: dict[str, float], question: str) -> list[MemoryHit]:
    if not hits:
        return []
    tokens = [[_stem(term) for term in _tokens(hit.text)] for hit in hits]
    frequencies = [Counter(terms) for terms in tokens]
    document_frequency = Counter(term for terms in frequencies for term in terms)
    average_length = sum(map(len, tokens)) / len(tokens) or 1
    question_terms = set(_tokens(question)) - _STOP_WORDS
    question_stems = {_stem(term) for term in question_terms}
    if "ebitda" in question_stems:
        question_stems.update({"income", "statement", "cash", "flow"})
    requested_headings = [
        variants for variants in _STATEMENT_HEADINGS if any(variant <= question_stems for variant in variants)
    ]
    scored: list[tuple[float, int, MemoryHit]] = []
    for index, (hit, counts, terms) in enumerate(zip(hits, frequencies, tokens, strict=True)):
        score = 0.0
        heading = set(terms[:80])
        for term, weight in weights.items():
            frequency = counts[term]
            if not frequency:
                continue
            inverse_frequency = math.log1p(
                (len(hits) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5)
            )
            denominator = frequency + 1.2 * (0.25 + 0.75 * len(terms) / average_length)
            score += weight * inverse_frequency * (frequency * 2.2 / denominator + (0.3 if term in heading else 0))
        # Honor explicitly requested statements even when a line item is absent there.
        for variants in requested_headings:
            if any(variant <= heading for variant in variants):
                score += 4 * sum(
                    math.log1p((len(hits) + 0.5) / (document_frequency[term] + 0.5))
                    for term in set.union(*(set(variant) for variant in variants)).intersection(heading)
                )
        title, separator, _body = hit.text.partition("\n")
        title_matches = question_terms.intersection(_tokens(title)) if separator else set()
        score *= 1 + sum(0.1 if term.isdigit() else 1.0 for term in title_matches)
        scored.append((score, index, hit))
    return [hit for score, _index, hit in sorted(scored, key=lambda item: (-item[0], item[1])) if score > 0]


async def search_pdf_pages(
    memory: ScopedMemoryApplication,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Search scoped Memory offline and return exact citations to distinct PDF pages.

    Retrieval uses at most 16 lexical queries, each returning at most 256 candidates.
    BM25 reranking is request-local; only persisted Memory hits can become citations.
    """

    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest

    if type(limit) is not int or limit < 0:
        raise ValueError("PDF search limit must be a non-negative integer")  # noqa: TRY003
    if not limit or not question.strip():
        return ()
    queries, weights = _search_queries(question)
    candidates: dict[tuple[str, str, int, str, str], MemoryHit] = {}
    for query in queries:
        result = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=min(256, max(80, limit * 8))))
        for hit in result.hits:
            key = (
                hit.memory_ref.family,
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            candidates.setdefault(key, hit)
    citations: list[MemoryCitation] = []
    seen_pages: set[str] = set()
    for hit in _rank_pages(list(candidates.values()), weights, question):
        citation = MemoryCitation(
            memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
        )
        entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
        page_refs = [source for source in entry.sources if source.source_type == "pdf-page"]
        if len(page_refs) != 1 or page_refs[0].source_id in seen_pages:
            continue
        seen_pages.add(page_refs[0].source_id)
        citations.append(citation)
        if len(citations) >= limit:
            break
    return tuple(citations)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "search_pdf_pages",
]
