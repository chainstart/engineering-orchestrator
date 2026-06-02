"""Engineering Orchestrator package for multi-repository workspaces."""

from .goal_intake import GoalIntakeValidationError, normalize_goal_intake, validate_goal_intake
from .supervisor_decision import (
    SUPERVISOR_DECISION_KIND,
    classify_supervisor_decision,
    validate_supervisor_decision,
)

__all__ = [
    "GoalIntakeValidationError",
    "SUPERVISOR_DECISION_KIND",
    "__version__",
    "classify_supervisor_decision",
    "normalize_goal_intake",
    "validate_goal_intake",
    "validate_supervisor_decision",
]

__version__ = "0.1.0"
