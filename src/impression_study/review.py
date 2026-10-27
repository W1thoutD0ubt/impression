"""Blind review export and deterministic summaries of HUMAN judgments."""

import json
import random
import uuid
from collections import defaultdict
from pathlib import Path
from statistics import mean

from .schemas import (
    QUALITY_FIELDS, model_input, nonempty, require, validate_cases,
    validate_output, validate_references,
)
from .storage import (
    digest, index_unique, read_csv, read_jsonl, write_csv, write_json, write_jsonl,
)


REVIEW_FIELDS = [
    "sample_id", "evaluator_id", "diagnostic_correct", "clinically_acceptable",
    "major_error", "omitted_item_ids", "unsupported_item_indices",
    "contradicted_item_indices", *QUALITY_FIELDS, "notes",
]


def validate_records(rows: list[dict], cases: dict) -> dict:
    records = index_unique(rows, "record_id")
    require(bool(records), "No records")
    experiment_keys = set()
    for row in rows:
        require(row["case_id"] in cases, "Record has unknown case")
        case = cases[row["case_id"]]
        require(row["input_sha256"] == digest(model_input(case)), "Input changed since recording")
        require(row["source"] in {"model", "reader"}, "Invalid source")
        require(nonempty(row["source_id"]), "source_id required")
        if row["source"] == "model":
            require(row["condition"] == "standalone" and row["session"] is None, "Invalid model condition")
        else:
            require(row["condition"] in {"aided", "unaided"} and row["session"] in {1, 2}, "Invalid reader condition")
        elapsed = row["elapsed_seconds"]
        require(type(elapsed) in (int, float) and 0 <= elapsed < float("inf"), "Invalid elapsed time")
        validate_output(row["output"], case, require_evidence=row["source"] == "model")
        key = (row["source"], row["source_id"], row["case_id"], row["condition"])
        require(key not in experiment_keys, f"Duplicate experimental observation: {key}")
        experiment_keys.add(key)
    return records


def export_review(cases_path: str, references_path: str, records_path: str, out: str, seed: int):
    cases = validate_cases(read_jsonl(cases_path))
    references = validate_references(read_jsonl(references_path), cases)
    records = list(validate_records(read_jsonl(records_path), cases).values())
    require(all(r["case_id"] in references for r in records), "Reference missing for a recorded case")
    random.Random(seed).shuffle(records)
    packets, keys, templates = [], [], []
    for record in records:
        sample_id = uuid.uuid4().hex
        case_id = record["case_id"]
        ref = references[case_id]
        # No case ID, reader ID, condition, timing, or model ID in reviewer material.
        packets.append({
            "sample_id": sample_id, "input": model_input(cases[case_id]),
            "reference": ref, "output": record["output"],
        })
        packets[-1]["reference"] = {k: v for k, v in ref.items() if k != "case_id"}
        keys.append({
            "sample_id": sample_id, "record": record,
            "patient_id": cases[case_id]["patient_id"],
            "reference": ref,
        })
        templates.append({field: sample_id if field == "sample_id" else "" for field in REVIEW_FIELDS})
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=False)
    write_jsonl(directory / "reviewer" / "packets.jsonl", packets)
    write_csv(directory / "reviewer" / "ratings.csv", REVIEW_FIELDS, templates)
    write_jsonl(directory / "private" / "key.jsonl", keys)
    return {"samples": len(packets), "reviewer_directory": str(directory / "reviewer")}


def parse_int(value: str, low: int, high: int, name: str) -> int:
    require(isinstance(value, str) and value.strip() in {str(x) for x in range(low, high + 1)},
            f"{name} must be {low}..{high}")
    return int(value)


def parse_list(value: str, name: str) -> list:
    try:
        result = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a JSON list, e.g. []") from exc
    require(isinstance(result, list), f"{name} must be a list")
    require(all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in result), f"Invalid {name} entries")
    require(len(result) == len(set(result)), f"Duplicate entries in {name}")
    return result


def load_ratings(ratings_path: str, key_path: str) -> list[dict]:
    keys = index_unique(read_jsonl(key_path), "sample_id")
    rows = read_csv(ratings_path)
    require(bool(keys) and bool(rows), "Empty review")
    observations, seen, coverage = [], set(), defaultdict(set)
    for row in rows:
        require(set(row) == set(REVIEW_FIELDS), "Unexpected review CSV columns")
        sample_id, evaluator = row["sample_id"], row["evaluator_id"].strip()
        require(sample_id in keys and nonempty(evaluator), "Unknown sample or missing evaluator_id")
        require((sample_id, evaluator) not in seen, "Duplicate sample/evaluator rating")
        seen.add((sample_id, evaluator))
        coverage[evaluator].add(sample_id)
        key = keys[sample_id]
        for field in ("diagnostic_correct", "clinically_acceptable", "major_error"):
            row[field] = parse_int(row[field], 0, 1, field)
        require(not (row["major_error"] and row["clinically_acceptable"]), "Major-error report cannot be clinically acceptable")
        for field in QUALITY_FIELDS:
            row[field] = parse_int(row[field], 1, 5, field)
        for field in ("omitted_item_ids", "unsupported_item_indices", "contradicted_item_indices"):
            row[field] = parse_list(row[field], field)
        required_ids = {item["item_id"] for item in key["reference"]["required_items"]}
        require(set(row["omitted_item_ids"]) <= required_ids, "Unknown omitted item ID")
        count = len(key["record"]["output"]["impression"])
        for field in ("unsupported_item_indices", "contradicted_item_indices"):
            require(all(type(x) is int and 1 <= x <= count for x in row[field]), "Output item indices must be 1-based")
        require(not set(row["unsupported_item_indices"]) & set(row["contradicted_item_indices"]), "Unsupported and contradicted categories must not overlap")
        observations.append({**key, "rating": row})
    # Do not silently turn missing assessments into negative/positive labels.
    for evaluator, samples in coverage.items():
        require(samples == set(keys), f"Incomplete review by {evaluator}; missing {len(set(keys) - samples)} samples")
    return observations


def fraction(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def summarize_group(observations: list[dict]) -> dict:
    required = omitted = critical = omitted_critical = output_count = unsupported = contradicted = 0
    for obs in observations:
        items = obs["reference"]["required_items"]
        missed = set(obs["rating"]["omitted_item_ids"])
        required += len(items)
        omitted += len(missed)
        critical += sum(item["critical"] for item in items)
        omitted_critical += sum(item["critical"] and item["item_id"] in missed for item in items)
        output_count += len(obs["record"]["output"]["impression"])
        unsupported += len(obs["rating"]["unsupported_item_indices"])
        contradicted += len(obs["rating"]["contradicted_item_indices"])
    n = len(observations)
    normal = [o for o in observations if o["reference"]["normal"]]
    abnormal = [o for o in observations if not o["reference"]["normal"]]
    return {
        "observations": n, "patients": len({o["patient_id"] for o in observations}),
        "diagnostic_accuracy": fraction(sum(o["rating"]["diagnostic_correct"] for o in observations), n),
        "clinically_acceptable_rate": fraction(sum(o["rating"]["clinically_acceptable"] for o in observations), n),
        "major_error_rate": fraction(sum(o["rating"]["major_error"] for o in observations), n),
        "checklist_recall": fraction(required - omitted, required),
        "critical_item_omission_rate": fraction(omitted_critical, critical),
        "unsupported_item_rate": fraction(unsupported, output_count),
        "contradicted_item_rate": fraction(contradicted, output_count),
        "reports_with_unsupported_items": fraction(sum(bool(o["rating"]["unsupported_item_indices"]) for o in observations), n),
        "normal_case_diagnostic_accuracy": fraction(sum(o["rating"]["diagnostic_correct"] for o in normal), len(normal)),
        "abnormal_case_diagnostic_accuracy": fraction(sum(o["rating"]["diagnostic_correct"] for o in abnormal), len(abnormal)),
        "quality_mean": {f: mean(o["rating"][f] for o in observations) for f in QUALITY_FIELDS},
        "mean_elapsed_seconds": mean(o["record"]["elapsed_seconds"] for o in observations),
    }


def summarize(ratings_path: str, key_path: str, out: str):
    observations = load_ratings(ratings_path, key_path)
    groups = defaultdict(list)
    for obs in observations:
        record = obs["record"]
        group = (obs["rating"]["evaluator_id"], record["source"], record["source_id"], record["condition"])
        groups[group].append(obs)
    result = {
        "note": "Descriptive summaries of human ratings; no automatic semantic correctness judgment. Evaluators are not pooled; model latency is not reader time.",
        "demo_only": any(o["record"].get("demo_only", False) for o in observations),
        "groups": [
            {"evaluator_id": key[0], "source": key[1], "source_id": key[2], "condition": key[3],
             **summarize_group(value)} for key, value in sorted(groups.items())
        ],
    }
    write_json(out, result)
    return result
