"""Legacy compatibility package for Engineering Orchestrator.

New code should import :mod:`engineering_orchestrator`. This package remains as
the staged migration bridge for existing users.
"""

from engineering_orchestrator import *  # noqa: F401,F403

