import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from impression_study.generation import OpenAIBackend, generate
from impression_study.reader_study import exact_mcnemar, make_plan, paired_summary, run_session
from impression_study.review import REVIEW_FIELDS, export_review, load_ratings, summarize
from impression_study.schemas import QUALITY_FIELDS, model_input, validate_cases, validate_output
from impression_study.storage import read_csv, read_json, read_jsonl, write_csv, write_jsonl


ROOT = Path(__file__).resolve().parents[1]
CASES = str(ROOT / "data/examples/cases.jsonl")
REFS = str(ROOT / "data/examples/references.jsonl")
READERS = str(ROOT / "data/examples/readers.jsonl")
CONFIG = str(ROOT / "configs/study.json")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def generation(self):
        path = self.out / "run"
        generate(CASES, CONFIG, str(path), "mock", None)
        return path

    def review(self):
        run = self.generation()
        path = self.out / "review"
        export_review(CASES, REFS, str(run / "predictions.jsonl"), str(path), 2026)
        return path

    def ratings(self, review):
        rows = read_csv(review / "reviewer/ratings.csv")
        for row in rows:
            row.update({
                "evaluator_id": "E1", "diagnostic_correct": "1", "clinically_acceptable": "1",
                "major_error": "0", "omitted_item_ids": "[]", "unsupported_item_indices": "[]",
                "contradicted_item_indices": "[]", **{f: "4" for f in QUALITY_FIELDS},
            })
        return rows

    def test_patient_split_leakage_and_extra_answer_field_rejected(self):
        rows = read_jsonl(CASES)
        rows[1]["patient_id"] = rows[0]["patient_id"]
        with self.assertRaisesRegex(ValueError, "leaks"):
            validate_cases(rows)
        rows = read_jsonl(CASES)
        rows[1]["impression"] = "leaked answer"
        with self.assertRaisesRegex(ValueError, "unexpected"):
            validate_cases(rows)

    def test_input_allowlist_and_evaluation_only(self):
        case = read_jsonl(CASES)[1]
        payload = model_input(case)
        self.assertEqual(set(payload), {"modality", "body_part", "clinical_info", "findings"})
        run = self.generation()
        records = read_jsonl(run / "predictions.jsonl")
        self.assertEqual({r["case_id"] for r in records}, {"EVAL001", "EVAL002", "EVAL003"})
        self.assertEqual(read_json(run / "manifest.json")["status"], "completed")
        self.assertTrue(all(r["demo_only"] for r in records))
        with self.assertRaises(FileExistsError):
            generate(CASES, CONFIG, str(run), "mock", None)

    def test_unknown_evidence_rejected_but_semantics_not_auto_judged(self):
        case = read_jsonl(CASES)[1]
        output = {"impression": [{"text": "故意错误的医学结论", "evidence_ids": ["F1"], "certainty": "certain"}],
                  "recommendations": [], "limitations": []}
        validate_output(output, case)  # Structural validation is NOT clinical evaluation.
        output["impression"][0]["evidence_ids"] = ["not-a-finding"]
        with self.assertRaisesRegex(ValueError, "Unknown evidence"):
            validate_output(output, case)

    def test_review_is_blinded_and_incomplete_ratings_fail(self):
        review = self.review()
        for packet in read_jsonl(review / "reviewer/packets.jsonl"):
            self.assertEqual(set(packet), {"sample_id", "input", "reference", "output"})
            self.assertNotIn("case_id", packet["reference"])
        with self.assertRaises(ValueError):
            load_ratings(str(review / "reviewer/ratings.csv"), str(review / "private/key.jsonl"))
        rows = self.ratings(review)[:-1]
        path = self.out / "partial.csv"
        write_csv(path, REVIEW_FIELDS, rows)
        with self.assertRaisesRegex(ValueError, "Incomplete review"):
            load_ratings(str(path), str(review / "private/key.jsonl"))

    def test_summary_uses_human_labels_and_checklist_counts(self):
        review = self.review()
        rows = self.ratings(review)
        keys = {k["sample_id"]: k for k in read_jsonl(review / "private/key.jsonl")}
        for row in rows:
            if keys[row["sample_id"]]["record"]["case_id"] == "EVAL002":
                row.update({"diagnostic_correct": "0", "clinically_acceptable": "0", "major_error": "1",
                            "omitted_item_ids": '["R1"]', "unsupported_item_indices": "[1]"})
        path = self.out / "complete.csv"
        write_csv(path, REVIEW_FIELDS, rows)
        result = summarize(str(path), str(review / "private/key.jsonl"), str(self.out / "summary.json"))
        group = result["groups"][0]
        self.assertEqual(group["diagnostic_accuracy"]["numerator"], 2)
        self.assertEqual(group["checklist_recall"]["value"], 4 / 5)
        self.assertEqual(group["critical_item_omission_rate"]["value"], 1)
        self.assertEqual(group["unsupported_item_rate"]["value"], 1 / 6)
        self.assertTrue(result["demo_only"])

    def test_review_cannot_double_count_or_use_invalid_indices(self):
        review = self.review()
        rows = self.ratings(review)
        rows[0]["unsupported_item_indices"] = "[99]"
        path = self.out / "bad.csv"
        write_csv(path, REVIEW_FIELDS, rows)
        with self.assertRaisesRegex(ValueError, "1-based"):
            load_ratings(str(path), str(review / "private/key.jsonl"))

    def test_counterbalanced_plan_and_exact_p_values(self):
        path = self.out / "plan.csv"
        make_plan(CASES, READERS, CONFIG, "2026-10-26", str(path))
        assignments = read_csv(path)
        self.assertEqual(len(assignments), 12)
        for reader in ("DEMO-JUNIOR", "DEMO-SENIOR"):
            for case in ("EVAL001", "EVAL002", "EVAL003"):
                pair = [a for a in assignments if a["reader_id"] == reader and a["case_id"] == case]
                self.assertEqual({a["condition"] for a in pair}, {"aided", "unaided"})
                dates = sorted(datetime.fromisoformat(a["scheduled_date"]) for a in pair)
                self.assertEqual((dates[1] - dates[0]).days, 28)
        self.assertEqual(exact_mcnemar(0, 0), 1)
        self.assertAlmostEqual(exact_mcnemar(4, 0), 0.125)
        self.assertAlmostEqual(exact_mcnemar(2, 2), 1)

    def test_reader_actual_washout_enforced(self):
        run = self.generation()
        plan = self.out / "plan.csv"
        make_plan(CASES, READERS, CONFIG, "2000-01-01", str(plan))
        path = self.out / "reader.jsonl"
        # Enter: readiness, impression, end, recommendations end, limitations end.
        answers = [answer for _ in range(3) for answer in ("", "模拟印象", "", "", "")]
        with patch("builtins.input", side_effect=answers), patch("builtins.print"):
            run_session(CASES, str(plan), str(run / "predictions.jsonl"), "DEMO-JUNIOR", 1, str(path))
        self.assertEqual(len(read_jsonl(path)), 3)
        with self.assertRaisesRegex(ValueError, "washout"):
            run_session(CASES, str(plan), str(run / "predictions.jsonl"), "DEMO-JUNIOR", 2, str(path))

    def test_openai_request_is_single_turn_and_omits_unsupported_temperature(self):
        # Exercise the actual adapter with an injected transport, without an SDK/network dependency.
        captured = []

        def create(**params):
            captured.append(params)
            return SimpleNamespace(model_dump=lambda **kwargs: {"choices": []})

        adapter = object.__new__(OpenAIBackend)
        adapter.model = "configured-model-snapshot"
        adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        payload = model_input(read_jsonl(CASES)[1])
        adapter.generate("LOCKED", payload, {"temperature": None, "max_completion_tokens": 100})
        self.assertEqual(len(captured), 1)
        params = captured[0]
        self.assertEqual(params["model"], "configured-model-snapshot")
        self.assertNotIn("temperature", params)
        self.assertEqual([m["role"] for m in params["messages"]], ["system", "user"])
        self.assertEqual(json.loads(params["messages"][1]["content"]), payload)
        self.assertTrue(params["response_format"]["json_schema"]["strict"])

    def test_failed_model_response_preserved_before_json_parsing(self):
        raw = {"choices": [{"finish_reason": "stop", "message": {"content": "not JSON", "refusal": None}}]}
        fake = SimpleNamespace(model="test-model", generate=lambda *args: raw)
        path = self.out / "failed"
        with patch("impression_study.generation.OpenAIBackend", return_value=fake):
            with self.assertRaises(ValueError):
                generate(CASES, CONFIG, str(path), "openai", "test-model")
        self.assertEqual(read_json(path / "manifest.json")["status"], "failed")
        self.assertFalse((path / "predictions.jsonl").exists())
        error = read_jsonl(path / "errors.jsonl")[0]
        self.assertEqual(error["case_id"], "EVAL001")
        self.assertEqual(read_json(path / "raw" / f"{error['record_id']}.json"), raw)

    def test_paired_pipeline_keeps_readers_separate_and_excludes_incomplete_pairs(self):
        run = self.generation()
        records = []
        for prediction in read_jsonl(run / "predictions.jsonl"):
            for reader in ("DEMO-JUNIOR", "DEMO-SENIOR"):
                for session, condition in ((1, "unaided"), (2, "aided")):
                    if reader == "DEMO-SENIOR" and prediction["case_id"] == "EVAL003" and condition == "aided":
                        continue
                    records.append({**prediction, "record_id": f"{reader}-{prediction['case_id']}-{session}",
                                    "source": "reader", "source_id": reader, "condition": condition,
                                    "session": session, "elapsed_seconds": 50 if condition == "aided" else 60})
        records_path = self.out / "readers.jsonl"
        write_jsonl(records_path, records)
        review = self.out / "reader_review"
        export_review(CASES, REFS, str(records_path), str(review), 2026)
        keys = {k["sample_id"]: k for k in read_jsonl(review / "private/key.jsonl")}
        rows = self.ratings(review)
        for row in rows:
            row["diagnostic_correct"] = str(int(keys[row["sample_id"]]["record"]["condition"] == "aided"))
        path = self.out / "reader_ratings.csv"
        write_csv(path, REVIEW_FIELDS, rows)
        result = paired_summary(str(path), str(review / "private/key.jsonl"), READERS, str(self.out / "paired.json"))
        self.assertEqual(result["incomplete_pairs"], 1)
        self.assertTrue(result["demo_only"])
        by_reader = {r["reader_id"]: r for r in result["readers"]}
        self.assertEqual(by_reader["DEMO-JUNIOR"]["pairs"], 3)
        self.assertEqual(by_reader["DEMO-SENIOR"]["pairs"], 2)
        self.assertEqual(by_reader["DEMO-JUNIOR"]["mean_time_difference_seconds"], -10)
        self.assertAlmostEqual(by_reader["DEMO-JUNIOR"]["exploratory_exact_mcnemar_p"], 0.25)

    def test_reader_second_session_after_actual_washout_and_resume(self):
        run = self.generation()
        plan = self.out / "plan.csv"
        make_plan(CASES, READERS, CONFIG, "2000-01-01", str(plan))
        reader_path = self.out / "readers.jsonl"
        answers = [a for _ in range(3) for a in ("", "模拟印象", "", "", "")]
        with patch("builtins.input", side_effect=answers), patch("builtins.print"):
            run_session(CASES, str(plan), str(run / "predictions.jsonl"), "DEMO-JUNIOR", 1, str(reader_path))
        records = copy.deepcopy(read_jsonl(reader_path))
        older = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()
        for record in records:
            record["created_at"] = older
        old_path = self.out / "aged_readers.jsonl"
        write_jsonl(old_path, records)
        with patch("builtins.input", side_effect=answers), patch("builtins.print"):
            result = run_session(CASES, str(plan), str(run / "predictions.jsonl"), "DEMO-JUNIOR", 2, str(old_path))
        self.assertEqual(result["new_records"], 3)
        self.assertEqual(len(read_jsonl(old_path)), 6)
        with patch("builtins.input", side_effect=AssertionError("Should skip completed cases")):
            resumed = run_session(CASES, str(plan), str(run / "predictions.jsonl"), "DEMO-JUNIOR", 2, str(old_path))
        self.assertEqual(resumed["new_records"], 0)


if __name__ == "__main__":
    unittest.main()
