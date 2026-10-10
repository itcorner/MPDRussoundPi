# Container Logs

This directory is mounted at `/logs` in the web container.

- `russound-web.log`: application logs, rotated at 10 MiB with five backups.
- `protocol-audit.log`: RNET audit frames, configured by `backend.protocol_audit_log_file` in the mounted config. This file is append-only and needs periodic cleanup.

Application logs also remain available through `docker logs russound-web`.