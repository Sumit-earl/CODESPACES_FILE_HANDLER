"""Unit tests for the pure logic: paths, ranges, sizes, session math."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from filebridge.server import Handler  # noqa: E402
from filebridge.store import Session  # noqa: E402
from filebridge.util import (  # noqa: E402
    etag_for,
    human_bytes,
    parse_size,
    safe_join,
    safe_rel,
    unique_path,
)


class SizeTests(unittest.TestCase):
    def test_parse_size_units(self):
        self.assertEqual(parse_size("4M"), 4 * 1024**2)
        self.assertEqual(parse_size("512kb"), 512 * 1024)
        self.assertEqual(parse_size("2G"), 2 * 1024**3)
        self.assertEqual(parse_size(1024), 1024)
        self.assertEqual(parse_size("1.5m"), int(1.5 * 1024**2))

    def test_parse_size_rejects_garbage(self):
        for bad in ("", "abc", "12 parsecs", "-1M"):
            with self.assertRaises(ValueError):
                parse_size(bad)

    def test_human_bytes(self):
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(1536), "1.5 KB")
        self.assertEqual(human_bytes(5 * 1024**3), "5.0 GB")


class PathTests(unittest.TestCase):
    def test_safe_rel_accepts(self):
        self.assertEqual(safe_rel("a/b.txt"), "a/b.txt")
        self.assertEqual(safe_rel("./a//b"), "a/b")
        self.assertEqual(safe_rel(""), "")

    def test_safe_rel_rejects_escapes(self):
        for bad in ("../x", "/etc/passwd", "a/../../b", "a/\x00b"):
            with self.assertRaises(ValueError, msg=bad):
                safe_rel(bad)

    def test_safe_join_containment(self):
        root = Path(tempfile.mkdtemp())
        self.assertEqual(safe_join(root, "x/y"), root.resolve() / "x/y")
        with self.assertRaises(ValueError):
            safe_join(root, "../outside")

    def test_safe_join_refuses_symlink_escape(self):
        root = Path(tempfile.mkdtemp())
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("nope")
        link = root / "link"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            safe_join(root, "link")

    def test_unique_path(self):
        root = Path(tempfile.mkdtemp())
        target = root / "a.txt"
        target.write_text("1")
        self.assertEqual(unique_path(target).name, "a (1).txt")
        unique_path(target).write_text("2")
        self.assertEqual(unique_path(target).name, "a (2).txt")


class RangeTests(unittest.TestCase):
    def test_single_range(self):
        self.assertEqual(Handler._parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(Handler._parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(Handler._parse_range("bytes=-16", 100), (84, 99))
        self.assertEqual(Handler._parse_range("bytes=0-9999", 100), (0, 99))

    def test_unsatisfiable(self):
        self.assertIsNone(Handler._parse_range("bytes=100-", 100))
        self.assertIsNone(Handler._parse_range("bytes=5-4", 100))
        self.assertIsNone(Handler._parse_range("bytes=abc", 100))
        self.assertIsNone(Handler._parse_range("items=0-1", 100))

    def test_content_range(self):
        self.assertEqual(Handler._parse_content_range("bytes 0-99/100"), (0, 99, 100))
        for bad in ("0-99/100", "bytes 0-99", "bytes -1-9/10"):
            with self.assertRaises(Exception, msg=bad):
                Handler._parse_content_range(bad)


class SessionTests(unittest.TestCase):
    def make(self, size, chunk=1024):
        session = Session(
            id="u_test", path="a.bin", size=size, chunk_size=chunk,
            created=0.0, updated=0.0,
        )
        return session

    def test_chunk_math(self):
        session = self.make(2500, 1024)
        self.assertEqual(session.nchunks, 3)
        self.assertEqual(session.chunk_bounds(0), (0, 1023))
        self.assertEqual(session.chunk_bounds(2), (2048, 2499))

    def test_zero_byte_file_has_no_chunks(self):
        session = self.make(0)
        self.assertEqual(session.nchunks, 0)
        self.assertEqual(session.missing_ranges(), [])
        self.assertTrue(session.done())

    def test_missing_ranges_merge(self):
        session = self.make(5 * 1024, 1024)
        self.assertEqual(session.missing_ranges(), [[0, 5 * 1024 - 1]])
        session.chunks = {"0": "a", "1": "a", "3": "a"}
        self.assertEqual(session.missing_ranges(), [[2048, 3071], [4096, 5119]])

    def test_committed_is_a_prefix(self):
        session = self.make(5 * 1024, 1024)
        session.chunks = {"0": "a", "1": "a", "2": "a"}
        session.committed_idx = 3
        self.assertEqual(session.committed, 3 * 1024)
        session.chunks = {}
        session.committed_idx = 0
        self.assertEqual(session.committed, 0)

    def test_roundtrip_serialisation(self):
        session = self.make(2048)
        session.chunks = {"0": "deadbeef"}
        clone = Session.from_dict(session.to_dict())
        self.assertEqual(clone.to_dict(), session.to_dict())


class EtagTests(unittest.TestCase):
    def test_etag_changes_with_size(self):
        path = Path(tempfile.mkdtemp()) / "f"
        path.write_text("a")
        first = etag_for(path.stat())
        path.write_text("ab")
        self.assertNotEqual(first, etag_for(path.stat()))


if __name__ == "__main__":
    unittest.main()
