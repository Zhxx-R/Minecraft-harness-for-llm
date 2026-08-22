from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from mc_agent_harness.db.models import KnowledgeChunkRecord
from mc_agent_harness.db.session import SessionFactory
from mc_agent_harness.knowledge.models import KnowledgeDocument
from mc_agent_harness.knowledge.static_provider import StaticKnowledgeProvider


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    """Serializable knowledge chunk stored for retrieval and audit."""

    id: str
    source: str
    title: str
    content: str
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None


class DatabaseKnowledgeStore:
    """SQL store for deterministic knowledge chunks before vector retrieval is introduced."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    def upsert_chunks(self, chunks: list[KnowledgeChunk]) -> int:
        """Insert or update knowledge chunks and return the number processed."""

        with self.session_factory() as session:
            for chunk in chunks:
                record = session.get(KnowledgeChunkRecord, chunk.id)
                if record is None:
                    record = KnowledgeChunkRecord(id=chunk.id)
                    session.add(record)
                record.source = chunk.source
                record.title = chunk.title
                record.content = chunk.content
                record.tags = list(chunk.tags)
                record.chunk_metadata = chunk.metadata or {}
            session.commit()
        return len(chunks)

    def upsert_static_provider(self, provider: StaticKnowledgeProvider) -> int:
        """Seed database chunks from the deterministic static knowledge provider."""

        chunks: list[KnowledgeChunk] = []
        for document in provider.bundle.documents:
            chunks.append(
                KnowledgeChunk(
                    id=document.id,
                    source="static_knowledge.documents",
                    title=document.title,
                    content=document.content,
                    tags=document.tags,
                    metadata={"kind": "document"},
                )
            )
        for term in provider.bundle.terms:
            chunks.append(
                KnowledgeChunk(
                    id=f"term:{term.kind}:{term.canonical_id}",
                    source="static_knowledge.terms",
                    title=term.name,
                    content=f"{term.description} aliases: {', '.join(term.aliases)}",
                    tags=term.tags,
                    metadata={"kind": term.kind, "canonical_id": term.canonical_id},
                )
            )
        for recipe in provider.bundle.recipes:
            ingredients = ", ".join(
                f"{ingredient.item_id} x{ingredient.count}" for ingredient in recipe.ingredients
            )
            chunks.append(
                KnowledgeChunk(
                    id=f"recipe:{recipe.output}",
                    source="static_knowledge.recipes",
                    title=f"Recipe for {recipe.output}",
                    content=(
                        f"{recipe.description} station: {recipe.station}; "
                        f"ingredients: {ingredients}; requires: {', '.join(recipe.requires)}"
                    ),
                    tags=("recipe", recipe.output),
                    metadata={"kind": "recipe", "output": recipe.output},
                )
            )
        return self.upsert_chunks(chunks)

    def retrieve_docs(self, query: str, limit: int = 5) -> list[KnowledgeDocument]:
        """Retrieve knowledge chunks using deterministic lexical overlap."""

        query_terms = _tokenize(query)
        if not query_terms:
            return []

        with self.session_factory() as session:
            chunks = list(
                session.scalars(
                    select(KnowledgeChunkRecord).where(
                        KnowledgeChunkRecord.enabled.is_(True)
                    )
                )
            )

        scored: list[tuple[int, KnowledgeChunkRecord]] = []
        for chunk in chunks:
            chunk_terms = _tokenize(" ".join([chunk.title, chunk.content, *chunk.tags]))
            score = len(query_terms & chunk_terms)
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda item: (-item[0], item[1].id))
        return [
            KnowledgeDocument(
                id=chunk.id,
                title=chunk.title,
                content=chunk.content,
                tags=tuple(chunk.tags),
            )
            for _, chunk in scored[:limit]
        ]

    def revision(self) -> str:
        """Return a cheap global revision for exact-query cache invalidation."""

        with self.session_factory() as session:
            count, version_sum, latest_update = session.execute(
                select(
                    func.count(KnowledgeChunkRecord.id),
                    func.coalesce(func.sum(KnowledgeChunkRecord.version), 0),
                    func.max(KnowledgeChunkRecord.updated_at),
                )
            ).one()
        updated = (
            latest_update.isoformat()
            if hasattr(latest_update, "isoformat")
            else str(latest_update or "none")
        )
        return f"knowledge:{int(count or 0)}:{int(version_sum or 0)}:{updated}"


def _tokenize(text: str) -> set[str]:
    """Tokenize local knowledge text for deterministic lexical retrieval."""

    return set(re.findall(r"[a-z0-9_]+", text.lower()))
