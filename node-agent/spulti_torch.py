"""One process = one epoch, with model/optimizer checkpoints selected by agent state.

PyTorch is an optional workload dependency, never imported by the agent itself.
"""

from __future__ import annotations

import hashlib
import os
import random
import tempfile
from pathlib import Path

import spulti
import torch

STATE_KEY = "_spulti_torch"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class TorchCheckpoint:
    """Restore before training, then call save() once after this epoch succeeds.

    Construct model, optimizer and optional scheduler before constructing this
    helper. Keep dataset generation independent of the restored training RNG.
    """

    def __init__(self, model, optimizer, *, scheduler=None):
        if spulti.step_number < 1:
            raise RuntimeError(
                "Run this script through spulti-agent run/submit/managed"
            )
        directory = os.environ.get("SPULTI_CHECKPOINT_DIR")
        if not directory:
            raise RuntimeError(
                "PyTorch training requires --checkpoint-dir "
                "(managed jobs enable it automatically)"
            )
        if spulti._torch_checkpoint_required:
            raise RuntimeError("Use only one TorchCheckpoint per epoch")
        self.root = Path(directory) / "torch"
        self.root.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.epoch = spulti.step_number
        self.saved = False
        reference = spulti.state.get(STATE_KEY)
        if reference is None:
            if self.epoch != 1:
                raise ValueError("Missing PyTorch checkpoint for the preceding epoch")
        else:
            previous = self.epoch - 1
            filename = f"epoch-{previous:08d}.pt"
            if reference.get("epoch") != previous or reference.get("file") != filename:
                raise ValueError(
                    "PyTorch checkpoint does not match the preceding epoch"
                )
            path = self.root / filename
            if digest(path) != reference.get("sha256"):
                raise ValueError("PyTorch checkpoint checksum mismatch")
            payload = torch.load(path, map_location="cpu", weights_only=True)
            if payload.get("version") != 1 or payload.get("epoch") != previous:
                raise ValueError("Invalid PyTorch checkpoint version/epoch")
            if (payload["scheduler"] is None) != (scheduler is None):
                raise ValueError("Scheduler configuration does not match checkpoint")
            model.load_state_dict(payload["model"])
            if scheduler is not None:
                scheduler.load_state_dict(payload["scheduler"])
            optimizer.load_state_dict(payload["optimizer"])
            random.setstate(payload["python_rng"])
            torch.set_rng_state(payload["torch_rng"])
            if payload["cuda_rng"] and torch.cuda.is_available():
                if len(payload["cuda_rng"]) != torch.cuda.device_count():
                    raise ValueError("CUDA device count differs from the checkpoint")
                torch.cuda.set_rng_state_all(payload["cuda_rng"])
            # Retain the referenced epoch throughout the active child. A timed
            # snapshot still describes that epoch, even after save() writes next.
            for old in self.root.glob("epoch-*.pt"):
                number = old.stem.removeprefix("epoch-")
                if number.isdigit() and int(number) < previous:
                    old.unlink()
        spulti._torch_checkpoint_required = True
        print(f"[torch] EPOCH={self.epoch} RESTORED_EPOCH={self.epoch - 1}", flush=True)

    def save(self, *, metrics: dict | None = None) -> None:
        """Atomically publish weights, then return the reference through JSON state.

        Call after optimizer/scheduler updates. A failed child never commits its
        reference, even if this file was already written successfully.
        """
        if self.saved:
            raise RuntimeError("Save the PyTorch checkpoint only once per epoch")
        metrics = {} if metrics is None else metrics
        spulti.encode_state(metrics)
        payload = {
            "version": 1,
            "epoch": self.epoch,
            "model": self.model.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "scheduler": self.scheduler.state_dict()
            if self.scheduler is not None
            else None,
            "python_rng": random.getstate(),
            "torch_rng": torch.get_rng_state(),
            "cuda_rng": torch.cuda.get_rng_state_all()
            if torch.cuda.is_available()
            else [],
        }
        filename = f"epoch-{self.epoch:08d}.pt"
        target = self.root / filename
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.root, suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                torch.save(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            reference = {
                "epoch": self.epoch,
                "file": filename,
                "sha256": digest(temporary),
                "metrics": metrics,
            }
            next_state = {**spulti.state, STATE_KEY: reference}
            spulti.encode_state(next_state)
            os.replace(temporary, target)
            spulti.state[STATE_KEY] = reference
            self.saved = True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
