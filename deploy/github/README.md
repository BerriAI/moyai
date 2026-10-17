# Private deployment workflow

This is a bootstrap template for `BerriAI/moyai-ops`, not an active workflow in
Moyai. The existing `.github/workflows/deploy-production.yml` remains the
production entry point until an explicit cutover.

The private repository owns the workflow, production environment and deployment
credentials. It checks out one full Moyai commit into `source/` and runs that
commit's release scripts there. Application contracts, migration registries and
release tooling therefore remain at the same revision. Extracting those scripts
into the private repository is a separate change: their imports and relative
paths still depend on the application checkout.

## Prepare

1. Merge private-workflow support into Moyai main. Choose a full main commit
   containing it. Old releases do not understand `--source-sha`.
2. Create **private** `BerriAI/moyai-ops`, restrict write access to deployers,
   protect `main`, and install `deploy-production.yml` from this directory as
   `.github/workflows/deploy-production.yml`.
3. Recreate the `moyai-production` environment and its main-only deployment
   branch policy. Verify the organization's plan supports environments and any
   required approval rules in a private repository.
4. Install a GitHub App on **Moyai only**, with read permissions for Contents,
   Actions, Checks and Commit statuses. Add `MOYAI_SOURCE_APP_ID` and
   `MOYAI_SOURCE_APP_PRIVATE_KEY` to the private environment.
5. Provision `RENDER_DEPLOY_API_KEY`, `RENDER_DEPLOY_SSH_KEY`, and the environment
   variable `MOYAI_API_SERVICE_ID` in the private environment. GitHub does not
   return existing secret values; use the approved credential source.

The App token reads Moyai's CI; the private repository's `GITHUB_TOKEN` does not
provide cross-repository access. No dispatch token or production credential
needs to be added to the public repository for the new manual entry point.

## Validate

The template verifies the selected SHA belongs to Moyai main **before running
any candidate source**, then checks out that exact SHA. From the checkout,
the credential-free check is:

```sh
python3 -m scripts.release.deploy --source-sha "$SOURCE_SHA" --check-source
```

It validates the main/manual workflow identity and that the release scripts
live in a clean Git checkout at the selected SHA. It prints separate `source`
and `workflow` identities. It does **not** validate CI, credentials, production
health, or admission to main. Those checks happen in the private bootstrap
and the real deployment preflight. Do not override `GITHUB_SHA` with the source
SHA: GitHub's workflow identity must remain accurate.

Run the private workflow with `preflight_only=true` (the default), retaining the
same source SHA throughout. Compare its receipt with the existing workflow's
preflight for the same SHA and release type. The real controller checks Moyai
CI, source ancestry and production identity before writing to Render.

Application dependencies and generated ignored frontend assets may be installed
in the source checkout. Tracked edits or untracked source files are rejected.
Keep receipts outside the checkout; the template uses `RUNNER_TEMP`.

## Cut over

GitHub concurrency groups apply within one repository. Identical group names do
not serialize the old and new workflows across repositories.

1. Verify both preflights, finish any existing release, and confirm no Render
   deployment is active.
2. Disable the old production workflow before the first mutating private run.
   Keep auto-deploy disabled on Render.
3. Perform one planned release from the private repo; inspect the receipt,
   actual role identities, readiness, and application behavior.
4. Remove the old deployment credentials and workflow after verification.
   Historical public workflow runs remain public; moving the YAML does not
   remove their logs or artifacts.

The new CI inventory reads Moyai's repository-level run history, so removing the
old workflow file does not break its endpoint. Historical deployment checks are
excluded only after verifying their exact workflow path, event, branch and SHA.
Other pending or failed checks still block deployment.

If the cutover fails, inspect the receipt and Render before retrying. Re-enabling
the old entry point requires first disabling the private one and ensuring no
release remains active. Existing release-type recovery rules still apply; this
migration does not introduce automatic rollback.
