"""Small explicit data contracts. Reference data never enter the model payload."""

from .storage import index_unique


QUALITY_FIELDS = [
    "quality_logic", "quality_completeness", "quality_terminology",
    "quality_actionability", "quality_structure",
]

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "impression": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "text": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "certainty": {"type": "string", "enum": ["certain", "probable", "indeterminate"]},
                },
                "required": ["text", "evidence_ids", "certainty"],
            },
        },
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["impression", "recommendations", "limitations"],
}


def require(condition: bool, message: str):
    if not condition:
        raise ValueError(message)


def nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def exact_keys(row: dict, keys: set[str], context: str):
    require(isinstance(row, dict), f"{context}: expected object")
    require(set(row) == keys, f"{context}: missing {keys - set(row)}, unexpected {set(row) - keys}")


def validate_cases(rows: list[dict]) -> dict:
    require(bool(rows), "Empty case dataset")
    cases = index_unique(rows, "case_id")
    patient_splits = {}
    keys = {"case_id", "patient_id", "center", "modality", "body_part", "split",
            "clinical_info", "findings", "input_qc_passed"}
    for case in rows:
        exact_keys(case, keys, "case")
        for key in ("case_id", "patient_id", "center", "modality", "body_part"):
            require(nonempty(case[key]), f"Invalid {key}")
        require(case["split"] in {"development", "evaluation"}, "Unknown split")
        require(type(case["input_qc_passed"]) is bool, "input_qc_passed must be boolean")
        patient = case["patient_id"]
        require(patient_splits.get(patient, case["split"]) == case["split"],
                f"Patient {patient} leaks between development and evaluation")
        patient_splits[patient] = case["split"]
        info = case["clinical_info"]
        exact_keys(info, {"age", "sex", "indication", "history", "prior_imaging"}, "clinical_info")
        require(info["age"] is None or (type(info["age"]) is int and 0 <= info["age"] <= 120), "Invalid age")
        require(info["sex"] in {"female", "male", "unknown"}, "Invalid sex")
        for key in ("indication", "history", "prior_imaging"):
            require(info[key] is None or nonempty(info[key]), f"Invalid clinical_info.{key}")
        require(isinstance(case["findings"], list) and bool(case["findings"]), "Findings required")
        for finding in case["findings"]:
            exact_keys(finding, {"finding_id", "text"}, "finding")
            require(nonempty(finding["finding_id"]) and nonempty(finding["text"]), "Invalid finding")
        index_unique(case["findings"], "finding_id")
    return cases


def validate_references(rows: list[dict], cases: dict) -> dict:
    references = index_unique(rows, "case_id")
    for ref in rows:
        exact_keys(ref, {"case_id", "basis", "reference_impression", "normal", "required_items"}, "reference")
        require(ref["case_id"] in cases, "Reference case not found")
        require(ref["basis"] in {"expert_consensus", "followup", "pathology"}, "Invalid reference basis")
        require(nonempty(ref["reference_impression"]), "Reference impression required")
        require(type(ref["normal"]) is bool, "normal must be boolean")
        require(isinstance(ref["required_items"], list) and bool(ref["required_items"]), "Checklist required, including normal cases")
        for item in ref["required_items"]:
            exact_keys(item, {"item_id", "text", "critical"}, "required_item")
            require(nonempty(item["item_id"]) and nonempty(item["text"]), "Invalid reference item")
            require(type(item["critical"]) is bool, "critical must be boolean")
        index_unique(ref["required_items"], "item_id")
    return references


def model_input(case: dict) -> dict:
    """Explicit allowlist: no patient IDs, split, reference impression, or follow-up."""
    return {key: case[key] for key in ("modality", "body_part", "clinical_info", "findings")}


def validate_output(output: dict, case: dict, *, require_evidence: bool = True):
    exact_keys(output, {"impression", "recommendations", "limitations"}, "output")
    require(isinstance(output["impression"], list) and bool(output["impression"]), "Empty impression")
    finding_ids = {f["finding_id"] for f in case["findings"]}
    for item in output["impression"]:
        exact_keys(item, {"text", "evidence_ids", "certainty"}, "impression item")
        require(nonempty(item["text"]), "Empty impression item")
        require(item["certainty"] in {"certain", "probable", "indeterminate"}, "Invalid certainty")
        require(isinstance(item["evidence_ids"], list) and all(nonempty(x) for x in item["evidence_ids"]), "Invalid evidence IDs")
        require(not require_evidence or bool(item["evidence_ids"]), "Model impression must cite a finding")
        require(set(item["evidence_ids"]) <= finding_ids, "Unknown evidence ID")
    for key in ("recommendations", "limitations"):
        require(isinstance(output[key], list) and all(nonempty(x) for x in output[key]), f"Invalid {key}")
