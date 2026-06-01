# Browser User Experience E2E Gates

Generated browser-facing roadmap tasks can use:

```bash
python3 -m engineering_orchestrator.browser_e2e --project-root . --journey-id JOURNEY_ID
```

The runner stays local-only. If a local Playwright executable and a matching spec are present, the
gate can run that spec. Otherwise it falls back to static HTML smoke coverage driven by a journey
declaration such as `tests/e2e/JOURNEY_ID.journey.json`.

Static declarations describe:

- `routes`: local HTML routes such as `/` or `dashboard/index.html`;
- `expect_text`: visible text that must appear;
- `expect_roles`: accessibility roles such as `main`, `form`, `button`, or `textbox`;
- `expect_forms`: form selectors plus expected fields and submit text.

The fallback writes DOM evidence under `artifacts/browser-e2e/<journey>/dom-evidence.json` and a
small DOM snapshot text file when configured. `engo status --json` exposes the machine-readable
summary at `browser_user_experience` and
`runtime_dashboard.browser_user_experience`.
