# Automatic skill learning

Hosted Moyai can learn reusable procedures after successful web-chat turns.
Enable **Learn personal skills** or **Learn shared organization skills** in
**Settings → Skills → Automatic skill learning**. Both start disabled.

Both scopes require an eligible individual sign-in; shared-password accounts
cannot enable learning or publish lessons. Personal learning reads only the
signed-in owner's private web chats and saves personal skills. Organization
learning reads only individual administrators' shared web
chats and saves organization skills. Members can use organization skills but
cannot enable organization learning or publish lessons through it. Slack channels,
automations, delegated turns, failed answers and previously completed turns are
not learning sources. Slack audiences can be narrower than an organization;
automatic publication does not widen them.

After the chat becomes idle, a durable background job asks the configured model
for at most two procedures supported by the requester's authored messages and
the visible final answer. It can choose to save nothing. It does not inspect
private model history, recalled memories, hidden tool output or attachments.
The model has no tools. Skills include when to use a procedure, its narrow
scope, steps, reasons and verification. The original turn's authority, current
settings, source quotes and skill revisions are checked again before publication.
Source matching establishes provenance; it does not independently rerun tests or
certify that a reported result is true.

Learned skills appear in the existing library and `/` picker. The
`learning-source.json` supporting file records the source session, turn, exact
supporting excerpt and verified repository. Personal provenance remains personal.
Relevant learned procedures are also supplied to later web turns in the same
verified repository and authorized scope while learning is enabled. This recall
uses keyword relevance and a bounded number of procedures; it is not a code index
or DeepWiki-style repository documentation.

Only automatically learned, unchanged skills may be automatically updated.
Manual edits, renames, archiving or deletion freeze a learned topic; background
work will not overwrite or recreate it. Updates retain revisioned source files.
Concurrent jobs cannot create the same scoped topic twice. Disabling learning
invalidates in-flight reviews, stops automatic capture and recall, and keeps
saved skills available for explicit invocation. Re-enabling does not backfill
old conversations.

The page reports pending reviews, the latest review outcome and the number of
learned records. A completed review with zero saved skills is normal. A failure
leaves the original successful chat unchanged; never retry a paid model request
whose outcome is uncertain.

## Deployment and verification

Native schema v26 adds preferences, learning jobs and learned-skill provenance.
Both the offline migration and frozen online expansion preserve earlier
receipts. Upgrade the API, coordinator, worker and broker to the same reviewed
release before enabling learning. The worker advertises a learning marker; the
coordinator waits for supporting workflow and activity pollers.

Optional deployment settings:

- `SKILL_LEARNING_ENABLED=false` disables the service globally.
- `SKILL_LEARNING_MODEL` chooses an allowed model; otherwise use the source turn's model.
- `SKILL_LEARNING_IDLE_SECONDS` defaults to 30.
- `SKILL_LEARNING_TIMEOUT_SECONDS` defaults to 60.

Learning uses the ordinary inference admission, accounting, maintenance slots,
encrypted Temporal payloads and requester revocation checks. It is excluded
from foreground dispatch timing.

Run the policy and migration checks with disposable PostgreSQL:

```sh
MOYAI_TEST_POSTGRES=postgresql://localhost/moyai_test uv run pytest \
  native/tests/test_skill_learning.py \
  native/tests/test_native_online_schema.py \
  native/tests/test_native_additive_schema.py \
  native/tests/test_product_schema_versions.py
```

The PR's before/after rehearsal runs the actual application with a configured
model. Deterministic policy tests are supplementary, not that deployment proof.
