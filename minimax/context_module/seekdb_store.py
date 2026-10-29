"""Embedded seekdb access for engineering chunks.

The connection setup is adapted from PowerContext's seekdb profile
(Apache-2.0): seekdb exposes a MySQL-compatible socket through pylibseekdb,
and the connection needs its own dialect plus an explicit autocommit reset.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from pyobvector import AsyncOceanBaseDialect
from sqlalchemy import text
from sqlalchemy.dialects import registry
from sqlalchemy.engine import URL
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from typing_extensions import override

_DIALECT_DRIVER = "mysql+aseekdb"
_DIALECT_REGISTRY_NAME = "mysql.aseekdb"

_DDL = """
CREATE TABLE IF NOT EXISTS engineering_chunks (
    chunk_id VARCHAR(255) PRIMARY KEY,
    source_type VARCHAR(16) NOT NULL,
    source_id VARCHAR(255) NOT NULL,
    path VARCHAR(512) NOT NULL,
    symbol VARCHAR(255) NULL,
    title VARCHAR(512) NULL,
    content LONGTEXT NOT NULL,
    metadata LONGTEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    FULLTEXT KEY ft_engineering_content (content)
)
"""

_INSERT = text(
    """
    INSERT INTO engineering_chunks (
        chunk_id, source_type, source_id, path, symbol, title, content, metadata, content_hash
    ) VALUES (
        :chunk_id, :source_type, :source_id, :path, :symbol, :title, :content, :metadata, :content_hash
    )
    """
)

_SEARCH = text(
    """
    SELECT chunk_id, source_type, source_id, path, symbol, title, content, metadata,
           MATCH(content) AGAINST(:query) AS score
    FROM engineering_chunks
    WHERE MATCH(content) AGAINST(:query)
    ORDER BY score DESC, chunk_id
    LIMIT :limit
    """
)


class AsyncSeekDBDialect(AsyncOceanBaseDialect):
    """OceanBase-compatible dialect with seekdb-safe connection shutdown."""

    supports_statement_cache = AsyncOceanBaseDialect.supports_statement_cache

    @override
    def do_close(self, dbapi_connection: DBAPIConnection) -> None:
        self.do_terminate(dbapi_connection)


def _load_binding() -> ModuleType:
    return import_module("pylibseekdb")


async def _open_instance(module: ModuleType, path: Path) -> Any:
    return await cast(Any, module).aopen(str(path))


def _create_engine(connection_options: Mapping[str, object]) -> AsyncEngine:
    options = dict(connection_options)
    username = str(options.pop("user", "root"))
    password = options.pop("password", None)
    host = str(options.pop("host", "localhost"))
    port = options.pop("port", None)
    options["init_command"] = "SET autocommit = 0"
    url = URL.create(
        _DIALECT_DRIVER,
        username=username,
        password=None if password is None else str(password),
        host=host,
        port=None if port is None else int(cast(int | str, port)),
        database="test",
        query={"charset": "utf8mb4"},
    )
    registry.register(_DIALECT_REGISTRY_NAME, __name__, "AsyncSeekDBDialect")
    return create_async_engine(url, connect_args=options, pool_pre_ping=True, hide_parameters=True)


class SeekDBStore:
    """One embedded seekdb instance holding engineering chunks."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    @asynccontextmanager
    async def open(cls, path: Path) -> AsyncIterator[SeekDBStore]:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        instance = await _open_instance(_load_binding(), path)
        engine = _create_engine(instance.connection_options())
        store = cls(engine)
        try:
            await store.initialize()
            yield store
        finally:
            close_task = asyncio.create_task(engine.dispose())
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                await close_task
                raise
            instance.close()

    async def initialize(self) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text(_DDL))

    async def replace_all(self, chunks: list[dict[str, Any]]) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("DELETE FROM engineering_chunks"))
            if chunks:
                await connection.execute(_INSERT, chunks)

    async def search(self, query: str, limit: int = 40) -> list[dict[str, Any]]:
        async with self._engine.connect() as connection:
            try:
                rows = (
                    await connection.execute(_SEARCH, {"query": query, "limit": limit})
                ).mappings().all()
            except Exception:
                rows = await self._search_like(connection, query, limit)
        return [dict(row) for row in rows]

    async def _search_like(self, connection: Any, query: str, limit: int) -> list[dict[str, Any]]:
        terms = [term for term in query.replace("/", " ").split() if term][:8]
        clause = " OR ".join("content LIKE :term%d" % index for index in range(len(terms)))
        if not clause:
            return []
        params: dict[str, Any] = {f"term{index}": f"%{term}%" for index, term in enumerate(terms)}
        params["limit"] = limit
        rows = (
            await connection.execute(
                text(
                    "SELECT chunk_id, source_type, source_id, path, symbol, title, content, metadata, "
                    f"0.0 AS score FROM engineering_chunks WHERE {clause} LIMIT :limit"
                ),
                params,
            )
        ).mappings().all()
        return [dict(row) for row in rows]

    async def stats(self) -> dict[str, int]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text("SELECT source_type, COUNT(*) AS count FROM engineering_chunks GROUP BY source_type")
                )
            ).mappings().all()
        return {str(row["source_type"]): int(row["count"]) for row in rows}
