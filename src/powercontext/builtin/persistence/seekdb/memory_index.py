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

"""One native seekdb statement for ordered lexical and vector Memory candidates."""

from __future__ import annotations

from pyobvector import VECTOR
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection
from typing_extensions import override

from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.artifacts.memory.canonical import validate_embedding
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex, memory_channel_hits
from powercontext.builtin.persistence.oceanbase.memory_index import OceanBaseMemoryFTSIndex, OceanBaseMemoryVectorIndex
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql

_FTS_CANDIDATES_SQL = """
    SELECT m.memory_artifact_id, m.head_revision, m.entry_id, m.entry_version_id,
           v.text, MATCH(m.searchable_text) AGAINST(:fts_query) AS lexical_score
    FROM pc_memory_entry_heads AS m
    JOIN pc_memory_entry_versions AS v
      ON v.scope_id = m.scope_id
     AND v.memory_artifact_id = m.memory_artifact_id
     AND v.entry_version_id = m.entry_version_id
    WHERE m.scope_id = :scope_id
      AND m.memory_artifact_id IN :memory_artifact_ids
      AND MATCH(m.searchable_text) AGAINST(:fts_query)
      {tag_predicate}
    ORDER BY lexical_score DESC, m.memory_artifact_id, m.entry_id, m.entry_version_id
    LIMIT :candidate_limit
"""
_VECTOR_CANDIDATES_SQL = """
    SELECT m.memory_artifact_id, m.head_revision, m.entry_id, m.entry_version_id,
           v.text, l2_distance(m.embedding, :query_vector) AS distance
    FROM pc_memory_vector_entries AS m
    JOIN pc_memory_entry_versions AS v
      ON v.scope_id = m.scope_id
     AND v.memory_artifact_id = m.memory_artifact_id
     AND v.entry_version_id = m.entry_version_id
    WHERE m.scope_id = :scope_id
      AND m.memory_artifact_id IN :memory_artifact_ids
    ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE
    LIMIT :candidate_limit
"""
# A correlated tag predicate above ANN top-k can discard the only eligible row
# in seekdb 1.3. Resolve tag membership first, then use native vector lookups for
# eligible current entries and rank them inside the same statement.
_TAGGED_VECTOR_CANDIDATES_SQL = """
    SELECT m.memory_artifact_id, m.head_revision, m.entry_id, m.entry_version_id, v.text,
           (SELECT l2_distance(x.embedding, :query_vector)
            FROM pc_memory_vector_entries AS x
            WHERE x.scope_id = m.scope_id AND x.memory_artifact_id = m.memory_artifact_id
              AND x.entry_id = m.entry_id AND x.entry_version_id = m.entry_version_id
            ORDER BY l2_distance(x.embedding, :query_vector) APPROXIMATE LIMIT 1) AS distance
    FROM pc_memory_entry_heads AS m
    JOIN pc_memory_entry_versions AS v
      ON v.scope_id = m.scope_id
     AND v.memory_artifact_id = m.memory_artifact_id
     AND v.entry_version_id = m.entry_version_id
    WHERE m.scope_id = :scope_id
      AND m.memory_artifact_id IN :memory_artifact_ids
      {tag_predicate}
    ORDER BY distance, m.memory_artifact_id, m.entry_id, m.entry_version_id
    LIMIT :candidate_limit
"""
_HYBRID_SQL = """
SELECT 'fts' AS channel, f.memory_artifact_id, f.head_revision,
       f.entry_id, f.entry_version_id, f.text, NULL AS distance, f.lexical_score
FROM ({fts}) AS f
UNION ALL
SELECT 'vector' AS channel, nearest.memory_artifact_id, nearest.head_revision,
       nearest.entry_id, nearest.entry_version_id, nearest.text, nearest.distance, NULL AS lexical_score
FROM ({vector}) AS nearest
ORDER BY channel, lexical_score DESC, distance, memory_artifact_id, entry_id, entry_version_id
"""


class SeekDBMemoryHybridIndex(CompositeMemoryIndex):
    """Keep existing projections and single channels, unifying only hybrid retrieval."""

    def __init__(self, profile: EmbeddingProfile) -> None:
        self._vector = OceanBaseMemoryVectorIndex(profile)
        super().__init__(OceanBaseMemoryFTSIndex(), self._vector)
        self._hybrid = text(
            _HYBRID_SQL.format(
                fts=_FTS_CANDIDATES_SQL.format(tag_predicate=""),
                vector=_VECTOR_CANDIDATES_SQL,
            )
        ).bindparams(
            bindparam("memory_artifact_ids", expanding=True),
            bindparam("query_vector", type_=VECTOR(profile.dimension)),
        )
        self._tagged_hybrid = text(
            _HYBRID_SQL.format(
                fts=_FTS_CANDIDATES_SQL.format(tag_predicate=memory_tag_sql("m")),
                vector=_TAGGED_VECTOR_CANDIDATES_SQL.format(tag_predicate=memory_tag_sql("m")),
            )
        ).bindparams(
            bindparam("memory_artifact_ids", expanding=True),
            bindparam("query_vector", type_=VECTOR(profile.dimension)),
            bindparam("tag_hashes", expanding=True),
            bindparam("tag_keys", expanding=True),
        )

    @override
    async def search(
        self, connection: AsyncConnection, scope_id: str, request: MemorySearchRequest, /
    ) -> MemorySearchChannels:
        if request.mode != "hybrid":
            return await super().search(connection, scope_id, request)
        profile = self._vector.profile
        if request.embedding_profile != profile or request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        if not await self._vector.vector_complete(connection, scope_id, request.memories, profile):
            raise CapabilityNotSupportedError("vector")
        rows = (
            (
                await connection.execute(
                    self._hybrid if request.tag_filter is None else self._tagged_hybrid,
                    {
                        "scope_id": scope_id,
                        "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
                        "fts_query": request.analyzed_query,
                        "query_vector": validate_embedding(request.query_vector, dimension=profile.dimension),
                        "candidate_limit": request.candidate_limit,
                        **memory_tag_parameters(request.tag_filter),
                    },
                )
            )
            .mappings()
            .all()
        )
        return MemorySearchChannels(
            fts=memory_channel_hits((row for row in rows if row["channel"] == "fts"), request.memories),
            vector=memory_channel_hits((row for row in rows if row["channel"] == "vector"), request.memories),
        )
