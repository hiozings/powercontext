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

"""Durable per-Scope evidence availability with atomic native Memory transitions."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryService
from powercontext.builtin.persistence.artifacts import ArtifactRepository
from powercontext.builtin.persistence.codec import dump_model, load_model
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE
from powercontext.builtin.sources import validate_scope_id
from powercontext.sources import SourceMaterialization, SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

LifecycleReference = SourceRef | MemoryCitation | ArtifactRef


class SourceLifecycleError(ValueError):
    """A lifecycle registration, event, dependency, or use is invalid."""


def _nonblank(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceLifecycleError("lifecycle identifiers and reasons must be non-blank strings")  # noqa: TRY003
    return value


class SourceLifecycleEvent(BaseModel):
    """One strictly typed availability update for a registered Source version."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    node_id: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        return _nonblank(value)


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str
    kind: Literal["source", "memory", "topic"]
    reference: LifecycleReference
    revision: int | None = None
    parents: tuple[str, ...] = ()
    policy: Literal["all", "any"] = "all"
    available: bool = True
    last_event: SourceLifecycleEvent | None = None

    def same_binding(self, other: _Node) -> bool:
        return self.model_dump(exclude={"available", "last_event"}) == other.model_dump(
            exclude={"available", "last_event"}
        )


class SourceLifecycleStore:
    """Own the bounded scope graph and join native changes to its transaction."""

    def __init__(
        self,
        database: AsyncDatabase,
        sources: SourceRepository,
        artifacts: ArtifactRepository,
        memory_service: Callable[[str, AsyncConnection], MemoryService],
        memory_artifact_id: str,
    ) -> None:
        self.database = database
        self.sources = sources
        self.artifacts = artifacts
        self.memory_service = memory_service
        self.memory_artifact_id = memory_artifact_id

    async def load(self, connection: AsyncConnection, scope: str) -> dict[str, _Node]:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        payloads = await connection.scalars(select(table.c.payload).where(table.c.scope_id == scope))
        nodes = [load_model(_Node, payload, kind="source-lifecycle", name=scope) for payload in payloads]
        return {node.node_id: node for node in nodes}

    async def save(self, connection: AsyncConnection, scope: str, node: _Node, *, new: bool = False) -> None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        key = hashlib.sha256(node.node_id.encode()).hexdigest()
        values = {
            "kind": node.kind,
            "artifact_id": node.reference.artifact_id if isinstance(node.reference, ArtifactRef) else None,
            "available": node.available,
            "payload": dump_model(node, kind="source-lifecycle", name=node.node_id),
        }
        statement = (
            insert(table).values(scope_id=scope, node_key=key, **values)
            if new
            else update(table).where(table.c.scope_id == scope, table.c.node_key == key).values(**values)
        )
        await connection.execute(statement)

    async def validate_reference(self, connection: AsyncConnection, scope: str, node: _Node) -> None:
        ref = node.reference
        if node.kind == "source" and isinstance(ref, SourceRef):
            source = await self.sources.get(connection, scope, ref)
            if source.value.materialization != SourceMaterialization.CAPTURED:
                raise SourceLifecycleError("lifecycle requires a captured Source")  # noqa: TRY003
        elif node.kind == "memory" and isinstance(ref, MemoryCitation):
            if ref.memory_ref.artifact_id != self.memory_artifact_id:
                raise SourceLifecycleError("Memory citation is outside the scoped Memory")  # noqa: TRY003
            await self.memory_service(scope, connection).validate_citation(ref)
        elif node.kind == "topic" and isinstance(ref, ArtifactRef) and ref.family == "topic-memory":
            await self.artifacts.get(connection, scope, ref)
        else:
            raise SourceLifecycleError("lifecycle reference does not match the node family")  # noqa: TRY003

    async def sync_memory(self, connection: AsyncConnection, scope: str, nodes: tuple[_Node, ...]) -> None:
        memory_nodes = [node for node in nodes if node.kind == "memory"]
        if not memory_nodes:
            return
        service = self.memory_service(scope, connection)
        head = await service.head(self.memory_artifact_id)
        entries = {entry.entry_id: entry for entry in await service.entries(head)}
        for available in (False, True):
            selected = []
            for node in memory_nodes:
                if node.available != available:
                    continue
                ref = node.reference
                if not isinstance(ref, MemoryCitation):
                    raise SourceLifecycleError("invalid persisted Memory binding")  # noqa: TRY003
                entry = entries.get(ref.entry_id)
                if entry is None or entry.entry_version_id != ref.entry_version_id:
                    raise SourceLifecycleError("registered Memory body version was changed")  # noqa: TRY003
                selected.append(entry)
            if selected:
                action = service.reactivate if available else service.forget
                head = await action(head, entries=selected, reason="source lifecycle availability")


def _propagate(nodes: dict[str, _Node]) -> dict[str, _Node]:
    try:
        order = tuple(TopologicalSorter({key: node.parents for key, node in nodes.items()}).static_order())
    except CycleError as error:
        raise SourceLifecycleError("lifecycle dependencies must be acyclic") from error  # noqa: TRY003
    updated = dict(nodes)
    for key in order:
        node = updated[key]
        if node.kind == "source":
            continue
        support = [updated[parent].available for parent in node.parents]
        available = all(support) if node.policy == "all" else any(support)
        updated[key] = node.model_copy(update={"available": available})
    return updated


class ScopedSourceLifecycle:
    """Single-writer lifecycle operations for one scope."""

    def __init__(self, runtime: BuiltinRuntime, store: SourceLifecycleStore, scope_id: str) -> None:
        self._runtime = runtime
        self._store = store
        self.scope_id = validate_scope_id(scope_id)

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("Source lifecycle revision must be a positive integer")  # noqa: TRY003
        await self._register(_Node(node_id=_nonblank(node_id), kind="source", reference=source_ref, revision=revision))

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: tuple[str, ...],
        policy: str = "all",
    ) -> None:
        await self._register(self._derived(node_id, "memory", citation, parents, policy))

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        *,
        parents: tuple[str, ...],
        policy: str = "all",
    ) -> None:
        await self._register(self._derived(node_id, "topic", artifact_ref, parents, policy))

    @staticmethod
    def _derived(
        node_id: str,
        kind: Literal["memory", "topic"],
        reference: LifecycleReference,
        parents: tuple[str, ...],
        policy: str,
    ) -> _Node:
        _nonblank(node_id)
        if (
            not isinstance(parents, tuple)
            or not parents
            or any(not isinstance(parent, str) or not parent.strip() for parent in parents)
        ):
            raise SourceLifecycleError("derived nodes require non-empty parent identifiers")  # noqa: TRY003
        if len(set(parents)) != len(parents) or node_id in parents:
            raise SourceLifecycleError("duplicate or cyclic parent dependency")  # noqa: TRY003
        if policy not in ("all", "any"):
            raise SourceLifecycleError("dependency policy must be all or any")  # noqa: TRY003
        return _Node(
            node_id=node_id,
            kind=kind,
            reference=reference,
            parents=tuple(sorted(parents)),
            policy=cast(Literal["all", "any"], policy),
        )

    async def _register(self, node: _Node) -> None:
        async with (
            self._runtime._scoped_operation(self.scope_id),
            self._runtime._locked(self.scope_id),
            self._store.database.transaction() as connection,
        ):
            nodes = await self._store.load(connection, self.scope_id)
            existing = nodes.get(node.node_id)
            if existing is not None:
                if not existing.same_binding(node):
                    raise SourceLifecycleError("node identifier is already bound differently")  # noqa: TRY003
                return
            if any(parent not in nodes for parent in node.parents):
                raise SourceLifecycleError("parents must already be registered in this scope")  # noqa: TRY003
            try:
                await self._store.validate_reference(connection, self.scope_id, node)
            except (LookupError, ValueError) as error:
                raise SourceLifecycleError("registered reference must exist and be readable in this scope") from error  # noqa: TRY003
            nodes[node.node_id] = node
            node = _propagate(nodes)[node.node_id]
            await self._store.sync_memory(connection, self.scope_id, (node,))
            await self._store.save(connection, self.scope_id, node, new=True)

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        async with (
            self._runtime._scoped_operation(self.scope_id),
            self._runtime._locked(self.scope_id),
            self._store.database.transaction() as connection,
        ):
            nodes = await self._store.load(connection, self.scope_id)
            root = nodes.get(event.node_id)
            if root is None or root.kind != "source" or root.revision != event.revision:
                raise SourceLifecycleError("unknown Source or mismatched lifecycle revision")  # noqa: TRY003
            previous = root.last_event
            if previous is not None and event.sequence <= previous.sequence:
                if previous == event:
                    return sorted(key for key, node in nodes.items() if node.available)
                raise SourceLifecycleError("stale or conflicting lifecycle event sequence")  # noqa: TRY003
            updated = dict(nodes)
            updated[root.node_id] = root.model_copy(update={"available": event.available, "last_event": event})
            updated = _propagate(updated)
            changed = tuple(node for key, node in updated.items() if node != nodes[key])
            await self._store.sync_memory(connection, self.scope_id, changed)
            for node in changed:
                await self._store.save(connection, self.scope_id, node)
            return sorted(key for key, node in updated.items() if node.available)

    async def resolve(self, node_id: str, *, purpose: str) -> LifecycleReference:
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError("unknown lifecycle reference purpose")  # noqa: TRY003
        async with self._runtime._scoped_operation(self.scope_id), self._store.database.transaction() as connection:
            node = (await self._store.load(connection, self.scope_id)).get(node_id)
            if node is None or (purpose == "generation" and not node.available):
                raise SourceLifecycleError("node is unknown or unavailable for generation")  # noqa: TRY003
            return node.reference


class SourceLifecycleApplication:
    def __init__(self, runtime: BuiltinRuntime, store: SourceLifecycleStore | None) -> None:
        self._runtime = runtime
        self._store = store

    def for_scope(self, scope_id: str) -> ScopedSourceLifecycle:
        if self._store is None:
            raise SourceLifecycleError("source lifecycle storage is not configured")  # noqa: TRY003
        return ScopedSourceLifecycle(self._runtime, self._store, scope_id)


__all__ = ["SourceLifecycleError", "SourceLifecycleEvent"]
