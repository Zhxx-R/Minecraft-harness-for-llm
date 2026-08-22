from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class KnowledgeTerm:
    """Canonical Minecraft term entry with aliases used for task grounding."""

    canonical_id: str
    kind: str
    name: str
    aliases: tuple[str, ...]
    description: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecipeIngredient:
    """One item-count pair required by an item-processing recipe."""

    item_id: str
    count: int


@dataclass(frozen=True, slots=True)
class Recipe:
    """Item-processing recipe with station, ingredients, and optional prerequisites."""

    output: str
    output_count: int
    station: str
    ingredients: tuple[RecipeIngredient, ...]
    description: str
    requires: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """Local documentation snippet retrievable by the context manager."""

    id: str
    title: str
    content: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedTerm:
    """Term resolution result enriched with matched aliases and recipe hints."""

    canonical_id: str
    kind: str
    name: str
    matched_aliases: tuple[str, ...]
    description: str
    tags: tuple[str, ...] = ()
    recipe: Recipe | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeBundle:
    """In-memory bundle loaded from the deterministic Week 1 knowledge file."""

    terms: tuple[KnowledgeTerm, ...] = field(default_factory=tuple)
    recipes: tuple[Recipe, ...] = field(default_factory=tuple)
    documents: tuple[KnowledgeDocument, ...] = field(default_factory=tuple)
