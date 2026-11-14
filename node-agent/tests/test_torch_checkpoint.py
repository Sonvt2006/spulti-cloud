"""Real CPU training: process restart, failed epoch, and replacement-VM restore."""

import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from checkpoint import CheckpointStore  # noqa: E402
from config import AgentConfig  # noqa: E402
from control_plane import RetryableRemoteError  # noqa: E402
from file_checkpoint import restore, snapshot  # noqa: E402
from job_runner import run_job  # noqa: E402
from program import ProgramError, PythonProgram  # noqa: E402

HAS_TORCH = importlib.util.find_spec("torch") is not None

TRAINING = """
import os, random, time
import torch
from spulti_torch import TorchCheckpoint
torch.set_num_threads(1)
torch.manual_seed(42)
random.seed(42)
model = torch.nn.Sequential(
    torch.nn.Linear(2, 4), torch.nn.Dropout(0.2), torch.nn.Linear(4, 1))
optimizer = torch.optim.Adam(model.parameters(), lr=0.05)
scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=0.8)
checkpoint = TorchCheckpoint(model, optimizer, scheduler=scheduler)
x = torch.randn(16, 2) * random.random()
y = x.sum(dim=1, keepdim=True)
optimizer.zero_grad()
loss = torch.nn.functional.mse_loss(model(x), y)
loss.backward()
optimizer.step()
scheduler.step()
checkpoint.save(metrics={'loss': loss.item()})
if checkpoint.epoch == 2 and os.getenv('SPULTI_TEST_FAIL') == '1':
    time.sleep(1.2)
    raise RuntimeError('failed AFTER writing epoch 2 weights')
"""


@unittest.skipUnless(HAS_TORCH, "Install torch to run real training/recovery tests")
class TorchCheckpointTests(unittest.TestCase):
    def setUp(self):
        import torch

        self.torch = torch
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = AgentConfig(
            job_id="torch-test",
            total_steps=3,
            checkpoint_every=0,
            heartbeat_every=1,
            step_delay=0,
            state_dir=self.root / "baseline",
            checkpoint_dir=self.root / "baseline/files",
        )
        self.program = PythonProgram(TRAINING, "train.py", 3)

    def checkpoint(self, config):
        return CheckpointStore(
            config.state_dir,
            config.job_id,
            config.total_steps,
            source_sha256=self.program.source_sha256,
        ).load()

    def payload(self, config):
        data = self.checkpoint(config)
        file = data["workload_state"]["_spulti_torch"]["file"]
        return self.torch.load(
            config.checkpoint_dir / config.job_id / "torch" / file,
            weights_only=True,
            map_location="cpu",
        )

    def assert_tree_equal(self, left, right):
        if isinstance(left, self.torch.Tensor):
            self.assertTrue(self.torch.equal(left, right))
        elif isinstance(left, dict):
            self.assertEqual(left.keys(), right.keys())
            for key in left:
                self.assert_tree_equal(left[key], right[key])
        elif isinstance(left, tuple | list):
            self.assertEqual(len(left), len(right))
            for a, b in zip(left, right, strict=True):
                self.assert_tree_equal(a, b)
        else:
            self.assertEqual(left, right)

    def test_failed_epoch_remote_restore_matches_uninterrupted_training(self):
        self.assertEqual(run_job(self.config, program=self.program), 0)
        expected = self.payload(self.config)
        remote = Mock()
        committed = None
        bundles = {}
        records = []
        fail_upload = False
        archive = self.root / "transfer.tar"

        class MemoryFiles:
            def __init__(self, root, client):
                self.root = root

            def upload(self, *, deadline):
                snapshot(self.root, archive, deadline)
                raw = archive.read_bytes()
                sha = hashlib.sha256(raw).hexdigest()
                bundles[sha] = raw
                return {"sha256": sha, "size": len(raw)}

            def download(self, reference, *, deadline):
                archive.write_bytes(bundles[reference["sha256"]])
                restore(archive, self.root)

        def save(data, **kwargs):
            nonlocal committed
            if fail_upload and data["last_completed_step"] == 2:
                raise RetryableRemoteError("Checkpoint commit unavailable")
            committed = copy.deepcopy(data)
            records.append(committed)
            # Every remotely accepted reference must select weights matching its
            # completed step, including snapshots taken during a failing child.
            reference = data["workload_state"].get("_spulti_torch")
            if reference:
                self.assertEqual(reference["epoch"], data["last_completed_step"])
                with tarfile.open(
                    fileobj=io.BytesIO(bundles[data["file_snapshot"]["sha256"]])
                ) as package:
                    raw = package.extractfile("torch/" + reference["file"]).read()
                    self.assertEqual(
                        hashlib.sha256(raw).hexdigest(), reference["sha256"]
                    )

        remote.save_checkpoint.side_effect = save
        remote.get_checkpoint.side_effect = lambda: copy.deepcopy(committed)
        first = replace(
            self.config,
            state_dir=self.root / "vm1",
            checkpoint_dir=self.root / "vm1/files",
            control_plane_url="http://localhost",
            agent_token="token",
            file_sync_seconds=1,
        )
        second = replace(
            first, state_dir=self.root / "vm2", checkpoint_dir=self.root / "vm2/files"
        )
        with (
            patch("job_runner.ControlPlaneClient", return_value=remote),
            patch("job_runner.FileCheckpointSync", MemoryFiles),
        ):
            with patch.dict(os.environ, {"SPULTI_TEST_FAIL": "1"}):
                with self.assertRaises(ProgramError):
                    run_job(first, program=self.program)
            self.assertEqual(committed["last_completed_step"], 1)
            # The failed epoch's weights exist, but must never be selected.
            self.assertTrue(
                (
                    first.checkpoint_dir / first.job_id / "torch/epoch-00000002.pt"
                ).exists()
            )
            fail_upload = True
            with self.assertRaises(RetryableRemoteError):
                run_job(second, program=self.program)
            # Local JSON is newer, but the remote snapshot remains authoritative.
            self.assertEqual(self.checkpoint(second)["last_completed_step"], 2)
            self.assertEqual(committed["last_completed_step"], 1)
            fail_upload = False
            self.assertEqual(run_job(second, program=self.program), 0)
        self.assert_tree_equal(expected, self.payload(second))
        self.assertEqual(committed["status"], "COMPLETED")
        self.assertTrue(any(r["last_completed_step"] == 2 for r in records))
        self.assertLessEqual(
            len(list((second.checkpoint_dir / second.job_id / "torch").glob("*.pt"))), 2
        )

    def test_missing_save_fails_without_advancing_epoch(self):
        program = PythonProgram(TRAINING.split("checkpoint.save(")[0], "missing.py", 3)
        with self.assertRaises(ProgramError):
            run_job(self.config, program=program)
        data = json.loads(
            (self.config.state_dir / "checkpoints/torch-test.json").read_text()
        )
        self.assertEqual(data["last_completed_step"], 0)

    def test_agent_hard_crash_replays_only_uncommitted_epoch(self):
        driver = self.root / "crash_driver.py"
        driver.write_text(
            f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n"
            "from pathlib import Path\n"
            "from config import AgentConfig\n"
            "from program import PythonProgram\n"
            "from job_runner import run_job\n"
            f"program = PythonProgram({TRAINING!r}, 'train.py', 3)\n"
            "config = AgentConfig(job_id='torch-test', total_steps=3, "
            "checkpoint_every=0, heartbeat_every=1, step_delay=0, "
            f"state_dir=Path({str(self.config.state_dir)!r}), "
            f"checkpoint_dir=Path({str(self.config.checkpoint_dir)!r}), "
            "simulate_crash_at=2)\n"
            "run_job(config, program=program)\n",
            encoding="utf-8",
        )
        crash = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(crash.returncode, 75, crash.stdout + crash.stderr)
        self.assertEqual(self.checkpoint(self.config)["last_completed_step"], 1)
        orphan = (
            self.config.checkpoint_dir / self.config.job_id / "torch/epoch-00000002.pt"
        )
        self.assertTrue(orphan.exists())
        before = self.torch.load(orphan, weights_only=True)
        self.assertEqual(run_job(self.config, program=self.program), 0)
        # Epoch 2 is replayed from epoch 1 with exactly the same model/RNG result.
        self.assert_tree_equal(before, self.torch.load(orphan, weights_only=True))
        self.assertEqual(self.checkpoint(self.config)["last_completed_step"], 3)

    def test_corrupt_weights_are_rejected_before_training(self):
        with patch.dict(os.environ, {"SPULTI_TEST_FAIL": "1"}):
            with self.assertRaises(ProgramError):
                run_job(self.config, program=self.program)
        path = (
            self.config.checkpoint_dir / self.config.job_id / "torch/epoch-00000001.pt"
        )
        path.write_bytes(b"corrupted")
        with self.assertRaises(ProgramError):
            run_job(self.config, program=self.program)
        self.assertEqual(self.checkpoint(self.config)["last_completed_step"], 1)

    def test_local_cli_runs_example_and_completed_job_is_not_repeated(self):
        args = [
            sys.executable,
            str(ROOT / "agent.py"),
            "run",
            str(ROOT / "examples/torch_regression.py"),
            "--job-id",
            "demo",
            "--total-steps",
            "2",
            "--state-dir",
            str(self.root / "cli"),
        ]
        first = subprocess.run(args, capture_output=True, text=True, timeout=90)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("EPOCH=2 RESTORED_EPOCH=1", first.stdout)
        second = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already COMPLETED", second.stdout)
