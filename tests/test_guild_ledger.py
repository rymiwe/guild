#!/usr/bin/env python3
"""guild-ledger: the outcome ledger's writer and its posted-findings backfill."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "bin" / "guild-ledger"
REPO = "AcmeCorp/widgets"
REVIEWED = "a" * 40
HEAD = "b" * 40
LATER_HEAD = "c" * 40
MERGED_HEAD = "d" * 40

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_GH_LOG"], "a") as log:
        log.write(json.dumps(args) + "\\n")
    routes = json.load(open(os.environ["FAKE_GH_ROUTES"]))
    path = args[-1]
    if args[0] != "api" or path not in routes:
        sys.stderr.write(f"gh: Server Error (HTTP 500) {path}\\n")
        sys.exit(1)
    body = routes[path]
    if body is None:
        sys.stdout.write('{"message":"Not Found","status":"404"}')
        sys.stderr.write("gh: Not Found (HTTP 404)\\n")
        sys.exit(1)
    if isinstance(body, str):
        sys.stdout.write(body)
    else:
        sys.stdout.write(json.dumps([body] if "--slurp" in args else body))
    """)


def event(**over):
    base = {"v": 1, "kind": "decision", "source": "conclave",
            "subject": {"repo": REPO, "pr": 7, "sha": REVIEWED, "finding": "f1"},
            "at": "2026-01-02T03:04:05+00:00", "data": {"action": "finding_posted"}}
    base.update(over)
    return base


def contents(file, sha):
    return f"repos/{REPO}/contents/{file}?ref={sha}"


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="guild-ledger-")
        self.tmp = Path(self.tmp_ctx.name)
        self.ledger = self.tmp / "state" / "ledger.jsonl"
        self.council = self.tmp / "council"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(0o755)
        self.log = self.tmp / "gh.log"
        self.routes_file = self.tmp / "routes.json"

    def tearDown(self) -> None:
        self.tmp_ctx.cleanup()

    def run_tool(self, *args, stdin=None):
        env = dict(os.environ, GUILD_LEDGER=str(self.ledger), FAKE_GH_LOG=str(self.log),
                   FAKE_GH_ROUTES=str(self.routes_file),
                   PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")
        return subprocess.run([sys.executable, str(TOOL), *args], input=stdin, capture_output=True,
                              text=True, env=env, timeout=60)

    def lines(self):
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]

    # --- append ----------------------------------------------------------

    def test_append_is_idempotent(self) -> None:
        first = self.run_tool("append", stdin=json.dumps(event()))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(first.stdout.startswith("appended "), first.stdout)
        again = self.run_tool("append", stdin=json.dumps(event()))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout.split()[1], first.stdout.split()[1])
        self.assertTrue(again.stdout.startswith("exists "), again.stdout)
        rows = self.lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], first.stdout.split()[1])
        self.assertIn("recorded_at", rows[0])

    def test_recorded_at_is_not_part_of_the_id(self) -> None:
        a = self.run_tool("append", stdin=json.dumps(event(recorded_at="2026-01-01T00:00:00+00:00")))
        b = self.run_tool("append", stdin=json.dumps(event(recorded_at="2026-02-01T00:00:00+00:00")))
        self.assertEqual(a.stdout.split()[1], b.stdout.split()[1])
        self.assertEqual(len(self.lines()), 1)

    def test_malformed_event_fails_closed_and_writes_nothing(self) -> None:
        bad = [
            "{not json",
            json.dumps([]),
            json.dumps(event(v=2)),
            json.dumps(event(v=True)),
            json.dumps(event(kind="guess")),
            json.dumps(event(source="")),
            json.dumps(event(extra=1)),
            json.dumps(event(subject={"repo": "nope", "pr": 7, "sha": REVIEWED})),
            json.dumps(event(subject={"repo": REPO, "pr": 0, "sha": REVIEWED})),
            json.dumps(event(subject={"repo": REPO, "pr": True, "sha": REVIEWED})),
            json.dumps(event(subject={"repo": REPO, "pr": 7, "sha": "abc"})),
            json.dumps(event(subject={"repo": REPO, "pr": 7, "sha": REVIEWED, "finding": ""})),
            json.dumps(event(subject={"repo": REPO, "pr": 7, "sha": REVIEWED, "who": "x"})),
            json.dumps(event(at="yesterday")),
            json.dumps({k: v for k, v in event().items() if k != "at"}),
            json.dumps(event(data={})),
            json.dumps(event(kind="outcome", data={"action": "x"})),
            json.dumps(event(id="0" * 64)),
        ]
        for text in bad:
            with self.subTest(event=text):
                p = self.run_tool("append", stdin=text)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn("malformed event", p.stderr)
        self.assertFalse(self.ledger.exists())

    def test_malformed_ledger_line_fails_closed(self) -> None:
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text("{half a line\n", encoding="utf-8")
        p = self.run_tool("append", stdin=json.dumps(event()))
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), "{half a line\n")

    # --- backfill --------------------------------------------------------

    def write_run(self, name, posted, findings=None) -> None:
        run = self.council / name
        run.mkdir(parents=True)
        (run / "posted.json").write_text(json.dumps(posted), encoding="utf-8")
        if findings is not None:
            (run / "findings.json").write_text(json.dumps({"findings": findings}), encoding="utf-8")

    def fixture(self, head=HEAD) -> None:
        """PR 7, open: one finding per proxy result. PR 8, merged before its review posted."""
        def inline(fid, path, line, comment_id, ballot="run-7"):
            return {"id": fid, "ballot": ballot, "posted_as": "inline", "path": path, "line": line,
                    "comment_id": comment_id, "url": "https://example.invalid"}
        self.write_run("run-7", {"repo": REPO, "reviews": {"7": {
            "commit_id": REVIEWED, "state": "COMMENTED", "review_id": 70,
            "posted_at": "2026-01-02T00:00:00+00:00",
            "findings": {
                "run-7:f1": inline("f1", "lib/gone.rb", 3, 101),
                "run-7:f2": inline("f2", "lib/kept.rb", 5, 102),
                "run-7:f3": inline("f3", "lib/kept.rb", 9, 103),
                "run-7:f4": inline("f4", "lib/moved.rb", 2, 104),
                "run-7:f5": inline("f5", "lib/kept.rb", 1, 105),
                "run-7:f6": inline("f6", "lib/deleted.rb", 4, 106),
                "run-7:f7": {"id": "f7", "ballot": "run-7", "posted_as": "review body"},
            }}}}, findings=[
            {"id": "f1", "cited_code": "  def gone\n    old_call(arg)\n  end"},
            {"id": "f2", "cited_code": "keep_me(1)"},
            {"id": "f3", "cited_code": "also_kept"},
            {"id": "f4", "cited_code": "never_there()"},
            {"id": "f5"},
            {"id": "f6", "cited_code": "doomed()"},
            {"id": "f7", "cited_code": "anything"},
        ])
        self.write_run("run-8", {"repo": REPO, "reviews": {
            "8": {"commit_id": MERGED_HEAD, "state": "COMMENTED", "review_id": 80,
                  "posted_at": "2026-01-05T06:00:00+00:00",
                  "findings": {"run-8:f1": inline("f1", "lib/late.rb", 1, 201, ballot="run-8")}},
            "9": {"commit_id": MERGED_HEAD, "state": "APPROVED", "review_id": 90,
                  "posted_at": "2026-01-05T06:00:00+00:00", "findings": {}},
        }}, findings=[{"id": "f1", "cited_code": "late_line"}])
        author = {"login": "alex-acme"}
        routes = {
            f"repos/{REPO}/pulls/7": {"user": author, "head": {"sha": head}, "state": "open",
                                      "merged_at": None, "closed_at": None},
            f"repos/{REPO}/pulls/7/comments?per_page=100": [
                {"id": 101, "user": {"login": "reviewer"}},
                {"id": 110, "in_reply_to_id": 101, "user": author},
                {"id": 111, "in_reply_to_id": 102, "user": author},
                {"id": 112, "in_reply_to_id": 103, "user": {"login": "someone-else"}},
            ],
            contents("lib/gone.rb", REVIEWED): "class A\n  def gone\n    old_call(arg)\n  end\nend\n",
            contents("lib/gone.rb", head): "class A\n  def gone\n    new_call(arg)\n  end\nend\n",
            contents("lib/kept.rb", REVIEWED): "keep_me(1)\nalso_kept\n",
            contents("lib/kept.rb", head): "# moved\nkeep_me(1)\n  also_kept   \n",
            contents("lib/moved.rb", REVIEWED): "something_else()\n",
            contents("lib/deleted.rb", REVIEWED): "doomed()\n",
            contents("lib/deleted.rb", head): None,
            f"repos/{REPO}/pulls/8": {"user": author, "head": {"sha": MERGED_HEAD}, "state": "closed",
                                      "merged_at": "2026-01-05T00:12:00Z",
                                      "closed_at": "2026-01-05T00:12:00Z"},
            f"repos/{REPO}/pulls/8/comments?per_page=100": [],
            contents("lib/late.rb", MERGED_HEAD): "late_line\n",
            f"repos/{REPO}/pulls/9": {"user": author, "head": {"sha": MERGED_HEAD}, "state": "closed",
                                      "merged_at": "2026-01-05T00:12:00Z",
                                      "closed_at": "2026-01-05T00:12:00Z"},
            f"repos/{REPO}/pulls/9/comments?per_page=100": [],
        }
        self.routes_file.write_text(json.dumps(routes), encoding="utf-8")

    def outcomes(self):
        return {(e["subject"]["pr"], e["subject"]["finding"]): e["data"]
                for e in self.lines() if e["kind"] == "outcome"}

    def test_backfill_joins_each_finding_to_its_outcome(self) -> None:
        self.fixture()
        p = self.run_tool("backfill", "--council-dir", str(self.council))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = {k: (v["result"], v.get("reason")) for k, v in self.outcomes().items()}
        self.assertEqual(got, {
            (7, "f1"): ("gone", None),
            (7, "f2"): ("present", None),
            (7, "f3"): ("present", None),
            (7, "f4"): ("untestable", "control_failed"),
            (7, "f5"): ("untestable", "no_cited_code"),
            (7, "f6"): ("file_deleted", None),
            (7, "f7"): ("untestable", "no_path"),
            (8, "f1"): ("present", None),
        })
        out = self.outcomes()
        self.assertTrue(out[(7, "f1")]["author_replied"])
        self.assertTrue(out[(7, "f2")]["author_replied"])
        self.assertEqual((out[(7, "f3")]["replies"], out[(7, "f3")]["author_replied"]), (1, False))
        self.assertTrue(all(d["proxy"] == "cited-code-at-head" for d in out.values()))
        self.assertTrue(out[(8, "f1")]["posted_after_merge"])
        self.assertFalse(out[(7, "f1")]["posted_after_merge"])
        self.assertEqual(out[(7, "f1")]["head_sha"], HEAD)
        decisions = [e for e in self.lines() if e["kind"] == "decision"]
        self.assertEqual(sum(1 for e in decisions if e["data"]["action"] == "review_posted"), 3)
        self.assertEqual(sum(1 for e in decisions if e["data"]["action"] == "finding_posted"), 8)
        self.assertIn("posted findings 8  testable 5", p.stdout)
        self.assertIn("gone 1  present 3  file_deleted 1", p.stdout)
        self.assertIn("events appended 19  already in ledger 0", p.stdout)
        calls = self.log.read_text(encoding="utf-8")
        self.assertNotIn("graphql", calls)
        # f4's control failed, so its head copy is never fetched.
        self.assertNotIn(contents("lib/moved.rb", HEAD), calls)

    def test_rerun_adds_nothing(self) -> None:
        self.fixture()
        self.assertEqual(self.run_tool("backfill", "--council-dir", str(self.council)).returncode, 0)
        before = self.ledger.read_text(encoding="utf-8")
        p = self.run_tool("backfill", "--council-dir", str(self.council))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("events appended 0  already in ledger 19", p.stdout)
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), before)

    def test_new_head_appends_only_new_outcomes(self) -> None:
        self.fixture()
        self.run_tool("backfill", "--council-dir", str(self.council))
        routes = json.loads(self.routes_file.read_text(encoding="utf-8"))
        self.fixture_routes_for_head(routes, LATER_HEAD)
        p = self.run_tool("backfill", "--council-dir", str(self.council))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        added = self.lines()[19:]
        self.assertEqual(len(added), 7, p.stdout)  # PR 7's seven outcomes; no decision repeats
        self.assertTrue(all(e["kind"] == "outcome" and e["data"]["head_sha"] == LATER_HEAD for e in added))

    def fixture_routes_for_head(self, routes, head) -> None:
        routes[f"repos/{REPO}/pulls/7"]["head"]["sha"] = head
        for key in list(routes):
            if key.endswith(f"?ref={HEAD}"):
                routes[key.replace(HEAD, head)] = routes[key]
        self.routes_file.write_text(json.dumps(routes), encoding="utf-8")

    def test_github_error_fails_closed(self) -> None:
        self.fixture()
        routes = json.loads(self.routes_file.read_text(encoding="utf-8"))
        del routes[contents("lib/kept.rb", HEAD)]  # the fake answers HTTP 500
        self.routes_file.write_text(json.dumps(routes), encoding="utf-8")
        p = self.run_tool("backfill", "--council-dir", str(self.council))
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("HTTP 500", p.stderr)
        self.assertEqual(self.lines(), [])

    def test_missing_council_dir_fails_closed(self) -> None:
        p = self.run_tool("backfill", "--council-dir", str(self.tmp / "nope"))
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
