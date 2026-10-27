"""Command-line entry points. Run python -m impression_study --help."""

import argparse
import json

from .generation import generate
from .reader_study import make_plan, paired_summary, run_session
from .review import export_review, summarize
from .schemas import validate_cases, validate_references
from .storage import read_jsonl


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="体检影像印象生成与读者研究骨架（文本输入）")
    commands = root.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="验证病例/参考数据与患者级拆分")
    validate.add_argument("--cases", required=True)
    validate.add_argument("--references")

    generation = commands.add_parser("generate", help="独立、单轮生成 evaluation 病例")
    generation.add_argument("--cases", required=True)
    generation.add_argument("--config", required=True)
    generation.add_argument("--out", required=True)
    generation.add_argument("--backend", choices=["mock", "openai"], default="mock")
    generation.add_argument("--model", help="覆盖 config 中的实际模型 ID")

    review = commands.add_parser("export-review", help="导出匿名评价材料与独立解盲表")
    review.add_argument("--cases", required=True)
    review.add_argument("--references", required=True)
    review.add_argument("--records", required=True)
    review.add_argument("--out", required=True)
    review.add_argument("--seed", type=int, default=2026)

    summary = commands.add_parser("summarize", help="汇总已完成的人工评价")
    summary.add_argument("--ratings", required=True)
    summary.add_argument("--key", required=True)
    summary.add_argument("--out", required=True)

    plan = commands.add_parser("plan-readers", help="创建顺序平衡、>=28天洗脱的交叉读者计划")
    plan.add_argument("--cases", required=True)
    plan.add_argument("--readers", required=True)
    plan.add_argument("--config", required=True)
    plan.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    plan.add_argument("--out", required=True)

    session = commands.add_parser("reader-session", help="交互式文本阅报、自动计时、逐例保存")
    session.add_argument("--cases", required=True)
    session.add_argument("--assignments", required=True)
    session.add_argument("--predictions", required=True)
    session.add_argument("--reader-id", required=True)
    session.add_argument("--session", type=int, choices=[1, 2], required=True)
    session.add_argument("--out", required=True)

    paired = commands.add_parser("paired-summary", help="按读者汇总配对变化；非研究总体效应检验")
    paired.add_argument("--ratings", required=True)
    paired.add_argument("--key", required=True)
    paired.add_argument("--readers", required=True)
    paired.add_argument("--out", required=True)
    return root


def main():
    command_parser = parser()
    args = vars(command_parser.parse_args())
    command = args.pop("command")
    try:
        if command == "validate":
            cases = validate_cases(read_jsonl(args["cases"]))
            refs = validate_references(read_jsonl(args["references"]), cases) if args["references"] else {}
            result = {"cases": len(cases), "references": len(refs)}
        else:
            if command == "generate":
                args["cases_path"] = args.pop("cases")
                args["config_path"] = args.pop("config")
                args["backend_name"] = args.pop("backend")
                result = generate(**args)
            elif command == "export-review":
                result = export_review(args["cases"], args["references"], args["records"], args["out"], args["seed"])
            elif command == "summarize":
                result = summarize(args["ratings"], args["key"], args["out"])
            elif command == "plan-readers":
                result = make_plan(args["cases"], args["readers"], args["config"], args["start_date"], args["out"])
            elif command == "reader-session":
                result = run_session(args["cases"], args["assignments"], args["predictions"], args["reader_id"], args["session"], args["out"])
            else:
                result = paired_summary(args["ratings"], args["key"], args["readers"], args["out"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        command_parser.exit(2, f"Error: {exc}\n")
