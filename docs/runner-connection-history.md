# Runner connection history

`omnigent_conversation_metadata.runner_connected_id` records which runner binding
had an observed heartbeat. It is nullable and limited to 64 characters, matching
the existing runner identifier column. It is separate from `runner_last_seen`,
which is cleared when a connection goes offline. Existing rows remain null;
a timestamp alone does not establish connection history.

## Rollout

Deploy schema revision `b8d6e1a4c729` before any application change that reads or
writes this column. This schema change adds no application reader or writer.
Older application versions continue to use their existing mapped columns.

The following application rollout must bound every marker write, including on
SQLite, which does not enforce `VARCHAR(64)`. Only a marker equal to the current
runner binding establishes that the runner connected. New bindings cannot reuse
an old runner's evidence, and offline cleanup must retain the marker.

Connection history does not establish that a turn completed. Running turns and
runners that never connected must keep their failure behavior.

## Rollback

Roll back the application first. Keep the nullable column while any deployed
application maps it. Once all readers and writers have been removed, the schema
can be downgraded to `mm1a2b3c4d5e`; this drops only connection history and preserves
the current runner binding and heartbeat. Reapplying the migration leaves the
marker null until a future heartbeat observes the binding again.

Managed distributions with additional migrations must reconcile their Alembic
chain before adopting this revision. Migration identifiers describe specific
changes and must not be reused for unrelated schema changes.
