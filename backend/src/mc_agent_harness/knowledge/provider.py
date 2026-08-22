from typing import Protocol

from mc_agent_harness.knowledge.models import KnowledgeDocument, Recipe, ResolvedTerm


class KnowledgeProvider(Protocol):
    """Provides Minecraft domain knowledge to the harness context manager."""

    def resolve_terms(self, task_text: str) -> list[ResolvedTerm]:
        """Resolve Minecraft-specific words in task text to canonical terms."""

        ...

    def get_recipe(self, item_id: str) -> Recipe | None:
        """Return the item-processing recipe for a canonical output item ID if known."""

        ...

    def retrieve_docs(self, query: str, limit: int = 5) -> list[KnowledgeDocument]:
        """Retrieve local documentation snippets relevant to the query."""

        ...
