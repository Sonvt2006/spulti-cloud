"""Install the CPU training runtime on managed Ubuntu VMs (run as root)."""

import os
import subprocess
import sys
from pathlib import Path

RUNTIME = Path("/opt/spulti-runtime")
TORCH_VERSION = "2.10.0+cpu"
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
CHECK = (
    "import torch; "
    f"assert torch.__version__ == {TORCH_VERSION!r}; "
    "assert torch.version.cuda is None; "
    "assert torch.ones(1).sum().item() == 1"
)


def verify(python: Path, *, quiet: bool = False) -> bool:
    # Check with the service account, not root: restrictive bootstrap umasks
    # must not leave the interpreter or installed packages unreadable by jobs.
    result = subprocess.run(
        ["runuser", "-u", "spulti-agent", "--", str(python), "-c", CHECK],
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
        timeout=60,
        check=False,
    )
    return result.returncode == 0


def install_runtime(root: Path = RUNTIME) -> None:
    python = root / "bin" / "python"
    if python.is_file() and verify(python, quiet=True):
        print(f"BOOTSTRAP: PyTorch {TORCH_VERSION} already ready", flush=True)
        return
    print(f"BOOTSTRAP: installing PyTorch CPU {TORCH_VERSION}", flush=True)
    previous_umask = os.umask(0o022)
    try:
        # PEP 668 on Ubuntu 24.04: never pip-install into the system Python.
        subprocess.run(
            ["apt-get", "install", "-y", "-qq", "python3-venv"],
            check=True,
            timeout=180,
        )
        subprocess.run(
            [sys.executable, "-m", "venv", str(root)], check=True, timeout=90
        )
        root.chmod(0o755)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-cache-dir",
                "--only-binary=:all:",
                "--retries",
                "3",
                "--timeout",
                "30",
                f"torch=={TORCH_VERSION}",
                "--index-url",
                TORCH_INDEX,
            ],
            check=True,
            timeout=420,
        )
        if not verify(python):
            raise RuntimeError("PyTorch verification failed for spulti-agent")
    finally:
        os.umask(previous_umask)
    print(f"BOOTSTRAP: PyTorch {TORCH_VERSION} ready", flush=True)


def main() -> int:
    if os.name != "posix" or os.geteuid() != 0:
        print(
            "Run install_runtime.py as root on the managed Ubuntu VM", file=sys.stderr
        )
        return 2
    try:
        install_runtime()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"BOOTSTRAP: PyTorch installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
