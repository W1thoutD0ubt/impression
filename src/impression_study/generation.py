"""Locked-prompt single-turn generation with offline and OpenAI backends."""

import json
import os
import platform
import time
import uuid
from pathlib import Path

from . import __version__
from .schemas import OUTPUT_SCHEMA, model_input, nonempty, require, validate_cases, validate_output
from .storage import append_jsonl, digest, now, read_json, read_jsonl, write_json


def load_config(path: str) -> dict:
    config = read_json(path)
    expected = {"study_id", "prompt_file", "model", "temperature", "max_completion_tokens", "seed", "washout_days"}
    require(set(config) == expected, "Unexpected config fields")
    require(nonempty(config["study_id"]) and nonempty(config["prompt_file"]), "study_id and prompt_file are required")
    require(config["model"] is None or nonempty(config["model"]), "model must be null or a nonempty model ID")
    require(type(config["seed"]) is int, "seed must be integer")
    require(type(config["washout_days"]) is int and config["washout_days"] >= 28, "washout_days must be >= 28")
    require(type(config["max_completion_tokens"]) is int and config["max_completion_tokens"] > 0, "Invalid token budget")
    require(config["temperature"] is None or
            (type(config["temperature"]) in (int, float) and 0 <= config["temperature"] <= 2), "Invalid temperature")
    config["prompt_file"] = str((Path(path).resolve().parent / config["prompt_file"]).resolve())
    return config


class MockBackend:
    """Copies findings; exercises plumbing, NOT diagnostic reasoning or quality."""

    model = "mock-echo-not-a-clinical-model"

    def generate(self, prompt: str, payload: dict, config: dict):
        output = {
            "impression": [
                {"text": f["text"], "evidence_ids": [f["finding_id"]], "certainty": "indeterminate"}
                for f in payload["findings"]
            ],
            "recommendations": [],
            "limitations": ["MOCK：仅复制所见，用于离线流程验证。"],
        }
        return {"mock": True, "output": output}


class OpenAIBackend:
    def __init__(self, model: str | None):
        require(isinstance(model, str) and bool(model.strip()), "Set --model or config.model to an available model ID")
        require(bool(os.environ.get("OPENAI_API_KEY")), "OPENAI_API_KEY is required")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ValueError('Install the API extra: python -m pip install -e ".[api]"') from exc
        self.client = OpenAI(timeout=120, max_retries=0)
        self.model = model

    def generate(self, prompt: str, payload: dict, config: dict):
        params = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "max_completion_tokens": config["max_completion_tokens"],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "radiology_impression", "strict": True, "schema": OUTPUT_SCHEMA,
            }},
        }
        # Some reasoning models reject temperature; omission is explicit in the manifest.
        if config["temperature"] is not None:
            params["temperature"] = config["temperature"]
        response = self.client.chat.completions.create(**params)
        return response.model_dump(mode="json")


def parse_openai_response(raw: dict) -> dict:
    require(bool(raw.get("choices")), "No model response choices")
    choice = raw["choices"][0]
    require(choice.get("finish_reason") == "stop", f"Incomplete response: {choice.get('finish_reason')}")
    message = choice["message"]
    require(not message.get("refusal"), f"Model refused: {message.get('refusal')}")
    require(nonempty(message.get("content")), "Empty model response")
    return json.loads(message["content"])


def generate(cases_path: str, config_path: str, out: str, backend_name: str, model: str | None):
    require(backend_name in {"mock", "openai"}, "Unknown backend")
    cases = validate_cases(read_jsonl(cases_path))
    selected = [case for case in cases.values() if case["split"] == "evaluation"]
    require(bool(selected), "No evaluation cases")
    require(all(c["input_qc_passed"] for c in selected), "Input QC must be completed before generation")
    config = load_config(config_path)
    prompt = Path(config["prompt_file"]).read_text(encoding="utf-8")
    backend = MockBackend() if backend_name == "mock" else OpenAIBackend(model or config["model"])
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=False)
    config["model"] = backend.model
    manifest = {
        "study_id": config["study_id"], "created_at": now(), "status": "running",
        "package_version": __version__, "python_version": platform.python_version(),
        "backend": backend_name, "demo_only": backend_name == "mock", "config": config,
        "prompt": prompt, "prompt_sha256": digest(prompt),
        "input_sha256": digest([model_input(c) for c in selected]), "case_count": len(selected),
        "completed": 0,
    }
    write_json(directory / "manifest.json", manifest)
    try:
        for case in selected:
            payload = model_input(case)
            record_id = uuid.uuid4().hex
            started = time.perf_counter()
            raw = backend.generate(prompt, payload, config)
            elapsed = time.perf_counter() - started
            # Preserve refusals, truncations, and malformed JSON before parsing.
            write_json(directory / "raw" / f"{record_id}.json", raw)
            output = raw["output"] if backend_name == "mock" else parse_openai_response(raw)
            validate_output(output, case)
            record = {
                "record_id": record_id, "case_id": case["case_id"], "source": "model",
                "source_id": backend.model, "condition": "standalone", "session": None,
                "created_at": now(), "elapsed_seconds": elapsed, "output": output,
                "input_sha256": digest(payload), "prompt_sha256": digest(prompt),
                "model_record_id": None, "demo_only": backend_name == "mock",
            }
            append_jsonl(directory / "predictions.jsonl", record)
            manifest["completed"] += 1
        manifest["status"] = "completed"
    except (Exception, KeyboardInterrupt) as exc:
        manifest["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        append_jsonl(directory / "errors.jsonl", {
            "case_id": case["case_id"], "record_id": record_id,
            "error_type": type(exc).__name__, "created_at": now(),
        })
        raise
    finally:
        write_json(directory / "manifest.json", manifest, overwrite=True)
    return manifest
