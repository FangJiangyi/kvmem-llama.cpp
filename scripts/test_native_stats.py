import json
import unittest
from speed_canary import parse_perf
from native_stats import response_report, speculative_report


class NativeStatsTests(unittest.TestCase):
    def test_native_fields_and_first_token_are_preserved(self):
        body = {"usage": {"completion_tokens": 5}, "timings": {
            "predicted_n": 5, "predicted_ms": 40, "predicted_per_second": 100,
            "draft_n": 6, "draft_n_accepted": 3}}
        report = response_report(body, "ninfer")
        self.assertEqual(report["gen_toks"], 100)
        self.assertEqual(report["native"]["timings"], body["timings"])
        self.assertEqual(report["speculative"]["acceptance_rate"], .5)
        self.assertFalse(report["speculative"]["available"])
        self.assertNotIn("ms_per_round", report["speculative"])

    def test_replay_and_fallback_use_explicit_rounds(self):
        spec = speculative_report(dict(drafted_tokens=6, accepted_tokens=3,
            generation_rounds=5, output_tokens=7, wall_ms=100,
            restore_rounds=1, fallback_rounds=1))
        self.assertEqual(spec["tokens_per_round"], 1.4)
        self.assertEqual(spec["ms_per_round"], 20)
        self.assertEqual(spec["restore_rounds"], 1)

    def test_zero_rounds_and_zero_drafts_are_unavailable(self):
        spec = speculative_report(dict(drafted_tokens=0, accepted_tokens=0,
            generation_rounds=0, output_tokens=1, wall_ms=0))
        self.assertIsNone(spec["acceptance_rate"])
        self.assertIsNone(spec["ms_per_round"])
        self.assertFalse(spec["available"])

    def test_invalid_stats_fail_instead_of_guessing(self):
        for changes in ({"accepted_tokens": 7}, {"generation_rounds": -1},
                        {"wall_ms": float("nan")}):
            with self.assertRaises(ValueError):
                speculative_report(dict(drafted_tokens=6, accepted_tokens=3,
                    generation_rounds=5, output_tokens=7, wall_ms=100) | changes)

    def test_cli_mtp_uses_committed_output_and_matching_wall_time(self):
        log = "KVMEM_PERF load_ms=1 prompt_n=10 prompt_ms=2 prompt_toks=5000 gen_n=3 gen_ms=60 gen_toks=50\n"
        baseline = parse_perf(log)
        self.assertEqual(baseline.gen_toks, 50)
        spec = dict(drafted_tokens=6, accepted_tokens=3, generation_rounds=3,
                    output_tokens=7, wall_ms=100)
        report = parse_perf(log + "KVMEM_SPEC_METRICS " + json.dumps(spec) + "\n")
        self.assertEqual(report.gen_n, 7)
        self.assertEqual(report.gen_toks, 70)
        self.assertEqual(report.speculative["ms_per_round"], 100 / 3)


if __name__ == "__main__":
    unittest.main()
