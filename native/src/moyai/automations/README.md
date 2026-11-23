# Product automations

`product_automations.py` owns authorized definition changes, occurrence
admission, routes and the registered Temporal declarations.
`product_automation_sync.py` reconciles saved intent with confirmed schedules.
The access, events and tools modules consume those interfaces. The model files
own their existing persistence records.

Import individual modules. The initializer does not register workflows,
instantiate services or perform I/O. Workflow and activity names, schedule
identities, task queues, receipts, lock ordering and caller-owned transactions
remain unchanged. The canonical `app.automation_*` definitions and historical
data remain in `app`.

`product_schema.application_metadata` registers models using their package
locations while retaining every migration identity and frozen expansion.
Existing `test_product_automation*`, automation continuity, private automation,
schema and Temporal suites exercise the interfaces.
