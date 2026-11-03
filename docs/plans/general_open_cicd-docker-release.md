# CI/CD pipeline and Docker image release path

Status: open; Stages 1-3 implemented; Stage 4 build-only validation accepted, registry/publication prerequisites remain. Publication disabled.
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

Stage 4 scope was expanded with approval on 2026-11-01 to web and ser2net,
targeting `linux/amd64` and `linux/arm64`, under Docker Hub namespace
`itcornernet`. The Pi agent remains native; its
[containerization plan](./mpd_new_agent-containerization.md) is separate.
Dummy image publication, 32-bit ARM and automatic deployment remain outside scope.

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
  documentation changes. These were subsequently committed and merged into
  `develop` through PR #1 during Stage 2.
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
- [x] Bootstrap the workflows from `develop` into `main` through a PR.

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
  branches were completed as recorded below.
- The first GitHub PR run passed both platform smoke checks but exposed
  host-uptime-dependent float rounding in a playback age test (19,999 rather
  than 20,000 ms). Its initial sample now uses a fixed monotonic timestamp;
  production age serialization is unchanged.
- Feature PR #1 and the first `develop` push passed CI. Bootstrap PR #2 exposed
  the same clock dependence in the exact freshness-threshold test. That test
  now also uses a fixed initial timestamp, without loosening its threshold
  assertions or changing production code.

Stage 2 accepted (2026-10-26):

- [PR #1](https://github.com/itcorner/MPDRussoundPi/pull/1) introduced validation
  and the branch workflow into `develop` after successful checks.
- [PR #3](https://github.com/itcorner/MPDRussoundPi/pull/3) corrected the second
  deterministic-clock test through a separate successful PR into `develop`.
- [PR #2](https://github.com/itcorner/MPDRussoundPi/pull/2) bootstrapped
  `develop` into `main` using a merge commit after both
  [develop push CI](https://github.com/itcorner/MPDRussoundPi/actions/runs/40349751340)
  and [bootstrap PR CI](https://github.com/itcorner/MPDRussoundPi/actions/runs/40349760448)
  passed.
- The final [main push CI](https://github.com/itcorner/MPDRussoundPi/actions/runs/40350054112)
  passed at `b0ba436ce2a02162062d0f89113e0471e65717e8`.
  `develop` is at `8fa7ac8249c301e6ebb0f6a48d01f5f0675931e3`; its tree matches
  the bootstrap merge, with ancestry preserved. `main` remains the default.
- The actual final gate name is `CI passed`. All three validation jobs
  (Python/JavaScript, AMD64 container, ARM64 container) succeeded on GitHub.
- No images were published and no deployments, release tags, registry secrets
  or branch rules were changed. Stage 3 has not started.
- The malformed local ref noted in Stage 1 was subsequently backed up outside
  Git's refs directory and removed with approval. Normal `git fetch origin`
  and `git fsck --full --no-dangling` passed on 2026-10-26. Local `main` and
  `develop` were then fast-forwarded to their remote commits without changing
  working files.

### Stage 3: Enforce branch protection

- [x] Confirm repository visibility/plan supports the intended rules and review
      existing server-side settings before changing them.
- [x] Add active rulesets for `main` and `develop`: PRs required, established CI
      checks required, force pushes and deletion blocked.
- [x] Require resolved conversations and validation against the current target
      branch, using up-to-date branches or a configured merge queue.
- [x] Avoid routine bypass permissions, including for administrators, so direct
      pushes to `main` are genuinely blocked.
- [x] For a solo maintainer, require PRs but zero approving reviews; require an
      independent approval only when another reviewer is available.
- [x] Define protected `v*` tag creation/update/deletion rules before releases;
      limit release-tag creation to designated maintainers or release automation.

Acceptance: inspect the effective rules and verify the PR-only path without
making a direct test commit to `main`; normal PRs remain mergeable after CI.

Stage 3 configuration (2026-10-26):

- Confirmed public repository visibility, a single administrator (`itcorner`),
  no existing rulesets or classic protection, and successful `CI passed` checks
  from GitHub Actions (integration ID `15368`).
- Activated [branch ruleset 25892348](https://github.com/itcorner/MPDRussoundPi/rules/25892348)
  for `main` and `develop`: PRs, strict/up-to-date `CI passed`, resolved threads,
  zero approving reviews, no force pushes or deletion, and no bypass actors.
- Activated [tag-creation ruleset 25892349](https://github.com/itcorner/MPDRussoundPi/rules/25892349)
  for `v*`: creation restricted, with repository administrators allowed to
  bypass only this creation restriction.
- Activated separate [tag-immutability ruleset 25892351](https://github.com/itcorner/MPDRussoundPi/rules/25892351):
  updates, deletion and force updates blocked with no bypass. No tag created.
- Read back both branches' effective rules and every ruleset's active state
  and bypass list. No destructive or direct-push probe was used.
- [PR #4](https://github.com/itcorner/MPDRussoundPi/pull/4) contains the
  housekeeping and protection documentation; its checks, merge state and
  acceptance comment record validation of the normal protected PR path.
  No CI publishing, registry credentials or deployment was enabled.

### Stage 4: Prepare publication without enabling it

Implementation record (2026-11-01):

- Namespace/publishing username chosen: `itcornernet`; repositories
  `mpdrussoundpi-web` and `mpdrussoundpi-ser2net`. Web is confirmed public;
  ser2net initially returned 404. After the user created it on 2026-11-03,
  anonymous inventory reads succeeded for both repositories.
- The user added `DOCKERHUB_TOKEN` directly in GitHub. Its existence metadata
  was verified without accessing the value. Both agreed Docker Hub variables
  are configured as repository variables; the token is a repository secret.
  The user confirmed the token is configured with Read & Write permissions;
  its authentication and repository access have not been exercised.
- Manual build-only workflow exports both architectures of both images with
  provenance/SBOM, caches builds, and smoke-tests the exported runtime artifacts.
  No login/push path is enabled. OCI archives are retained for seven days.
- Tag policy covers strict versions, candidates, write-once tags and
  non-regressing stable aliases. Registry inventory checks and actual serialized
  alias publication are deliberately not wired while publication is disabled;
  they remain prerequisites before Stage 5.
- Maintained procedures and pending gates are in
  [Docker image preparation](../documentation/docker-image-release.md).
- GitHub build-only acceptance passed as recorded below. Both repositories are
  publicly readable; token authentication/repository access verification remains
  pending. No version tag or Docker Hub image has been created.

- [x] Choose the Docker Hub namespace and create/confirm the repository
      `<namespace>/mpdrussoundpi-web`.
- [x] Create/confirm the public repository
      `<namespace>/mpdrussoundpi-ser2net`.
- [x] Configure `DOCKERHUB_NAMESPACE` and `DOCKERHUB_USERNAME` as repository
      variables and `DOCKERHUB_TOKEN` as a repository secret; never commit the
      token.
- [x] Confirm the token is configured for Read & Write, without Delete
      permission; do not expose the token value.
- [x] Verify token authentication and granted pull/push scopes for both intended
      repositories without publishing an image; actual uploads remain untested.
- [x] Add a main-only manual credential check requesting repository pull/push
      grants and validating registry authentication without writes.
- [x] Add a separate image-publication workflow that reuses CI validation.
      Initially allow only an explicitly selected build-only mode, with login
      and push disabled; do not create version tags as a test.
- [x] Implement and locally test the event/ref matrix, strict tag policy and
      source-branch ancestry checks, keeping publication disabled.
- [x] Test write-once tag collisions, non-regressing stable alias decisions and
      current-tip validation for `dev` using inventory/Git fixtures.
- [x] Wire opt-in, read-only public registry inventory and write-once version
      checks into build-only preparation.
- [x] Verify live read-only inventory checks locally for both intended
      repositories.
- [ ] Require a fresh inventory in the future trusted publisher.
- [x] Add serialized, no-write publication planning with verified artifacts,
      fresh source/inventory checks and tested digest/approval recovery gates.
- [ ] Implement serialized, non-regressing alias publication and explicit
      recovery gates; enabling registry writes requires separate approval.
- [x] Implement and test the publisher/recovery engine against a disposable
      local registry, with Docker Hub write transport and workflow wiring disabled.
- [x] Validate the per-platform artifacts that will be published, rather than
      testing one image and rebuilding a different one for publication.
- [x] Add Buildx caching, OCI source/revision/version labels, provenance and SBOM.
- [x] Pin third-party Actions to reviewed commit SHAs. Default to
      `contents: read`; isolate registry credentials to trusted publishing jobs.
      Use `pull_request`, not `pull_request_target`, to test submitted code.
- [x] Document environment approval for stable publication, Docker Hub tag
      retention/immutability capabilities, and how failed publication is retried
      without silently replacing an existing version.

Acceptance: build-only runs demonstrate architecture, metadata and tag
selection for development, prerelease and stable cases. No image is pushed.
Review exact workflow enabling steps before moving to Stage 5.

Stage 4 build-only acceptance (2026-11-01):

- [PR #6](https://github.com/itcorner/MPDRussoundPi/pull/6) passed required CI
  and merged into `develop`; [PR #7](https://github.com/itcorner/MPDRussoundPi/pull/7)
  passed CI and bootstrapped the workflow into `main` using a merge commit.
- [Development simulation](https://github.com/itcorner/MPDRussoundPi/actions/runs/41359721867),
  [candidate simulation](https://github.com/itcorner/MPDRussoundPi/actions/runs/41360244091)
  (`0.1.0-rc.1`) and
  [stable simulation](https://github.com/itcorner/MPDRussoundPi/actions/runs/41359725618)
  (`0.1.0`) all passed. These are input values, not created version tags.
- Each run passed reusable CI, source/tag policy and all four image jobs:
  web/ser2net on AMD64/ARM64. Every job verified OCI labels/digests,
  SPDX SBOM and SLSA provenance subjects, then loaded and smoke-tested the
  exported runtime. All four prepared archives are available in each run.
- The initially queued candidate run was cancelled by GitHub's concurrency
  pending-run replacement; it was dispatched alone and passed. Future manual
  scenarios should be submitted sequentially.
- Local verification passed 54 JavaScript tests and actionlint. Local ARM64
  ser2net startup and tagged OCI archive checks passed; validation images,
  archives and logs were cleaned up. Running deployments were not changed.
- Pending before publication: verify token authentication and intended
  repository access; require fresh publisher-side registry inventory and
  write-once checks, serialized/current-tip alias promotion, and explicit
  recovery gates. No registry writes are implemented in this build-only
  workflow. Stable environment/approval remains a later gate.
- On 2026-11-03, the user confirmed `DOCKERHUB_NAMESPACE` and
  `DOCKERHUB_USERNAME` are repository variables and `DOCKERHUB_TOKEN` is a
  repository secret. Token authentication and Docker Hub repository write
  access remain unverified.
- On 2026-11-03, the user confirmed the token's Docker Hub permission setting is
  Read & Write. The token value was not accessed; authentication and repository
  write access have not been tested.
- No Docker Hub login or push occurred. Stage 5 is not authorized.

Stage 4 policy implementation (2026-11-03):

- The build-only CLI validates manual branch dispatches, channel/version input
  and ancestry. Publication mode remains rejected by the CLI.
- Library event rules derive future development/candidate/stable publication
  channels from allowed push refs only; PRs, `main` pushes and manual publication
  are rejected. No new workflow trigger or registry operation was added.
- Local tests cover exact-tag collisions, numeric stable-version ordering,
  prerelease exclusions, real Git ancestry failures and stale `dev` promotion.
  A current-tip helper is available for the future serialized publisher; it
  must refresh remote state before checking. Registry inventory and actual
  alias promotion remain unwired.
- Maintained rules and the event matrix are documented in
  [Docker image preparation](../documentation/docker-image-release.md#event-and-source-matrix).
- Local JavaScript regression validation passed all 63 tests, including 12
  image-policy tests. Publication remains disabled; no registry credentials
  were used.

Stage 4 registry inventory implementation (2026-11-03):

- Added opt-in `check_registry` to the build-only workflow (default false).
  Anonymous Docker Hub API reads collect all pages for web and ser2net, then
  check exact-tag collisions and planned aliases independently per repository.
  Prepared image names and inventory use the same required namespace variable.
- The helper fails explicitly on HTTP/transport/JSON errors, malformed pages,
  duplicate tags, changing/incomplete counts and unsafe/looping pagination.
  No token, registry login, push or alias update is used.
- Live local verification returned an empty web tag inventory and HTTP 404 for
  ser2net initially; the 404 was not treated as an empty repository.
- After the user created ser2net on 2026-11-03, live local policy checks succeeded
  for both public repositories. Each returned an empty tag inventory and planned
  the current commit's exact development tag plus `dev`, without writing either.
  GitHub execution of the updated workflow remains to be validated after merge.
- Inventory is a read-only snapshot. Fresh publisher-side inventory,
  serialization, registry immutability and recovery gates remain prerequisites
  before enabling writes.
- Live pagination validation collected all 219 tags from the public
  `library/busybox` repository across multiple pages. Local JavaScript regression
  validation passed all 73 tests, including 10 registry-inventory tests;
  actionlint passed for the updated workflow.

Stage 4 no-write safeguards (2026-11-03):

- Added a manual credential-check workflow restricted to `main`. It uses the
  repository secret only in its check step, requests pull/push grants for both
  intended repositories and verifies registry acceptance without any upload.
  The actual secret was not accessed locally; GitHub credential-check acceptance
  remains pending after reviewed changes reach `main`.
- Added a final serialized dry-run job to registry-enabled build-only runs.
  It downloads and re-verifies all four tested OCI artifacts, refreshes source
  ancestry/current development tip and obtains fresh inventory. Its report
  specifies exact manifests before aliases and preserves attestations.
- Recovery checks require matching recorded/registry manifest digests and
  separate explicit approval, rejecting overwrite or evidence-free retries.
  No actual upload, alias promotion or recovery workflow is enabled.
- Local JavaScript validation passed all 82 tests, including end-to-end dry-run
  artifact/Git/inventory verification; all workflows passed actionlint.
- Stage 4 is not yet accepted: the new workflows need GitHub execution, token
  grants need verification, and actual serialized write/recovery implementation
  and registry immutability remain prerequisites before Stage 5.

Stage 4 GitHub no-write verification (2026-11-03):

- [PR #16](https://github.com/itcorner/MPDRussoundPi/pull/16) passed required CI
  and merged into `develop`. [PR #17](https://github.com/itcorner/MPDRussoundPi/pull/17)
  passed CI and bootstrapped the changes to `main` with a merge commit.
- [Credential check](https://github.com/itcorner/MPDRussoundPi/actions/runs/41731056726)
  succeeded on `main`: the authorization server granted pull/push scopes for
  both intended repositories and the registry accepted authentication.
  No token value was accessed locally or logged, and no upload was attempted.
- [Development build-only verification](https://github.com/itcorner/MPDRussoundPi/actions/runs/41731060294)
  succeeded on `develop` at `6e539a5a7f441a70606fe70ccedf26289340562d`
  with `check_registry=true`. Reusable CI, live inventory, all four image jobs
  and serialized publication planning passed. Tested artifacts were downloaded
  and re-verified, and fresh source/inventory checks passed without registry writes.
- These results supersede the pending GitHub credential/development dry-run
  verification notes above. Stage 4 remains open for actual serialized
  write/recovery implementation and registry immutability review.
  Stage 5 is not authorized; no Docker Hub image or Git version tag was created.

Stage 4 immutability deferral (2026-11-04):

- Selective immutability is available in both Docker Hub repository settings,
  but saving the reviewed regex failed with HTTP 500. Manual attempts also
  failed; both repositories remain mutable. Empty inventories are a possible
  cause, not a verified explanation.
- With user agreement, retry selective immutability after the first separately
  approved development publication. This is a temporary development-only
  exception, not authorization to publish or enable automatic publication.
- Before that first publication, complete serialized write/recovery safeguards,
  reject existing exact tags using fresh inventory and direct tag checks, and
  record expected/observed manifest digests. Use only the trusted publisher and
  avoid concurrent manual or external writes. These controls do not provide
  registry-enforced immutability; deploy by digest for reproducibility.
- Retry immediately after the first exact development tags exist. If saving
  still fails, investigate with Docker support before further publication.
  Stable/candidate publication remains disabled pending resolution or a
  separately reviewed alternative.

Stage 4 local publisher/recovery implementation (2026-11-04):

- Implemented verified OCI blob/manifest uploads and deterministic combined
  AMD64/ARM64 indices preserving SBOM/provenance manifests. Existing tested
  archives are checksummed and verified without rebuilding.
- Both repositories are preflighted before uploads. Direct exact-tag checks,
  full digest read-back, refreshed alias eligibility and source-tip checks
  guard promotion; aliases follow verification of both exact images.
- Expected digests are journaled before writes. Recovery requires matching
  original artifacts/journal, explicit approval and matching existing registry
  digests; completed exact tags are never overwritten. Local process locking
  rejects simultaneous publications to the same endpoint/namespace.
- The write transport is restricted to loopback; the CLI accepts only local-test
  mode. No Docker Hub publisher/recovery workflow or authenticated write
  transport is enabled. These remain separately approved Stage 5 wiring.
- Integration tests use a digest-pinned disposable registry in JavaScript CI.
  They cover actual uploads, attestations, collisions, partial failures,
  approved recovery, digest mismatch, stale tips, concurrency and stable aliases.
  Docker Hub upload compatibility and tag immutability remain untested.
- Local validation passed all 95 JavaScript tests with registry integration
  enabled, including a checksummed-archive CLI publication against real Git
  ancestry and the disposable registry. All workflows passed actionlint.

### Stage 5: Enable development publication (explicit approval required)

- [ ] Enable pushes only for successful `develop` builds.
- [ ] Publish and verify the first approved development image: both platform
      manifests, source revision, digest and pull/run behaviour.
- [ ] Retry selective Docker Hub immutability immediately after the first
      development publication and verify the saved rules on both repositories.
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
