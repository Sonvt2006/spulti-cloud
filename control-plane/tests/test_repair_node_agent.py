"""Repair must preserve per-attempt credentials and use the configured release."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-deployment-12345")

from app.scripts.repair_node_agent import repair_script  # noqa: E402


class RepairNodeTests(unittest.TestCase):
    @patch("app.scripts.repair_node_agent.agent_settings")
    def test_repair_uses_shared_installer_without_replacing_credentials(self, settings):
        settings.return_value = (
            "https://api.example.test",
            "https://release.example.test/new.tar.gz",
            "a" * 64,
        )
        for provider in ("AWS", "GCP"):
            with self.subTest(provider=provider):
                script = repair_script(provider)
                self.assertIn("if [ ! -s /etc/spulti-node.env ]; then", script)
                self.assertNotIn("base64 -d", script)
                self.assertNotIn("SPULTI_NODE_TOKEN=", script)
                self.assertIn("new.tar.gz", script)
                self.assertIn("SPULTI_AGENT_SHA256=" + "a" * 64, script)
                self.assertIn("EnvironmentFile=/etc/spulti-node.env", script)
                self.assertIn(f"SPULTI_CLOUD_PROVIDER={provider.lower()}", script)
                self.assertIn("enable --now spulti-node-agent.service", script)
                self.assertIn("agent.py managed", script)
                self.assertLess(
                    script.index("if [ ! -s /etc/spulti-node.env ]; then"),
                    script.index("apt-get"),
                )
        with self.assertRaises(ValueError):
            repair_script("unknown")
