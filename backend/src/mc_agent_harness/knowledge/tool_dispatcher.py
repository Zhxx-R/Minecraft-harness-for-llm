from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mc_agent_harness.knowledge.models import KnowledgeDocument, Recipe, ResolvedTerm
from mc_agent_harness.knowledge.provider import KnowledgeProvider
from mc_agent_harness.knowledge.static_provider import StaticKnowledgeProvider
from mc_agent_harness.schemas.action import HarnessAction


KNOWLEDGE_ACTION_TYPES = frozenset({"resolve_terms", "get_recipe", "retrieve_docs"})


@dataclass(frozen=True, slots=True)
class KnowledgeToolPolicy:
    """Safety and budget policy for agent-selected knowledge retrieval."""

    allowed_scopes: tuple[str, ...] = ("local", "minecraft", "minecraft_wiki", "mineflayer", "wiki")
    max_query_chars: int = 240
    max_docs_limit: int = 5
    max_terms_limit: int = 12
    max_doc_chars: int = 700
    online_enabled: bool = False


class KnowledgeToolError(ValueError):
    """Raised when a knowledge tool call violates the harness contract."""


class KnowledgeToolDispatcher:
    """Dispatch read-only knowledge actions selected by the agent."""

    def __init__(
        self,
        provider: KnowledgeProvider | None = None,
        policy: KnowledgeToolPolicy | None = None,
    ) -> None:
        self.provider = provider or StaticKnowledgeProvider()
        self.policy = policy or KnowledgeToolPolicy()

    def knowledge_revision(self) -> str:
        """Return the provider revision used to isolate deterministic cache entries."""

        revision = getattr(self.provider, "revision", None)
        if callable(revision):
            try:
                value = revision()
            except Exception:  # noqa: BLE001 - retrieval can safely fall back to static knowledge.
                return "knowledge:unavailable"
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        elif revision is not None:
            normalized = str(revision).strip()
            if normalized:
                return normalized
        return "knowledge:static"

    async def dispatch(
        self,
        action: HarnessAction,
        *,
        knowledge_revision: str | None = None,
    ) -> dict[str, Any]:
        """Validate and execute one read-only knowledge tool action."""

        if action.type not in KNOWLEDGE_ACTION_TYPES:
            raise KnowledgeToolError(f"Unsupported knowledge tool action: {action.type}")
        if action.type == "resolve_terms":
            result = self._resolve_terms(action.args)
        elif action.type == "get_recipe":
            result = self._get_recipe(action.args)
        else:
            result = self._retrieve_docs(action.args)
        return {
            **result,
            "knowledge_revision": knowledge_revision or self.knowledge_revision(),
        }

    def _resolve_terms(self, args: dict[str, Any]) -> dict[str, Any]:
        """Resolve terms from model-provided text under a small input budget."""

        text = _bounded_text(args.get("text") or args.get("query"), self.policy.max_query_chars)
        limit = _bounded_int(args.get("limit"), default=self.policy.max_terms_limit, maximum=self.policy.max_terms_limit)
        if not text:
            return _error("resolve_terms", "missing_text", "resolve_terms requires args.text or args.query.")
        terms = self.provider.resolve_terms(text)[:limit]
        return {
            "ok": True,
            "action_type": "resolve_terms",
            "tool": "resolve_terms",
            "query": text,
            "terms": [_resolved_term_payload(term) for term in terms],
            "source_policy": _source_policy_payload(self.policy),
            "state_summary": f"resolve_terms returned {len(terms)} canonical term(s) for: {text}",
        }

    def _get_recipe(self, args: dict[str, Any]) -> dict[str, Any]:
        """Look up one item-processing recipe by canonical output item id."""

        item_id = _bounded_text(
            args.get("item") or args.get("item_id") or args.get("output"),
            self.policy.max_query_chars,
        )
        if not item_id:
            return _error("get_recipe", "missing_item", "get_recipe requires args.item, args.item_id, or args.output.")
        recipe = self.provider.get_recipe(item_id)
        return {
            "ok": recipe is not None,
            "action_type": "get_recipe",
            "tool": "get_recipe",
            "item": item_id,
            "recipe": _recipe_payload(recipe) if recipe is not None else None,
            "source_policy": _source_policy_payload(self.policy),
            "error_code": None if recipe is not None else "recipe_not_found",
            "message": None if recipe is not None else f"No local item-processing recipe found for {item_id}.",
            "state_summary": _recipe_summary(item_id, recipe),
        }

    def _retrieve_docs(self, args: dict[str, Any]) -> dict[str, Any]:
        """Retrieve local documentation snippets with scope and length controls."""

        query = _bounded_text(args.get("query") or args.get("text"), self.policy.max_query_chars)
        scope = _bounded_text(args.get("scope") or "local", 64)
        limit = _bounded_int(args.get("limit"), default=3, maximum=self.policy.max_docs_limit)
        if not query:
            return _error("retrieve_docs", "missing_query", "retrieve_docs requires args.query.")
        if scope not in self.policy.allowed_scopes:
            return _error(
                "retrieve_docs",
                "scope_not_allowed",
                f"Knowledge scope is not allowed: {scope}",
                extra={"scope": scope, "allowed_scopes": list(self.policy.allowed_scopes)},
            )
        docs = self.provider.retrieve_docs(query, limit=limit)
        return {
            "ok": True,
            "action_type": "retrieve_docs",
            "tool": "retrieve_docs",
            "query": query,
            "scope": scope,
            "docs": [_document_payload(document, self.policy.max_doc_chars) for document in docs],
            "source_policy": _source_policy_payload(self.policy),
            "state_summary": f"retrieve_docs returned {len(docs)} local document snippet(s) for: {query}",
        }


def _resolved_term_payload(term: ResolvedTerm) -> dict[str, Any]:
    """Convert a resolved term into an audited tool result payload."""

    return {
        "canonical_id": term.canonical_id,
        "kind": term.kind,
        "name": term.name,
        "matched_aliases": list(term.matched_aliases),
        "description": term.description,
        "tags": list(term.tags),
        "recipe": _recipe_payload(term.recipe) if term.recipe else None,
    }


def _recipe_payload(recipe: Recipe | None) -> dict[str, Any] | None:
    """Convert one recipe to a compact JSON payload."""

    if recipe is None:
        return None
    return {
        "output": recipe.output,
        "output_count": recipe.output_count,
        "station": recipe.station,
        "ingredients": [
            {"item_id": ingredient.item_id, "count": ingredient.count}
            for ingredient in recipe.ingredients
        ],
        "requires": list(recipe.requires),
        "description": recipe.description,
    }


def _document_payload(document: KnowledgeDocument, max_chars: int) -> dict[str, Any]:
    """Convert one document into a bounded, source-preserving payload."""

    content = document.content
    truncated = len(content) > max_chars
    return {
        "id": document.id,
        "title": document.title,
        "content": content[:max_chars],
        "truncated": truncated,
        "tags": list(document.tags),
    }


def _source_policy_payload(policy: KnowledgeToolPolicy) -> dict[str, Any]:
    """Expose retrieval safety settings for audit and model-facing feedback."""

    return {
        "mode": "offline_local",
        "online_enabled": policy.online_enabled,
        "allowed_scopes": list(policy.allowed_scopes),
        "max_query_chars": policy.max_query_chars,
        "max_docs_limit": policy.max_docs_limit,
        "max_doc_chars": policy.max_doc_chars,
    }


def _recipe_summary(item_id: str, recipe: Recipe | None) -> str:
    """Render a one-sentence recipe lookup result for the next ReAct observation."""

    if recipe is None:
        return f"get_recipe found no local item-processing recipe for {item_id}."
    ingredients = ", ".join(f"{ingredient.item_id}x{ingredient.count}" for ingredient in recipe.ingredients)
    return f"get_recipe({item_id}) returned {ingredients} -> {recipe.output}x{recipe.output_count} at {recipe.station}."


def _error(tool: str, error_code: str, message: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a structured knowledge-tool error without raising into the run loop."""

    return {
        "ok": False,
        "action_type": tool,
        "tool": tool,
        "error_code": error_code,
        "message": message,
        "recoverable": True,
        **(extra or {}),
    }


def _bounded_text(value: Any, max_chars: int) -> str:
    """Coerce model-provided text to a safe bounded string."""

    if value is None:
        return ""
    return str(value).strip()[:max_chars]


def _bounded_int(value: Any, *, default: int, maximum: int) -> int:
    """Coerce model-provided limits into a positive bounded integer."""

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))
