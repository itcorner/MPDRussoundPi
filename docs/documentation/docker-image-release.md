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

[Images (build only)](../../.github/workflows/images-build-only.yml) is manual
(`workflow_dispatch`), not triggered by pushes or tags. Its sole mode is
`build-only`. It runs reusable CI and validates source ancestry: development
and candidates must be reachable from `develop`, stable simulations from
`main`. Version input omits the `v` prefix. This creates no Git tag.

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

There is **no Docker Hub login or push step**, and `push: false` is explicit.
No secret is passed to these jobs. All publication stages need separate approval.
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

Build-only execution simulates tags against an empty inventory. **It does not
check or update Docker Hub tags.** Before publication is enabled, fetch the
complete registry inventory (including pagination) and fail explicitly on
authentication/API errors. Feed existing tags into the policy helper, reject
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

The public `itcornernet/mpdrussoundpi-web` repository was confirmed on
2026-11-01. The ser2net lookup still returned 404; that does not distinguish
missing from private repositories. Confirm ser2net existence and intended
visibility in Docker Hub before publication.

GitHub variables `DOCKERHUB_NAMESPACE=itcornernet` and
`DOCKERHUB_USERNAME=itcornernet` are configured. The user added `DOCKERHUB_TOKEN`
directly in GitHub; only its existence metadata was checked, not its value.
Token scope and Docker Hub write access have not been validated. Never paste
tokens into chat, source, inventory or logs. Build-only jobs do not use this secret.

Before stable publication, configure a release environment allowing only
protected version tags. Independent reviewer approval needs another reviewer;
a solo maintainer must not configure an impossible self-approval gate. Keep
an explicit release authorization gate instead until that decision is settled.
Enforce immutable exact-version tags in Docker Hub where the account supports
it; exclude moving aliases from immutability. Review retention before deleting
old versions so deployment rollback remains possible.

Publishing does not redeploy containers. Stable deployments should select exact
versions or digests and preserve configuration/state/log/secret mounts.
