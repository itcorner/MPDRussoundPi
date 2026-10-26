# CI/CD pipeline and Docker image release path

Status: open; Stage 1 established, Stage 2 implementation and acceptance in progress.
Created: 2026-10-26.

## Goal and scope

Separate stable code on `main` from integration work on `develop`, protect
both branches, and introduce GitHub Actions validation and Docker Hub
publication for the web application.

Implement this plan step by step. Each stage needs explicit approval before
starting the next. Adding this plan does not authorize branch changes,
repository-setting changes, creation of release tags, or image publication.
**Do not publish any Docker image until explicitly approved.** Keep publication
disabled while introducing and validating CI.

Initial image scope is the web application built by the root
[Dockerfile](../../Dockerfile), targeting `linux/amd64` and `linux/arm64`.
The Pi playback agent remains a separate native deployment. Publishing the
ser2net and dummy backend images, 32-bit ARM support, and automatic deployment
are outside the initial scope.

## Current baseline

- `main` is the current development and default branch; no `develop` branch
  or version tags were listed during the initial inspection.
- There are no GitHub Actions workflows in the checkout.
- The [native web](../../ansible/playbooks/deploy-russound-web.yml) and
  [Docker](../../ansible/playbooks/deploy-russound-docker.yml) Ansible
  deployments default to `repo_version: main`.
- Both [hardware](../../docker-compose.yml) and
  [dummy](../../docker-compose.dummy.yml) Compose stacks build local images.
- Existing Python and JavaScript test commands are in the
  [README](../../README.md#testing).
- The Dockerfile copies `publicnetwork.py`, but the initial
  [.dockerignore](../../.dockerignore) does not explicitly re-include it
  alongside the other shared playback helpers. Stage 2 clean builds and import
  checks succeeded without that entry, so the suspected missing-file failure
  was not reproduced. An explicit inclusion now documents the intended context.

Creating `develop` preserves history but does not establish that the current
`main` is already release-qualified. Validate the baseline before tagging it.

## Target branch and release policy

| Ref | Purpose | Publication after approval |
|---|---|---|
| `feature/*`, `fix/*` | Short-lived work; PR into `develop` | None |
| `develop` | Integrated development code | `dev`, `dev-<full-commit-sha>` |
| `main` | Stable, release-ready code; release PR from `develop` | None on branch push |
| `vX.Y.Z-rc.N` | Release candidate from a commit reachable from `develop` | `X.Y.Z-rc.N` only |
| `vX.Y.Z` | Stable release from a commit reachable from `main` | `X.Y.Z`, `X.Y`, optionally `latest` as described below |

Keep `main` as the GitHub default branch. Development instructions must
explicitly select `develop`, and feature PRs must target it.

Release tags must pass strict SemVer validation. Exact release and development
SHA image tags are write-once: fail rather than overwrite an existing tag.
Mutable aliases must not move backward when an older release or delayed build
finishes. `latest` identifies only the highest stable release; prereleases
never update it. A minor alias identifies the highest patch in its series.
Omit major-version aliases initially.

Use merge commits for long-lived `develop` -> `main` release PRs to preserve
shared ancestry. Feature PRs may be squash-merged. Hotfixes branch from `main`,
return through a PR and patch release, then merge back into `develop` by PR.

## Staged implementation

### Stage 1: Establish branches and working conventions

- [x] Recheck local/remote branch state and preserve unrelated local changes.
- [x] Create `develop` from the agreed `main` baseline and push it without
      rewriting history.
- [x] Switch local development to `develop`; make subsequent changes on feature
      branches with PRs targeting `develop`.
- [x] Document development, release PRs and hotfix back-merges.

Acceptance: both branches exist at the intended baseline; no running
deployment, release tag or registry image has changed.

Stage 1 implementation (2026-10-26):

- Created and pushed `develop` at `39e3e91e5364cdc06c09ac1bcb280435ba029563`,
  matching local and remote `main`, and configured tracking of `origin/develop`.
- Switched through `develop` to `feature/cicd-branch-workflow` for the
  documentation changes. These remain uncommitted; a PR into `develop` is
  pending commit and publication of this feature branch.
- Added maintained [branch workflow documentation](../documentation/development-workflow.md)
  and linked it from the README and documentation index.
- Preserved the unrelated local `.vscode/` files. No deployment, tag, workflow,
  registry image or repository settings were changed.
- A pre-existing malformed local ref, `.git/refs/heads/main 2`, prevents
  `git fetch origin` from completing. It was left untouched. The baseline
  was verified with `git ls-remote` before creating the branch; repair of the
  malformed ref remains a separate local maintenance task.

### Stage 2: Add validation only

- [x] Add reusable CI validation and a caller for PRs targeting either branch
      and pushes to `develop` and `main`. Do not add an enabled publication path.
- [x] Run the existing Python and JavaScript tests with Python 3.12 (the image
      runtime) and a supported Node.js LTS.
- [x] Verify/fix the Docker build context and build the web image cleanly.
- [x] Smoke-test the image with empty configuration/state volumes: setup is
      accessible, the UI responds and the container healthcheck succeeds.
      Check expected setup/API behaviour, not only a generic HTTP 200.
- [x] Exercise both target architectures using Buildx/QEMU or suitable native
      runners; report which runtime checks actually ran on each platform.
- [x] Always clean up test containers and volumes, and expose useful failure
      logs without credentials or runtime secrets.
- [ ] Bootstrap the workflows from `develop` into `main` through a PR.

Acceptance: CI succeeds on both branches and a representative PR, stable check
names are known, and no registry credentials or image pushes were needed.
Hardware connectivity is not implied by a successful web smoke test.

Stage 2 local validation (2026-10-26):

- Reusable validation plus a CI caller are implemented with a final `CI passed`
  gate, read-only permissions and SHA-pinned Actions. No registry login or push
  is configured; the image build action explicitly uses `push: false`.
- Linux/Python 3.12: 564 tests passed with one optional FFmpeg test skipped
  because FFmpeg is absent from the slim validation container. Node.js 22:
  all 49 tests passed. GitHub-hosted runner results remain to be recorded.
- Clean AMD64 and ARM64 images both passed the same runtime smoke test locally
  (AMD64 emulated, ARM64 native on Docker Desktop). Architecture and Python
  runtime are checked inside each container. CI uses AMD64 runners with QEMU
  for ARM64.
- Workflow linting passed with actionlint. An initial smoke assertion used an
  incorrect HTML marker; the failure exposed logs and cleaned up its container
  and volumes. After correcting the marker, both platform checks passed.
- Documentation for the check names, local reproduction, isolation and
  publication restrictions is in the maintained development workflow guide.
- PR bootstrap and successful GitHub checks on the PR and both long-lived
  branches are still required before Stage 2 acceptance.
- The first GitHub PR run passed both platform smoke checks but exposed
  host-uptime-dependent float rounding in a playback age test (19,999 rather
  than 20,000 ms). Its initial sample now uses a fixed monotonic timestamp;
  production age serialization is unchanged.

### Stage 3: Enforce branch protection

- [ ] Confirm repository visibility/plan supports the intended rules and review
      existing server-side settings before changing them.
- [ ] Add active rulesets for `main` and `develop`: PRs required, established CI
      checks required, force pushes and deletion blocked.
- [ ] Require resolved conversations and validation against the current target
      branch, using up-to-date branches or a configured merge queue.
- [ ] Avoid routine bypass permissions, including for administrators, so direct
      pushes to `main` are genuinely blocked.
- [ ] For a solo maintainer, require PRs but zero approving reviews; require an
      independent approval only when another reviewer is available.
- [ ] Define protected `v*` tag creation/update/deletion rules before releases;
      limit release-tag creation to designated maintainers or release automation.

Acceptance: inspect the effective rules and verify the PR-only path without
making a direct test commit to `main`; normal PRs remain mergeable after CI.

### Stage 4: Prepare publication without enabling it

- [ ] Choose the Docker Hub namespace and create/confirm the repository
      `<namespace>/mpdrussoundpi-web`.
- [ ] Configure `DOCKERHUB_NAMESPACE` and `DOCKERHUB_USERNAME` variables and a
      minimally scoped `DOCKERHUB_TOKEN` secret; never commit the token.
- [ ] Add a separate image-publication workflow that reuses CI validation.
      Initially allow only an explicitly selected build-only mode, with login
      and push disabled; do not create version tags as a test.
- [ ] Implement the event/tag policy, source-branch ancestry validation,
      write-once version checks, and serialized, non-regressing alias updates.
- [ ] Validate the per-platform artifacts that will be published, rather than
      testing one image and rebuilding a different one for publication.
- [ ] Add Buildx caching, OCI source/revision/version labels, provenance and SBOM.
- [ ] Pin third-party Actions to reviewed commit SHAs. Default to
      `contents: read`; isolate registry credentials to trusted publishing jobs.
      Use `pull_request`, not `pull_request_target`, to test submitted code.
- [ ] Document environment approval for stable publication, Docker Hub tag
      retention/immutability capabilities, and how failed publication is retried
      without silently replacing an existing version.

Acceptance: build-only runs demonstrate architecture, metadata and tag
selection for development, prerelease and stable cases. No image is pushed.
Review exact workflow enabling steps before moving to Stage 5.

### Stage 5: Enable development publication (explicit approval required)

- [ ] Enable pushes only for successful `develop` builds.
- [ ] Publish and verify the first approved development image: both platform
      manifests, source revision, digest and pull/run behaviour.
- [ ] Confirm PRs, `main` pushes and release tags cannot publish stable images
      while stable publication is still disabled.

Acceptance: only the approved development channel is active; no stable or
`latest` image exists as a side effect.

### Stage 6: Enable the release path (separate approval required)

- [ ] Validate the release candidate and stable baseline, including manual
      hardware acceptance where relevant; CI does not certify physical audio.
- [ ] Enable protected prerelease/stable publication and release approvals.
- [ ] Create the first version tag only after explicit approval, then verify
      exact tags, alias policy, image digest and architecture availability.
- [ ] Add registry-based deployment instructions/configuration while retaining
      local source builds. Development uses `:dev`; stable deployments use an
      exact version or digest.
- [ ] Keep Ansible's stable default; document `-e repo_version=develop` for
      source-based test deployments and release tags for repeatable production.
- [ ] Document rollback and persistent configuration/state compatibility.
      Publishing must not automatically redeploy the local or production stack.

Acceptance: the approved release can be pulled and run, prior versions remain
addressable, and deployment/rollback procedures identify their data-compatibility
limits.

## Completion

Record stage acceptance and any deviations here as implementation proceeds.
Move lasting contributor and deployment procedures into maintained
documentation. Rename this plan from `new` to `open` when implementation begins
and to `done` only after the approved release path and its acceptance are complete.
