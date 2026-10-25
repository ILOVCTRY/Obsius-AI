import json
import subprocess
import unittest
from unittest.mock import Mock

from core.intel.searxng import SearXNG


class SearXNGTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.missing = False
        def runner(args, **kwargs):
            self.calls.append(args)
            output = ""
            if args[1:3] == ["image", "inspect"]:
                output = "present"
            elif args[1:2] == ["inspect"]:
                output = "" if self.missing else "running"
            elif args[1:2] == ["run"]:
                output = "container-id"
            return subprocess.CompletedProcess(args, 0, output, "")
        self.service = SearXNG({"image": "image", "container": "box"}, runner=runner,
                               opener=lambda *a, **k: Mock(status=200))

    def test_running_container_is_reused(self):
        self.assertEqual(self.service.start(), {"status": "running", "reused": True})
        self.assertFalse(any(call[1:2] == ["run"] for call in self.calls))

    def test_missing_docker_cli_is_reported_as_unavailable(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError("docker")
        self.service.runner = missing
        status = self.service.status()
        self.assertFalse(status["docker"])
        self.assertEqual(self.service.start()["status"], "error")

    def test_connection_probe_does_not_search(self):
        result = self.service.connection_test()
        self.assertTrue(result["ok"])
        self.assertNotIn("/search", str(result))

    def test_missing_image_does_not_pull(self):
        self.missing = True
        def missing_runner(args, **kw):
            if args[1:3] == ["image", "inspect"]:
                return subprocess.CompletedProcess(args, 1, "", "")
            if args[1:2] == ["inspect"]:
                return subprocess.CompletedProcess(args, 1, "", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        self.service.runner = missing_runner
        result = self.service.start()
        self.assertEqual(result["status"], "error")
        self.assertNotIn("pull", str(self.calls))

    def test_settings_use_relay_hostname_and_do_not_expose_host_loopback(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            self.service.config.update({
                "settings_path": str(Path(directory) / "settings.yml"),
                "upstream_proxy": "http://127.0.0.1:7890",
                "relay_port": 7891,
            })
            password = __import__("core.intel.searxng", fromlist=["_relay_password"])._relay_password(
                self.service.config)
            self.service.config["relay_auth_token"] = f"cyberstrike:{password}"
            path = self.service._write_settings()
            settings = json.loads(Path(path).read_text(encoding="utf-8"))
            proxy = settings["outgoing"]["proxies"]["all://"][0]
            encoded = __import__("base64").b64encode(
                f"cyberstrike:{password}".encode("ascii")).decode("ascii")
            self.assertTrue(proxy.startswith(f"http://{encoded}@host.docker.internal:7891"))
            self.assertNotIn("127.0.0.1", proxy)
            secret = settings["server"]["secret_key"]
            again = json.loads(Path(self.service._write_settings()).read_text(encoding="utf-8"))
            self.assertEqual(again["server"]["secret_key"], secret)

    def test_start_publishes_local_service_and_authenticated_relay(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        self.missing = True
        with TemporaryDirectory() as directory:
            self.service.config["settings_path"] = str(Path(directory) / "settings.yml")
            self.service._start_relay = lambda: True
            result = self.service.start()
            self.assertEqual(result["status"], "started")
            command = self.calls[-1]
            self.assertIn("127.0.0.1:8888:8080", command)
            self.assertNotIn("-p", command[command.index("-p") + 2:])
            self.assertIn("-v", command)
            self.assertIn("/etc/searxng/settings.yml:ro", command[-2])
            self.assertFalse(any(arg == "pull" for arg in command))

    def test_relay_forwards_bytes_to_upstream(self):
        import base64
        import socket
        import threading
        from contextlib import closing

        with closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            upstream_port = probe.getsockname()[1]
        received = []
        upstream = socket.socket()
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream.bind(("127.0.0.1", upstream_port))
        upstream.listen(1)
        def accept_and_echo():
            conn, _ = upstream.accept()
            data = conn.recv(64)
            received.append(data)
            conn.sendall(data)
            conn.close()
        worker = threading.Thread(target=accept_and_echo, daemon=True)
        worker.start()
        with closing(socket.socket()) as relay_probe:
            relay_probe.bind(("0.0.0.0", 0))
            relay_port = relay_probe.getsockname()[1]
        self.service.config.update({"upstream_proxy": f"http://127.0.0.1:{upstream_port}",
                                    "relay_port": relay_port})
        self.service._probe_proxy = lambda: True
        self.service._start_relay()
        try:
            self.assertEqual(self.service._relay.server_address[0], "127.0.0.1")
            with socket.create_connection(("127.0.0.1", relay_port), timeout=2) as client:
                client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
                client.settimeout(0.5)
                try:
                    self.assertEqual(client.recv(64), b"")
                except TimeoutError:
                    pass
            self.assertEqual(received, [])
            auth = base64.b64encode(
                self.service.config["relay_auth_token"].encode("ascii")).decode("ascii")
            with socket.create_connection(("127.0.0.1", relay_port), timeout=2) as client:
                client.sendall(
                    f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode("ascii"))
                client.settimeout(2)
                self.assertEqual(client.recv(64),
                                 b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
            worker.join(timeout=2)
            self.assertEqual(len(received), 1)
            self.assertNotIn(b"Proxy-Authorization", received[0])
        finally:
            upstream.close()
            self.service._stop_relay()


if __name__ == "__main__":
    unittest.main()
