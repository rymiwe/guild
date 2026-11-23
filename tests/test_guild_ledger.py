#!/usr/bin/env python3
"""guild-ledger: the outcome ledger's writer and its posted-findings backfill."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
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

    def run_tool(self, *args, stdin=None, tool=TOOL):
        env = dict(os.environ, GUILD_LEDGER=str(self.ledger), FAKE_GH_LOG=str(self.log),
                   FAKE_GH_ROUTES=str(self.routes_file),
                   FAKE_GUILD_CONFIG=str(self.tmp / "guild.json"),
                   FAKE_NOTIFY_LOG=str(self.tmp / "notify.log"),
                   PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")
        env.pop("GUILD_LEDGER_COUNCIL_DIR", None)
        return subprocess.run([sys.executable, str(tool), *args], input=stdin, capture_output=True,
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
            json.dumps(event(data={"action": "human_stop"})),
            json.dumps(event(data={"action": "human_stop", "stop": "unknown_stop"})),
            json.dumps(event(data={"action": "human_stop", "stop": 123})),
            json.dumps(event(data={"action": "human_stop", "stop": "posting_go", "answered_at": "yesterday"})),
            json.dumps(event(data={"action": "human_stop", "stop": "posting_go", "answered_at": 123})),
        ]
        for text in bad:
            with self.subTest(event=text):
                p = self.run_tool("append", stdin=text)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn("malformed event", p.stderr)
        self.assertFalse(self.ledger.exists())

    def test_human_stop_accepts_known_stops_and_optional_answered_at(self) -> None:
        stops = ("split_decision", "posting_go", "manual_merge", "ack_loop", "one_way_ack", "brief_approval")
        for i, stop in enumerate(stops, 1):
            data = {"action": "human_stop", "stop": stop}
            if i == 1:
                data["answered_at"] = "2026-01-02T03:05:00+00:00"
            elif i == 2:
                data["answered_at"] = None
            p = self.run_tool("append", stdin=json.dumps(
                event(subject={"repo": REPO, "pr": i, "sha": REVIEWED, "finding": None}, data=data)))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.lines()), len(stops))

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

    def test_backfill_reports_outcomes_by_verification_tier(self) -> None:
        self.fixture()
        f7 = self.council / "run-7" / "findings.json"
        doc = json.loads(f7.read_text(encoding="utf-8"))
        doc["verified_at"] = REVIEWED
        doc["findings"][0]["repro_executed"] = True
        f7.write_text(json.dumps(doc), encoding="utf-8")
        p = self.run_tool("backfill", "--council-dir", str(self.council))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(
            "by verification: executed 1/1 gone (1 total)  checked 0/3 gone (6 total)  none 0/1 gone (1 total)",
            p.stdout,
        )

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


    # --- backfill-prs ----------------------------------------------------

    def pulls_page(self, page):
        return (f"repos/{REPO}/pulls?state=closed&sort=updated&direction=desc"
                f"&per_page=100&page={page}")

    def pr_fixture(self, old_last_on_page_one=False):
        """101 merged PRs over two list pages, one revert, one closed unmerged, one old PR.

        With old_last_on_page_one the first page ends on a PR last updated before the
        window, so the list must stop there without asking for page 2."""
        now = datetime.now(timezone.utc)
        iso = lambda hours_ago: (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        author = {"login": "alex-acme", "type": "User"}

        def pr(n, opened, ended, merged=True, title="Add a widget", body=""):
            return {"number": n, "user": author, "head": {"sha": f"{n:040x}"}, "title": title,
                    "body": body, "created_at": iso(opened), "closed_at": iso(ended),
                    "merged_at": iso(ended) if merged else None, "updated_at": iso(ended - 0.01)}
        # Newest-updated first, as GitHub sorts it. PR n merged n hours ago after n/100 hours open.
        merged = [pr(n, n + n / 100, n) for n in range(1, 102)]
        merged[4] = pr(5, 6, 5, title='Revert "Add a widget"', body="Reverts AcmeCorp/widgets#3")
        merged[5] = pr(6, 7, 6, title="Revert the gadget", body="It broke the build.")
        unmerged = pr(102, 200, 150, merged=False)
        old = pr(103, 2000, 1000)
        routes = {}
        if old_last_on_page_one:
            routes[self.pulls_page(1)] = merged[:99] + [old]
            listed = merged[:99]
        else:
            routes[self.pulls_page(1)] = merged[:100]
            routes[self.pulls_page(2)] = [merged[100], unmerged, old]
            listed = merged + [unmerged]
        for item in listed:
            routes[f"repos/{REPO}/pulls/{item['number']}/reviews?per_page=100"] = []
        routes[f"repos/{REPO}/pulls/7/reviews?per_page=100"] = [
            {"user": {"login": "pat-acme", "type": "User"}, "state": "CHANGES_REQUESTED"},
            {"user": {"login": "pat-acme", "type": "User"}, "state": "APPROVED"},
            {"user": {"login": "sam-acme", "type": "User"}, "state": "COMMENTED"},
            {"user": {"login": "sam-acme", "type": "User"}, "state": "PENDING"},
            {"user": author, "state": "COMMENTED"},
            {"user": {"login": "lint-bot", "type": "Bot"}, "state": "COMMENTED"},
            {"user": {"login": "scanner[bot]", "type": "User"}, "state": "COMMENTED"},
        ]
        self.routes_file.write_text(json.dumps(routes), encoding="utf-8")

    def pr_outcomes(self):
        return {e["subject"]["pr"]: e for e in self.lines()}

    def test_backfill_prs_records_each_closed_pr(self) -> None:
        self.pr_fixture()
        p = self.run_tool("backfill-prs", "--repo", REPO, "--days", "30")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = self.pr_outcomes()
        self.assertEqual(sorted(out), list(range(1, 103)))  # past page 1; PR 103 closed too early
        e = out[7]
        self.assertEqual((e["kind"], e["source"], e["subject"]["sha"], e["subject"]["finding"]),
                         ("outcome", "github", f"{7:040x}", None))
        self.assertEqual(e["at"], e["data"]["merged_at"])
        self.assertEqual((e["data"]["proxy"], e["data"]["state"], e["data"]["author"]),
                         ("pr-lifecycle", "merged", "alex-acme"))
        # pat twice and sam once; the author, both bots and the pending review do not count.
        self.assertEqual((e["data"]["reviewers"], e["data"]["reviews"]), (2, 3))
        self.assertAlmostEqual(e["data"]["hours_open"], 0.07, places=2)
        self.assertEqual((out[5]["data"]["is_revert"], out[5]["data"]["reverts_pr"]), (True, 3))
        self.assertEqual((out[6]["data"]["is_revert"], out[6]["data"]["reverts_pr"]), (True, None))
        self.assertEqual((out[8]["data"]["is_revert"], out[8]["data"]["reverts_pr"]), (False, None))
        closed = out[102]
        self.assertEqual((closed["data"]["state"], closed["at"]), ("closed", closed["data"]["closed_at"]))
        self.assertNotIn("merged_at", closed["data"])
        self.assertAlmostEqual(closed["data"]["hours_open"], 50, places=2)
        # Hours open are n/100 for PR n, except the two reverts at 1 h. Of 101: median rank 51,
        # nearest-rank p90 rank 91.
        self.assertIn(f"{REPO}: merged 101  closed unmerged 1  median 0.53 h  p90 0.93 h"
                      "  reverts 2 [3]  authors 1  reviewed by others 1", p.stdout)
        self.assertIn("events appended 102  already in ledger 0", p.stdout)
        self.assertNotIn("graphql", self.log.read_text(encoding="utf-8"))

    def test_backfill_prs_rerun_adds_nothing(self) -> None:
        self.pr_fixture()
        self.assertEqual(self.run_tool("backfill-prs", "--repo", REPO).returncode, 0)
        before = self.ledger.read_text(encoding="utf-8")
        p = self.run_tool("backfill-prs", "--repo", REPO)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("events appended 0  already in ledger 102", p.stdout)
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), before)

    def test_backfill_prs_stops_paging_at_the_window(self) -> None:
        self.pr_fixture(old_last_on_page_one=True)  # no page 2 route: asking for it fails
        p = self.run_tool("backfill-prs", "--repo", REPO)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(sorted(self.pr_outcomes()), list(range(1, 100)))
        self.assertNotIn("page=2", self.log.read_text(encoding="utf-8"))

    def test_backfill_prs_github_error_fails_closed(self) -> None:
        self.pr_fixture()
        routes = json.loads(self.routes_file.read_text(encoding="utf-8"))
        del routes[f"repos/{REPO}/pulls/101/reviews?per_page=100"]  # the fake answers HTTP 500
        self.routes_file.write_text(json.dumps(routes), encoding="utf-8")
        p = self.run_tool("backfill-prs", "--repo", REPO)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("HTTP 500", p.stderr)
        self.assertEqual(self.lines(), [])

    def test_backfill_prs_bad_arguments(self) -> None:
        for args in ([], ["--repo", "nope"], ["--repo", REPO, "--days", "0"],
                     ["--repo", REPO, "--days", "x"]):
            with self.subTest(args=args):
                p = self.run_tool("backfill-prs", *args)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse(self.log.exists())


    # --- refresh ---------------------------------------------------------

    ORG_REPO = "OtherOrg/app"
    TOOLS = ["AcmeCorp/guild", "AcmeCorp/tool-a", "AcmeCorp/tool-b"]

    def refresh_fixture(self, org_key=True) -> Path:
        """A Guild checkout under AcmeCorp with a two-tool roster, the council fixture, one
        closed PR on the org repo, and fake guild-config and job-notify."""
        checkout = self.tmp / "guild"
        (checkout / "bin").mkdir(parents=True)
        shutil.copy(TOOL, checkout / "bin" / "guild-ledger")
        (checkout / "roster").write_text("# tools\ntool-a\n\ntool-b  # second\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                        "git@github.com:AcmeCorp/guild.git"], check=True)
        for name, body in {
            "guild-config": """\
                #!/usr/bin/env python3
                import json, os, sys
                key = sys.argv[-1]
                try:
                    cfg = json.load(open(os.environ["FAKE_GUILD_CONFIG"]))
                    for part in key.split("."):
                        cfg = cfg[part]
                except (OSError, KeyError):
                    sys.stderr.write(f"guild-config: guild.json: missing key {key}\\n")
                    sys.exit(3)
                print(json.dumps(cfg))
                """,
            "job-notify": """\
                #!/usr/bin/env python3
                import json, os, sys
                with open(os.environ["FAKE_NOTIFY_LOG"], "a") as log:
                    log.write(json.dumps(sys.argv[1:]) + "\\n")
                """,
        }.items():
            path = self.bin / name
            path.write_text(textwrap.dedent(body), encoding="utf-8")
            path.chmod(0o755)
        if org_key:
            (self.tmp / "guild.json").write_text(
                json.dumps({"guild-ledger": {"pr_repos": [self.ORG_REPO]}}), encoding="utf-8")
        self.fixture()
        routes = json.loads(self.routes_file.read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for repo in [*self.TOOLS, self.ORG_REPO]:
            routes[f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=1"] = []
        routes[f"repos/{self.ORG_REPO}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=1"] = [
            {"number": 4, "user": {"login": "alex-acme", "type": "User"}, "head": {"sha": "e" * 40},
             "title": "Add a widget", "body": "", "created_at": now, "closed_at": now,
             "merged_at": now, "updated_at": now}]
        routes[f"repos/{self.ORG_REPO}/pulls/4/reviews?per_page=100"] = []
        self.routes_file.write_text(json.dumps(routes), encoding="utf-8")
        return checkout / "bin" / "guild-ledger"

    def pings(self):
        log = self.tmp / "notify.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_refresh_runs_both_backfills_and_a_rerun_adds_nothing(self) -> None:
        tool = self.refresh_fixture()
        p = self.run_tool("refresh", "--council-dir", str(self.council), tool=tool)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.lines()), 20)  # 19 from the council fixture, 1 PR lifecycle
        listed = [json.loads(line)[-1].split("/pulls?")[0].removeprefix("repos/")
                  for line in self.log.read_text().splitlines() if "/pulls?state=closed" in line]
        self.assertEqual(listed, [*self.TOOLS, self.ORG_REPO])
        self.assertTrue(p.stdout.rstrip().endswith("refresh ok"), p.stdout)
        before = self.ledger.read_text(encoding="utf-8")
        again = self.run_tool("refresh", "--council-dir", str(self.council), tool=tool)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), before)
        self.assertEqual(again.stdout.count("events appended 0"), 2, again.stdout)
        self.assertEqual(self.pings(), [])

    def test_refresh_failure_pings_once_per_kind_until_it_passes(self) -> None:
        tool = self.refresh_fixture()
        missing = str(self.tmp / "nope")
        p = self.run_tool("refresh", "--council-dir", missing, tool=tool)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual(len(self.lines()), 1)  # the PR step still ran
        self.assertIn("refresh FAILED: council", p.stdout)
        pings = self.pings()
        self.assertEqual(len(pings), 1, pings)
        self.assertEqual(pings[0][:4], ["--send", str(self.ledger.parent), "guild-ledger-refresh", "FAILED"])
        self.assertTrue(pings[0][4].startswith("council: ") and "not a directory" in pings[0][4], pings)
        self.assertEqual(self.run_tool("refresh", "--council-dir", missing, tool=tool).returncode, 3)
        self.assertEqual(len(self.pings()), 1)  # same kind, no second ping
        self.assertEqual(self.run_tool("refresh", "--council-dir", str(self.council), tool=tool).returncode, 0)
        self.assertEqual(self.run_tool("refresh", "--council-dir", missing, tool=tool).returncode, 3)
        self.assertEqual(len(self.pings()), 2)  # a pass in between re-arms the ping

    def test_refresh_without_the_org_key_fails_closed(self) -> None:
        tool = self.refresh_fixture(org_key=False)
        p = self.run_tool("refresh", "--council-dir", str(self.council), tool=tool)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("guild-ledger.pr_repos", p.stderr)
        self.assertEqual(len(self.lines()), 19)  # the council step still ran; no PR was listed
        self.assertNotIn("/pulls?state=closed", self.log.read_text())
        pings = self.pings()
        self.assertEqual(len(pings), 1, pings)
        self.assertTrue(pings[0][4].startswith("prs: "), pings)

    def test_council_dir_comes_from_the_environment(self) -> None:
        env = dict(os.environ, GUILD_LEDGER=str(self.ledger), GUILD_LEDGER_COUNCIL_DIR=str(self.tmp / "nope"))
        p = subprocess.run([sys.executable, str(TOOL), "backfill"], capture_output=True, text=True,
                           env=env, timeout=60)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn(str(self.tmp / "nope"), p.stderr)

    # --- touches ---------------------------------------------------------

    def touches_fixture(self) -> None:
        now = datetime.now(timezone.utc)
        iso = lambda days_ago: (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def pr_outcome(repo, pr, days_ago, state="merged", sha=HEAD):
            ended = iso(days_ago)
            data = {"proxy": "pr-lifecycle", "state": state, "author": "alex-acme",
                    "created_at": iso(days_ago + 1),
                    ("merged_at" if state == "merged" else "closed_at"): ended,
                    "hours_open": 24.0, "reviewers": 0, "reviews": 0,
                    "is_revert": False, "reverts_pr": None}
            return {"v": 1, "kind": "outcome", "source": "github",
                    "subject": {"repo": repo, "pr": pr, "sha": sha, "finding": None},
                    "at": ended, "data": data}

        def stop(repo, pr, name, days_ago=1, sha=REVIEWED, finding=None):
            return {"v": 1, "kind": "decision", "source": "conclave",
                    "subject": {"repo": repo, "pr": pr, "sha": sha, "finding": finding},
                    "at": iso(days_ago),
                    "data": {"action": "human_stop", "stop": name, "answered_at": iso(days_ago)}}

        events = [
            # REPO: PR 1 merged in window with 2 stops (different commits/findings), PR 2 merged with 0 stops.
            pr_outcome(REPO, 1, 2, sha=REVIEWED),
            pr_outcome(REPO, 1, 2, sha=HEAD),  # later outcome on same PR
            stop(REPO, 1, "split_decision", sha=REVIEWED, finding="f1"),
            stop(REPO, 1, "posting_go", sha=HEAD),
            pr_outcome(REPO, 2, 3),
            # PR 3 closed unmerged in window with a stop: excluded from merged PRs and stops on them.
            pr_outcome(REPO, 3, 1, state="closed"),
            stop(REPO, 3, "manual_merge"),
            # PR 4 merged outside the 7-day window with a stop: excluded.
            pr_outcome(REPO, 4, 10),
            stop(REPO, 4, "ack_loop", days_ago=10),
            # Stop on an open PR with no lifecycle outcome: excluded.
            stop(REPO, 5, "brief_approval"),
            # Non-stop decision on PR 1: ignored.
            event(subject={"repo": REPO, "pr": 1, "sha": REVIEWED, "finding": "f1"}),
            # Second repo: 1 merged PR with 3 stops (two of the same stop name).
            pr_outcome(self.ORG_REPO, 10, 1),
            stop(self.ORG_REPO, 10, "posting_go"),
            stop(self.ORG_REPO, 10, "posting_go", days_ago=2),
            stop(self.ORG_REPO, 10, "manual_merge"),
        ]
        for e in events:
            p = self.run_tool("append", stdin=json.dumps(e))
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_touches_joins_human_stops_to_merged_prs_per_repo(self) -> None:
        self.touches_fixture()
        p = self.run_tool("touches")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("(7 days)", p.stdout)
        self.assertIn(
            f"{REPO}: merged 2  human stops 2  touches/merged PR 1.00"
            "  by stop {'posting_go': 1, 'split_decision': 1}",
            p.stdout,
        )
        self.assertIn(
            f"{self.ORG_REPO}: merged 1  human stops 3  touches/merged PR 3.00"
            "  by stop {'manual_merge': 1, 'posting_go': 2}",
            p.stdout,
        )
        self.assertFalse(self.log.exists())

    def test_touches_filters_by_repo_and_handles_zero_stops_and_zero_merged(self) -> None:
        self.touches_fixture()
        # Only PR 2 has 0 stops; test a repo filter and an explicit empty repo.
        empty_repo = "AcmeCorp/empty"
        p = self.run_tool("touches", "--days", "7", "--repo", empty_repo, "--repo", REPO)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn(self.ORG_REPO, p.stdout)
        self.assertIn(f"{empty_repo}: merged 0  human stops 0  touches/merged PR -  by stop {{}}", p.stdout)
        self.assertIn(f"{REPO}: merged 2  human stops 2  touches/merged PR 1.00", p.stdout)

    def test_touches_pr_with_zero_stops(self) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pr = {"v": 1, "kind": "outcome", "source": "github",
              "subject": {"repo": REPO, "pr": 1, "sha": HEAD, "finding": None},
              "at": now,
              "data": {"proxy": "pr-lifecycle", "state": "merged", "author": "alex-acme",
                       "created_at": now, "merged_at": now, "hours_open": 0.1,
                       "reviewers": 0, "reviews": 0, "is_revert": False, "reverts_pr": None}}
        self.assertEqual(self.run_tool("append", stdin=json.dumps(pr)).returncode, 0)
        p = self.run_tool("touches", "--repo", REPO)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"{REPO}: merged 1  human stops 0  touches/merged PR 0.00  by stop {{}}", p.stdout)

    def test_touches_bad_arguments_and_unreadable_ledger(self) -> None:
        for args in (["--repo", "nope"], ["--days", "0"], ["--days", "x"]):
            with self.subTest(args=args):
                p = self.run_tool("touches", *args)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        p = self.run_tool("touches")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
