# Preferences and Settings

`settings_api.routes` binds authenticated Settings operations to the runtime.
`settings_views.py` owns their wire models and `settings_models.py` owns their
native records. `general_preferences.py` owns validated composer defaults and
owner-bound instructions. `settings_runtime.py` retains the standalone
automation and skill activities/workflows.

`settings.py` at the runtime root remains process configuration, distinct from
user-facing Settings. `api.py` composes this package's router. Bundled skill
lookup still resolves the same installed resources and source-tree fallback.

No package entrypoint eagerly loads routes, models or Temporal declarations.
Workflow/activity names and payloads retain their existing identities. The
existing Settings, private-instruction, bundled-skill and Temporal capacity
tests exercise these interfaces.
