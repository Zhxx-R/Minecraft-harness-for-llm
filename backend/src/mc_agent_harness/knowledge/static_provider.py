from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mc_agent_harness.knowledge.models import (
    KnowledgeBundle,
    KnowledgeDocument,
    KnowledgeTerm,
    Recipe,
    RecipeIngredient,
    ResolvedTerm,
)


FAMILY_CANONICAL_ALIASES: dict[str, str] = {
    "log": "oak_log",
    "logs": "oak_log",
    "wood": "oak_log",
    "wooden_log": "oak_log",
    "plank": "oak_planks",
    "planks": "oak_planks",
    "wooden_plank": "oak_planks",
    "wooden_planks": "oak_planks",
    "wooden_button": "oak_button",
    "wooden button": "oak_button",
    "wood_button": "oak_button",
    "button": "oak_button",
    "wool": "white_wool",
    "fish": "cod",
    "raw_fish": "cod",
    "reeds": "sugar_cane",
    "web": "cobweb",
}


OBTAINING_DOCUMENTS: tuple[KnowledgeDocument, ...] = (
    KnowledgeDocument(
        id="obtaining-feather",
        title="Obtaining Feather",
        tags=("minecraft_wiki", "obtaining", "drop", "entity:chicken", "item:feather"),
        content=(
            "Feathers are normally obtained as drops from chickens and parrots. "
            "For MineDojo harvest tasks, a feather target usually implies finding a chicken, "
            "defeating it or otherwise causing drops, moving into pickup range, and verifying inventory."
        ),
    ),
    KnowledgeDocument(
        id="obtaining-porkchop-and-pig",
        title="Pig Locations and Drops",
        tags=("minecraft_wiki", "obtaining", "drop", "entity:pig", "item:porkchop"),
        content=(
            "Pigs are passive animals that commonly spawn on grass blocks in overworld biomes such as "
            "plains, forest, and similar grassy areas. Pig combat or porkchop harvest tasks require "
            "locating a reachable pig, approaching it, attacking if needed, then checking kill stats or drops."
        ),
    ),
    KnowledgeDocument(
        id="obtaining-wood-family",
        title="Wood Family Defaults",
        tags=("minecraft_wiki", "obtaining", "crafting", "item:oak_log", "item:oak_planks"),
        content=(
            "Generic MineDojo wood targets can map to oak materials by default in an overworld run: "
            "log means oak_log, planks means oak_planks, and wooden_button can be crafted as oak_button "
            "from one oak_planks. Other wood variants may also satisfy a human goal, but the harness uses "
            "oak variants as canonical executable defaults unless a task specifies a different variant."
        ),
    ),
    KnowledgeDocument(
        id="obtaining-basic-animal-drops",
        title="Basic Animal Drops",
        tags=("minecraft_wiki", "obtaining", "drop", "animals"),
        content=(
            "Common passive animal drops are source-dependent: chickens can drop feather and chicken, "
            "cows can drop leather and beef, sheep can drop wool and mutton, pigs can drop porkchop, "
            "and rabbits can drop rabbit items. If the target is an animal drop, first identify the source "
            "entity, then search suitable overworld terrain before combat or collection."
        ),
    ),
)


class StaticKnowledgeProvider:
    """File-backed Week 1 knowledge provider.

    This provider is deliberately deterministic and small. It gives the harness stable
    Minecraft-specific grounding even after SQL persistence is introduced.
    """

    def __init__(
        self,
        knowledge_path: Path | None = None,
        supplemental_paths: tuple[Path, ...] | None = None,
    ) -> None:
        self.knowledge_path = knowledge_path or default_knowledge_path()
        self.supplemental_paths = supplemental_paths if supplemental_paths is not None else default_supplemental_paths()
        self.bundle = merge_knowledge_bundles(
            (load_knowledge_bundle(self.knowledge_path),)
            + tuple(
                load_knowledge_bundle(path)
                for path in self.supplemental_paths
                if path.exists()
            )
            + (KnowledgeBundle(documents=OBTAINING_DOCUMENTS),)
        )
        self._recipes_by_output = {recipe.output: recipe for recipe in self.bundle.recipes}
        self._canonical_by_alias = _canonical_alias_index(self.bundle)
        self._canonical_by_alias.update(FAMILY_CANONICAL_ALIASES)
        self._terms_by_canonical = {
            (term.kind, term.canonical_id): term for term in self.bundle.terms
        }

    def resolve_terms(self, task_text: str) -> list[ResolvedTerm]:
        """Resolve known Minecraft aliases in task text to canonical terms."""

        normalized_text = task_text.lower()
        resolved: list[ResolvedTerm] = []

        for term in self.bundle.terms:
            matched_aliases = tuple(
                alias
                for alias in term.aliases
                if _contains_phrase(normalized_text, alias.lower())
            )
            if not matched_aliases:
                continue

            resolved.append(
                ResolvedTerm(
                    canonical_id=term.canonical_id,
                    kind=term.kind,
                    name=term.name,
                    matched_aliases=matched_aliases,
                    description=term.description,
                    tags=term.tags,
                    recipe=self.get_recipe(term.canonical_id),
                )
            )

        resolved_by_id = {term.canonical_id for term in resolved}
        for alias, canonical_id in FAMILY_CANONICAL_ALIASES.items():
            if canonical_id in resolved_by_id:
                continue
            if not _contains_phrase(normalized_text, alias.lower()):
                continue
            term = self._term_for_canonical(canonical_id)
            if term is None:
                continue
            resolved.append(
                ResolvedTerm(
                    canonical_id=term.canonical_id,
                    kind=term.kind,
                    name=term.name,
                    matched_aliases=(alias,),
                    description=term.description,
                    tags=(*term.tags, "family_alias"),
                    recipe=self.get_recipe(term.canonical_id),
                )
            )
            resolved_by_id.add(canonical_id)

        return sorted(resolved, key=lambda term: term.canonical_id)

    def get_recipe(self, item_id: str) -> Recipe | None:
        """Return the recipe for one canonical item ID if available."""

        normalized = _normalize_item_id(item_id)
        canonical = self._canonical_by_alias.get(normalized, normalized)
        return self._recipes_by_output.get(canonical)

    def retrieve_docs(self, query: str, limit: int = 5) -> list[KnowledgeDocument]:
        """Retrieve local documents using deterministic lexical overlap."""

        query_terms = _tokenize(query)
        if not query_terms:
            return []

        scored: list[tuple[int, KnowledgeDocument]] = []
        for document in self.bundle.documents:
            document_terms = _tokenize(" ".join([document.title, document.content, *document.tags]))
            score = len(query_terms & document_terms)
            score += _exact_alias_bonus(query, document)
            if score > 0:
                scored.append((score, document))

        if scored:
            scored.sort(key=lambda item: (-item[0], item[1].id))
            return [document for _, document in scored[:limit]]

        vector_scored = [
            (_cosine_token_score(query_terms, _tokenize(" ".join([document.title, document.content, *document.tags]))), document)
            for document in self.bundle.documents
        ]
        vector_scored = [(score, document) for score, document in vector_scored if score > 0]
        vector_scored.sort(key=lambda item: (-item[0], item[1].id))
        return [document for _, document in vector_scored[:limit]]

    def _term_for_canonical(self, canonical_id: str) -> KnowledgeTerm | None:
        """Return the preferred term entry for one canonical id."""

        return (
            self._terms_by_canonical.get(("item", canonical_id))
            or self._terms_by_canonical.get(("block", canonical_id))
            or self._terms_by_canonical.get(("entity", canonical_id))
        )


def default_knowledge_path() -> Path:
    """Return the repository-local deterministic knowledge file path."""

    return Path(__file__).resolve().parents[4] / "knowledge" / "raw" / "minimal_minecraft_knowledge.json"


def default_supplemental_paths() -> tuple[Path, ...]:
    """Return generated local knowledge supplements when present."""

    root = Path(__file__).resolve().parents[4]
    return (root / "knowledge" / "processed" / "minecraft_1_20_1_knowledge.json",)


def load_knowledge_bundle(path: Path) -> KnowledgeBundle:
    """Load terms, recipes, and local documents from a JSON knowledge file."""

    payload = json.loads(path.read_text(encoding="utf-8"))

    terms = tuple(_parse_term(item) for item in payload.get("terms", []))
    recipes = tuple(_parse_recipe(item) for item in payload.get("recipes", []))
    documents = tuple(_parse_document(item) for item in payload.get("documents", []))
    return KnowledgeBundle(terms=terms, recipes=recipes, documents=documents)


def merge_knowledge_bundles(bundles: tuple[KnowledgeBundle, ...]) -> KnowledgeBundle:
    """Merge base and generated knowledge while letting base entries override generated ones."""

    terms_by_key: dict[tuple[str, str], KnowledgeTerm] = {}
    recipes_by_output: dict[str, Recipe] = {}
    documents_by_id: dict[str, KnowledgeDocument] = {}
    for bundle in reversed(bundles):
        for term in bundle.terms:
            terms_by_key[(term.kind, term.canonical_id)] = term
        for recipe in bundle.recipes:
            recipes_by_output[recipe.output] = recipe
        for document in bundle.documents:
            documents_by_id[document.id] = document
    return KnowledgeBundle(
        terms=tuple(sorted(terms_by_key.values(), key=lambda term: (term.kind, term.canonical_id))),
        recipes=tuple(sorted(recipes_by_output.values(), key=lambda recipe: recipe.output)),
        documents=tuple(sorted(documents_by_id.values(), key=lambda document: document.id)),
    )


def _parse_term(payload: dict[str, Any]) -> KnowledgeTerm:
    """Convert a JSON term object into a typed knowledge term."""

    return KnowledgeTerm(
        canonical_id=payload["canonical_id"],
        kind=payload["kind"],
        name=payload["name"],
        aliases=tuple(payload.get("aliases", [])),
        description=payload.get("description", ""),
        tags=tuple(payload.get("tags", [])),
    )


def _parse_recipe(payload: dict[str, Any]) -> Recipe:
    """Convert a JSON recipe object into a typed recipe."""

    return Recipe(
        output=payload["output"],
        output_count=int(payload.get("output_count", 1)),
        station=payload.get("station", "inventory"),
        ingredients=tuple(
            RecipeIngredient(item_id=item["item_id"], count=int(item["count"]))
            for item in payload.get("ingredients", [])
        ),
        requires=tuple(payload.get("requires", [])),
        description=payload.get("description", ""),
    )


def _parse_document(payload: dict[str, Any]) -> KnowledgeDocument:
    """Convert a JSON document object into a retrievable local document."""

    return KnowledgeDocument(
        id=payload["id"],
        title=payload["title"],
        content=payload["content"],
        tags=tuple(payload.get("tags", [])),
    )


def _contains_phrase(text: str, phrase: str) -> bool:
    """Check phrase presence using simple token boundaries."""

    escaped = re.escape(phrase)
    return re.search(rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])", text) is not None


def _tokenize(text: str) -> set[str]:
    """Tokenize local knowledge text for deterministic lexical retrieval."""

    return set(re.findall(r"[a-z0-9_]+", text.lower()))


def _exact_alias_bonus(query: str, document: KnowledgeDocument) -> int:
    """Boost documents whose title or tags exactly mention a resolved family alias."""

    normalized = _normalize_item_id(query)
    canonical = FAMILY_CANONICAL_ALIASES.get(normalized, normalized)
    haystack = " ".join([document.title, *document.tags]).lower()
    if canonical and canonical in haystack.replace(":", "_"):
        return 4
    return 0


def _cosine_token_score(query_terms: set[str], document_terms: set[str]) -> float:
    """Return a tiny local vector-style cosine score over binary token features."""

    if not query_terms or not document_terms:
        return 0.0
    overlap = len(query_terms & document_terms)
    if overlap == 0:
        return 0.0
    return overlap / ((len(query_terms) * len(document_terms)) ** 0.5)


def _canonical_alias_index(bundle: KnowledgeBundle) -> dict[str, str]:
    """Build a lookup from aliases and names to canonical ids for get_recipe."""

    index: dict[str, str] = {}
    for term in bundle.terms:
        candidates = {term.canonical_id, term.name, *term.aliases}
        for candidate in candidates:
            normalized = _normalize_item_id(candidate)
            if normalized:
                index[normalized] = term.canonical_id
    return index


def _normalize_item_id(value: str) -> str:
    """Normalize namespaced ids and natural-language item labels."""

    return value.lower().removeprefix("minecraft:").strip().replace(" ", "_")
