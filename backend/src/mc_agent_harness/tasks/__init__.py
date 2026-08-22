"""Task provider adapters, catalog readers, and task similarity planning."""

from mc_agent_harness.tasks.catalog import (
    MineDojoCatalogSource,
    MineDojoCatalogSummary,
    MineDojoProgrammaticCatalog,
    MineDojoProgrammaticTask,
)
from mc_agent_harness.tasks.minedojo_adapter import (
    MineDojoManifestAdaptation,
    MineDojoManifestBuildSummary,
    MineDojoSpecMatcher,
    adapt_programmatic_catalog,
    adapt_programmatic_task,
)
from mc_agent_harness.tasks.minedojo_creative_adapter import (
    MineDojoCreativeBuildSummary,
    adapt_creative_catalog,
    write_creative_manifest_jsonl,
)
from mc_agent_harness.tasks.similarity import (
    DiverseBatchPlanner,
    DiverseBatchSelection,
    TaskDescriptor,
    TaskSimilarityBreakdown,
    TaskSimilarityScorer,
)

__all__ = [
    "DiverseBatchPlanner",
    "DiverseBatchSelection",
    "MineDojoCatalogSource",
    "MineDojoCatalogSummary",
    "MineDojoCreativeBuildSummary",
    "MineDojoManifestAdaptation",
    "MineDojoManifestBuildSummary",
    "MineDojoProgrammaticCatalog",
    "MineDojoProgrammaticTask",
    "MineDojoSpecMatcher",
    "TaskDescriptor",
    "TaskSimilarityBreakdown",
    "TaskSimilarityScorer",
    "adapt_programmatic_catalog",
    "adapt_programmatic_task",
    "adapt_creative_catalog",
    "write_creative_manifest_jsonl",
]
