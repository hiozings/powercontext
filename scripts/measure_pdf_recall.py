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

"""Measure evidence-page Recall@20 for ``search_pdf_pages`` on local FinanceBench data.

Example::

    uv run python scripts/measure_pdf_recall.py \\
        --pdf-dir /data/financebench/pdfs \\
        --gold /data/financebench/financebench_open_source.jsonl \\
        --document PEPSICO_2022_10K --document AMD_2022_10K

The gold file is FinanceBench's public JSONL. Each document is parsed with the product
parser, persisted page by page into a temporary SQLite scope, and queried with the
original questions. The script reports per-document and overall evidence-page recall.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    SourceObservation,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


class PagePipeline:
    """Turn each captured page into one Memory entry, like the acceptance harness."""

    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        entries = []
        for source in request.sources:
            content = cast(dict[str, Any], cast(SourceObservation, source).projection(TEXT_EVIDENCE_PROJECTION_KEY))[
                "content"
            ]
            if content.strip():
                entries.append(MemoryEntryInput(kind="source-evidence", text=content, sources=(source,)))
        return tuple(entries)


def load_questions(gold_path: Path, documents: tuple[str, ...]) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in gold_path.read_text().splitlines() if line.strip()]
    selected = [record for record in records if record["doc_name"] in documents]
    if not selected:
        raise SystemExit(f"no questions for {documents} in {gold_path}")  # noqa: TRY003
    return selected


async def persist_pages(config: BuiltinConfig, contents: list[tuple[str, bytes]]) -> str:
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    async with open_builtin_runtime(config, candidate_pipeline=PagePipeline()) as runtime:
        scopes = runtime.scopes
        if scopes is None:
            raise RuntimeError("runtime does not expose scopes")  # noqa: TRY003
        scope = await scopes.create(ScopeDraft(title="Recall", summary="Recall", idempotency_key="recall"))
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        for document, raw in contents:
            pages = parse_pdf_document(PDFDocumentInput(document_id=document, content=raw))
            print(f"{document}: {len(pages)} pages", flush=True)
            for page in pages:
                source = await registry.resolve(page)
                await runtime.ingestion.submit(
                    SubmitSourceObservation(
                        scope_id=scope.scope_id,
                        observation=project_source_for_transport(registry, source),
                    )
                )
        memory = runtime.memory.for_scope(scope.scope_id)
        while (await memory.flush()).processed:
            pass
    return scope.scope_id


async def page_locations(config: BuiltinConfig, scope_id: str) -> dict[str, tuple[str, int]]:
    async with open_builtin_contexts(config) as contexts:
        journal = await (await contexts.get(scope_id)).sources.journal.entries()
    locations = {}
    for entry in journal:
        source = cast(SourceObservation, entry.source)
        metadata = cast(dict[str, Any], source.projection(TEXT_EVIDENCE_PROJECTION_KEY))["metadata"]
        locations[entry.source_ref.source_id] = (metadata["document_id"], metadata["page_number"])
    return locations


async def score_questions(
    config: BuiltinConfig,
    scope_id: str,
    locations: dict[str, tuple[str, int]],
    questions: list[dict[str, Any]],
    limit: int,
) -> dict[str, list[int]]:
    scores: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope_id)
        for record in questions:
            citations = await search_pdf_pages(memory, record["question"], limit=limit)
            found = set()
            for citation in citations:
                entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                for source in entry.sources:
                    if source.source_type == "pdf-page":
                        found.add(locations[source.source_id])
            gold = {(record["doc_name"], evidence["evidence_page_num"] + 1) for evidence in record["evidence"]}
            scores[record["doc_name"]][0] += len(gold & found)
            scores[record["doc_name"]][1] += len(gold)
            print(f"  {record['financebench_id']}: {len(gold & found)}/{len(gold)}", flush=True)
    return scores


async def measure(contents: list[tuple[str, bytes]], questions: list[dict[str, Any]], limit: int) -> None:
    with TemporaryDirectory() as directory:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{Path(directory) / 'pages.db'}"))
        scope_id = await persist_pages(config, contents)
        locations = await page_locations(config, scope_id)
        scores = await score_questions(config, scope_id, locations, questions, limit)
    hits = sum(score[0] for score in scores.values())
    total = sum(score[1] for score in scores.values())
    for document, (document_hits, document_total) in scores.items():
        print(f"{document}: {document_hits}/{document_total} = {document_hits / document_total:.3f}")
    print(f"Recall@{limit}: {hits}/{total} = {hits / total:.3f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, required=True, help="directory holding <DOCUMENT>.pdf files")
    parser.add_argument("--gold", type=Path, required=True, help="FinanceBench JSONL with evidence pages")
    parser.add_argument(
        "--document", action="append", dest="documents", required=True, help="document name, repeatable"
    )
    parser.add_argument("--limit", type=int, default=20, help="citation limit per question (default: 20)")
    arguments = parser.parse_args()
    contents = [(document, (arguments.pdf_dir / f"{document}.pdf").read_bytes()) for document in arguments.documents]
    questions = load_questions(arguments.gold, tuple(arguments.documents))
    asyncio.run(measure(contents, questions, arguments.limit))


if __name__ == "__main__":
    main()
