"""Offline plumbing demo. All cases, reader answers, and ratings are SYNTHETIC."""

import argparse
from pathlib import Path

from impression_study.generation import generate
from impression_study.reader_study import make_plan, paired_summary
from impression_study.review import REVIEW_FIELDS, export_review, summarize
from impression_study.schemas import QUALITY_FIELDS
from impression_study.storage import read_jsonl, write_csv, write_jsonl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="outputs/demo", help="Must be a new directory")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    cases = str(root / "data/examples/cases.jsonl")
    refs = str(root / "data/examples/references.jsonl")
    readers = str(root / "data/examples/readers.jsonl")
    config = str(root / "configs/study.json")
    generate(cases, config, str(out / "generation"), "mock", None)
    predictions = read_jsonl(out / "generation/predictions.jsonl")
    records = list(predictions)
    for prediction in predictions:
        for reader_id in ("DEMO-JUNIOR", "DEMO-SENIOR"):
            for session, condition in ((1, "unaided"), (2, "aided")):
                records.append({
                    **prediction,
                    "record_id": f"SYNTHETIC-{reader_id}-{prediction['case_id']}-{session}",
                    "source": "reader", "source_id": reader_id, "session": session,
                    "condition": condition, "elapsed_seconds": 60 if condition == "unaided" else 50,
                    "model_record_id": prediction["record_id"] if condition == "aided" else None,
                    "demo_only": True,
                })
    write_jsonl(out / "synthetic_records.jsonl", records)
    export_review(cases, refs, str(out / "synthetic_records.jsonl"), str(out / "review"), 2026)
    keys = read_jsonl(out / "review/private/key.jsonl")
    ratings = []
    for key in keys:
        # Deliberately synthetic toggles, NOT automatic clinical evaluation.
        corrected = key["record"]["condition"] == "aided"
        row = {
            "sample_id": key["sample_id"], "evaluator_id": "SYNTHETIC-NOT-EXPERT",
            "diagnostic_correct": int(corrected), "clinically_acceptable": int(corrected),
            "major_error": 0, "omitted_item_ids": "[]", "unsupported_item_indices": "[]",
            "contradicted_item_indices": "[]", "notes": "SYNTHETIC：任意填充值，非专家评分，不能用于研究结果。",
            **{field: 3 for field in QUALITY_FIELDS},
        }
        ratings.append(row)
    write_csv(out / "synthetic_ratings.csv", REVIEW_FIELDS, ratings)
    summarize(str(out / "synthetic_ratings.csv"), str(out / "review/private/key.jsonl"), str(out / "summary.json"))
    paired_summary(str(out / "synthetic_ratings.csv"), str(out / "review/private/key.jsonl"), readers, str(out / "paired.json"))
    make_plan(cases, readers, config, "2026-10-26", str(out / "reader_assignments.csv"))
    print(f"Offline demo complete: {out.resolve()}")
    print("All outputs and ratings are SYNTHETIC. This is NOT evidence of model quality.")


if __name__ == "__main__":
    main()
