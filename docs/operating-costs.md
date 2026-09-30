# Operating costs — deferred rollout

This feature is intentionally **off by default**. Keep its PR unmerged until the
team chooses to enable cost reporting after the trial/paid-plan transition. No
Render, Modal, Temporal, gateway, credential or subscription changes are part of
this work. There is no background billing collector or scheduled provider call.

## What the draft implements

An administrator-only monthly overview at **Spend** combines:

- Recorded LLM response costs, including retained rows from older gateway keys.
- Modal, Temporal Cloud and Render provider statements, scoped to Moyai.
- Explicitly allocated subscription/support fees.
- Credits actually applied to those charges, separately from gross usage.

The existing per-user report keeps its current-key and custom-date behavior.
The new overview has its own UTC calendar-month selector. It never spreads a
monthly invoice across daily ranges or treats an absent statement as zero.
Provider observation dates are visible; provisional statements older than a day
are flagged for refresh. The combined amount is always a **known subtotal**:
unknown model costs, pre-tracking usage and requests outside Moyai are excluded.
It is not a payment record or an invoice-exact gateway lifetime reconciliation.

Statements have one current revision per provider/month. Updating a month
replaces its previous total; retries cannot double count it. Monetary values stay
decimal strings, with at most 12 fractional digits. Revisions use optimistic
concurrency and retain actor, timestamp and previous values in a private audit.
All reads require an admin session, and writes/provider-prefill require CSRF.

## Collection and limits

**Modal:** the optional **Load Modal usage** button uses the public SDK's
`Workspace.billing.report()` API at hourly resolution. It requires a Team or
Enterprise workspace and an explicit allowlist of Moyai object IDs. It sums only
the listed app/storage objects, not the whole LiteLLM workspace. The returned
amount is a prefill: an admin must review it and save the monthly statement.
The current incomplete hour is excluded. No matching rows leave the form empty;
absence is not evidence of free usage.

Modal documents billing exports as gross amounts before credits, reservations
and the egress allowance. Reconcile those adjustments with the provider bill
before marking a statement final. Include all relevant apps (including old web
hosting if the chosen month includes it), image builds and attributable storage.
The list must be reviewed when resources are added or replaced. An app-only
export is not proof that separately billed volumes/snapshots are covered.

**Temporal and Render:** enter the Moyai-specific monthly statement in the UI.
Automatic billing API collection for these two providers is not implemented in
this draft. The existing Temporal worker credential was denied access to the
read-only Cloud usage API during research; it was not expanded. Render's shared
workspace also hosts other services, so its workspace total is inappropriate.

For every provider, use **usage charges** for resource charges, and **allocated
fees** for Moyai's agreed share of workspace subscription/support charges not
already included in usage. A nonzero allocation requires an explanation.
**Credits applied** accepts an explicit zero or an unknown value. Never subtract
an entire trial/promo balance from Moyai, and never deduct the same credit twice.
All amounts are USD and before taxes. Refunds/negative adjustments are not
imported as separate entries; enter a reconciled nonnegative monthly total.

## Activation checklist — complete when the team is ready

1. Confirm current plans, pricing and billing permissions. A trial ending alone
   does not enable this feature or detect a new plan automatically.
2. Agree how shared Modal subscription fees and any shared support fees are
   allocated. Record that basis in each monthly statement. Do not charge the
   entire shared account to Moyai by default.
3. Set `OPERATING_COSTS_ENABLED=true` in the chosen deployment. For optional Modal
   prefill, configure `OPERATING_COSTS_MODAL_OBJECT_IDS` with exact app and storage
   object IDs, separated by commas. Verify the existing Modal credential has
   billing access; obtain approval before expanding access if required.
4. Verify a real Modal export against its provider dashboard. Live paid-plan
   export verification is deferred; automated tests use SDK-shaped fixtures.
5. In Spend, choose the billing month and enter/review one statement per provider.
   Include Temporal actions, active/retained storage and developer support;
   include Render service, disk and attributable network/build overages.
6. Reconcile figures after month-end and mark statements final only when billed
   amounts and applied credits are confirmed. A plan change, fee change or credit
   expiry requires another review. Statement dates make stale inputs visible.

No new gateway permissions, callbacks or user keys are needed. This does not
allocate shared infrastructure to individual users. SQLite/persistent-disk
backup practices continue to apply to this ledger.

## Pricing research (September 30, 2026; recheck before activation)

- [Modal billing](https://modal.com/docs/guide/billing): exports are available on
  Team/Enterprise and can lag. [Pricing](https://modal.com/pricing): Starter is
  $0 plus compute; Team is $250/month plus compute with $100 included compute.
  The workspace was on Starter during research; no upgrade was made.
- [Temporal pricing](https://temporal.io/pricing): Developer has no base fee,
  $50/million actions, $0.042/GB-hour active storage,
  $0.00105/GB-hour retained storage and 10% usage support. The console confirmed
  $150 trial credits expiring December 28, 2026. Its billing reports lag current
  usage; trial balance is not the same as credits actually applied to an invoice.
- [Render pricing](https://render.com/pricing): Moyai uses a $7/month Starter web
  service and 1 GB disk at $0.25/month, plus possible metered overages. The
  workspace is Hobby; its other services must be excluded from Moyai costs.

These are researched rates, not hardcoded charges or an automatic forecast.
The UI does not compare Moyai with Devin.
