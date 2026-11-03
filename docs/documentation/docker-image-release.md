# Docker image preparation and release

## Image boundaries

| Repository | Current scope |
|---|---|
| `itcornernet/mpdrussoundpi-web` | Web UI and gateway, not the audio stack |
| `itcornernet/mpdrussoundpi-ser2net` | Serial-to-TCP gateway |
| `itcornernet/mpdrussoundpi-mpdagent` | Proposed; no container implementation yet |

The dummy backend has its own Dockerfile for hardware-free development. It is
not a production publication target. The native Pi MPD agent needs the separate
[containerization plan](../plans/mpd_new_agent-containerization.md).

## Build-only preparation

[Images (development and build only)](../../.github/workflows/images-build-only.yml)
supports manual `build-only` simulations and preparation on pushes to `develop`.
Only develop push runs can publish, gated by the explicit repository variable
`DEVELOPMENT_PUBLICATION_ENABLED=true`. Manual runs never publish. It runs
reusable CI and validates source ancestry: development
and candidates must be reachable from `develop`, stable simulations from
`main`. Version input omits the `v` prefix. This creates no Git tag.
The policy CLI rejects non-manual events, tag-ref dispatches and every mode
other than `build-only`. Development simulations require an empty version input.

The workflow builds web and ser2net for AMD64 and ARM64, exporting OCI archives
with source/revision/version labels, BuildKit provenance and SBOM attestations.
Skopeo loads the runtime image from each archive into the runner's Docker daemon
for smoke testing; no second build is substituted. Successful archives and
checksums are saved as GitHub Actions artifacts for seven days. Archive
verification checks platform/source labels, blob checksums, and SBOM/provenance
subjects against the runtime digest; the verified metadata is saved too. They are
preparation artifacts, not published releases. Preserve the OCI archive for
future publication: a Docker-daemon roundtrip alone does not retain attestations.

Web smoke tests exercise setup and offline saves. Ser2net smoke tests override
the serial connector with `stdio` and check generated configuration, non-root
runtime, architecture and the TCP listener on an isolated internal network.
They do not certify serial-device passthrough or communication with controllers.

All build jobs use `push: false` and receive no Docker Hub secrets. The separate
trusted development publisher consumes the tested archives after validation;
only that publishing step receives the Docker Hub secret. Stable/candidate
publication remains disabled and requires separate approval.
To appear in the Actions UI, the manual workflow must also reach the default
branch through a reviewed bootstrap PR.

The workflow is available on both branches after PRs #6 and #7. Development,
candidate and stable build-only runs passed on 2026-11-01 (links in the
[acceptance record](../plans/general_open_cicd-docker-release.md#stage-4-prepare-publication-without-enabling-it)).
Dispatch manual scenarios sequentially: the shared concurrency group preserves
the running job, but GitHub can replace an older pending job with a newer one.

## Intended tags and publication safeguards

The [policy helper](../../tool/ci/image-policy.mjs) and its tests define:

- Development: write-once `dev-<full-sha>` and moving `dev`.
- Candidate: `X.Y.Z-rc.N` only.
- Stable: write-once `X.Y.Z`; `X.Y` only when not regressing its series;
  `latest` only when not regressing the highest stable version.

### Event and source matrix

| Mode | Event/ref | Channel/version | Required source ancestry |
|---|---|---|---|
| Build-only | Manual dispatch on a branch | Selected channel and simulated version | `develop` for development/candidate; `main` for stable |
| Future publication | Push to `develop` | Development; no version input | `develop` |
| Future publication | Push of `vX.Y.Z-rc.N` | Candidate; version derived from tag | `develop` |
| Future publication | Push of `vX.Y.Z` | Stable; version derived from tag | `main` |

Development push publication is now wired behind its explicit enable variable.
Candidate/stable rules remain tested library logic only, without enabled
triggers. PRs, manual publication and pushes to `main` cannot publish.

Source checks fail on missing remote refs, unknown commits or failed Git
commands. A development commit may be reachable from `develop` without being
its current tip: preparing its exact SHA image is valid, but promoting `dev`
requires the tip check. The helper supports that check; a future publisher must
refresh the remote ref and invoke it immediately before alias promotion.

The focused tests use temporary Git repositories to exercise ancestry,
unintegrated commits and a branch advancing before a delayed build promotes
`dev`. Inventory fixtures also cover exact-tag collisions, numeric version
ordering (including large components), prerelease exclusions and older releases.

By default, build-only execution simulates tags against an empty inventory.
Select `check_registry: true` on the manual workflow to query live public tags
for both image repositories before building. The
[inventory helper](../../tool/ci/registry-inventory.mjs) follows every Docker Hub
API page and applies the same write-once and alias policy independently to each
repository. The workflow uses the required `DOCKERHUB_NAMESPACE` repository
variable for both inventory and prepared image names.

This check uses anonymous GET requests only, not `DOCKERHUB_TOKEN`. It rejects
HTTP errors (including 401/403/404/429), malformed responses, inconsistent counts,
duplicate tags, incomplete pagination and unexpected pagination destinations.
An inaccessible or private repository is an error, not an empty inventory.
The resulting inventory/tag decisions appear in the job log and Actions summary.
No tag or alias is updated. A live inventory check is a preparation snapshot,
not a reservation or atomic write-once guarantee.

Before publication is enabled, require inventory checks in the trusted publisher,
support authenticated inventory if private repositories are intended, reject
existing write-once versions, and recheck aliases under a serialized publisher.
For `dev`, verify the publishing SHA still matches the current `develop` tip.
The preparation concurrency group serializes manual runs but is not sufficient
by itself to prevent stale publication; GitHub may replace pending queued runs.

Publish the tested OCI artifacts without rebuilding; assemble the multi-platform
manifest and record its digest. Complete both architecture uploads before
changing aliases. On failure after a version was written, fail rather than
overwrite it: inspect the recorded digest and use an explicitly approved
recovery/promotion procedure. Do not silently retry as a new release.

## Docker Hub setup (pending)

### No-write credential verification

After the reviewed changes reach `main`, manually run
[Docker Hub access (no writes)](../../.github/workflows/registry-access-check.yml)
on `main`. Other refs cannot execute its credential job. The checkout does not
persist Git credentials, and the Docker Hub secret is provided only to the
credential-check step, never to PR validation or artifact builds.

The helper sends credentials only to Docker Hub's HTTPS token endpoint, requests
`pull,push` scopes for both intended repositories and checks the registry accepts
each returned bearer token. Only repository names and granted actions are logged;
credentials, bearer tokens and authentication response bodies are not logged.
Failure to grant both actions fails the check. This verifies authentication and
the authorization server's grants, not an actual image upload or exact-tag write.
Those remain Stage 5 acceptance checks.

The [first credential check](https://github.com/itcorner/MPDRussoundPi/actions/runs/41731056726)
passed on `main` on 2026-11-03, confirming registry authentication and pull/push
grants for both repositories without uploading an image.

### Serialized dry-run planning and recovery

With `check_registry: true`, build-only preparation also runs a final
`publication-plan` job after all four image jobs succeed. It downloads the current
run's tested archives, re-verifies their labels, runtime digests and attestations,
refreshes the required source branch and reads a fresh registry inventory.
Development dry runs fail if their SHA is no longer the current `develop` tip.
The job uses concurrency group `image-publication`, with cancellation disabled;
future actual publishers must share this group. GitHub may still replace a
pending run, so concurrency is not a durable FIFO queue or a registry lock.

The resulting report specifies uploads preserving attestations, verification of
both exact multi-platform tags, digest recording and alias promotion last.
It performs none of those writes. Registry-side immutability and rechecks at
actual write/promotion time are still required before enabling publication.

If a future publication partially succeeds, stop: do not rerun a normal
publication against an existing exact tag. Record the completed registry manifest
digests, inspect both repositories and verify architecture/attestation coverage.
The tested recovery gate accepts only matching recorded/registry manifest
digests and separate explicit approval; mismatches or missing evidence fail.
This is a planning safeguard, not an enabled recovery/promotion workflow.
An approved recovery must also refresh inventory and branch-tip checks before
moving aliases. Do not rebuild or overwrite exact tags as a recovery shortcut.

The [development dry run](https://github.com/itcorner/MPDRussoundPi/actions/runs/41731060294)
passed on 2026-11-03 with `check_registry=true`: reusable CI, all four image jobs,
live inventory and the serialized artifact-verification/publication-plan job
succeeded. No registry writes occurred.

The public `itcornernet/mpdrussoundpi-web` repository was confirmed on
2026-11-01. After the user created `itcornernet/mpdrussoundpi-ser2net` on
2026-11-03, anonymous inventory checks succeeded for both repositories, each
with no tags. Both repositories are publicly readable. This verifies read
access only. Subsequent GitHub execution verified live inventory and the no-write
credential check as recorded above; actual image publication remains untested.

GitHub variables `DOCKERHUB_NAMESPACE=itcornernet` and
`DOCKERHUB_USERNAME=itcornernet` are configured. The user added `DOCKERHUB_TOKEN`
directly in GitHub; only its existence metadata was checked, not its value.
On 2026-11-03, the user confirmed the token is configured for Read & Write
without Delete. The no-write Actions check subsequently verified authentication
and granted pull/push scopes for both repositories. Actual uploads have not been
tested. Never paste tokens into chat, source, inventory or logs. Build-only jobs
do not use this secret.

Before stable publication, configure a release environment allowing only
protected version tags. Independent reviewer approval needs another reviewer;
a solo maintainer must not configure an impossible self-approval gate. Keep
an explicit release authorization gate instead until that decision is settled.
Enforce immutable exact-version tags in Docker Hub where the account supports
it; exclude moving aliases from immutability. Review retention before deleting
old versions so deployment rollback remains possible.

### Temporary development-only immutability exception

On 2026-11-04, the user agreed to defer selective immutability configuration:
Docker Hub returned HTTP 500 when saving the rule, and manual attempts failed
too. Both repositories remain mutable. An empty tag inventory is only a
hypothesis, not a confirmed cause.

Retry immediately after the first separately approved development publication.
Until then, require a serialized trusted publisher, fresh inventory and direct
exact-tag existence checks, and recorded expected/observed manifest digests.
Avoid concurrent manual or external publication. These checks cannot guarantee
write-once tags against other writers; pin deployments by digest.

This exception does not enable publication or waive the remaining write/recovery
safeguards. If immutability still cannot be saved after the first development
tags exist, investigate with Docker support before further publication.
Candidate/stable publication remains disabled pending resolution or a separately
reviewed alternative.

The first development images were uploaded at source
`902ab4b0ffbad4d6a656150943c73231bb73ec81`; both `dev` and its exact SHA tag exist.
The enable variable was then reset to `false` to suspend further publication.
Retrying selective immutability after those uploads still produced HTTP 500 for
both repositories. Investigation with Docker support or a separately reviewed
alternative is required before more publication.

### Publisher implementation and local-registry validation

The [publisher](../../tool/ci/image-publisher.mjs) implements OCI blob/manifest
uploads, deterministic two-platform indices, digest read-back verification,
exact-tag collision checks and alias promotion. It reuses the
[prepared archive loader](../../tool/ci/publication-plan.mjs), including archive
checksums and runtime/attestation validation; it never rebuilds an image.

Stage 4 validated the HTTP loopback write transport with `MODE=local-test`.
Stage 5 development publication was explicitly approved on 2026-11-04.
The authenticated Docker Hub transport is restricted to the intended namespace
and two image repositories. It obtains repository-scoped bearer tokens, treats
authentication/API failures as errors and never sends bearer credentials to blob
download CDNs. Fresh authenticated registry inventory is mandatory before
uploads and alias promotion, independent of the optional simulation input.

`MODE=publish-development` requires a develop push, an empty version input,
development channel and `DEVELOPMENT_PUBLICATION_ENABLED=true`. Disable that
variable to suspend automatic development publication. Both normal publication
and recovery share the `image-publication` concurrency group. Neither path
publishes candidate/stable tags or `latest`, and neither deploys containers.

Before uploads, the publisher records expected exact-index digests for both
repositories in a new journal and preflights both inventories/direct tags.
It verifies both exact images and all referenced blobs/manifests before aliases,
refreshes alias eligibility and checks the development tip immediately before
promotion. A process lock rejects simultaneous local publications to the same
endpoint/namespace. Journal updates use flushed temporary files and atomic
replacement; preserve the journal and original tested archives after failure.

Recovery requires `RECOVERY_APPROVED=true` and the existing matching journal.
Existing exact tags must match the recorded digest; absent exact tags can be
completed from the original artifacts. Completed tags are verified, not
overwritten. Normal retries against an existing journal fail. A crashed local
process may leave a lock or journal temporary file: inspect the journal and
verify no publisher is running before manually removing only that stale file.
Do not silently discard evidence or start a competing publication.

The [recovery workflow](../../.github/workflows/recover-development.yml) is manual
on `main` with `approve_recovery=true`. It validates that `source_run_id` is an
original failed/cancelled develop push run of the image workflow in this
repository, downloads only its tested archives and publication journal, and uses
the original source SHA rather than the recovery workflow's SHA. It executes
trusted code from `main`, never code from the downloaded artifacts. Missing,
expired or mismatching evidence fails; normal publication reruns do not silently
recover an existing exact tag.

Publishing jobs preserve journals for 90 days even on failure. OCI archives are
retained for seven days; download and preserve them promptly if recovery is
needed. Abrupt runner termination can prevent evidence upload; absent evidence
requires manual investigation, not a blind publication retry. After publication
or recovery, both architectures are pulled by recorded registry digest and
runtime-smoke-tested. Check the Actions summary for immutable digest references.

Runtime verification pulls each platform's child digest separately, avoiding
Docker's classic image-store conflict when two platforms share an index
reference. If uploads completed but this post-publication check failed, use the
[verification-only workflow](../../.github/workflows/verify-development-publication.yml)
on `main` with the original failed run ID and completed digest journal. It uses
no Docker Hub secret and performs no publication or recovery writes.
Verification and recovery pass the validated original source through
`PUBLICATION_SOURCE_SHA`; GitHub's reserved `GITHUB_SHA` remains the trusted
workflow commit. Ordinary development publishing always uses `GITHUB_SHA`.

The first images passed all four registry pull/runtime checks in
[verification run 41741967600](https://github.com/itcorner/MPDRussoundPi/actions/runs/41741967600).
Development aliases are `itcornernet/mpdrussoundpi-web:dev` and
`itcornernet/mpdrussoundpi-ser2net:dev`. While exact tags lack registry-enforced
immutability, prefer the verified index references in Compose:

```yaml
services:
  web:
    image: itcornernet/mpdrussoundpi-web@sha256:25e1c9382029c7485eba22aba2b6fcfe73e72e93393ad5fcb178f05b7de97d54
  ser2net:
    image: itcornernet/mpdrussoundpi-ser2net@sha256:10f4c490f1c6a5e9a8d27c42c144a5a465ef75246ed32f395ea3d9844af1a314
```

These are image-reference examples, not a complete deployment configuration;
retain the existing ports, configuration/state mounts and serial-device access.
No deployment was performed as part of publication acceptance.

The JavaScript CI job runs these upload/recovery tests against a digest-pinned
disposable registry service. Tests include actual OCI uploads/read-back,
attestation preservation, collisions, partial failures, approved recovery,
digest mismatches, simultaneous publications, stale development builds and
non-regressing stable aliases. Synthetic platform images test registry mechanics,
not hardware or production Docker Hub upload compatibility.

To repeat locally, start a disposable registry bound to loopback, set
`REGISTRY_TEST_URL=http://127.0.0.1:<published-port>`, and run:

```sh
node --test tests/test_image_publisher.js
```

Stop and remove that specific test container afterward. Without the environment
variable, registry-write integration tests are skipped; policy/assembly tests
still run. Docker Hub registry immutability is not simulated by these tests.

Publishing does not redeploy containers. Stable deployments should select exact
versions or digests and preserve configuration/state/log/secret mounts.
