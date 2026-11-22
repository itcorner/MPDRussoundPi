from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tool import deployment_cleanup as cleanup_module
from tool.deployment_cleanup import CleanupError, after_update, cleanup, keep_set, parse_size, plan, summary


def digest(char: str) -> str:
    return "sha256:" + char * 64


CURRENT_WEB, CURRENT_SER2NET = digest("a"), digest("b")
PREVIOUS_WEB, PREVIOUS_SER2NET = digest("c"), digest("d")
OLD_WEB, OLD_SER2NET, OLD_LOCAL, SHARED, STOPPED = digest("e"), digest("f"), digest("1"), digest("2"), digest("3")
FOREIGN = digest("4")
BACKUP = "33548cc0-a2d4-4257-93f8-76182169d225"


def entry(repository, tag, image, size="215MB"):
    return {"Repository": repository, "Tag": tag, "ID": image, "Size": size}


IMAGES = [
    entry("itcornernet/mpdrussoundpi-web", "<none>", CURRENT_WEB),
    entry("itcornernet/mpdrussoundpi-ser2net", "<none>", CURRENT_SER2NET, "195MB"),
    entry("mpdrussoundpi-recovery/web", BACKUP, PREVIOUS_WEB),
    entry("mpdrussoundpi-recovery/ser2net", BACKUP, PREVIOUS_SER2NET, "195MB"),
    entry("mpdrussoundpi-recovery/web", "13ef37df-0d60-4c5f-ac6d-5760eeca6b90", OLD_WEB),
    entry("itcornernet/mpdrussoundpi-web", "<none>", OLD_WEB),
    entry("mpdrussoundpi-recovery/ser2net", "13ef37df-0d60-4c5f-ac6d-5760eeca6b90", OLD_SER2NET, "195MB"),
    entry("mpdrussoundpi-local/web", "05ec6486-f224-4482-93c0-808b0ac84ec7", OLD_LOCAL, "212MB"),
    entry("mpdrussoundpi-local/web", "75267e59-bedc-4193-bd2a-ba033717519a", OLD_LOCAL, "212MB"),
    entry("mpdrussoundpi-local/ser2net", "x", SHARED, "192MB"),
    entry("mpdrussoundpi-ser2net", "latest", SHARED, "192MB"),
    entry("mpdrussoundpi-recovery/web", "old-but-stopped", STOPPED),
    entry("python", "3.12-slim", FOREIGN, "205MB"),
    entry("<none>", "<none>", digest("5")),
]


def status(**overrides):
    record = {"committed": {"selection": {
        "backup": BACKUP,
        "candidate": {"images": {"web": {"index_digest": digest("9"), "platform_digest": digest("8"),
                                         "config_digest": CURRENT_WEB},
                                 "ser2net": {"index_digest": digest("7"), "platform_digest": digest("6"),
                                             "config_digest": CURRENT_SER2NET}}}}},
        "pending": None}
    record.update(overrides.pop("record", {}))
    return {"schema_version": 1, "blocked": False, "reservation": None,
            "deployments": {"russound-dev": record}, **overrides}


class FakeDocker:
    def __init__(self, images=IMAGES, used=(CURRENT_WEB, CURRENT_SER2NET, STOPPED), fail=()):
        self.images, self.used, self.fail = list(images), list(used), set(fail)
        self.calls: list[list[str]] = []
        self.size = 1_486_000_000

    def __call__(self, args):
        self.calls.append(args)
        assert "--force" not in args and "-f" not in args and "prune" not in args
        if args[:2] == ["image", "ls"]:
            return "\n".join(json.dumps(item) for item in self.images) + "\n"
        if args[:2] == ["container", "ls"]:
            return "\n".join(f"c{index}" for index in range(len(self.used)))
        if args[:2] == ["container", "inspect"]:
            return "\n".join(self.used) + "\n"
        if args[:2] == ["system", "df"]:
            return json.dumps({"Type": "Images", "Size": f"{self.size / 1e9:.3f}GB"}) + "\n"
        if args[:2] == ["image", "rm"]:
            if args[2] in self.fail:
                raise CleanupError("conflict: unable to remove repository reference")
            self.size -= 100_000_000
            return "Untagged: " + args[2]
        raise AssertionError(args)

    def removed(self):
        return [args[2] for args in self.calls if args[:2] == ["image", "rm"]]


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assets = Path(directory.name)
        (self.assets / BACKUP).mkdir()
        (self.assets / BACKUP / "images.json").write_text(json.dumps({
            "web": {"reference": PREVIOUS_WEB, "config": PREVIOUS_WEB, "tag": f"mpdrussoundpi-recovery/web:{BACKUP}"},
            "ser2net": {"reference": PREVIOUS_SER2NET, "config": PREVIOUS_SER2NET}}))


class KeepSetTests(Fixture):
    def test_current_and_previous_versions_are_kept_with_reasons(self):
        keep = keep_set(status(), self.assets)
        self.assertEqual(keep[CURRENT_WEB], "committed version of russound-dev")
        self.assertEqual(keep[digest("9")], "committed version of russound-dev", "index digest too")
        self.assertEqual(keep[PREVIOUS_SER2NET], "previous version of russound-dev")

    def test_unsafe_states_remove_nothing(self):
        for state in (status(blocked=True), status(reservation={"operation": "x"}),
                      status(record={"pending": {"backup": "x"}}), status(deployments={}),
                      status(record={"committed": None}), status(record={"committed": {"selection": {"candidate": {}}}})):
            with self.subTest(state=state), self.assertRaises(CleanupError):
                keep_set(state, self.assets)
        docker = FakeDocker()
        with self.assertRaises(CleanupError):
            cleanup(docker, status(blocked=True), self.assets, apply=True)
        self.assertEqual(docker.calls, [], "the state is checked before Docker is even asked")

    def test_a_missing_previous_backup_keeps_only_the_current_version(self):
        keep = keep_set(status(record={"committed": {"selection": {**status()["deployments"]["russound-dev"]
                                                                   ["committed"]["selection"], "backup": None}}}),
                        self.assets)
        self.assertNotIn(PREVIOUS_WEB, keep)
        self.assertIn(CURRENT_WEB, keep)

    def test_local_candidates_keep_their_loaded_images(self):
        local = {"committed": {"selection": {"backup": None, "candidate": {"kind": "local"},
                                             "loaded_images": {"web": OLD_LOCAL}}}}
        self.assertEqual(keep_set(status(record=local), self.assets), {OLD_LOCAL: "committed version of russound-dev"})


class PlanTests(Fixture):
    def test_only_unneeded_project_images_are_removed(self):
        decided = plan(IMAGES, {CURRENT_WEB, CURRENT_SER2NET, STOPPED}, keep_set(status(), self.assets))
        removed = {item["image"]: item for item in decided["remove"]}
        self.assertEqual(set(removed), {OLD_WEB, OLD_SER2NET, OLD_LOCAL, SHARED})
        self.assertEqual(removed[OLD_WEB]["tags"], ["mpdrussoundpi-recovery/web:13ef37df-0d60-4c5f-ac6d-5760eeca6b90"])
        self.assertTrue(removed[OLD_WEB]["by_id"], "the untagged registry reference goes with the ID")
        self.assertFalse(removed[SHARED]["by_id"], "an image also tagged outside the project keeps existing")
        kept = {item["image"]: item["reason"] for item in decided["kept"]}
        self.assertEqual(kept[STOPPED], "used by a container")
        self.assertEqual(kept[PREVIOUS_WEB], "previous version of russound-dev")
        self.assertEqual(decided["outside"], ["mpdrussoundpi-ser2net:latest", "python:3.12-slim"])
        self.assertNotIn(FOREIGN, removed)

    def test_sizes_are_decimal(self):
        self.assertEqual((parse_size("215MB"), parse_size("1.486GB"), parse_size("0B"), parse_size("12.5kB"),
                          parse_size("N/A")), (215_000_000, 1_486_000_000, 0, 12_500, 0))


class CleanupTests(Fixture):
    def test_dry_run_removes_nothing(self):
        docker = FakeDocker()
        report = cleanup(docker, status(), self.assets, apply=False)
        self.assertEqual(docker.removed(), [])
        self.assertEqual(len(report["remove"]), 4)
        self.assertEqual(report["estimated_bytes"], 215_000_000 + 195_000_000 + 212_000_000)

    def test_apply_removes_tags_then_the_image_and_reports(self):
        docker = FakeDocker(fail={"mpdrussoundpi-local/web:75267e59-bedc-4193-bd2a-ba033717519a"})
        report = cleanup(docker, status(), self.assets, apply=True)
        removed = docker.removed()
        self.assertLess(removed.index("mpdrussoundpi-recovery/web:13ef37df-0d60-4c5f-ac6d-5760eeca6b90"),
                        removed.index(OLD_WEB), "tags first, the ID last")
        self.assertIn("mpdrussoundpi-local/ser2net:x", removed)
        self.assertNotIn(SHARED, removed, "never by ID when something outside the project refers to it")
        self.assertFalse(set(removed) & {CURRENT_WEB, CURRENT_SER2NET, PREVIOUS_WEB, PREVIOUS_SER2NET, STOPPED, FOREIGN})
        self.assertEqual(report["removed"], 4)
        self.assertEqual(report["skipped"], [{"reference": "mpdrussoundpi-local/web:75267e59-bedc-4193-bd2a-ba033717519a",
                                              "error": "conflict: unable to remove repository reference"}])
        self.assertEqual(report["freed_bytes"], (len(removed) - 1) * 100_000_000, "the refused removal frees nothing")
        self.assertEqual(summary(report), {"removed": 4, "skipped": 1, "freed_bytes": report["freed_bytes"], "error": None})
        self.assertIn("Removed 4 image(s)", cleanup_module.render(report))


class AfterUpdateTests(Fixture):
    def test_never_raises_and_reports_the_reason(self):
        class Store:
            def status(self):
                return status(blocked=True)

        class Target:
            docker, env, store = ["docker"], {}, Store()
            storage = {"assets": self.assets}

        result = after_update(Target())
        self.assertEqual((result["removed"], result["freed_bytes"]), (0, 0))
        self.assertIn("blocked", result["error"])

    def test_runs_the_target_docker_and_summarizes(self):
        docker = FakeDocker()

        class Store:
            def status(self):
                return status()

        class Target:
            docker_cmd = None
            store = Store()
            storage = {"assets": self.assets}
            docker, env = ["docker", "--context", "default"], {}

        original = cleanup_module.docker_runner
        seen = []

        def runner(command, env):
            seen.append(command)
            return docker
        cleanup_module.docker_runner = runner
        try:
            result = after_update(Target())
        finally:
            cleanup_module.docker_runner = original
        self.assertEqual(seen, [["docker", "--context", "default"]])
        self.assertEqual(result["removed"], 4)
        self.assertIsNone(result["error"])


if __name__ == "__main__":
    unittest.main()
