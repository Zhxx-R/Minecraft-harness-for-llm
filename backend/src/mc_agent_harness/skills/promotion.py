from __future__ import annotations

from mc_agent_harness.schemas.skill import SkillSpec
from mc_agent_harness.skills.library import SkillLibrary


class SkillPromotionService:
    """Coordinates skill candidate promotion through the database-backed library."""

    def __init__(self, library: SkillLibrary | None = None) -> None:
        self.library = library or SkillLibrary()

    async def promote(self, candidate: int | SkillSpec) -> SkillSpec:
        """Promote a persisted candidate into the reusable skill library."""

        return await self.library.promote(candidate)
