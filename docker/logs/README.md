# Container Logs

Legacy checkouts mounted this directory at `/logs`. Logs left here are not
deleted or migrated. Current deployments use an explicit data root; see the
[container guide](../../docs/documentation/container-deployment.md).

- `russound-web.log`: application logs, rotated at 10 MiB with five backups.
- `protocol-audit.log`: RNET audit frames, configured by `backend.protocol_audit_log_file` in the mounted config. Current source rotates this separate opt-in log at 5 MiB with three backups; old images may not.

For current deployments use `docker compose` with the exact selected files
and project, followed by `logs russound-web`; do not assume a fixed container name.