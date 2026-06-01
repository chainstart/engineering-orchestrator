"""Engineering Orchestrator compatibility package for multi-repository workspaces."""

from .goal_intake import GoalIntakeValidationError, normalize_goal_intake, validate_goal_intake

__all__ = ["GoalIntakeValidationError", "__version__", "normalize_goal_intake", "validate_goal_intake"]

__version__ = "0.1.0"
