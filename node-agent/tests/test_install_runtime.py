"""Runtime setup skips healthy installs and fails closed before starting jobs."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from install_runtime import TORCH_INDEX, TORCH_VERSION, install_runtime  # noqa: E402


class RuntimeInstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.python = self.root / "bin/python"

    def test_healthy_runtime_does_not_download_or_modify_packages(self):
        self.python.parent.mkdir()
        self.python.touch()
        with patch(
            "install_runtime.subprocess.run", return_value=Mock(returncode=0)
        ) as run:
            install_runtime(self.root)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            run.call_args.args[0][:5],
            ["runuser", "-u", "spulti-agent", "--", str(self.python)],
        )

    def test_fresh_runtime_installs_cpu_wheel_and_checks_service_permissions(self):
        with patch(
            "install_runtime.subprocess.run", return_value=Mock(returncode=0)
        ) as run:
            install_runtime(self.root)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:2], ["apt-get", "install"])
        self.assertEqual(commands[1], [sys.executable, "-m", "venv", str(self.root)])
        self.assertIn(f"torch=={TORCH_VERSION}", commands[2])
        self.assertIn(TORCH_INDEX, commands[2])
        self.assertIn("--no-cache-dir", commands[2])
        self.assertEqual(commands[3][:4], ["runuser", "-u", "spulti-agent", "--"])

    def test_failed_download_restores_umask_and_can_retry(self):
        with (
            patch("install_runtime.os.umask", return_value=0o077) as umask,
            patch(
                "install_runtime.subprocess.run",
                side_effect=[
                    Mock(returncode=0),
                    Mock(returncode=0),
                    subprocess.CalledProcessError(1, "pip"),
                ],
            ),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                install_runtime(self.root)
        self.assertEqual(
            [call.args[0] for call in umask.call_args_list], [0o022, 0o077]
        )
        # A subsequent boot retries installation; no success marker masks failure.
        with patch(
            "install_runtime.subprocess.run", return_value=Mock(returncode=0)
        ) as run:
            install_runtime(self.root)
        self.assertEqual(run.call_count, 4)

    def test_wrong_version_is_reinstalled_and_failed_import_is_not_success(self):
        self.python.parent.mkdir()
        self.python.touch()
        with patch(
            "install_runtime.subprocess.run",
            side_effect=[
                Mock(returncode=1),
                Mock(returncode=0),
                Mock(returncode=0),
                Mock(returncode=0),
                Mock(returncode=1),
            ],
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                install_runtime(self.root)
        self.assertEqual(run.call_count, 5)
