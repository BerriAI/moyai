# Moyai

Terms used for connected accounts and durable conversation work.

## Language

**Canonical connection**:
A connected account managed by an existing Moyai installation, with its current
credentials, ownership and access policy.

**Grant**:
A conversation's selected account authority at a particular revision. Changing or
revoking that authority invalidates the selection for subsequent actions.

**Answer settlement**:
The completion of an accepted answer or deferred question after its computer
checkpoint succeeds, fails or is interrupted. An accepted answer remains saved
even when the checkpoint cannot be confirmed.

**Workspace checkpoint**:
A saved computer image bound to the conversation, accepted result and authority
that produced it. A continuation can restore it only while those bindings remain
valid.
