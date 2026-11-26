from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from tool import system_updates as module
from tool.system_updates import (SystemRefused, SystemUpdates, compute_check, last_automatic_run, parse_simulation,
                                 restart_reasons, security_versions)

ACTOR = {"session": "3f467261", "client": "192.0.2.5"}

SIMULATION = """NOTE: This is only a simulation!
Reading package lists...
Inst libc6 [2.41-12+rpt1+deb13u2] (2.41-12+rpt1+deb13u4 Raspberry Pi Foundation:stable [arm64])
Inst openssh-server [1:10.0p1-7+deb13u2] (1:10.0p1-7+deb13u4 Debian:13.8/stable [arm64]) []
Inst docker-ce [5:29.7.2-1~debian.13~trixie] (5:29.10.1-1~debian.13~trixie Docker CE:trixie [arm64])
Inst libcamera0.8 (0.8.0+rpt20261001-1 Raspberry Pi Foundation:stable [arm64])
Inst libasound2t64:armhf [1.2.14-1+rpt1] (1.2.14-1+rpt1+deb13u1 Raspberry Pi Foundation:stable [armhf])
Remv pcmanfm [1.4.0-1+rpt9]
Conf libc6 (2.41-12+rpt1+deb13u4 Raspberry Pi Foundation:stable [arm64])
"""

POLICY = """libc6:
  Installed: 2.41-12+rpt1+deb13u2
  Candidate: 2.41-12+rpt1+deb13u4
  Version table:
     2.41-12+rpt1+deb13u4 500
        500 http://archive.raspberrypi.com/debian trixie/main arm64 Packages
 *** 2.41-12+rpt1+deb13u2 100
        100 /var/lib/dpkg/status
openssh-server:
  Installed: 1:10.0p1-7+deb13u2
  Candidate: 1:10.0p1-7+deb13u4
  Version table:
     1:10.0p1-7+deb13u4 500
        500 http://deb.debian.org/debian trixie/main arm64 Packages
        500 http://deb.debian.org/debian-security trixie-security/main arm64 Packages
 *** 1:10.0p1-7+deb13u2 100
        100 /var/lib/dpkg/status
docker-ce:
  Installed: 5:29.7.2-1~debian.13~trixie
  Candidate: 5:29.10.1-1~debian.13~trixie
  Version table:
     5:29.10.1-1~debian.13~trixie 500
        500 https://download.docker.com/linux/debian trixie/stable arm64 Packages
"""

UNATTENDED = """2026-11-26 06:41:05,100 INFO Starting unattended upgrades script
2026-11-26 06:41:09,200 INFO No packages found that can be upgraded unattended and no pending auto-removals
2026-11-27 06:12:01,300 INFO Starting unattended upgrades script
2026-11-27 06:12:04,400 INFO Allowed origins are: origin=Debian,codename=trixie-security,label=Debian-Security
2026-11-27 06:12:09,500 INFO Packages that will be upgraded: openssh-client openssh-server
2026-11-27 06:13:30,600 INFO All upgrades installed
"""

DOCKER_PS = "russound-web-1\trunning\tUp 5 minutes (healthy)\nrussound-ser2net-1\trunning\tUp 6 minutes (healthy)\n"
UNITS = ("mpdrussoundpi-updater.service loaded active running x\n"
         "mpdrussoundpi-web-proxy.service loaded active running x\ndocker.service loaded active running x\n")


class FakeHost:
    """Answers the fixed commands the system part runs, and records them."""

    def __init__(self):
        self.commands = []
        self.simulation = SIMULATION
        self.docker_ps = DOCKER_PS
        self.units = UNITS
        self.fail = set()
        self.started = threading.Event()
        self.unit_active = False
        self.audit = ""

    def __call__(self, args, timeout):
        self.commands.append(args)
        name = " ".join(args[:2])
        if any(args[0] == failing or name == failing for failing in self.fail):
            return 1, "failed"
        if args[:2] == ["apt-get", "-s"]:
            return 0, self.simulation
        if args[:2] == ["apt-cache", "policy"]:
            return 0, POLICY
        if args[:2] == ["apt-get", "update"]:
            return 0, ""
        if args[0] == "docker":
            return 0, self.docker_ps
        if args[:2] == ["systemctl", "list-units"]:
            return 0, self.units
        if args[:2] == ["systemctl", "is-active"]:
            return (0, "active\n") if self.unit_active else (3, "inactive\n")
        if args[0] == "systemd-run":
            self.unit_active = True  # like systemd: the unit runs until it wrote its exit code
            self.started.set()
            return 0, ""
        if args[:2] == ["dpkg", "--audit"]:
            return 0, self.audit
        return 0, ""


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.evidence = self.root / "evidence"
        self.evidence.mkdir(mode=0o700)
        lists = self.root / "lists"
        lists.mkdir()
        (lists / "deb.debian.org_debian_dists_trixie_InRelease").write_text("x")
        self.boot = time.time() - 3600
        (self.root / "stat").write_text(f"cpu 1 2 3\nbtime {int(self.boot)}\n")
        (self.root / "boot_id").write_text("11111111-2222-3333-4444-555555555555\n")
        (self.root / "dpkg.log").write_text("")
        (self.root / "status").write_text("")
        (self.root / "unattended.log").write_text(UNATTENDED)
        for name in ("lock-frontend", "lock"):
            (self.root / name).write_text("")
        self.paths = {
            "lists": lists, "dpkg_status": self.root / "status", "dpkg_logs": (self.root / "dpkg.log",),
            "reboot_flag": self.root / "reboot-required", "unattended_log": self.root / "unattended.log",
            "boot_id": self.root / "boot_id", "proc_stat": self.root / "stat",
            "dpkg_locks": (self.root / "lock-frontend", self.root / "lock"),
            "operator_lock": self.evidence / "operator.lock",
            "cgroups": self.root / "cgroups",
        }
        self.host = FakeHost()
        self.clear = True
        self.other = False
        self.reboots = []

    def make(self):
        return SystemUpdates(self.evidence, self.root / "system.json", run=self.host, paths=self.paths,
                             sleep=lambda seconds: time.sleep(0.01), deployment_clear=lambda: self.clear,
                             other_busy=lambda: self.other,
                             reboot=lambda: self.reboots.append(True) or (0, ""))

    def wait_for(self, system, *phases, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = system.status()
            if status["phase"] in phases:
                return status
            time.sleep(0.01)
        self.fail(f"phase {system.status()['phase']} never became {phases}")

    def log_upgrade(self, name, moment=None):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment or time.time()))
        with (self.root / "dpkg.log").open("a") as log:
            log.write(f"{stamp} upgrade {name} 1 2\n")


class ParsingTests(Fixture):
    def test_simulation_lists_installs_and_removals(self):
        installs, removals = parse_simulation(SIMULATION)
        self.assertEqual([item["name"] for item in installs],
                         ["libc6", "openssh-server", "docker-ce", "libcamera0.8", "libasound2t64:armhf"])
        self.assertEqual(installs[3]["from"], None, "a new package has no old version")
        self.assertEqual(removals, ["pcmanfm"])

    def test_security_means_the_candidate_is_offered_by_a_security_suite(self):
        found = security_versions(POLICY)
        self.assertEqual(found, {"openssh-server": {"1:10.0p1-7+deb13u4"}},
                         "openssh-server counts although the simulation named Debian stable")

    def test_the_check_counts_and_marks_security(self):
        check = compute_check(self.host, self.paths, time.time)
        self.assertEqual((check["upgrades"], check["new"], check["removals"], check["security"]), (4, 1, 1, 1))
        self.assertEqual([item["name"] for item in check["packages"] if item["security"]], ["openssh-server"])
        self.assertFalse(check["index_stale"])
        self.assertEqual(check["automatic"]["result"], "installed")
        self.assertTrue(check["digest"].startswith("sha256:"))

    def test_a_failed_simulation_is_never_up_to_date(self):
        self.host.fail.add("apt-get -s")
        with self.assertRaises(SystemRefused):
            compute_check(self.host, self.paths, time.time)

    def test_old_lists_are_stale(self):
        old = time.time() - 3 * 86400
        for entry in self.paths["lists"].iterdir():
            os.utime(entry, (old, old))
        self.assertTrue(compute_check(self.host, self.paths, time.time)["index_stale"])

    def test_restart_needed_after_a_listed_package_since_the_boot(self):
        self.assertEqual(restart_reasons(self.paths), [])
        self.log_upgrade("openssh-server:arm64")
        self.log_upgrade("linux-image-rpi-v8:arm64", self.boot - 60)
        self.assertEqual(restart_reasons(self.paths), [], "before the boot or not listed")
        self.log_upgrade("linux-image-rpi-v8:arm64")
        self.log_upgrade("libc6:arm64")
        self.assertEqual(restart_reasons(self.paths), ["updated since the last start: libc6, linux-image-rpi-v8"])
        self.paths["reboot_flag"].write_text("")
        self.assertIn("a package asked for a restart", restart_reasons(self.paths))

    def test_last_automatic_run(self):
        run = last_automatic_run(self.paths)
        self.assertEqual((run["result"], run["packages"]), ("installed", ["openssh-client", "openssh-server"]))
        self.paths["unattended_log"].write_text(UNATTENDED + "2026-11-28 06:00:00,000 INFO Starting unattended upgrades script\n"
                                                "2026-11-28 06:00:05,000 ERROR dpkg returned an error\n")
        self.assertEqual(last_automatic_run(self.paths)["result"], "failed")
        self.paths["unattended_log"].unlink()
        self.assertIsNone(last_automatic_run(self.paths))


class InstallTests(Fixture):
    def finish(self, code):
        self.assertTrue(self.host.started.wait(5))
        operation = self.system.status()["operation"]
        (self.evidence / (operation + ".system.log")).write_text(
            "Setting up foo\nConfiguration file '/etc/avahi/avahi-daemon.conf'\n ==> Keeping old config file as default.\n")
        # Like the unit's script: write, then rename, so the watcher never reads a half-written file.
        temporary = self.evidence / (operation + ".system.exit.tmp")
        temporary.write_text(f"{code}\n")
        temporary.rename(self.evidence / (operation + ".system.exit"))
        self.host.unit_active = False
        return operation

    def review(self):
        self.system = self.make()
        operation = self.system.handle({"op": "system_review", "kind": "install"}, lambda: ACTOR)["operation"]
        status = self.wait_for(self.system, "awaiting_approval")
        return operation, status["review"]

    def test_review_approve_install_and_verify(self):
        operation, review = self.review()
        summary = review["summary"]
        self.assertEqual((summary["upgrades"], summary["security"], summary["removed"]), (4, 1, ["pcmanfm"]))
        self.assertTrue(summary["docker"])
        self.assertIn("libc6", summary["restart_after"])
        self.assertIn("no undo", summary["undo"])
        self.assertFalse(self.host.started.is_set(), "nothing runs before approval")
        with self.assertRaises(SystemRefused):
            self.system.approve(operation, "sha256:other", ACTOR)
        self.system.approve(operation, review["digest"], ACTOR)
        self.finish(0)
        status = self.wait_for(self.system, "installed")
        self.assertEqual(status["result"]["checks"], "passed")
        self.assertEqual(status["result"]["kept_configs"], ["/etc/avahi/avahi-daemon.conf"])
        command = next(args for args in self.host.commands if args[0] == "systemd-run")
        script = command[-1]
        self.assertIn("flock -n -E 75 " + str(self.paths["operator_lock"]), script)
        self.assertIn("--force-confold full-upgrade", script)
        self.assertIn("DEBIAN_FRONTEND=noninteractive", command)
        self.assertEqual(self.system.state["approved_by"], ACTOR)

    def test_containers_that_do_not_come_back_fail_the_checks(self):
        operation, review = self.review()
        self.system.approve(operation, review["digest"], ACTOR)
        self.assertTrue(self.host.started.wait(5))
        self.host.docker_ps = "russound-web-1\trunning\tUp 1 minute (unhealthy)\n"
        clock = [time.time()]
        self.system._clock = lambda: clock[0]
        self.system._sleep = lambda seconds: clock.__setitem__(0, clock[0] + 60)
        self.finish(0)
        status = self.wait_for(self.system, "checks_failed")
        self.assertIn("container russound-web-1", status["error"])
        self.assertIn("container russound-ser2net-1", status["error"])

    def test_a_failed_install_and_an_interrupted_dpkg(self):
        for audit, phase in (("", "install_failed"), ("The following packages are only half configured", "outcome_unknown")):
            with self.subTest(phase=phase):
                self.host = FakeHost()
                self.host.audit = audit
                operation, review = self.review()
                self.system.approve(operation, review["digest"], ACTOR)
                self.finish(100)
                status = self.wait_for(self.system, phase)
                if phase == "outcome_unknown":
                    self.assertIn("dpkg --configure -a", status["error"])

    def test_a_changed_plan_after_the_review_installs_nothing(self):
        operation, review = self.review()
        self.host.simulation = SIMULATION.replace("deb13u4 Raspberry", "deb13u5 Raspberry")
        self.system.approve(operation, review["digest"], ACTOR)
        status = self.wait_for(self.system, "cancelled")
        self.assertIn("changed since the review", status["error"])
        self.assertFalse(self.host.started.is_set())

    def test_nothing_pending_is_up_to_date(self):
        self.host.simulation = "Reading package lists...\n"
        system = self.make()
        system.handle({"op": "system_review", "kind": "install"}, lambda: ACTOR)
        self.wait_for(system, "up_to_date")

    def test_refusals(self):
        system = self.make()
        for setup, message in ((lambda: setattr(self, "other", True), "image update"),
                               (lambda: setattr(self, "clear", False), "unfinished"),
                               (lambda: None, "No restart")):
            self.other, self.clear = False, True
            setup()
            kind = "restart" if message == "No restart" else "install"
            with self.subTest(message=message), self.assertRaisesRegex(SystemRefused, message):
                system.review(kind, ACTOR)
        with self.assertRaisesRegex(SystemRefused, "Unknown"):
            system.review("reboot-now", ACTOR)

    def test_the_operator_lock_and_dpkg_lock_refuse(self):
        import fcntl
        system = self.make()
        fd = os.open(self.paths["operator_lock"], os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with self.assertRaisesRegex(SystemRefused, "Another operator"):
            system.review("install", ACTOR)
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(os.stat(self.paths["operator_lock"]).st_mode & 0o777, 0o600)
        # A POSIX lock held by another process (as apt holds it) is visible through lockf.
        reader, writer = os.pipe()
        pid = os.fork()
        if pid == 0:
            held = os.open(self.paths["dpkg_locks"][0], os.O_RDWR)
            fcntl.lockf(held, fcntl.LOCK_EX)
            os.write(writer, b"x")
            time.sleep(2)
            os._exit(0)
        os.read(reader, 1)
        try:
            with self.assertRaisesRegex(SystemRefused, "being installed"):
                system.review("install", ACTOR)
        finally:
            os.kill(pid, 9)
            os.waitpid(pid, 0)

    def test_a_restart_of_the_updater_during_the_install(self):
        operation, review = self.review()
        self.system.approve(operation, review["digest"], ACTOR)
        self.assertTrue(self.host.started.wait(5))
        self.wait_for(self.system, "installing")
        self.host.unit_active = True
        again = self.make()
        self.assertEqual(again.status()["phase"], "installing", "the unit still runs: keep watching")
        again.resume()
        (self.evidence / (operation + ".system.exit.tmp")).write_text("0\n")
        (self.evidence / (operation + ".system.exit.tmp")).rename(self.evidence / (operation + ".system.exit"))
        self.wait_for(again, "installed")
        self.host.unit_active = False
        state = json.loads((self.root / "system.json").read_text())
        state.update(phase="installing", operation="00000000-0000-0000-0000-000000000000", unit=module.UNIT_PREFIX + "0")
        (self.root / "system.json").write_text(json.dumps(state))
        lost = self.make()
        self.assertEqual(lost.status()["phase"], "outcome_unknown", "no unit and no exit code: unknown")
        self.assertIn("dpkg --audit", lost.status()["error"])

    def test_a_failing_systemctl_during_the_install_is_not_the_end(self):
        # On the Pi, systemctl failed once while dpkg replaced libssl (2026-11-26).
        operation, review = self.review()
        real = self.host.__call__
        calls = {"is-active": 0}

        def flaky(args, timeout):
            if args[:2] == ["systemctl", "is-active"]:
                calls["is-active"] += 1
                if calls["is-active"] <= 3:
                    return 127, "systemctl: error while loading shared libraries: libssl.so.3"
            return real(args, timeout)

        self.system._run = flaky
        self.system.approve(operation, review["digest"], ACTOR)
        self.assertTrue(self.host.started.wait(5))
        deadline = time.monotonic() + 5
        while calls["is-active"] <= 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.system.status()["phase"], "installing", "an unclear answer keeps watching")
        self.finish(0)
        self.wait_for(self.system, "installed")

    def test_the_unit_cgroup_counts_as_running_and_one_inactive_answer_is_not_enough(self):
        operation, review = self.review()
        self.system.approve(operation, review["digest"], ACTOR)
        self.assertTrue(self.host.started.wait(5))
        self.wait_for(self.system, "installing")
        cgroup = self.paths["cgroups"] / (module.UNIT_PREFIX + operation[:8] + ".service")
        cgroup.mkdir(parents=True)
        self.host.unit_active = False  # systemctl says inactive, the cgroup says it runs
        time.sleep(0.3)
        self.assertEqual(self.system.status()["phase"], "installing")
        cgroup.rmdir()
        self.wait_for(self.system, "outcome_unknown")
        self.assertGreaterEqual(sum(1 for c in self.host.commands if c[:2] == ["systemctl", "is-active"]),
                                module.ENDED_CONFIRMATIONS)

    def test_an_install_declared_unknown_too_early_is_watched_again_after_a_restart(self):
        operation, review = self.review()
        self.system.approve(operation, review["digest"], ACTOR)
        self.assertTrue(self.host.started.wait(5))
        self.wait_for(self.system, "installing")
        self.system._stop.set()
        state = json.loads((self.root / "system.json").read_text())
        state.update(phase="outcome_unknown", error="The installation ended without recording its result.")
        (self.root / "system.json").write_text(json.dumps(state))
        self.host.unit_active = True
        again = self.make()
        self.assertEqual(again.status()["phase"], "installing")
        self.assertIsNone(again.status()["error"])
        again.resume()
        temporary = self.evidence / (operation + ".system.exit.tmp")
        temporary.write_text("0\n")
        temporary.rename(self.evidence / (operation + ".system.exit"))
        self.host.unit_active = False
        self.wait_for(again, "installed")

    def test_dpkg_lock_alone_makes_packages_busy(self):
        system = self.make()
        self.assertFalse(system.packages_busy())
        pid = os.fork()
        if pid == 0:
            import fcntl as f
            handle = open(self.paths["dpkg_locks"][1], "r+")
            f.lockf(handle, f.LOCK_EX)
            time.sleep(30)
            os._exit(0)
        try:
            deadline = time.monotonic() + 5
            while not system.packages_busy() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(system.packages_busy())
            self.assertFalse(system.busy(), "the phase alone does not know about it")
        finally:
            os.kill(pid, 9)
            os.waitpid(pid, 0)

    def test_manual_check_is_rate_limited(self):
        system = self.make()
        system.manual_check()
        deadline = time.monotonic() + 5
        while system.check_view()["state"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)
        view = system.check_view()
        self.assertEqual((view["state"], view["upgrades"], view["security"]), ("ready", 4, 1))
        self.assertIn(["apt-get", "update", "-q"], self.host.commands)
        with self.assertRaisesRegex(SystemRefused, "try again in"):
            system.manual_check()
        self.assertGreater(system.check_view()["retry_after_seconds"], 500)

    def test_a_failed_refresh_keeps_the_last_result(self):
        system = self.make()
        system.recompute()
        self.host.fail.add("apt-get update")
        system.manual_check()
        deadline = time.monotonic() + 5
        while system.check_view()["state"] == "checking" and time.monotonic() < deadline:
            time.sleep(0.01)
        view = system.check_view()
        self.assertEqual((view["state"], view["upgrades"]), ("failed", 4))
        self.assertIn("last known", view["error"])


class RestartTests(Fixture):
    def test_restart_is_offered_only_when_needed_and_verified_after_the_boot(self):
        self.log_upgrade("linux-image-rpi-v8:arm64")
        system = self.make()
        self.assertTrue(system.check_view()["restart"]["needed"])
        operation = system.review("restart", ACTOR)["operation"]
        review = self.wait_for(system, "awaiting_approval")["review"]
        self.assertIn("linux-image-rpi-v8", review["summary"]["reasons"][0])
        system.approve(operation, review["digest"], ACTOR)
        self.wait_for(system, "rebooting")
        deadline = time.monotonic() + 5
        while not self.reboots and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.reboots, [True])
        # The updater starts again after the boot with a new boot ID.
        (self.root / "boot_id").write_text("99999999-2222-3333-4444-555555555555\n")
        (self.root / "stat").write_text(f"btime {int(time.time()) + 1}\n")
        after = self.make()
        after.resume()
        status = self.wait_for(after, "restarted")
        self.assertEqual(status["result"]["checks"], "passed")
        self.assertFalse(status["result"]["restart_needed"], "the kernel update was before this boot")

    def test_no_new_boot_is_a_failed_restart(self):
        self.log_upgrade("libc6:arm64")
        system = self.make()
        operation = system.review("restart", ACTOR)["operation"]
        review = self.wait_for(system, "awaiting_approval")["review"]
        system.approve(operation, review["digest"], ACTOR)
        self.wait_for(system, "rebooting")
        state = json.loads((self.root / "system.json").read_text())
        state["reboot_requested_at"] = time.time() - module.REBOOT_SECONDS - 1
        (self.root / "system.json").write_text(json.dumps(state))
        after = self.make()
        after.resume()
        self.assertIn("did not restart", self.wait_for(after, "restart_failed")["error"])


if __name__ == "__main__":
    unittest.main()
