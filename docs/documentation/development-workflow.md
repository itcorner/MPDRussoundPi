# Development and release workflow

## Branches

- `main` is the default branch and the target for stable, release-ready code.
  Creating the development branch does not certify the existing baseline as
  release-qualified.
- `develop` contains integrated development work.
- `feature/*` and `fix/*` are short-lived branches, normally based on `develop`.

Use pull requests rather than committing or pushing directly to either
long-lived branch. Active GitHub rules require PRs, a successful **CI passed**
check from GitHub Actions, validation against the current target branch, and
resolved review conversations on both `main` and `develop`. Direct pushes,
force pushes and branch deletion are blocked, with no administrator or bot
bypass. If the target branch changes, update the PR branch and let CI run again.

No approving review is required for the current solo-maintainer setup; you can
inspect and merge your own PR after checks pass. This does not waive the PR or
CI requirement. The [branch ruleset](https://github.com/itcorner/MPDRussoundPi/rules/25892348)
is maintained in GitHub settings, not by the CI workflow.

## Starting development

A normal clone checks out `main`. Select `develop` explicitly for development:

```bash
git fetch origin
git switch develop
git pull --ff-only
git switch -c feature/my-change
```

If the clone has no local `develop` branch, use
`git switch --track origin/develop` instead of `git switch develop`.

Commit only files belonging to the change, run the relevant tests from the
[README](../../README.md#testing), and publish the feature branch:

```bash
git push --set-upstream origin feature/my-change
```

Open a PR with **base `develop`**, not the default `main`. Feature PRs may be
squash-merged. After merging, return to `develop`, update with
`git pull --ff-only`, and start the next feature branch.

## Automated validation

The [CI workflow](../../.github/workflows/ci.yml) runs on PRs targeting
`develop` or `main`, pushes to either branch, and manual dispatch. It calls
[reusable validation](../../.github/workflows/validation.yml), which runs:

- The existing Python test suite on Python 3.12 and JavaScript tests on Node.js 22.
- Separate clean builds of the web image for `linux/amd64` and `linux/arm64`.
  ARM64 is executed using QEMU on the AMD64 GitHub runner.
- The [image smoke test](../../tool/ci/smoke-web-image.sh) on each built image:
  non-root runtime, expected architecture/Python version, writable empty
  volumes, successful healthcheck, UI/static content, initial setup API,
  rejection of invalid setup, and offline configuration save/persistence.

The final **CI passed** check fails unless every validation job succeeds.
Branch rules require this verified check name, bound to the GitHub Actions
integration. Validation jobs have bounded timeouts.

Smoke containers have no external network, published host ports or host mounts.
An exit trap removes their anonymous configuration, state and log volumes on
success or failure, and prints container logs/state on failure. Runner teardown
also isolates cancellation from the live deployment. These tests do not verify
physical Russound connectivity or audio.

To repeat a container check locally (Docker and architecture emulation required):

```bash
docker buildx build --platform linux/arm64 --no-cache --load \
  -t mpdrussoundpi-web:ci-arm64 .
bash tool/ci/smoke-web-image.sh mpdrussoundpi-web:ci-arm64 arm64
```

Use `amd64` in place of `arm64` to check the other platform. Local validation
images are not pushed. The workflows contain no registry login or publication
job, require no Docker Hub credentials, and use read-only repository permissions.
Third-party Actions are pinned to commit SHAs and checkout does not persist
credentials. There are no tag-triggered release workflows yet.

## Promoting a release

1. Integrate and validate changes on `develop`.
2. Open a release PR with **base `main`, compare `develop`**.
3. Complete automated checks when available and relevant manual acceptance.
4. Merge using a **merge commit**, preserving shared ancestry between the
   long-lived branches rather than squash-merging the entire release.
5. Merge `main` back into `develop` through a PR when needed to bring the
   branches back together, particularly after release-only changes.

Version tagging and Docker Hub publication are not enabled yet. Future stable
tags will identify validated commits on `main`; development images will come
from `develop`. Creating or merging a PR does not itself authorize a release.

Active rules restrict `v*` tag creation to repository administrators. A separate
ruleset blocks updating or deleting those tags with no bypass, including for
administrators. The administrator exception applies only to tag creation, never
branch protection or tag replacement. Do not create a tag to test these rules.
These rules protect refs; SemVer and source-branch validation belong to the
future release pipeline and are not implemented yet.

## Urgent stable hotfix

1. Update local `main` from `origin/main` and branch to `fix/<topic>`.
2. Make the smallest required change and open a PR targeting `main`.
3. Validate and merge the fix; a patch release requires separate approval
   until the release pipeline is established.
4. Open a PR from `main` into `develop` and merge it so ongoing development
   retains the fix.

## Deployment remains explicit

Switching branches does not update running containers or Pi services.
Local Compose builds use the checked-out code when explicitly rebuilt.
Ansible web deployment playbooks retain `repo_version: main`; source-based
test deployments can explicitly pass `-e repo_version=develop`. Use an exact
release tag or commit for repeatable production deployments once validated.

See the [CI/CD and Docker release plan](../plans/general_open_cicd-docker-release.md)
for the remaining approval-gated stages. No Docker image publication is
authorized at this stage.
