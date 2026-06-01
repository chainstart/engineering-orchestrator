"""Compatibility wrapper for :mod:`engineering_orchestrator.browser_e2e`."""

from engineering_orchestrator.browser_e2e import *  # noqa: F401,F403

if __name__ == "__main__":
    from engineering_orchestrator.browser_e2e import main

    raise SystemExit(main())

