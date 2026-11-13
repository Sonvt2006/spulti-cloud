"""Validate the release that nodes download, including CLI imports."""

import hashlib
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from package_agent import RUNTIME_FILES, build_release  # noqa: E402


class ReleaseTests(unittest.TestCase):
    def test_reproducible_archive_contains_only_bootable_runtime_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, digest = build_release(root / "first")
            second, other_digest = build_release(root / "second")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(digest, other_digest)
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), digest)
            manifest = json.loads(first.with_name(first.name + ".json").read_text())
            self.assertEqual(manifest["sha256"], digest)
            with tarfile.open(first) as archive:
                self.assertEqual(set(archive.getnames()), set(RUNTIME_FILES))
                for member in archive:
                    self.assertTrue(member.isfile())
                    content = archive.extractfile(member).read()
                    self.assertEqual(
                        hashlib.sha256(content).hexdigest(),
                        manifest["files"][member.name],
                    )
