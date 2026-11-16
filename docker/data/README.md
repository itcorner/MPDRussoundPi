Legacy mount point for persisted runtime state. Data left by older checkouts
is not deleted or migrated. Current deployments use an explicit data root;
see the [container guide](../../docs/documentation/container-deployment.md).

The container writes `/data/russound_state.json` here, plus the protocol audit
log when `backend.protocol_audit_log_file` points into this directory.
