from mc_agent_harness.schemas.action import HarnessAction


class LifecycleHooks:
    """Policy and audit interception points around model and tool calls."""

    async def before_action(self, action: HarnessAction) -> HarnessAction:
        """Apply pre-action policy checks before dispatching to the runtime."""

        return action

    async def after_action(self, action: HarnessAction, result: dict) -> None:
        """Record or enforce post-action policy after the runtime returns."""

        _ = (action, result)
