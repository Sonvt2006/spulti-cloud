"""Run each submitted Python script iteration in its own child process."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from interruption import StopRequest
from spulti import MAX_STATE_BYTES, encode_state

MAX_PROGRAM_BYTES = 16384


def validate_source(source: str) -> None:
    if not source or len(source.encode("utf-8")) > MAX_PROGRAM_BYTES:
        raise ValueError("Program must contain 1..16384 UTF-8 bytes")
    try:
        # Compile, but never execute. Also rejects return outside a function.
        compile(source, "<submission>", "exec")
    except SyntaxError as exc:
        raise ValueError(
            f"Python syntax error at line {exc.lineno}, column {exc.offset}: {exc.msg}"
        ) from exc
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"Cannot compile Python program: {exc}") from exc


class ProgramError(RuntimeError):
    """A script iteration failed; its progress/state must not be committed."""


class StepInterruptedError(Exception):
    """The child was stopped before finishing its iteration."""


class PythonProgram:
    def __init__(self, source: str, filename: str, total_steps: int):
        validate_source(source)
        if Path(filename).name != filename or not filename.endswith(".py"):
            raise ValueError("Program filename must be a simple .py filename")
        self.checkpoint_dir = None
        self.source = source
        self.filename = filename
        self.total_steps = total_steps
        self.source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()

    def run(
        self,
        number: int,
        state: dict,
        stop: StopRequest,
        *,
        maintenance: Callable[[], None] | None = None,
    ) -> dict:
        if stop.event.is_set():
            raise StepInterruptedError
        # Temporary IPC files, not durable checkpoints. Only the latest
        # successful state is kept in agent memory between iterations.
        with tempfile.TemporaryDirectory(prefix="spulti-run-") as temporary:
            directory = Path(temporary)
            source_path = directory / self.filename
            request = directory / "input.json"
            result = directory / "output.json"
            source_path.write_text(self.source, encoding="utf-8")
            request.write_text(encode_state(state), encoding="utf-8")
            env = os.environ.copy()
            env.update(
                SPULTI_STEP=str(number), SPULTI_TOTAL_STEPS=str(self.total_steps)
            )
            if self.checkpoint_dir is not None:
                env["SPULTI_CHECKPOINT_DIR"] = str(self.checkpoint_dir)
            worker = Path(__file__).with_name("script_worker.py")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(worker),
                    str(source_path),
                    str(request),
                    str(result),
                    str(number),
                    str(self.total_steps),
                ],
                env=env,
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
            try:
                while process.poll() is None:
                    if stop.event.wait(0.05):
                        self._terminate(process)
                        break
                    if maintenance is not None:
                        maintenance()
                if process.returncode != 0:
                    if stop.event.is_set():
                        raise StepInterruptedError
                    raise ProgramError(
                        f"Python script failed at step {number} "
                        f"(exit {process.returncode})"
                    )
                if not result.is_file():
                    if stop.event.is_set():
                        raise StepInterruptedError
                    raise ProgramError("Python script exited without returning state")
                with result.open("rb") as stream:
                    raw = stream.read(MAX_STATE_BYTES + 1)
                if len(raw) > MAX_STATE_BYTES:
                    raise ProgramError("Python script state exceeds 8 KiB")
                try:
                    next_state = json.loads(raw)
                    encode_state(next_state)
                except (ValueError, TypeError, RecursionError) as exc:
                    raise ProgramError(
                        "Python script returned invalid JSON state"
                    ) from exc
                return next_state
            finally:
                if process.poll() is None:
                    self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        # Leave the interruption budget for upload/reporting. On Linux Spot
        # VMs also stop descendants in the same process group.
        def send(sig):
            try:
                if os.name != "nt":
                    os.killpg(process.pid, sig)
                elif process.poll() is None:
                    process.terminate()
            except ProcessLookupError:
                pass

        send(signal.SIGTERM)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        if os.name != "nt":
            send(signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=1)
