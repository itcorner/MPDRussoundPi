Legacy dummy runtime data may remain here from older checkouts. Repository
cleanup does not delete or migrate it. Current isolated dummy deployments use
an explicit data root; see the [dummy procedure](../../docs/documentation/container-deployment.md#dummy-no-hardware).

`dummy_state.json` is seeded from `tool/dummy_backend/example_state.json` on first
start and is written back by the TUI when you press `S`.
