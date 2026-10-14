# Runner connection history

`omnigent_conversation_metadata.runner_connected_id` records which runner binding
had an observed heartbeat. It is nullable and limited to 64 characters, matching
the existing runner identifier column. SQLite enforces TEXT with no embedded NUL,
at most 64 characters, and at most 256 stored bytes: four UTF-8 bytes per character
allow every valid 64-character Unicode identifier. Binary values are rejected.
Other backends enforce the bounded `VARCHAR(64)` type. It is separate from `runner_last_seen`,
which is cleared when a connection goes offline. Existing rows remain null;
a timestamp alone does not establish connection history.

## Rollout

Deploy schema revision `mn1a2b3c4d5e` before any application change that reads or
writes this column. This schema change adds no application reader or writer.
Deploy the CockroachDB bootstrap prerequisite first. The schema-only build keeps
its model snapshot revision unchanged and applies this later migration.

The following application rollout must keep invalid legacy IDs out of marker
writes without interrupting the other heartbeat updates. Only a marker equal to the current
runner binding establishes that the runner connected. New bindings cannot reuse
an old runner's evidence, and offline cleanup must retain the marker.

Connection history does not establish that a turn completed. Running turns and
runners that never connected must keep their failure behavior.

## Rollback

Roll back the application to the schema-only build first. That build knows this
migration revision while using the older ORM mapping; builds that predate the
migration reject an unknown newer revision. Keep the nullable column while any deployed
application maps it. Once all readers and writers have been removed, the schema
can be downgraded to `mm1a2b3c4d5e`; this drops only connection history and preserves
the current runner binding and heartbeat. Reapplying the migration leaves the
marker null until a future heartbeat observes the binding again.

Managed distributions with additional migrations must reconcile their Alembic
chain before adopting this revision. Migration identifiers describe specific
changes and must not be reused for unrelated schema changes.
