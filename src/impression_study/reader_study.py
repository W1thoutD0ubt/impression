"""Counterbalanced text-based reader sessions and paired descriptive analysis."""

import json
import math
import random
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean

from .generation import load_config
from .review import load_ratings, validate_records
from .schemas import exact_keys, model_input, nonempty, require, validate_cases, validate_output
from .storage import (
    append_jsonl, digest, index_unique, now, read_csv, read_jsonl, write_csv, write_json,
)


def load_readers(path: str) -> dict:
    rows = read_jsonl(path)
    readers = index_unique(rows, "reader_id")
    require(bool(readers), "No readers")
    for row in rows:
        exact_keys(row, {"reader_id", "seniority", "years_experience"}, "reader")
        require(nonempty(row["reader_id"]), "reader_id required")
        require(row["seniority"] in {"junior", "senior"}, "Invalid seniority")
        require(type(row["years_experience"]) in (int, float) and 0 <= row["years_experience"] <= 70, "Invalid experience")
    return readers


def make_plan(cases_path: str, readers_path: str, config_path: str, start_date: str, out: str):
    cases = validate_cases(read_jsonl(cases_path))
    readers = load_readers(readers_path)
    config = load_config(config_path)
    selected = [c["case_id"] for c in cases.values() if c["split"] == "evaluation"]
    require(bool(selected), "No evaluation cases")
    rng = random.Random(config["seed"])
    rows = []
    for reader in readers.values():
        order = selected.copy()
        rng.shuffle(order)
        first_aided = set(order[:len(order) // 2])
        for session in (1, 2):
            rng.shuffle(order)
            scheduled = date.fromisoformat(start_date) + timedelta(days=(session - 1) * config["washout_days"])
            for position, case_id in enumerate(order, 1):
                aided = (case_id in first_aided) == (session == 1)
                rows.append({
                    "reader_id": reader["reader_id"], "seniority": reader["seniority"],
                    "case_id": case_id, "session": session, "order": position,
                    "condition": "aided" if aided else "unaided", "scheduled_date": scheduled.isoformat(),
                    "washout_days": config["washout_days"],
                })
    fields = ["reader_id", "seniority", "case_id", "session", "order", "condition", "scheduled_date", "washout_days"]
    write_csv(out, fields, rows)
    return {"assignments": len(rows), "readers": len(readers), "cases": len(selected)}


def read_lines(label: str, *, required: bool = False) -> list[str]:
    print(label + "（每行一项，空行结束）")
    result = []
    while True:
        line = input("> ").strip()
        if not line:
            if required and not result:
                print("至少输入一项。")
                continue
            return result
        result.append(line)


def run_session(cases_path: str, assignments_path: str, predictions_path: str,
                reader_id: str, session: int, out: str):
    cases = validate_cases(read_jsonl(cases_path))
    predictions = list(validate_records(read_jsonl(predictions_path), cases).values())
    require(all(r["source"] == "model" for r in predictions), "Assistance must be standalone model output")
    by_case = index_unique(predictions, "case_id")
    assignments = [r for r in read_csv(assignments_path)
                   if r["reader_id"] == reader_id and int(r["session"]) == session]
    require(bool(assignments), "No assignments for this reader/session")
    index_unique(assignments, "case_id")
    previous = read_jsonl(out) if Path(out).exists() else []
    if previous:
        validate_records(previous, cases)
    require(all(r["source"] == "reader" for r in previous), "Reader output file contains non-reader records")
    done = {(r["source_id"], r["case_id"], r["session"]) for r in previous}
    # Preflight the whole session before presenting any case.
    for assignment in assignments:
        case_id = assignment["case_id"]
        require(case_id in cases and cases[case_id]["split"] == "evaluation", "Invalid assignment case")
        require(cases[case_id]["input_qc_passed"], "Input QC not passed")
        require(assignment["condition"] in {"aided", "unaided"}, "Invalid condition")
        require(int(assignment["washout_days"]) >= 28, "Washout must be >= 28 days")
        require(date.today() >= date.fromisoformat(assignment["scheduled_date"]), "Session scheduled in the future")
        if assignment["condition"] == "aided":
            require(case_id in by_case, "Missing model assistance")
        if session == 2:
            first = [r for r in previous if r["source_id"] == reader_id and r["case_id"] == case_id and r["session"] == 1]
            require(len(first) == 1, "Session 1 result required in the same output file")
            require(first[0]["condition"] != assignment["condition"], "Crossover requires opposite conditions")
            elapsed_days = (datetime.fromisoformat(now()) - datetime.fromisoformat(first[0]["created_at"])).total_seconds() / 86400
            require(elapsed_days >= int(assignment["washout_days"]), "Actual washout interval has not elapsed")
    count = 0
    for assignment in sorted(assignments, key=lambda r: int(r["order"])):
        case_id = assignment["case_id"]
        if (reader_id, case_id, session) in done:
            continue
        case = cases[case_id]
        input("准备好后按 Enter 展示下一例并开始计时。")
        started = time.perf_counter()
        print(json.dumps(model_input(case), ensure_ascii=False, indent=2))
        aided = assignment["condition"] == "aided"
        if aided:
            print("模型辅助：")
            print(json.dumps(by_case[case_id]["output"], ensure_ascii=False, indent=2))
        impression = read_lines("最终印象；每项尽量只写一个可核查命题", required=True)
        recommendations = read_lines("必要建议")
        limitations = read_lines("信息局限")
        output = {"impression": [{"text": text, "evidence_ids": [], "certainty": "indeterminate"} for text in impression],
                  "recommendations": recommendations, "limitations": limitations}
        validate_output(output, case, require_evidence=False)
        record = {
            "record_id": uuid.uuid4().hex, "case_id": case_id, "source": "reader", "source_id": reader_id,
            "condition": assignment["condition"], "session": session, "created_at": now(),
            "elapsed_seconds": time.perf_counter() - started, "output": output,
            "input_sha256": digest(model_input(case)), "prompt_sha256": None,
            "model_record_id": by_case[case_id]["record_id"] if aided else None,
            "demo_only": by_case[case_id].get("demo_only", False) if aided else False,
        }
        append_jsonl(out, record)
        count += 1
    return {"new_records": count}


def exact_mcnemar(b: int, c: int) -> float:
    """Two-sided exact binomial test of discordant pairs; no scipy dependency."""
    n = b + c
    if not n:
        return 1.0
    # log-space avoids overflow for large cohorts.
    probabilities = [math.exp(math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) - n * math.log(2))
                     for k in range(min(b, c) + 1)]
    return min(1.0, 2 * math.fsum(probabilities))


def paired_summary(ratings_path: str, key_path: str, readers_path: str, out: str):
    observations = load_ratings(ratings_path, key_path)
    readers = load_readers(readers_path)
    pairs = defaultdict(dict)
    for obs in observations:
        record = obs["record"]
        if record["source"] != "reader":
            continue
        reader_id = record["source_id"]
        require(reader_id in readers, "Unknown reader")
        key = (obs["rating"]["evaluator_id"], reader_id, record["case_id"])
        require(record["condition"] not in pairs[key], "Duplicate reader-condition assessment")
        pairs[key][record["condition"]] = obs
    require(bool(pairs), "No reader results")
    groups = defaultdict(list)
    incomplete = 0
    for key, pair in pairs.items():
        if set(pair) != {"aided", "unaided"}:
            incomplete += 1
            continue
        groups[key[:2]].append(pair)
    rows = []
    for (evaluator_id, reader_id), values in sorted(groups.items()):
        b = sum(not p["unaided"]["rating"]["diagnostic_correct"] and p["aided"]["rating"]["diagnostic_correct"] for p in values)
        c = sum(p["unaided"]["rating"]["diagnostic_correct"] and not p["aided"]["rating"]["diagnostic_correct"] for p in values)
        independent = len({p["unaided"]["patient_id"] for p in values}) == len(values)
        rows.append({
            "evaluator_id": evaluator_id, "reader_id": reader_id, "seniority": readers[reader_id]["seniority"],
            "pairs": len(values), "wrong_to_correct": b, "correct_to_wrong": c,
            "accuracy_difference": (b - c) / len(values),
            "mean_time_difference_seconds": mean(p["aided"]["record"]["elapsed_seconds"] - p["unaided"]["record"]["elapsed_seconds"] for p in values),
            "exploratory_exact_mcnemar_p": exact_mcnemar(b, c) if independent else None,
            "independent_patients": independent,
        })
    result = {
        "note": "Per-reader exploratory tests only, unadjusted for multiplicity. For a study-level assistance effect use a prespecified crossed reader/case model, including period/order and seniority; not implemented here.",
        "demo_only": any(o["record"].get("demo_only", False) for o in observations),
        "incomplete_pairs": incomplete, "readers": rows,
    }
    write_json(out, result)
    return result
