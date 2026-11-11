"""Immutable tar snapshots. Workloads must atomically publish checkpoint files."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from control_plane import RemoteError, RetryableRemoteError

MAX_BYTES = 256 * 1024 * 1024
MAX_FILES = 10000
CHUNK = 1024 * 1024


def budget(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RetryableRemoteError("File checkpoint deadline exceeded")
    return min(3.0, remaining)


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if path.name.endswith(".tmp") and stat.S_ISREG(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Checkpoint directory must contain only regular files")
        result[path.relative_to(root).as_posix()] = (
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        if len(result) > MAX_FILES:
            raise ValueError("Too many checkpoint files")
    if sum(entry[1] for entry in result.values()) > MAX_BYTES:
        raise ValueError("Checkpoint exceeds 256 MiB snapshot limit")
    return result


def digest_file(path, deadline=None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(CHUNK):
            if deadline is not None:
                budget(deadline)
            digest.update(block)
    return digest.hexdigest()


class DeadlineReader:
    def __init__(self, source, deadline):
        self.source = source
        self.deadline = deadline

    def read(self, size=-1):
        budget(self.deadline)
        return self.source.read(size)


def snapshot(root, archive, deadline):
    if root.is_symlink():
        raise ValueError("Checkpoint directory cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    before = inventory(root)
    with tarfile.open(archive, "w") as output:
        for name in before:
            budget(deadline)
            path = root / name
            # O_NOFOLLOW prevents a workload replacing a file with a symlink.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Checkpoint file changed type")
                member = tarfile.TarInfo(name)
                member.size = info.st_size
                member.mode = 0o600
                output.addfile(member, DeadlineReader(stream, deadline))
    if before != inventory(root):
        raise RetryableRemoteError(
            "Checkpoint files changed during snapshot; retry later"
        )
    if archive.stat().st_size > MAX_BYTES:
        raise ValueError("Checkpoint tar exceeds 256 MiB limit including headers")
    budget(deadline)


def restore(archive, root):
    """Validate in staging before swapping directories. Preserve old data on failure."""
    if root.is_symlink():
        raise ValueError("Checkpoint directory cannot be a symlink")
    root.parent.mkdir(parents=True, exist_ok=True)
    backup = root.with_name(root.name + ".spulti-backup")
    if backup.exists():
        if root.exists():
            shutil.rmtree(backup)
        else:
            os.replace(backup, root)
    with tempfile.TemporaryDirectory(
        dir=root.parent, prefix=".spulti-restore-"
    ) as temp:
        stage = Path(temp) / "files"
        stage.mkdir()
        seen = set()
        total = 0
        with tarfile.open(archive, "r:") as package:
            for member in package:
                name = PurePosixPath(member.name)
                if (
                    not member.isfile()
                    or name.is_absolute()
                    or ".." in name.parts
                    or "\\" in member.name
                    or not name.parts
                    or member.name in seen
                ):
                    raise ValueError("Unsafe or duplicate checkpoint archive entry")
                seen.add(member.name)
                total += member.size
                if len(seen) > MAX_FILES or total > MAX_BYTES:
                    raise ValueError("Checkpoint archive exceeds extraction limits")
                target = stage.joinpath(*name.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, CHUNK)
                target.chmod(0o600)
        if root.exists():
            os.replace(root, backup)
        try:
            os.replace(stage, root)
        except OSError:
            if backup.exists():
                os.replace(backup, root)
            raise
        if backup.exists():
            shutil.rmtree(backup)


def object_request(url, *, data=None, headers=None, deadline):
    parsed = urlparse(url)
    # HTTP loopback is exclusively for the local integration test server.
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    ):
        raise RemoteError("File transfer requires HTTPS")
    request = Request(
        url,
        data=data,
        headers=headers or {},
        method="PUT" if data is not None else "GET",
    )
    try:
        return urlopen(request, timeout=budget(deadline))
    except (HTTPError, URLError, OSError):
        # Signed URLs must never appear in logs or exception messages.
        raise RetryableRemoteError("S3 file transfer failed") from None


class FileCheckpointSync:
    def __init__(self, root, remote):
        self.root = root
        self.remote = remote

    def upload(self, *, deadline):
        stopping = bool(self.remote.stop_event and self.remote.stop_event.is_set())
        with tempfile.TemporaryDirectory(prefix="spulti-files-") as temp:
            archive = Path(temp) / "snapshot.tar"
            snapshot(self.root, archive, deadline)
            size = archive.stat().st_size
            sha256 = digest_file(archive, deadline)
            ticket = self.remote._request(
                "POST",
                "files/upload",
                {"size": size, "sha256": sha256},
                deadline=deadline,
            )

            def chunks():
                with archive.open("rb") as source:
                    while block := source.read(CHUNK):
                        budget(deadline)
                        if (
                            not stopping
                            and self.remote.stop_event
                            and self.remote.stop_event.is_set()
                        ):
                            raise RetryableRemoteError(
                                "File upload interrupted; final sync will retry"
                            )
                        yield block

            headers = {**ticket["headers"], "Content-Length": str(size)}
            with object_request(
                ticket["url"], data=chunks(), headers=headers, deadline=deadline
            ) as response:
                response.read(1024)
            budget(deadline)
            print(f"[agent] FILES_UPLOADED bytes={size}", flush=True)
            return ticket["reference"]

    def download(self, reference, *, deadline):
        if not 0 < reference["size"] <= MAX_BYTES:
            raise ValueError("Invalid file snapshot size")
        ticket = self.remote._request(
            "POST", "files/download", reference, deadline=deadline
        )
        with tempfile.TemporaryDirectory(prefix="spulti-files-") as temp:
            archive = Path(temp) / "snapshot.tar"
            received = 0
            with object_request(ticket["url"], deadline=deadline) as response:
                with archive.open("wb") as output:
                    while block := response.read(CHUNK):
                        budget(deadline)
                        received += len(block)
                        if received > reference["size"]:
                            raise ValueError("File snapshot exceeds declared size")
                        output.write(block)
            if (
                received != reference["size"]
                or digest_file(archive, deadline) != reference["sha256"]
            ):
                raise ValueError("File snapshot checksum/size mismatch")
            restore(archive, self.root)
            print(f"[agent] FILES_RESTORED bytes={received}", flush=True)
