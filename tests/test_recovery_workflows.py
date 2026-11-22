"""The recovery workflows smoke-test images with the scripts of the commit they were built from."""

from __future__ import annotations

from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SMOKE = "tool/ci/smoke-web-image.sh tool/ci/smoke-ser2net-image.sh"


class RecoveryWorkflowTests(unittest.TestCase):
    def test_smoke_scripts_come_from_the_source_commit_in_a_step_without_secrets(self):
        for name in ("recover-development.yml", "verify-development-publication.yml"):
            with self.subTest(workflow=name):
                workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
                steps = next(iter(workflow["jobs"].values()))["steps"]
                checkout = steps[0]
                self.assertEqual(checkout["with"].get("fetch-depth"), 0, "the source commit must be in the clone")
                runs = [step.get("run", "") for step in steps]
                source = next(index for index, run in enumerate(runs) if SMOKE in run)
                verify = next(index for index, run in enumerate(runs) if "verify-published-images.mjs" in run)
                self.assertLess(source, verify)
                step = steps[source]
                self.assertEqual(step["run"], f'git checkout "$PUBLICATION_SOURCE_SHA" -- {SMOKE}')
                self.assertEqual(step["env"], {"PUBLICATION_SOURCE_SHA": "${{ steps.source.outputs.sha }}"})
                self.assertNotIn("secrets.", str(steps[verify]) + str(step))


if __name__ == "__main__":
    unittest.main()
