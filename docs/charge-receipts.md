# Durable gateway charge receipts

Receipt mode attributes each gateway charge to the original native admission. The broker signs the request ID, gateway scope, key hash, method, path, and body digest after its admission transaction commits. The gateway verifies that context before dispatching inference.

The gateway commits its key charge and a delivery record together. A separate publisher retries delivery to a dedicated Moyai listener. The listener acknowledges each event only after committing its amount to the existing consolidated ledger. Duplicate delivery has no monetary effect. Conflicting events remain unacknowledged.

One ledger row still represents one admitted request. Its `billing_receipts` JSONB field retains immutable receipt evidence, and its amount is the exact decimal sum of those receipts. A scoped event lock and indexed lookup prevent the same receipt being assigned to another request. Receipt ingestion preserves the original user, session, turn, purpose, execution status, and replay barrier.

Deploy the gateway change and finish its schema migration before enabling receipt mode. All gateway replicas must support the protocol. Moyai's explicit native schema provision adds version 23 without changing the 22 earlier migration manifests or historical amounts. The existing billing storage must already be physically consolidated. Follow the application's maintenance and drain procedure for those release commands.

Roll out in this order:

1. Deploy gateway support with receipt enforcement disabled. This version removes the signed context from forwarded provider headers and logging copies.
2. Provision Moyai's schema and consolidate billing storage. Deploy the broker with `LITELLM_CHARGE_RECEIPT_SECRET` set, but keep `LITELLM_CHARGE_RECEIPTS_ENABLED=false`. It will sign context while retaining normal response accounting.
3. Start the receipt listener and gateway publisher. Configure the same shared secret and original gateway/key hashes.
4. Enable required receipts for the application key on every gateway replica.
5. Enable `LITELLM_CHARGE_RECEIPTS_ENABLED=true` and set `LITELLM_SPEND_RECOVERY_ENABLED=false` on Moyai. Startup checks the configured gateway's receipt capability and scope.

Use the same strong, whitespace-free ASCII secret, at least 32 characters, for the broker, listener, and gateway destination authentication. Keep it in the deployment's secret store.

The listener requires only these service-specific settings:

```sh
MOYAI_DATABASE_URL=...
MOYAI_NATIVE_SCHEMA=native
LITELLM_CHARGE_RECEIPTS_ENABLED=true
LITELLM_CHARGE_RECEIPT_SECRET=...
LITELLM_CHARGE_RECEIPT_POOL_SIZE=2
python -m moyai.product_start receipts --host 127.0.0.1 --port 8788
```

It uses its own bounded database pool and does not start a model worker or connect to Temporal, object storage, or the model gateway. Expose it through a private route or an authenticated HTTPS deployment. The endpoint is `POST /billing/receipts`; `/health` checks its database connection. The publisher's fixed destination URL should point to that endpoint.

In receipt mode, response parsing still records usage and execution outcomes. Monetary totals remain pending until a receipt arrives. Moyai does not poll gateway spend logs. The original owner and key scope remain valid for delayed receipts after key rotation or account changes.

The gateway supports synchronous Responses, streaming Responses, Chat Completions, and Responses compaction through the qualified proxy paths. Required-receipt keys reject background and WebSocket inference before provider dispatch. A failed response with no authoritative charge remains unresolved; it is not converted to a free request.

Delivery can stop during deployments without discarding committed receipts. A publisher crash after Moyai's commit but before its acknowledgement causes a harmless replay. A gateway crash before charge capture commits can still leave an attributed request with an unknown amount. This protocol does not reconstruct unreported provider charges, repair historical pricing, or authorize replaying inference.

When Moyai disconnects during an in-flight stream, the gateway finishes reading that call within its configured drain deadline, 120 seconds by default. This also applies when the user presses Stop. No new call is started, but the existing call may finish and incur its remaining cost. The gateway needs a graceful shutdown window longer than this deadline. A hard gateway kill or a provider call that exceeds the deadline can still leave an unknown amount.

The listener accepts at most 32 events per batch and 1,024 receipts per original request. Body, token, identifier, and monetary bounds are validated. Rejections and database contention do not acknowledge a charge. Monitor the publisher's pending events and rejection reasons, and resolve permanent conflicts rather than dropping them.

Each accepted transaction forces synchronous commit and refuses acknowledgement when PostgreSQL has `fsync` disabled. The database's backup and failover configuration must preserve committed writes.

Receipt amounts use decimal strings. Existing gateway key counters use floating-point storage, so comparisons of their raw aggregate values must allow that representation's rounding. Compare the same original key scope and reporting period.

Historical cost repair is a separate explicit operation. Enabling this feature neither rewrites old amounts nor performs a production backfill.
