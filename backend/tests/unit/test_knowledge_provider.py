from mc_agent_harness.knowledge import StaticKnowledgeProvider


def test_resolve_core_minecraft_terms() -> None:
    provider = StaticKnowledgeProvider()

    terms = provider.resolve_terms("Craft a wooden pickaxe from logs, planks, and a crafting table.")
    by_id = {term.canonical_id: term for term in terms}

    assert "wooden_pickaxe" in by_id
    assert "oak_log" in by_id
    assert "oak_planks" in by_id
    assert "crafting_table" in by_id
    assert by_id["wooden_pickaxe"].recipe is not None
    assert by_id["wooden_pickaxe"].recipe.station == "crafting_table"
    assert by_id["wooden_pickaxe"].recipe.requires == ("crafting_table",)


def test_get_recipe_returns_ingredients_and_station() -> None:
    provider = StaticKnowledgeProvider()

    recipe = provider.get_recipe("wooden_pickaxe")

    assert recipe is not None
    assert recipe.output_count == 1
    assert recipe.station == "crafting_table"
    assert {item.item_id: item.count for item in recipe.ingredients} == {
        "oak_planks": 3,
        "stick": 2,
    }


def test_generated_minecraft_knowledge_provides_compass_recipe() -> None:
    provider = StaticKnowledgeProvider()

    recipe = provider.get_recipe("minecraft:compass")

    assert recipe is not None
    assert recipe.output == "compass"
    assert recipe.output_count == 1
    assert recipe.station == "crafting_table"
    assert recipe.requires == ("crafting_table",)
    assert {item.item_id: item.count for item in recipe.ingredients} == {
        "iron_ingot": 4,
        "redstone": 1,
    }


def test_generated_minecraft_knowledge_resolves_programmatic_task_terms() -> None:
    provider = StaticKnowledgeProvider()

    terms = provider.resolve_terms("Craft a compass from redstone and iron ingot.")
    by_id = {term.canonical_id: term for term in terms}

    assert {"compass", "redstone", "iron_ingot"}.issubset(by_id)
    assert by_id["compass"].recipe is not None
    assert by_id["compass"].recipe.station == "crafting_table"


def test_minedojo_family_aliases_resolve_to_executable_defaults() -> None:
    provider = StaticKnowledgeProvider()

    terms = provider.resolve_terms("Harvest wooden_button after collecting log and planks.")
    by_id = {term.canonical_id: term for term in terms}
    recipe = provider.get_recipe("wooden_button")

    assert {"oak_button", "oak_log", "oak_planks"}.issubset(by_id)
    assert recipe is not None
    assert recipe.output == "oak_button"


def test_obtaining_docs_cover_animal_drop_sources() -> None:
    provider = StaticKnowledgeProvider()

    docs = provider.retrieve_docs("how to obtain feather harvest task chicken", limit=3)

    assert docs
    assert docs[0].id == "obtaining-feather"
    assert "chicken" in docs[0].content.lower()


def test_retrieve_docs_returns_relevant_local_guides() -> None:
    provider = StaticKnowledgeProvider()

    docs = provider.retrieve_docs("how should the harness use mineflayer actions for wood")

    assert docs
    assert docs[0].id in {"mineflayer-operation-guide", "early-game-wood-chain"}


def test_retrieve_docs_returns_generated_recipe_index() -> None:
    provider = StaticKnowledgeProvider()

    docs = provider.retrieve_docs("compass recipe", limit=3)

    assert [document.id for document in docs] == ["minecraft-data-1.20.1-crafting-recipes"]
