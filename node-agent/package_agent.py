"""Build and smoke-test a reproducible release with only agent runtime files."""

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Explicit list: never publish credentials, state, tests, or packaging tools.
RUNTIME_FILES = (
    "agent.py",
    "checkpoint.py",
    "config.py",
    "control_plane.py",
    "file_checkpoint.py",
    "heartbeat.py",
    "interruption.py",
    "job_runner.py",
    "managed.py",
    "node_daemon.py",
    "program.py",
    "script_worker.py",
    "spulti.py",
    "submission.py",
)


def build_release(output_dir: Path) -> tuple[Path, str]:
    files = {
        name: (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
        for name in RUNTIME_FILES
    }
    # Test the exact packaged bytes, isolated from other files in this checkout.
    with tempfile.TemporaryDirectory() as directory:
        stage = Path(directory)
        for name, data in files.items():
            (stage / name).write_bytes(data)
        for command in ([], ["daemon"], ["managed"], ["submit"], ["resume"]):
            subprocess.run(
                [
                    sys.executable,
                    "-E",
                    "-s",
                    str(stage / "agent.py"),
                    *command,
                    "--help",
                ],
                cwd=stage,
                check=True,
                capture_output=True,
                timeout=30,
            )

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(data))
    payload = buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"node-agent-{digest}.tar.gz"
    target.write_bytes(payload)
    target.with_name(target.name + ".sha256").write_text(
        f"{digest}  {target.name}\n", encoding="utf-8"
    )
    manifest = {
        "archive": target.name,
        "sha256": digest,
        "tested_python": sys.version.split()[0],
        "files": {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        },
    }
    target.with_name(target.name + ".json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target, digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT.parent / "dist")
    args = parser.parse_args()
    target, digest = build_release(args.output_dir)
    print(f"Archive: {target}\nSHA256: {digest}")
    print(f"CLI checks passed on Python {sys.version.split()[0]}")


if __name__ == "__main__":
    main()
