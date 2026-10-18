---
name: support-check
description: Answer "do we support X?" for a linked provider doc, feature, or API by checking the code, then reply with a TL;DR verdict, what it would take, and an offer to do it.
---

1. Read the linked page and list every separate capability it describes (a page can
   cover two APIs, or a model plus a tool). Each one gets its own verdict.
2. For each capability, find where we would support it: the provider registry
   or enum, the endpoint routes, and the provider's existing folder. Note the
   repo and commit you checked.
3. If it's missing, find the closest thing we already have (a sibling provider
   for the same endpoint, or nothing at all) and size the work from it: the files
   to add or touch, roughly how many lines, and whether it is a new provider on
   an existing endpoint or a new endpoint.
4. A live call to the provider proves the provider works, not that we support
   it. Only run one if it changes the answer.
5. Reply in Slack size:

   TL;DR: <yes / no / partly> (checked <repo> main @ <commit>).

   <Capability A>: <verdict, with the file you read>. Adding it is <small / big>: <files, size,
   which existing provider to model it on>.

   <Capability B>: <verdict>. This would need <a new endpoint / provider>.

   Want me to open a PR for <the smallest useful piece> now?

Don't open the PR or start the work until they say yes. Leave out a separate
"what I checked" list, side notes about unrelated support, and anything you
couldn't run unless it changes the verdict.
