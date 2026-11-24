"""Behavior tests for lint-scorecard and scorecard-append (Paul prose-to-checks #8)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(os.path.dirname(HERE), "bin")
LINT = os.path.join(ROOT, "lint-scorecard")
APPEND = os.path.join(ROOT, "scorecard-append")

TS = "2026-11-20T07:00:00-08:00"
TS2 = "2026-11-20T07:00:05-08:00"


def good_decision(did="cole-x-2026-11-24", bot="Code Cole"):
    return [
        {"ts": TS, "bot": bot, "decision_id": did, "stage": "pending",
         "options": {"A": "go", "B": "hold"}, "bot_lean": "A", "bot_lean_ts": TS,
         "automatable_bot": "no", "gate_candidate": ""},
        {"ts": TS2, "bot": bot, "decision_id": did, "stage": "jev",
         "jev_lean": "A", "jev_ts": TS2, "jev_conf": 0.8, "automatable_jev": "no"},
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.log = os.path.join(self.dir, "log.jsonl")
        agents = os.path.join(self.dir, "agents")
        for i, name in enumerate(["Code Cole · Personal code & fleet tooling",
                                  "Work Wes · Work coding lead", "Family"]):
            os.makedirs(os.path.join(agents, str(i)))
            with open(os.path.join(agents, str(i), "profile.json"), "w") as f:
                json.dump({"name": name}, f)
        self.env = dict(os.environ, AGENTS_DIR=agents, SCORECARD_FILE=self.log)

    def tearDown(self):
        self.tmp.cleanup()

    def read_bytes(self):
        with open(self.log, "rb") as f:
            return f.read()

    def read_text(self):
        with open(self.log, encoding="utf-8") as f:
            return f.read()

    def write(self, rows, raw_lines=()):
        with open(self.log, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
            for line in raw_lines:
                f.write(line + "\n")

    def baseline(self, n):
        with open(self.log + ".lint-baseline", "w") as f:
            f.write(f"# lint-scorecard baseline\nlines: {n}\n")

    def lint(self, *args):
        return subprocess.run([sys.executable, LINT, *args], env=self.env,
                              capture_output=True, text=True)

    def append(self, row_text, *args, env=None):
        return subprocess.run([sys.executable, APPEND, *args], input=row_text,
                              env=env or self.env, capture_output=True, text=True)

    def env_with(self, **kv):
        env = {k: v for k, v in self.env.items()
               if k not in ("SCORECARD_FILE", "HITL_SCORECARD_LOG")}
        env.update(kv)
        return env


class LintTests(Base):
    def test_clean_log_passes_fail_any(self):
        self.write(good_decision())
        r = self.lint("--fail-any")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 bad", r.stdout)

    def test_noncanonical_bot_names_flagged_with_fix(self):
        rows = good_decision(bot="work-wes")
        self.write(rows)
        r = self.lint("--fail-any")
        self.assertEqual(r.returncode, 1)
        self.assertIn("BOT", r.stdout)
        self.assertIn("'Work Wes'", r.stdout)  # suggests canonical form

    def test_suffix_and_placeholder_bot_names_flagged(self):
        for bad in ("Work Wes overnight", "Work Wes · Work coding lead", "?", "A", "Family"):
            self.write(good_decision(bot=bad))
            r = self.lint("--fail-any")
            self.assertEqual(r.returncode, 1, bad)

    def test_missing_decision_id_flagged(self):
        rows = good_decision()
        del rows[1]["decision_id"]
        self.write(rows)
        r = self.lint("--fail-any")
        self.assertEqual(r.returncode, 1)
        self.assertIn("DECISION_ID", r.stdout)

    def test_bad_json_line_flagged(self):
        self.write(good_decision(), raw_lines=["{not json"])
        r = self.lint("--fail-any")
        self.assertEqual(r.returncode, 1)
        self.assertIn("JSON", r.stdout)

    def test_decision_without_jev_lean_flagged(self):
        self.write(good_decision()[:1])
        r = self.lint("--fail-any")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NO_JEV_LEAN", r.stdout)

    def test_jev_unavailable_with_reason_passes(self):
        rows = good_decision()
        rows[1] = {"ts": TS2, "bot": "Code Cole", "decision_id": rows[0]["decision_id"],
                   "stage": "jev", "jev_lean": "unavailable", "jev_reason": "API 503 after retry"}
        self.write(rows)
        r = self.lint("--fail-any")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_jev_unavailable_without_reason_flagged(self):
        rows = good_decision()
        rows[1] = {"ts": TS2, "bot": "Code Cole", "decision_id": rows[0]["decision_id"],
                   "stage": "jev", "jev_lean": "unavailable"}
        self.write(rows)
        r = self.lint("--fail-any")
        self.assertEqual(r.returncode, 1)
        self.assertIn("JEV_REASON", r.stdout)

    def test_missing_bot_lean_ts_flagged(self):
        rows = good_decision()
        del rows[0]["bot_lean_ts"]
        self.write(rows)
        r = self.lint("--fail-any")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NO_BOT_LEAN_TS", r.stdout)

    def test_null_bot_lean_counts_as_present(self):
        rows = good_decision()
        rows[0]["bot_lean"] = None
        self.write(rows)
        self.assertEqual(self.lint("--fail-any").returncode, 0)

    def test_jev_before_bot_lean_flagged(self):
        rows = good_decision()
        rows[0]["bot_lean_ts"] = TS2
        rows[1]["jev_ts"] = TS
        self.write(rows)
        r = self.lint("--fail-any")
        self.assertEqual(r.returncode, 1)
        self.assertIn("LEAN_ORDER", r.stdout)

    def test_default_mode_reports_but_exits_zero(self):
        self.write(good_decision(bot="?"))
        r = self.lint()
        self.assertEqual(r.returncode, 0)
        self.assertIn("BOT", r.stdout)

    def test_fail_new_ignores_rows_inside_baseline(self):
        legacy = good_decision(did="old-1", bot="wes")
        self.write(legacy + good_decision(did="new-1"))
        self.baseline(len(legacy))
        r = self.lint("--fail-new")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("old", r.stdout)

    def test_fail_new_fails_on_row_after_baseline(self):
        legacy = good_decision(did="old-1")
        self.write(legacy + good_decision(did="new-1", bot="code-cole"))
        self.baseline(len(legacy))
        r = self.lint("--fail-new")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NEW", r.stdout)

    def test_fail_new_without_baseline_is_loud_not_silent(self):
        self.write(good_decision(bot="?"))
        r = self.lint("--fail-new")
        self.assertEqual(r.returncode, 2)
        self.assertIn("baseline", r.stderr)

    def test_by_bot_counts(self):
        self.write(good_decision(did="a", bot="wes") + good_decision(did="b", bot="wes"))
        r = self.lint("--by-bot")
        self.assertIn("wes: 4", r.stdout)

    def test_json_output(self):
        self.write(good_decision(bot="?"))
        r = self.lint("--json")
        data = json.loads(r.stdout)
        self.assertGreaterEqual(data["bad"], 1)
        self.assertTrue(all({"line", "code", "why", "new"} <= set(f) for f in data["findings"]))

    def test_fresh_pending_row_inside_grace_not_flagged(self):
        self.write(good_decision()[:1])
        r = self.lint("--fail-any", "--now", "2026-11-20T07:05:00-08:00")
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.lint("--fail-any", "--now", "2026-11-20T08:00:00-08:00")
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_check_mode_validates_one_row_on_stdin(self):
        ok = json.dumps(good_decision()[0])
        r = subprocess.run([sys.executable, LINT, "--check"], input=ok, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        bad = json.dumps(dict(good_decision()[0], bot="cole"))
        r = subprocess.run([sys.executable, LINT, "--check"], input=bad, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)

    def test_missing_file_exits_2(self):
        r = self.lint("--file", os.path.join(self.dir, "nope.jsonl"))
        self.assertEqual(r.returncode, 2)

    def test_lint_never_edits_log(self):
        self.write(good_decision(bot="?"))
        before = self.read_bytes()
        self.lint("--fail-any")
        self.assertEqual(self.read_bytes(), before)


class AppendTests(Base):
    def test_good_row_appended_once(self):
        self.write(good_decision())
        row = {"ts": TS, "bot": "Code Cole", "decision_id": "cole-y", "stage": "pending",
               "options": {"A": "x"}, "bot_lean": "A", "bot_lean_ts": TS,
               "automatable_bot": "no"}
        r = self.append(json.dumps(row))
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = self.read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[-1])["decision_id"], "cole-y")

    def test_bad_bot_refused_file_unchanged(self):
        self.write(good_decision())
        before = self.read_bytes()
        row = {"ts": TS, "bot": "work-wes", "decision_id": "w-1", "stage": "pending"}
        r = self.append(json.dumps(row))
        self.assertEqual(r.returncode, 1)
        self.assertIn("Work Wes", r.stderr)
        self.assertEqual(self.read_bytes(), before)

    def test_missing_ts_refused(self):
        self.write([])
        r = self.append(json.dumps({"bot": "Code Cole", "decision_id": "c", "stage": "pending"}))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.read_text(), "")

    def test_not_json_refused(self):
        self.write([])
        r = self.append("hello")
        self.assertEqual(r.returncode, 1)

    def test_multiline_input_refused(self):
        self.write([])
        row = json.dumps({"ts": TS, "bot": "Code Cole", "decision_id": "c", "stage": "pending"})
        r = self.append(row + "\n" + row)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.read_text(), "")

    def test_bad_automatable_value_refused(self):
        self.write([])
        row = {"ts": TS, "bot": "Code Cole", "decision_id": "c", "stage": "pending",
               "automatable_bot": "yes"}
        r = self.append(json.dumps(row))
        self.assertEqual(r.returncode, 1)
        self.assertIn("automatable_bot", r.stderr)


def pending_row(did, bot="Code Cole"):
    return {"ts": TS, "bot": bot, "decision_id": did, "stage": "pending",
            "options": {"A": "go"}, "bot_lean": "A", "bot_lean_ts": TS,
            "automatable_bot": "no"}


class EnvAliasTests(Base):
    """$HITL_SCORECARD_LOG is an alias of $SCORECARD_FILE (Paul audit 2026-11-24 change 1).

    Precedence: --file, then the env vars, then the fleet default. If both env vars
    are set to different files, both tools refuse (exit 2) instead of guessing.
    """

    def other_log(self):
        path = os.path.join(self.dir, "other.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps(good_decision(did="other")[0]) + "\n")
        return path

    def test_lint_reads_hitl_alias_when_only_it_is_set(self):
        self.write(good_decision())
        env = self.env_with(HITL_SCORECARD_LOG=self.log)
        r = subprocess.run([sys.executable, LINT, "--json"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["file"], self.log)

    def test_append_writes_hitl_alias_when_only_it_is_set(self):
        self.write([])
        r = self.append(json.dumps(pending_row("alias-1")),
                        env=self.env_with(HITL_SCORECARD_LOG=self.log))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.read_text().splitlines()), 1)

    def test_both_set_to_same_file_is_fine(self):
        self.write([])
        env = self.env_with(HITL_SCORECARD_LOG=self.log, SCORECARD_FILE=self.log)
        self.assertEqual(self.append(json.dumps(pending_row("same-1")), env=env).returncode, 0)
        r = subprocess.run([sys.executable, LINT], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_both_set_to_different_files_refuses(self):
        self.write([])
        other = self.other_log()
        env = self.env_with(HITL_SCORECARD_LOG=other, SCORECARD_FILE=self.log)
        r = self.append(json.dumps(pending_row("diff-1")), env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("HITL_SCORECARD_LOG", r.stderr)
        self.assertEqual(self.read_text(), "")
        with open(other) as f:
            self.assertEqual(len(f.read().splitlines()), 1)
        r = subprocess.run([sys.executable, LINT], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("HITL_SCORECARD_LOG", r.stderr)

    def test_file_flag_beats_both_env_vars(self):
        self.write([])
        other = self.other_log()
        env = self.env_with(HITL_SCORECARD_LOG=other, SCORECARD_FILE=other + ".nope")
        r = self.append(json.dumps(pending_row("flag-1")), "--file", self.log, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.read_text().splitlines()), 1)
        r = subprocess.run([sys.executable, LINT, "--json", "--file", self.log], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["file"], self.log)


class LockAndBaselineTests(Base):
    def test_concurrent_appends_lose_nothing(self):
        self.write([])
        n = 20
        procs = [subprocess.Popen([sys.executable, APPEND], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=self.env, text=True) for _ in range(n)]
        for i, p in enumerate(procs):
            p.stdin.write(json.dumps(pending_row(f"lock-{i:02d}")))
            p.stdin.close()
        codes = [p.wait() for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(codes, [0] * n)
        text = self.read_text()
        self.assertTrue(text.endswith("\n"))
        ids = [json.loads(line)["decision_id"] for line in text.splitlines()]
        self.assertEqual(sorted(ids), [f"lock-{i:02d}" for i in range(n)])

    def test_write_baseline_counts_rows_like_the_lint(self):
        rows = good_decision(did="old-1", bot="wes")
        self.write(rows, raw_lines=["", "   "])
        r = self.lint("--write-baseline")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.log + ".lint-baseline") as f:
            self.assertIn("lines: 2\n", f.read())
        with open(self.log, "a") as f:
            f.write(json.dumps(pending_row("new-1", bot="code-cole")) + "\n")
        r = self.lint("--fail-new", "--json")
        data = json.loads(r.stdout)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(sorted({f["decision_id"] for f in data["findings"] if f["new"]}), ["new-1"])
        self.assertTrue(all(not f["new"] for f in data["findings"] if f["decision_id"] == "old-1"))

    def test_jev_raw_is_not_a_jev_lean(self):
        rows = good_decision()
        rows[1] = {"ts": TS2, "bot": "Code Cole", "decision_id": rows[0]["decision_id"],
                   "stage": "jev", "jev_raw": "A", "jev_ts": TS2}
        self.write(rows)
        r = self.lint("--fail-any")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NO_JEV_LEAN", r.stdout)


if __name__ == "__main__":
    unittest.main()
