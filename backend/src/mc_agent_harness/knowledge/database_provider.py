from __future__ import annotations

from mc_agent_harness.knowledge.chunk_store import DatabaseKnowledgeStore
from mc_agent_harness.knowledge.models import KnowledgeDocument, Recipe, ResolvedTerm
from mc_agent_harness.knowledge.static_provider import StaticKnowledgeProvider


class DatabaseKnowledgeProvider:
    """Knowledge provider that retrieves document chunks from SQL storage."""

    def __init__(
        self,
        store: DatabaseKnowledgeStore,
        fallback: StaticKnowledgeProvider | None = None,
    ) -> None:
        self.store = store
        self.fallback = fallback or StaticKnowledgeProvider()

    def resolve_terms(self, task_text: str) -> list[ResolvedTerm]:
        """Resolve Minecraft terms through the deterministic fallback provider."""

        return self.fallback.resolve_terms(task_text)

    def get_recipe(self, item_id: str) -> Recipe | None:
        """Return a recipe from the deterministic fallback provider."""

        return self.fallback.get_recipe(item_id)

    def revision(self) -> str:
        """Expose the SQL corpus revision used by the run-scoped exact cache."""

        return self.store.revision()

    def retrieve_docs(self, query: str, limit: int = 5) -> list[KnowledgeDocument]:
        """Retrieve only managed SQL chunks so archive/edit controls stay authoritative."""

        return self.store.retrieve_docs(query, limit=limit)
