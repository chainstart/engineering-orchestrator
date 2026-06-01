"""Compatibility wrapper for :mod:`engineering_orchestrator.cli`."""

from engineering_orchestrator.cli import *  # noqa: F401,F403

if __name__ == "__main__":
    from engineering_orchestrator.cli import main

    raise SystemExit(main())

