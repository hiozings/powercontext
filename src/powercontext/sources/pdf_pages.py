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
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field, field_validator

from powercontext.artifacts import ArtifactRef
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from pypdf import PageObject, PdfReader
    from pypdf.generic import DictionaryObject, StreamObject

    from powercontext.builtin.artifacts.memory.models import MemoryCitation
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
    much many more most between during able about against also into over under
""".split()  # noqa: SIM905
)

# General accounting vocabulary bridges abbreviations and financial-statement labels.
# These groups contain neither company names nor document/question identifiers.
_FINANCIAL_TERMS = (
    (
        "geography geographical geographic geographies region regions country countries international",
        "geographic segment international markets north america latin europe africa middle east south asia pacific "
        "australia china",
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


def _key(term: str) -> str:
    """Collapse inflections so ``geographies``, ``geographic`` and ``geography`` meet."""

    if len(term) > 4 and term.endswith("ies"):
        term = term[:-3] + "y"
    elif len(term) > 3 and term.endswith("es") and not term.endswith(("ses", "xes", "zes", "ches", "shes")):
        term = term[:-2]
    elif len(term) > 3 and term.endswith("s") and not term.endswith(("ss", "us", "is")):
        term = term[:-1]
    # A five-character prefix keeps shared stems ("shared"/"share", "operations"/"operating")
    # together without the false merges of full stemming.
    return term[:5]


# Structural headings keep content words such as "state" that the question stop list drops.
_LINK_KEYS = frozenset(
    _key(word)
    for word in ["a", "an", "the", "and", "or", "for", "of", "to", "in", "on", "at", "by", "from", "with", "as"]
)

_STATEMENT_PATTERNS = (
    (
        "income",
        (
            ("conso", "state", "incom"),
            ("conso", "state", "opera"),
            ("state", "incom"),
            ("state", "opera"),
            ("state", "earni"),
            ("incom", "state"),
            ("opera", "state"),
            ("earni", "state"),
        ),
    ),
    ("cash", (("conso", "state", "cash", "flow"), ("state", "cash", "flow"), ("cash", "flow", "opera"))),
    ("balance", (("conso", "balan", "sheet"), ("balan", "sheet"), ("state", "finan", "posit"))),
    ("mdna", (("manag", "discu", "analy"), ("manag", "discu", "analysi"))),
    ("earnings-analysis", (("analy", "conso", "earni"), ("analy", "conso", "incom"))),
)

_INCOME_INTENT = frozenset(
    _key(word)
    for word in [
        "income",
        "earning",
        "revenue",
        "sales",
        "profit",
        "margin",
        "ebitda",
        "cost",
        "expense",
        "gross",
        "tax",
    ]
)
_CASH_INTENT = frozenset(
    _key(word)
    for word in [
        "cash",
        "flows",
        "capex",
        "capital",
        "expenditures",
        "depreciation",
        "amortization",
        "ebitda",
        "investing",
    ]
)
_BALANCE_INTENT = frozenset(
    _key(word)
    for word in [
        "balance",
        "sheet",
        "assets",
        "liabilities",
        "equity",
        "debt",
        "liquidity",
        "borrowings",
        "current",
        "goodwill",
        "inventory",
    ]
)


@dataclass(frozen=True)
class _QueryProfile:
    weights: dict[str, float]
    intents: frozenset[str]
    pairs: tuple[tuple[str, str], ...]
    names: frozenset[str]
    proper_names: frozenset[str]


def _heading_keys(text: str, content_keys: tuple[str, ...]) -> tuple[str, ...]:
    """Collect short title lines that carry the page's topic, plus its lead-in words."""

    keys = list(content_keys[:20])
    for line in text.splitlines()[1:40]:
        stripped = line.strip()
        if not stripped or stripped == "Table of Contents" or sum(character.isalpha() for character in stripped) < 3:
            continue
        if not stripped.endswith(".") and _HEADING_LINE.match(stripped):
            keys.extend(_key(token) for token in _tokens(stripped))
        if len(keys) >= 48:
            break
    return tuple(dict.fromkeys(keys))[:48]


@dataclass(frozen=True)
class _PageView:
    keys: tuple[str, ...]
    heading_keys: tuple[str, ...]
    document_tokens: frozenset[str]
    document_key: str
    statement: str | None
    is_toc: bool


_TOC_LEADER = re.compile(r"\.{4,}")
_HEADING_LINE = re.compile(r"^[A-Z0-9][^!?]{2,90}$")


@lru_cache(maxsize=8192)
def _page_view(text: str) -> _PageView:
    keys = tuple(_key(token) for token in _tokens(text))
    first_line, _separator, _rest = text.partition("\n")
    document_tokens = (
        frozenset(_tokens(first_line))
        if first_line and not any(character.isspace() for character in first_line)
        else frozenset()
    )
    content_keys = tuple(key for key in keys if key not in _LINK_KEYS)
    document_key = "".join(re.findall(r"[a-z0-9]+", _document_name(text).casefold()))
    heading_keys = _heading_keys(text, content_keys)
    return _PageView(
        keys=keys,
        heading_keys=heading_keys,
        document_tokens=document_tokens,
        document_key=document_key,
        statement=_statement_of(heading_keys),
        is_toc=len(_TOC_LEADER.findall(text)) >= 4,
    )


def _statement_of(keys: tuple[str, ...]) -> str | None:
    for statement, patterns in _STATEMENT_PATTERNS:
        for pattern in patterns:
            size = len(pattern)
            if any(keys[start : start + size] == pattern for start in range(len(keys) - size + 1)):
                return statement
    return None


_CAPITAL_KEYS = frozenset({_key("capital"), _key("capex"), _key("capitalize")})


def _statement_intents(keys: set[str]) -> frozenset[str]:
    intents: set[str] = set()
    if keys & _INCOME_INTENT:
        intents.add("income")
    if keys & _CASH_INTENT:
        intents.add("cash")
    if keys & _BALANCE_INTENT:
        intents.add("balance")
    if keys & _CAPITAL_KEYS:
        # Capital intensity is evidenced by the income statement, balance sheet and cash flow.
        intents.update({"income", "cash", "balance"})
    return frozenset(intents)


def _query_names(tokens: list[str]) -> frozenset[str]:
    names: set[str] = set()
    for index, token in enumerate(tokens):
        if token.isalpha():
            names.add(token)
        if index:
            names.add(tokens[index - 1] + token)
    return frozenset(names)


def _query_profile(question: str) -> _QueryProfile:
    tokens = [token for token in _tokens(question) if len(token) > 1 and token not in _STOP_WORDS]
    weights: dict[str, float] = {}
    for token in tokens:
        key = _key(token)
        weights[key] = max(weights.get(key, 0.0), 0.3 if token.isdigit() else 1.0)
    for triggers, expansion in _FINANCIAL_TERMS:
        if set(tokens).intersection(triggers.split()):
            for term in expansion.split():
                weights.setdefault(_key(term), 0.5)
    stemmed = [_key(token) for token in tokens]
    pairs = tuple((left, right) for left, right in pairwise(stemmed) if left != right)[:8]
    proper_names = {
        match.casefold()
        for match in re.findall(r"\b[A-Z][A-Za-z&.\-]{1,}|\b\d[A-Za-z]+\b", question)
        if match.casefold() not in _STOP_WORDS
    }
    return _QueryProfile(
        weights=weights,
        intents=_statement_intents(set(weights)),
        pairs=pairs,
        names=_query_names(tokens),
        proper_names=frozenset(proper_names),
    )


def _proximity_bonus(view: _PageView, pairs: tuple[tuple[str, str], ...], inverse_frequency: dict[str, float]) -> float:
    if not pairs:
        return 0.0
    positions: dict[str, list[int]] = {}
    for position, key in enumerate(view.keys):
        positions.setdefault(key, []).append(position)
    bonus = 0.0
    for left, right in pairs:
        left_idf = inverse_frequency.get(left)
        right_idf = inverse_frequency.get(right)
        if left_idf is None or right_idf is None:
            continue
        for start in positions.get(left, ())[:3]:
            if any(abs(end - start) <= 5 for end in positions.get(right, ())[:6]):
                bonus += 0.6 * (left_idf + right_idf)
                break
    return bonus


def _score_page(
    view: _PageView,
    rare: dict[str, float],
    inverse_frequency: dict[str, float],
    rare_total: float,
    average_length: float,
    profile: _QueryProfile,
) -> float:
    counts = Counter(view.keys)
    length = len(view.keys)
    score = 0.0
    matched = 0
    for key, weight in rare.items():
        frequency = counts[key]
        if not frequency:
            continue
        matched += 1
        denominator = frequency + 1.2 * (0.25 + 0.75 * length / average_length)
        score += weight * inverse_frequency[key] * (frequency * 2.2 / denominator)
    if matched:
        score *= 0.5 + 1.2 * matched / len(rare)
        heading = set(view.heading_keys)
        score += sum(inverse_frequency[key] for key in rare if key in heading)
        score += _proximity_bonus(view, profile.pairs, inverse_frequency)
    # A statement page often answers a margin, EBITDA or liquidity question without
    # repeating the question's words, so structure is a first-class signal.
    if view.statement in profile.intents:
        score += 1.6 * rare_total
    elif view.statement in {"mdna", "earnings-analysis"} and "income" in profile.intents:
        score += 0.9 * rare_total
    if profile.names and not view.document_tokens.isdisjoint(profile.names):
        score *= 2.2
    if view.is_toc:
        score *= 0.5
    return score


def _rank_pages(hits: list[_Candidate], profile: _QueryProfile) -> list[_Candidate]:
    if not hits or not profile.weights:
        return []
    views = [_page_view(hit.text) for hit in hits]
    total = len(views)
    document_frequency = Counter(key for view in views for key in set(view.keys))
    average_length = sum(len(view.keys) for view in views) / total or 1.0
    # Terms present on more than half of the scoped pages carry no page-selection signal:
    # document identifiers, fiscal years and running headers fall into this bucket.
    rare = {key: weight for key, weight in profile.weights.items() if 0 < document_frequency[key] / total <= 0.5} or {
        key: weight for key, weight in profile.weights.items() if document_frequency[key] < total
    }
    if not rare:
        return []
    inverse_frequency = {
        key: math.log1p((total - document_frequency[key] + 0.5) / (document_frequency[key] + 0.5)) for key in rare
    }
    rare_total = sum(inverse_frequency.values()) or 1.0
    scored = [
        (
            _score_page(view, rare, inverse_frequency, rare_total, average_length, profile),
            index,
            hit,
        )
        for index, (hit, view) in enumerate(zip(hits, views, strict=True))
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [hit for score, _index, hit in scored if score > 0]


def _document_name(text: str) -> str:
    """Return the projection's document identifier line, when present."""

    first_line, _separator, _rest = text.partition("\n")
    if (
        first_line
        and not any(character.isspace() for character in first_line)
        and any(character.isalpha() for character in first_line)
    ):
        return first_line
    return ""


@dataclass(frozen=True)
class _Candidate:
    """One active current-head entry and its single page Source, when it has exactly one."""

    text: str
    memory_ref: ArtifactRef
    entry_id: str
    entry_version_id: str
    source_id: str | None


async def _scoped_pages(memory: ScopedMemoryApplication) -> tuple[_Candidate, ...]:
    """Read the scope through the public Memory API so every stored page can be ranked."""

    page = await memory.list()
    candidates = []
    for record in page.entries:
        if record.state != "active":
            continue
        references = [source for source in record.entry.sources if source.source_type == "pdf-page"]
        candidates.append(
            _Candidate(
                text=record.entry.text,
                memory_ref=record.memory_ref,
                entry_id=record.entry.entry_id,
                entry_version_id=record.entry.entry_version_id,
                source_id=references[0].source_id if len(references) == 1 else None,
            )
        )
    return tuple(candidates)


def _is_document_alias(alias: str, token: str) -> bool:
    """Match a capitalized question name to a longer document token ("JnJ"/"johnsonjohnson")."""

    if alias == token:
        return True
    if len(alias) < 2 or len(token) < len(alias) * 2 or alias[0] != token[0]:
        return False
    remaining = iter(token)
    return all(character in remaining for character in alias)


def _anchor_pages(pages: tuple[_Candidate, ...], profile: _QueryProfile) -> tuple[_Candidate, ...] | None:
    """Prefer pages of the document the question names, when that document is identifiable."""

    if not profile.names:
        return None
    anchored: list[_Candidate] = []
    for candidate in pages:
        view = _page_view(candidate.text)
        if any(
            token in profile.names
            or any(_is_document_alias(alias, view.document_key) for alias in profile.proper_names)
            for token in view.document_tokens
            if not token.isdigit()
        ):
            anchored.append(candidate)
    return tuple(anchored) if len(anchored) >= 5 else None


async def search_pdf_pages(
    memory: ScopedMemoryApplication,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Search scoped Memory offline and return exact citations to distinct PDF pages.

    Retrieval reads the scope's active entries through the public Memory API, then ranks
    them locally by inflected term evidence, statement structure, heading and proximity.
    Reading every stored page is deliberate: an evidence page may share no literal term
    with the question, for example a bare income statement. Only stored entries can
    become citations, and ties keep the stored order.
    """

    from powercontext.builtin.artifacts.memory.models import MemoryCitation

    if type(limit) is not int or limit < 0:
        raise ValueError("PDF search limit must be a non-negative integer")  # noqa: TRY003
    if not limit or not question.strip():
        return ()
    pages = await _scoped_pages(memory)
    if not pages:
        return ()
    profile = _query_profile(question)
    ranked = _rank_pages(list(_anchor_pages(pages, profile) or pages), profile)
    citations: list[MemoryCitation] = []
    seen_pages: set[str] = set()
    for candidate in ranked:
        if candidate.source_id is None or candidate.source_id in seen_pages:
            continue
        seen_pages.add(candidate.source_id)
        citations.append(
            MemoryCitation(
                memory_ref=candidate.memory_ref,
                entry_id=candidate.entry_id,
                entry_version_id=candidate.entry_version_id,
            )
        )
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
