"""The release publication gates in the images workflow (docs/plans/general_new_release-channels.md, stage 1)."""

from __future__ import annotations

from pathlib import Path
import unittest

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/images-build-only.yml"


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(WORKFLOW.read_text())
        self.jobs = self.workflow["jobs"]

    def test_version_tags_trigger_preparation_with_the_channel_from_the_tag(self):
        push = self.workflow[True]["push"]  # YAML 1.1 reads the key "on" as True
        self.assertEqual((push["branches"], push["tags"]), (["develop"], ["v*"]))
        policy = next(step for step in self.jobs["policy"]["steps"] if step.get("id") == "policy")
        self.assertIn("'prepare-release'", policy["env"]["MODE"])
        self.assertEqual(self.jobs["policy"]["outputs"]["channel"], "${{ steps.policy.outputs.channel }}")
        decide = next(step for step in self.jobs["changes"]["steps"] if step.get("id") == "decide")
        self.assertIn('if [ "$REF_TYPE" = tag ]', decide["run"], "a release always builds both images")

    def test_release_publication_needs_the_variable_the_environment_and_a_reviewer(self):
        job = self.jobs["publish-release"]
        self.assertIn("vars.RELEASE_PUBLICATION_ENABLED == 'true'", job["if"])
        self.assertIn("github.ref_type == 'tag'", job["if"])
        self.assertEqual(job["environment"], "release")
        self.assertEqual(job["concurrency"]["group"], self.jobs["publish-development"]["concurrency"]["group"],
                         "one publication at a time across all channels")
        names = [step.get("name", step.get("uses", "")) for step in job["steps"]]
        check = names.index("Require a reviewer on the release environment")
        publish = names.index("Publish verified release images with mandatory fresh inventory")
        self.assertLess(check, publish)
        step = job["steps"][publish]
        self.assertEqual((step["env"]["MODE"], step["env"]["RECOVERY_APPROVED"]), ("publish-release", "false"))
        self.assertEqual(step["env"]["CHANNEL"], "${{ needs.policy.outputs.channel }}")
        with_secret = [step.get("name") for step in job["steps"] if "secrets." in str(step)]
        self.assertEqual(with_secret, ["Publish verified release images with mandatory fresh inventory"])

    def test_development_publication_never_runs_for_tags(self):
        self.assertIn("github.ref == 'refs/heads/develop'", self.jobs["publish-development"]["if"])
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})


if __name__ == "__main__":
    unittest.main()
