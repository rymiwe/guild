"""Behavior tests for bin/poteto-judge (Paul's 13 prototype tests, ported, plus the shared-call,
never-gated, no-Cursor-fallback, timeout-budget and card-truncation tests).

Run: python3 tests/test_poteto_judge.py -v
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "bin", "poteto-judge")
TRUE_CHECK = os.path.join(ROOT, "bin", "true-check")
RUBRIC = os.path.join(ROOT, "docs", "rubric-v1.md")

ROWS = [
    {"ts": "2026-11-20T12:45:03-08:00", "bot": "Ledger Leo", "decision_id": "d1", "stage": "pending",
     "options": {"A": "Hide dupes now", "B": "Hold cleanup", "C": "Ask first"},
     "bot_lean": "A", "notes": "restore validation"},
    {"ts": "2026-11-20T12:45:04-08:00", "decision_id": "d1", "stage": "jev", "jev_lean": "B"},
    {"ts": "2026-11-20T12:46:00-08:00", "decision_id": "d1", "stage": "card-built", "card_id": "abc"},
    {"ts": "2026-11-20T13:07:00-08:00", "decision_id": "d1", "stage": "resolution", "ryan_pick": "A",
     "notes": "RYAN SAID A"},
]


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def stub(d, out, rc=0, sleep=0, name="stub.sh", count=None):
    seen = os.path.join(d, "prompt.txt")
    path = os.path.join(d, name)
    tally = f"echo x >> {count}\n" if count else ""
    with open(path, "w") as f:
        f.write(f"#!/bin/sh\n{tally}cat > {seen}\nsleep {sleep}\nprintf '%s' '{out}'\nexit {rc}\n")
    os.chmod(path, 0o755)
    return path, seen


def run(stub_out, stub_rc=0, sleep=0, timeout="5", did="d1", extra_env=None):
    d = tempfile.mkdtemp()
    log = os.path.join(d, "log.jsonl")
    with open(log, "w") as f:
        for r in ROWS:
            f.write(json.dumps(r) + "\n")
    cmd, seen = stub(d, stub_out, stub_rc, sleep)
    env = dict(os.environ, POTETO_RUBRIC=RUBRIC, HITL_SCORECARD_LOG=log,
               POTETO_JUDGE_CMD=cmd, POTETO_JUDGE_TIMEOUT=timeout)
    env.pop("SCORECARD_FILE", None)
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, TOOL, "card", did], capture_output=True, text=True, env=env)
    prompt = ""
    if os.path.exists(seen):
        with open(seen) as f:
            prompt = f.read()
    with open(log) as f:
        after = f.read()
    return p, prompt, after, log


GOOD = '{"lean": "B", "principle": "Subtract Before You Add", "why": "hold the risky half", "conf": 0.7}'


class T(unittest.TestCase):
    def test_good_answer_prints_one_card_line_naming_principle(self):
        p, _, _, _ = run(GOOD)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["line"], "🥔 Poteto: B · Subtract Before You Add")
        self.assertEqual(out["row"]["poteto_lean"], "B")
        self.assertEqual(out["row"]["poteto_principle"], "Subtract Before You Add")
        self.assertIn("poteto_rubric", out["row"])

    def test_unlisted_principle_is_unavailable_not_a_lean(self):
        p, _, _, _ = run('{"lean": "B", "principle": "Vibes", "why": "x", "conf": 0.9}')
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        self.assertTrue(out["line"].startswith("🥔 Poteto: unavailable"), out)
        self.assertEqual(out["row"]["poteto_lean"], "unavailable")

    def test_letter_not_in_options_is_unavailable(self):
        p, _, _, _ = run('{"lean": "Z", "principle": "Build the Lever", "why": "x", "conf": 0.9}')
        self.assertEqual(json.loads(p.stdout)["row"]["poteto_lean"], "unavailable")

    def test_model_error_never_blocks(self):
        p, _, _, _ = run("boom", stub_rc=1)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(json.loads(p.stdout)["line"].startswith("🥔 Poteto: unavailable"))

    def test_timeout_never_blocks(self):
        p, _, _, _ = run(GOOD, sleep=3, timeout="1")
        self.assertEqual(p.returncode, 0)
        self.assertIn("timeout", json.loads(p.stdout)["row"]["poteto_reason"])

    def test_null_lean_is_shown_as_no_lean(self):
        p, _, _, _ = run('{"lean": null, "principle": null, "why": "taste call", "conf": 0.2}')
        self.assertEqual(json.loads(p.stdout)["line"], "🥔 Poteto: no lean (taste call)")

    def test_blind_packet_has_no_leans_and_no_post_card_rows(self):
        _, prompt, _, _ = run(GOOD)
        self.assertIn("Hide dupes now", prompt)
        self.assertNotIn("RYAN SAID A", prompt)
        self.assertNotIn("bot_lean", prompt)
        self.assertNotIn("jev_lean", prompt)

    def test_never_writes_the_scorecard(self):
        _, _, after, _ = run(GOOD)
        self.assertEqual(after.count("\n"), len(ROWS))


RULE = ('{"lean": "A", "principle": "Build the Lever", "why": "x", "conf": 0.8, "automate": "rule", '
        '"rule_kind": "check", "rule": "Hide restored rows that pair 1:1 by date and amount after a CSV backup", '
        '"automate_why": "same pattern each restore"}')
RULE_TEXT = "Hide restored rows that pair 1:1 by date and amount after a CSV backup"


class Automate(unittest.TestCase):
    def test_rule_answer_gives_one_text_line_and_a_candidate(self):
        p, _, _, _ = run(RULE)
        out = json.loads(p.stdout)
        # Paul audit change 4: the card line carries at most 60 characters of the rule.
        self.assertEqual(out["text"], "🤖 Automate: rule (check) · " + RULE_TEXT[:59] + "…")
        self.assertEqual(out["row"]["auto_state"], "rule")
        c = out["candidate"]
        self.assertEqual((c["kind"], c["bot"], c["decision_id"]), ("check", "Ledger Leo", "d1"))
        self.assertEqual(len(c["key"]), 12)
        self.assertEqual(c["rule"], RULE_TEXT)  # the full rule goes to rule-candidates

    def test_rule_without_rule_text_is_unavailable_and_no_candidate(self):
        p, _, _, _ = run('{"lean": "A", "principle": "Build the Lever", "why": "x", "automate": "rule", '
                         '"rule_kind": null, "rule": null}')
        out = json.loads(p.stdout)
        self.assertEqual(out["row"]["auto_state"], "unavailable")
        self.assertIsNone(out["candidate"])
        self.assertEqual(out["line"], "🥔 Poteto: A · Build the Lever")

    def test_human_and_assist_lines(self):
        p, _, _, _ = run('{"lean": null, "principle": null, "why": "taste", "automate": "human", '
                         '"automate_why": "taste call"}')
        self.assertEqual(json.loads(p.stdout)["text"], "🤖 Automate: keep human · taste call")
        p, _, _, _ = run('{"lean": null, "principle": null, "why": "t", "automate": "assist", '
                         '"automate_why": "script drafts it"}')
        self.assertEqual(json.loads(p.stdout)["text"], "🤖 Automate: assist · script drafts it")

    def test_never_gated_action_cannot_be_rule(self):
        p, _, _, _ = run('{"lean": "A", "principle": "Build the Lever", "why": "x", "automate": "rule", '
                         '"rule_kind": "gate-row", "rule": "Auto-send the nudge email to Tim"}')
        self.assertEqual(json.loads(p.stdout)["row"]["auto_state"], "unavailable")


class Rubric(unittest.TestCase):
    def test_every_rubric_principle_is_accepted(self):
        _, names, _ = load(TOOL, "poteto_judge_r").rubric(RUBRIC)
        for n in ("Outcome-Oriented Execution", "Separate Before Serializing Shared State", "Build the Lever"):
            self.assertIn(n, names)
        self.assertEqual(len(names), 17)


class Shared(unittest.TestCase):
    """One judge, shared with true-check (design 2, audit changes 2 and 3)."""

    def test_rubric_principles_are_true_check_principles(self):
        pj, tc = load(TOOL, "poteto_judge_s1"), load(TRUE_CHECK, "true_check_s1")
        _, names, _ = pj.rubric(RUBRIC)
        for n in names:
            self.assertIn(pj.principle_slug(n), tc.PRINCIPLE_NAMES, n)

    def test_never_gated_list_is_read_from_true_check(self):
        # "phishing" is only in true-check's NEVER_GATED, not in the keyword backstop.
        p, _, _, _ = run('{"lean": "A", "principle": "Build the Lever", "why": "x", "automate": "rule", '
                         '"rule_kind": "policy", "rule": "Mark phishing lookalikes as read"}')
        row = json.loads(p.stdout)["row"]
        self.assertEqual((row["auto_state"], row["auto_reason"]), ("unavailable", "never-gated action"))

    def test_shared_call_true_check_reads_only_e_and_p(self):
        """One request carries true-check's E/P questions and the poteto question. The same
        answer gives true-check its letters (poteto keys ignored) and poteto-judge its lean."""
        pj, tc = load(TOOL, "poteto_judge_s2"), load(TRUE_CHECK, "true_check_s2")
        subject = {"action": "local-build", "goal": "make the card lean deterministic",
                   "diff": {"patch": "diff --git a/x.py b/x.py\n"}, "evidence": ["tests/test_x.py"]}
        packet = {"bot": "Ledger Leo", "options": {"A": "go", "B": "hold"}}
        req = pj.shared_request(packet, subject, RUBRIC)
        self.assertEqual(set(req["questions"]), {"E", "P", "poteto"})
        self.assertEqual(req["questions"]["E"], tc.judge_request(subject, False, None, None)["questions"]["E"])
        self.assertEqual(req["principles"], list(tc.PRINCIPLE_NAMES))
        ep = {"E": {"choice": "pass", "confidence": 0.9, "cites": "tests/test_x.py"},
              "P": {"choice": "pass", "confidence": 0.9}}
        combined = {"id": "c1", "answers": dict(ep, poteto={
            "lean": "A", "principle": "Prove It Works", "why": "w", "conf": 0.8,
            "automate": "assist", "automate_why": "script preps it"})}
        self.assertEqual(tc.judge_letters(combined, "", subject),
                         tc.judge_letters({"id": "c1", "answers": ep}, "", subject))
        a = pj.answer_of(combined)
        self.assertEqual((a["lean"], a["principle"]), ("A", "Prove It Works"))


class Seat(unittest.TestCase):
    def test_default_seat_is_gemini_neon_low_through_agy(self):
        pj = load(TOOL, "poteto_judge_seat")
        self.assertEqual(pj.DEFAULT_MODEL, "gemini-4-neon-low")
        self.assertTrue(pj.default_cmd().startswith("agy --model gemini-4-neon-low "))
        self.assertEqual(pj.DEFAULT_TIMEOUT, 20.0)

    def test_no_agy_is_unavailable_and_never_falls_back_to_cursor(self):
        d = tempfile.mkdtemp()
        calls = os.path.join(d, "cursor-calls")
        for name in ("cursor-agent", "cursor", "agent", "judge-ask"):
            stub(d, GOOD, name=name, count=calls)
        log = os.path.join(d, "log.jsonl")
        with open(log, "w") as f:
            for r in ROWS:
                f.write(json.dumps(r) + "\n")
        env = {"PATH": d + ":/usr/bin:/bin", "HITL_SCORECARD_LOG": log, "POTETO_RUBRIC": RUBRIC}
        p = subprocess.run([sys.executable, TOOL, "card", "d1"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["row"]["poteto_lean"], "unavailable")
        self.assertFalse(os.path.exists(calls), "a Cursor or other seat was called")

    def test_one_retry_inside_the_budget_then_unavailable(self):
        d = tempfile.mkdtemp()
        calls = os.path.join(d, "calls")
        cmd, _ = stub(d, "boom", rc=1, count=calls)
        p, _, _, _ = run("unused", extra_env={"POTETO_JUDGE_CMD": cmd})
        self.assertEqual(json.loads(p.stdout)["row"]["poteto_lean"], "unavailable")
        with open(calls) as f:
            self.assertEqual(len(f.read().split()), 2)

    def test_row_carries_bot_and_model_for_the_scorecard(self):
        p, _, _, _ = run(GOOD)
        row = json.loads(p.stdout)["row"]
        self.assertEqual((row["bot"], row["stage"], row["decision_id"]), ("Ledger Leo", "poteto-shadow", "d1"))
        self.assertIn("poteto_model", row)
        self.assertIsInstance(row["poteto_latency_s"], float)


if __name__ == "__main__":
    unittest.main()
