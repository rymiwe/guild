#!/usr/bin/env python3
"""Hermetic tests for bin/memory-lint (temp memory tree only)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "bin" / "memory-lint"


def run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), "--root", str(root)],
                          capture_output=True, text=True)


class MemoryLintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def project(self, name: str, index: str | None, files: list[str]) -> None:
        mem = self.root / name / "memory"
        mem.mkdir(parents=True)
        if index is not None:
            (mem / "MEMORY.md").write_text(index, encoding="utf-8")
        for f in files:
            (mem / f).write_text("---\nname: x\n---\nbody\n", encoding="utf-8")

    def test_clean_tree_is_quiet(self):
        self.project("p", "- [A](a.md) — hook\n", ["a.md"])
        r = run(self.root)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_reports_orphan_and_dangling(self):
        self.project("p", "- [A](a.md) — hook\n- [Gone](gone.md) — hook\n", ["a.md", "lost.md"])
        r = run(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.splitlines(), ["orphan    p/lost.md", "dangling  p/gone.md"])

    def test_missing_index_makes_every_file_an_orphan(self):
        self.project("p", None, ["a.md"])
        r = run(self.root)
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "orphan    p/a.md"))

    def test_missing_root_exits_2(self):
        r = run(self.root / "nope")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
