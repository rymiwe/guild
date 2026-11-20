#!/usr/bin/env python3
"""grokbot-pace-check: one 1.1x pace threshold on the Grok Bot weekly meter, fail closed."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "bin" / "grokbot-pace-check"
WEEK = 604800.0
NOW = 1_800_000_000.0  # fixed clock for every case


def meter(used_pct, elapsed_frac, age_s=300.0, **extra):
    """A quartermaster latest.json cut to the Grok Bot fields, read age_s before NOW."""
    read_at = NOW - age_s
    start = read_at - elapsed_frac * WEEK
    data = {
        "v": 1,
        "ts": read_at,
        "as_of": {"grokbot": read_at},
        "bot_pct": used_pct,
        "bot_reset_ts": start + WEEK,
        "bot_window_s": WEEK,
        "bot_exhausted": False,
    }
    data.update(extra)
    return data


class PaceCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="pace-check-")
        self.tmp = Path(self.tmp_ctx.name)
        self.cache = self.tmp / "latest.json"

    def tearDown(self) -> None:
        self.tmp_ctx.cleanup()

    def run_tool(self, *args, env_extra=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SEAT_USAGE_CACHE_DIR", "GUILD_PACE_MAX_AGE_S")}
        env.update(env_extra or {})
        cmd = [sys.executable, str(TOOL), "--now", str(NOW), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)

    def write(self, data) -> None:
        self.cache.write_text(json.dumps(data), encoding="utf-8")

    def assert_one_line(self, proc) -> str:
        out = proc.stdout.strip()
        self.assertTrue(out, f"no output; stderr={proc.stderr!r}")
        self.assertEqual(len(out.splitlines()), 1, out)
        return out

    def test_invalid_meter_fields_hold(self) -> None:
        for overrides in ({"bot_exhausted": None}, {"bot_exhausted": 1},
                          {"bot_window_s": 1}, {"bot_pct": 10 ** 400}):
            with self.subTest(overrides=overrides):
                self.write(meter(4.0, 0.5, **overrides))
                result = self.run_tool("--cache", str(self.cache))
                self.assertEqual(result.returncode, 3)
                self.assert_one_line(result)

    def test_full_allowance_stops_even_when_under_pace(self) -> None:
        self.write(meter(100.0, 0.95))
        result = self.run_tool("--cache", str(self.cache))
        self.assertEqual(result.returncode, 1)
        self.assert_one_line(result)

    def test_nonfinite_clock_is_bad_usage(self) -> None:
        self.write(meter(4.0, 0.5))
        result = self.run_tool("--cache", str(self.cache), "--now", "nan")
        self.assertEqual(result.returncode, 2)
        self.assert_one_line(result)

    # --- the threshold -------------------------------------------------

    def test_above_threshold_fails(self) -> None:
        self.write(meter(30.0, 0.25))  # 1.20x
        p = self.run_tool("--cache", str(self.cache))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        line = self.assert_one_line(p)
        self.assertIn("1.20x", line)
        self.assertIn("1.1x", line)

    def test_just_above_threshold_fails(self) -> None:
        # 1.15x passed under the old 1.2x halve tier; the single 1.1x threshold stops it.
        self.write(meter(23.0, 0.20))
        p = self.run_tool("--cache", str(self.cache))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        line = self.assert_one_line(p)
        self.assertNotIn("halve", line.lower())
        self.assertNotIn("pause", line.lower())

    def test_just_over_threshold_unrounded_fails(self) -> None:
        # 1.104x prints as 1.10x but is above 1.1x, so it must stop.
        self.write(meter(11.04, 0.10))
        p = self.run_tool("--cache", str(self.cache))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assert_one_line(p)

    def test_future_reading_fails_closed(self) -> None:
        self.write(meter(20.0, 0.50, age_s=-600))  # stamped 10 min after now
        p = self.run_tool("--cache", str(self.cache))
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("future", self.assert_one_line(p))

    def test_used_pct_out_of_range_fails_closed(self) -> None:
        for bad in (-0.5, 100.5):
            with self.subTest(bot_pct=bad):
                self.write(meter(bad, 0.50))
                p = self.run_tool("--cache", str(self.cache))
                self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
                self.assertIn("bot_pct", self.assert_one_line(p))

    def test_exactly_threshold_passes(self) -> None:
        self.write(meter(11.0, 0.10))  # 1.10x
        p = self.run_tool("--cache", str(self.cache))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("1.10x", self.assert_one_line(p))

    def test_below_threshold_passes(self) -> None:
        self.write(meter(20.0, 0.50))  # 0.40x
        p = self.run_tool("--cache", str(self.cache))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0.40x", self.assert_one_line(p))

    def test_early_week_usage_under_floor_passes(self) -> None:
        # Two hours in, 1.1x is 1.3% used; one busy session must not stop every routine.
        self.write(meter(4.9, 2 / 168))
        p = self.run_tool("--cache", str(self.cache))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("floor", self.assert_one_line(p))

    def test_early_week_usage_over_floor_fails(self) -> None:
        self.write(meter(5.0, 2 / 168))
        p = self.run_tool("--cache", str(self.cache))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assert_one_line(p)

    def test_exhausted_fails(self) -> None:
        self.write(meter(10.0, 0.50, bot_exhausted=True))
        p = self.run_tool("--cache", str(self.cache))
        self.assertNotEqual(p.returncode, 0)
        self.assert_one_line(p)

    # --- fail closed ---------------------------------------------------

    def test_missing_file_fails(self) -> None:
        p = self.run_tool("--cache", str(self.tmp / "nope.json"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("nope.json", self.assert_one_line(p))

    def test_missing_field_fails(self) -> None:
        data = meter(20.0, 0.50)
        del data["bot_pct"]
        self.write(data)
        p = self.run_tool("--cache", str(self.cache))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("bot_pct", self.assert_one_line(p))

    def test_missing_reading_stamp_fails(self) -> None:
        data = meter(20.0, 0.50)
        data["as_of"] = {}
        self.write(data)
        p = self.run_tool("--cache", str(self.cache))
        self.assertNotEqual(p.returncode, 0)
        self.assert_one_line(p)

    def test_unparseable_fails(self) -> None:
        self.cache.write_text("{not json", encoding="utf-8")
        p = self.run_tool("--cache", str(self.cache))
        self.assertNotEqual(p.returncode, 0)
        self.assert_one_line(p)

    def test_stale_fails(self) -> None:
        self.write(meter(20.0, 0.50, age_s=2 * 3600))  # default window is 1 hour
        p = self.run_tool("--cache", str(self.cache))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("stale", self.assert_one_line(p))

    def test_staleness_window_is_configurable(self) -> None:
        self.write(meter(20.0, 0.50, age_s=2 * 3600))
        p = self.run_tool("--cache", str(self.cache), "--max-age-s", "10800")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_tool("--cache", str(self.cache), env_extra={"GUILD_PACE_MAX_AGE_S": "10800"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.write(meter(20.0, 0.50, age_s=600))
        p = self.run_tool("--cache", str(self.cache), "--max-age-s", "300")
        self.assertNotEqual(p.returncode, 0)

    def test_ended_window_fails(self) -> None:
        # The reading belongs to a week that has already reset.
        self.write(meter(20.0, 0.50, age_s=300, bot_reset_ts=NOW - 60))
        p = self.run_tool("--cache", str(self.cache))
        self.assertNotEqual(p.returncode, 0)
        self.assert_one_line(p)

    def test_default_path_follows_quartermaster_cache_dir(self) -> None:
        self.write(meter(20.0, 0.50))
        p = self.run_tool(env_extra={"SEAT_USAGE_CACHE_DIR": str(self.tmp)})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_tool(env_extra={"SEAT_USAGE_CACHE_DIR": str(self.tmp / "empty")})
        self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
