# PR publishing investigation (2026-10-10)

The reported symptom was a completed local change followed by an uncertain PR
creation result and a request for permission to retry. The screenshot alone does
not establish whether GitHub accepted the write or what caused the failed reply.
No production outage was induced for this investigation.

## What other agents use

- [Claude Code](https://code.claude.com/docs/en/common-workflows#create-pull-requests)
  documents `gh pr create` (and `glab mr create` for GitLab), and associates the
  resulting PR with the session.
- [GitHub Copilot cloud agent](https://docs.github.com/en/copilot/concepts/copilot-surfaces/copilot-on-github)
  manages branches, commits and pushing through its hosted GitHub integration.
  Its documentation does not establish how ambiguous create responses are retried.
- Moyai uses its GitHub App broker, creates Git objects and a deterministic branch,
  then calls the GitHub pull-request API and persists a validated receipt.

The available evidence does not measure comparative failure rates. Switching to
`gh` alone would not eliminate the possibility of an accepted write with a lost
response. Moyai's advantage should be durable recovery that the agent can invoke
without constructing another publication or asking the user to adjudicate a
transport failure.

## Current behavior and remaining gap

The checked-out main already reconciles failed PR acknowledgements with a bounded
read, validates repository/branch/commit identity, and persists an attempted marker
to prevent duplicate PR creation. That is existing behavior, not introduced here.

However, a subsequent recovery ran branch preparation before reconciliation. If
the branch had been deleted after the original attempt, it recreated that branch.
This contradicted the promise of read-only recovery even when no PR was visible.
The fix suppresses branch creation when the durable PR-attempt marker is set.
Initial publication still creates its branch normally. Existing changed-branch
and authorization checks remain in force.

Two broker-level regression cases simulate an accepted PR with a lost reply,
initially invisible readback and subsequent branch deletion. Recovery must issue
only GET requests, leave the branch absent, and either confirm the matching PR
or preserve uncertainty. Both cases failed on the unchanged implementation at
the no-additional-writes assertion.

## Recommended next step

Expose a separate read-only publication-status tool keyed by the saved publication
identity. Today recovery is accessed through a write-labelled creation tool and
requires repeating unchanged arguments. This makes the safe recovery path hard
for agents to distinguish from a forbidden automatic write retry. A status tool
could return confirmed, still unconfirmed, or action required with the destination
and sanitized failure details. It should never infer from an empty PR list that
another create is safe. This API change is a recommendation, not part of this PR.
