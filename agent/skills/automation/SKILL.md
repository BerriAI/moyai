---
name: automation
description: Create, inspect, revise and verify recurring or future work within the owner's authorized scheduling scope.
---

Use `automation_list` before creating or editing a schedule to find an existing definition and the current control context. Prefer updating the intended definition over creating duplicates. Keep task instructions in the prompt, reusable installation steps in a versioned project environment and metadata descriptive. Resolve the requested timezone, repository, enabled connections and owner recipient from verified identities.

Use the discovered create/update/enable/pause tools with fresh revisions and stable request keys. Preserve unrelated settings. Where saves begin paused, complete an authorized scheduling request by enabling it with the returned revision. Report a next-run time only when confirmed by the scheduler; pending_sync or scheduler_unavailable means saved but unconfirmed. Refresh after a conflict rather than overwriting a newer change. Do not substitute a GitHub workflow for a missing scheduling operation.

Use `automation_environments` to reuse an appropriate environment. Inspect webhook setup with `automation_webhook_info`; configure it with `automation_webhook_setup` and a persistent credential handle. A receiver setup does not register the provider webhook. Keep credentials out of prompts, URLs and artifacts.

Automations and delegated workers may manage only the definition and fields explicitly granted by their owner. They cannot expand recipients, credentials, repository scope or budgets. Lack of a self-management grant is distinct from lack of scheduling capability. For owner notifications use `slack_send` with `channel='me'`; the server resolves that owner. Verify the saved revision and effective enabled/paused state without creating a rescheduling loop.
