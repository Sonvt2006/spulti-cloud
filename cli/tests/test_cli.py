"""CLI contract and regression tests; no cloud resources required."""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from spulti_cli import client, main


class CLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                size = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(size)) if size else None
                requests.append((self.command, self.path, dict(self.headers), body))
                if self.path == "/bad-json":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"<html>proxy error</html>")
                    return
                if self.path == "/unauthorized":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"detail":"Expired key"}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id":1,"status":"PENDING"}')

            do_GET = do_POST = do_DELETE = handle_request  # noqa: N815

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_path = Path(self.directory.name) / "config.json"
        self.enterContext(patch.object(client, "CONFIG_PATH", self.config_path))
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "SPULTI_API_URL": self.url,
                    "SPULTI_API_KEY": "spk_test",
                },
            )
        )

    def run_cli(self, *arguments):
        output = io.StringIO()
        with patch.object(main.sys, "argv", ["spulti", *arguments]):
            with (
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                main.main()
        return output.getvalue()

    def test_create_machine_contract_and_idempotency(self):
        key = "acb1cffa-e859-48e7-98a5-845285ec7a48"
        result = self.run_cli(
            "machine",
            "create",
            "training",
            "--vcpu",
            "2",
            "--ram-gib",
            "4",
            "--provider",
            "aws",
            "--idempotency-key",
            key,
            "--json",
        )
        method, path, headers, body = self.requests[-1]
        self.assertEqual((method, path), ("POST", "/api/machines"))
        self.assertEqual(headers["Idempotency-Key"], key)
        self.assertEqual(headers["Authorization"], "Bearer spk_test")
        self.assertEqual(body["name"], "training")
        self.assertEqual(body["provider"], "AWS")
        self.assertEqual(body["ram_gib"], 4)
        self.assertEqual(json.loads(result)["status"], "PENDING")

    def test_routes_and_pagination(self):
        for action, method, suffix in (
            ("show", "GET", ""),
            ("delete", "DELETE", ""),
            ("retry", "POST", "/retry"),
            ("instances", "GET", "/instances"),
        ):
            self.run_cli("machine", action, "7")
            self.assertEqual(
                self.requests[-1][:2], (method, "/api/machines/7" + suffix)
            )
        self.run_cli("machine", "events", "7", "--limit", "5", "--offset", "10")
        self.assertEqual(
            self.requests[-1][1], "/api/machines/7/events?limit=5&offset=10"
        )

    def test_catalog_does_not_send_credentials(self):
        self.run_cli("catalog", "--provider", "gcp")
        self.assertNotIn("Authorization", self.requests[-1][2])
        self.assertIn("provider=GCP", self.requests[-1][1])

    def test_ssh_key_reads_file_and_rejects_private_key(self):
        key = Path(self.directory.name) / "id.pub"
        key.write_text("ssh-ed25519 AAAA test\n")
        self.run_cli("ssh-key", "add", "laptop", "--file", str(key))
        self.assertEqual(self.requests[-1][3]["public_key"], "ssh-ed25519 AAAA test")
        count = len(self.requests)
        key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
        with self.assertRaises(SystemExit) as error:
            self.run_cli("ssh-key", "add", "laptop", "--file", str(key))
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(len(self.requests), count)

    def test_bad_input_fails_before_network(self):
        for value in ("nan", "inf", "0", "-1"):
            with self.assertRaises(SystemExit) as error:
                self.run_cli("placement", "--vcpu", "2", "--ram-gib", value)
            self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.requests)

    def test_http_and_non_json_errors(self):
        for path, status in (("/bad-json", 0), ("/unauthorized", 401)):
            with self.assertRaises(client.APIError) as error:
                client.call("GET", self.url, path)
            self.assertEqual(error.exception.status, status)

    def test_config_atomic_save_and_permissions(self):
        client.save_config({"api_key": "old"})
        with patch.object(client.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                client.save_config({"api_key": "new"})
        self.assertEqual(client.load_config(), {"api_key": "old"})
        self.assertEqual(list(self.config_path.parent.iterdir()), [self.config_path])
        if os.name != "nt":
            self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    def test_invalid_config(self):
        self.config_path.write_text("[]")
        with self.assertRaises(SystemExit) as error:
            self.run_cli("whoami")
        self.assertEqual(error.exception.code, 1)

    def test_login_creation_failure_keeps_old_key(self):
        client.save_config({"api_url": self.url, "api_key": "old", "api_key_id": 1})
        with patch.object(main.getpass, "getpass", return_value="password"):
            with patch.object(
                main,
                "call",
                side_effect=[{"access_token": "jwt"}, client.APIError(500, "failed")],
            ) as api:
                with self.assertRaises(SystemExit):
                    self.run_cli("login", "--username", "alice")
        self.assertEqual(
            [args.args[0] for args in api.call_args_list], ["POST", "POST"]
        )
        self.assertEqual(client.load_config()["api_key"], "old")

    def test_logout_ignores_environment_server_and_key(self):
        client.save_config(
            {"api_url": "https://saved.example", "api_key": "old", "api_key_id": 1}
        )
        with patch.object(main, "call", return_value=None) as api:
            self.run_cli("logout")
        api.assert_called_once_with(
            "DELETE", "https://saved.example", "/api/api-keys/1", token="old"
        )
        self.assertFalse(self.config_path.exists())

    def test_placement_and_job_contract(self):
        self.run_cli(
            "placement",
            "--vcpu",
            "4",
            "--ram-gib",
            "8",
            "--optimization-mode",
            "saving",
            "--region",
            "singapore",
        )
        method, path, _, body = self.requests[-1]
        self.assertEqual((method, path), ("POST", "/api/placement/recommendations"))
        self.assertEqual(body["optimization_mode"], "SAVING")
        self.assertEqual(body["region"], "singapore")
        job = "acb1cffa-e859-48e7-98a5-845285ec7a48"
        self.run_cli("job", "checkpoint", job)
        self.assertEqual(self.requests[-1][1], f"/api/agent/jobs/{job}/checkpoint")

    def test_registration_password_confirmation(self):
        with patch.object(main.getpass, "getpass", side_effect=["password", "wrong"]):
            with self.assertRaises(SystemExit):
                self.run_cli(
                    "register",
                    "--name",
                    "Alice",
                    "--username",
                    "alice",
                    "--email",
                    "alice@example.com",
                )
        self.assertFalse(self.requests)
        with patch.object(main.getpass, "getpass", return_value="password"):
            self.run_cli(
                "register",
                "--name",
                "Alice",
                "--username",
                "alice",
                "--email",
                "alice@example.com",
            )
        self.assertEqual(self.requests[-1][1], "/api/auth/register")
        self.assertNotIn("Authorization", self.requests[-1][2])
        self.assertFalse(self.config_path.exists())

    def test_login_saves_new_key_before_revoking_old(self):
        client.save_config({"api_url": self.url, "api_key": "old", "api_key_id": 1})

        def reply(method, url, path, **kwargs):
            if path == "/api/auth/login":
                return {"access_token": "jwt"}
            if method == "POST":
                return {"key": "new", "id": 2}
            self.assertEqual(client.load_config()["api_key"], "new")
            self.assertEqual(kwargs["token"], "old")
            return None

        with patch.object(main.getpass, "getpass", return_value="password"):
            with patch.object(main, "call", side_effect=reply):
                self.run_cli("login", "--username", "alice")
        self.assertEqual(client.load_config()["api_key_id"], 2)

    def test_timeout_and_invalid_url(self):
        with patch.object(client, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(client.APIError) as error:
                client.call("GET", self.url, "/health")
            self.assertEqual(error.exception.status, 0)
        with self.assertRaises(ValueError):
            client.call("GET", "file:///etc/passwd", "/health")

    def test_empty_json_output(self):
        with patch.object(main, "call", return_value=[]):
            self.assertEqual(json.loads(self.run_cli("machine", "list", "--json")), [])


if __name__ == "__main__":
    unittest.main()
