# Customer spend attribution

Moyai attaches the authenticated turn actor to each tracked model request sent
through its configured LiteLLM gateway. Existing product deployments use the
canonical account email, normalized to lowercase. Standalone accounts use the
stable `moyai:<actor-id>` identifier. Shared conversations and delegated work keep
the identity of the actor whose turn initiated the request.

The server overwrites caller-supplied customer attribution after admission.
When a customer organization alias is configured, it sends only the two customer
portal headers: `x-litellm-customer-org-alias` and `x-litellm-customer-member-id`.
The legacy `x-litellm-customer-id` header is removed, including caller-supplied
values. Without an organization alias, it sends `x-litellm-customer-id` for
LiteLLM's end-user spend tracking instead.

To populate LiteLLM's customer portal:

1. Create a customer organization in the same gateway and select its default
   budget profile. For attribution only, leave every limit unset. Do not use a
   zero-dollar budget: that can deny requests. If the dashboard requires a limit,
   the management API supports an uncapped profile:

   ```json
   {
     "customer_org_id": "moyai",
     "alias": "moyai",
     "default_budgets": [{"name": "attribution-only"}],
     "default_budget_template": "attribution-only"
   }
   ```

   Send this to `POST /management/v1/customer_organizations` using a gateway
   administrator credential. Keep that credential outside Moyai.

2. Set `MOYAI_CUSTOMER_ORG_ALIAS=moyai` alongside the matching `LITELLM_API_BASE`
   and inference-only `LITELLM_API_KEY`. Keep these settings consistent across
   deployment roles. Changing gateways requires updating the URL and key together
   and checking that the configured model names exist there.

3. Roll out the configuration and send a real request from each of two accounts.
   Moyai sends `x-litellm-customer-org-alias: moyai` and, for an account with
   email `alice@example.com`, `x-litellm-customer-member-id: moyai:alice@example.com`.
   LiteLLM creates members under the organization's default team.
   Check that each member's spend is
   separate and that their total rolls up to the organization after accounting
   finishes.

Leave the alias empty to disable customer-portal grouping while retaining
end-user attribution. The organization must exist before enabling its alias;
unknown organizations or missing default profiles can cause gateway admission
failures. Existing gateway/key limits still apply. These headers affect future
requests and do not backfill historical usage.
