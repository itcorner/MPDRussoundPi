import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from uuid import uuid4

from tool.deployment_images import DIGEST


ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker")
ENABLED = os.environ.get("RUSSOUND_RUN_CONTAINER_TESTS") == "1"


@unittest.skipUnless(DOCKER and ENABLED, "opt in with RUSSOUND_RUN_CONTAINER_TESTS=1 on local Docker")
class SplitRuntimeTests(unittest.TestCase):
    def test_separate_networks_use_published_gateway_and_authenticated_verified_agent_tls(self):
        self.assertNotIn("DOCKER_HOST", os.environ)
        self.assertNotIn("DOCKER_CONTEXT", os.environ)
        image = os.environ["RUSSOUND_TEST_WEB_IMAGE"]
        self.assertTrue(DIGEST.fullmatch(image.split("@")[-1]), "Use an exact cached image digest")
        context = json.loads(subprocess.check_output([DOCKER, "context", "inspect"], text=True))[0]
        self.assertTrue(context["Endpoints"]["docker"]["Host"].startswith(("unix://", "npipe://")))
        project = "split-test-" + uuid4().hex[:12]
        network = project + "-backend"
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("COMPOSE_", "RUSSOUND_"))}
        environment["COMPOSE_DISABLE_ENV_FILE"] = "1"

        def run(arguments, *, timeout=100):
            result = subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=timeout)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout.strip()

        run([DOCKER, "network", "create", network])
        try:
            address = json.loads(run([DOCKER, "network", "inspect", network]))[0]["IPAM"]["Config"][0]["Gateway"]
            desktop = "Docker Desktop" in run([DOCKER, "info", "--format", "{{.OperatingSystem}}"])
            listener = "127.0.0.1" if desktop else address
            endpoint = "host.docker.internal" if desktop else address
            with tempfile.TemporaryDirectory(prefix="russound-split-runtime-") as directory:
                root = Path(directory).resolve()
                for folder in ("config", "data", "logs", "secrets"):
                    (root / folder).mkdir()
                token = root / "secrets/token"
                token.write_text("synthetic-split-test-token-" + "a" * 32)
                token.chmod(0o600)
                certificate, key = root / "secrets/ca.crt", root / "agent.key"
                run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                     "-subj", "/CN=split-test", "-addext",
                     "subjectAltName=" + ("DNS:" if desktop else "IP:") + endpoint,
                     "-keyout", str(key), "-out", str(certificate)])
                config = {
                    "controllers": [{"id": 1, "zone_count": 2}],
                    "zones": [{"id": "test", "name": "Synthetic room", "controller": 1, "zone": 1}],
                    "inputs": [{"id": 1, "name": "Synthetic source"}],
                    "protocol_audit": {"enabled": False},
                }
                config_path = root / "config/russound_config.json"
                config_path.write_text(json.dumps(config))
                original_config, original_token = config_path.read_bytes(), token.read_bytes()
                runtime = {
                    "image": image, "user": f"{os.getuid()}:{os.getgid()}",
                    "command": [],
                    "working_dir": "/workspace",
                    "volumes": [
                        {"type": "bind", "source": str(ROOT), "target": "/workspace", "read_only": True},
                        {"type": "bind", "source": str(root), "target": "/fixture", "read_only": True},
                    ],
                    "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
                }
                agent_script = """
import http.server,json,ssl
from pathlib import Path
fixtures=json.loads(Path('/workspace/tests/fixtures/playback-read-v1.json').read_text())
token=Path('/fixture/secrets/token').read_text()
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  if self.headers.get('Authorization') != 'Bearer '+token:
   self.send_response(401); self.end_headers(); return
  name={'/v1/health':'health','/v1/capabilities':'capabilities','/v1/outputs':'healthy_inventory'}.get(self.path)
  if name is None:
   self.send_response(404); self.end_headers(); return
  raw=json.dumps(fixtures[name]).encode()
  self.send_response(200); self.send_header('Content-Type','application/json')
  self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
server=http.server.ThreadingHTTPServer(('0.0.0.0',8443),Handler)
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/fixture/secrets/ca.crt','/fixture/agent.key')
server.socket=context.wrap_socket(server.socket,server_side=True)
server.serve_forever()
"""
                backend_file, web_file = root / "backend.json", root / "web.json"
                backend_file.write_text(json.dumps({
                    "name": project + "-backend", "networks": {"default": {"external": True, "name": network}},
                    "services": {
                        "gateway": {**runtime, "entrypoint": [
                            "python", "-m", "tool.dummy_backend.dummy_backend", "--serve",
                            "--host", "0.0.0.0", "--port", "6666", "--state", "/tmp/dummy.json"],
                            "ports": [{"host_ip": listener, "target": 6666, "published": "0"}]},
                        "agent": {**runtime, "entrypoint": ["python", "-c", agent_script],
                                  "ports": [{"host_ip": listener, "target": 8443, "published": "0"}]},
                    },
                }))
                backend = [DOCKER, "compose", "--env-file", "/dev/null", "-f", str(backend_file)]
                web = [DOCKER, "compose", "--env-file", "/dev/null", "-f", str(web_file)]
                try:
                    run(backend + ["up", "-d", "--no-build", "--pull", "never"])
                    gateway_id = run(backend + ["ps", "-q", "gateway"])
                    agent_id = run(backend + ["ps", "-q", "agent"])
                    def published(container, port):
                        inspected = json.loads(run([DOCKER, "inspect", container]))[0]
                        self.assertTrue(inspected["State"]["Running"], run(backend + ["logs", "--no-color"]))
                        bindings = inspected["NetworkSettings"]["Ports"][str(port) + "/tcp"]
                        self.assertEqual(len(bindings), 1, {
                            "bindings": inspected["HostConfig"]["PortBindings"],
                            "networks": inspected["NetworkSettings"]["Networks"],
                        })
                        self.assertEqual(bindings[0]["HostIp"], listener)
                        value = bindings[0]["HostPort"]
                        self.assertTrue(value.isdecimal() and 1 <= int(value) <= 65535, bindings)
                        return value
                    gateway_port, agent_port = published(gateway_id, 6666), published(agent_id, 8443)
                    agent_url = f"https://{endpoint}:{agent_port}"
                    web_file.write_text(json.dumps({
                        "name": project + "-web", "services": {"russound-web": {
                            **runtime,
                            "entrypoint": ["python", "-m", "web.russound_server"],
                            "volumes": runtime["volumes"][:1] + [
                                {"type": "bind", "source": str(root / folder), "target": target,
                                 "read_only": folder == "secrets"}
                                for folder, target in (("config", "/config"), ("data", "/data"),
                                                       ("logs", "/logs"), ("secrets", "/run/secrets"))
                            ],
                            "environment": {
                                "PYTHONDONTWRITEBYTECODE": "1", "RUSSOUND_WEB_HOST": "0.0.0.0",
                                "RUSSOUND_CONFIG": "/config/russound_config.json",
                                "RUSSOUND_STATE": "/data/russound_state.json",
                                "RUSSOUND_BACKEND_HOST": endpoint, "RUSSOUND_BACKEND_PORT": gateway_port,
                                "RUSSOUND_PLAYBACK_AGENTS": json.dumps([{
                                    "agent_id": "musicpi", "url": agent_url,
                                    "token_file": "/run/secrets/token", "tls_ca_file": "/run/secrets/ca.crt",
                                }]),
                            },
                        }},
                    }))
                    run(web + ["up", "-d", "--no-build", "--pull", "never"])
                    web_id = run(web + ["ps", "-q", "russound-web"])
                    inspected = json.loads(run([DOCKER, "inspect", web_id, gateway_id, agent_id]))
                    self.assertTrue(set(inspected[0]["NetworkSettings"]["Networks"]).isdisjoint(
                        inspected[1]["NetworkSettings"]["Networks"]))
                    probe = (
                        "import json,urllib.request; "
                        "r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/rooms')); "
                        "p=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/playback/health')); "
                        "assert r.get('backend_status',{}).get('connected') and p.get('status')=='ok'"
                    )

                    def wait_ready(container):
                        deadline = time.monotonic() + 40
                        while time.monotonic() < deadline:
                            result = subprocess.run([DOCKER, "exec", container, "python", "-c", probe],
                                                    env=environment, capture_output=True, text=True, timeout=15)
                            if result.returncode == 0:
                                return
                            state = json.loads(run([DOCKER, "inspect", container]))[0]["State"]
                            if not state["Running"]:
                                self.fail("Synthetic web exited: " + run(web + ["logs", "--no-color"]))
                            time.sleep(0.5)
                        self.fail("Synthetic split-host routing failed: " + result.stderr
                                  + run(web + ["logs", "--no-color"])
                                  + run(backend + ["logs", "--no-color"]))

                    wait_ready(web_id)
                    negative = """
import socket,ssl,sys,urllib.request,urllib.error,urllib.parse
url=sys.argv[1]+'/v1/health'
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
try: opener.open(url,timeout=5)
except urllib.error.URLError as error: assert isinstance(error.reason,ssl.SSLCertVerificationError)
else: raise AssertionError('untrusted certificate was accepted')
context=ssl.create_default_context(cafile='/run/secrets/ca.crt')
parts=urllib.parse.urlsplit(url)
with socket.create_connection((parts.hostname,parts.port),timeout=5) as raw:
 try: context.wrap_socket(raw,server_hostname='wrong.invalid')
 except ssl.SSLCertVerificationError: pass
 else: raise AssertionError('certificate hostname mismatch was accepted')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
try: opener.open(url,timeout=5)
except urllib.error.HTTPError as error: assert error.code==401
else: raise AssertionError('missing authentication was accepted')
"""
                    run([DOCKER, "exec", web_id, "python", "-c", negative, agent_url])
                    run(web + ["up", "-d", "--no-build", "--pull", "never"])
                    self.assertEqual(run(web + ["ps", "-q", "russound-web"]), web_id)
                    run(web + ["up", "-d", "--no-build", "--pull", "never", "--force-recreate"])
                    recreated = run(web + ["ps", "-q", "russound-web"])
                    self.assertNotEqual(recreated, web_id)
                    wait_ready(recreated)
                    self.assertEqual(run(backend + ["ps", "-q", "gateway"]), gateway_id)
                    self.assertEqual(run(backend + ["ps", "-q", "agent"]), agent_id)
                    self.assertEqual(config_path.read_bytes(), original_config)
                    self.assertEqual(token.read_bytes(), original_token)
                finally:
                    if web_file.exists():
                        run(web + ["down", "--volumes", "--timeout", "10"])
                    run(backend + ["down", "--volumes", "--timeout", "10"])
        finally:
            run([DOCKER, "network", "rm", network])
