class TaskMemory:
    """Task-scoped memory namespace for reflections and failed attempts."""

    async def append_reflection(self, task_id: str, content: str) -> None:
        """Persist one reflection in the task-local memory namespace."""

        _ = (task_id, content)

    async def retrieve(self, task_id: str, query: str, limit: int = 5) -> list[str]:
        """Retrieve task-local memories relevant to the query."""

        _ = (task_id, query, limit)
        return []
