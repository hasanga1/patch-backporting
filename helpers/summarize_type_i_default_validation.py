#!/usr/bin/env python3
"""Summarize default validation pass ratios by patch type and project.

The script reads java_backports.csv to count totals by Type and then looks up
dataset_runs/row-<index>/<project>/validation_summary.json to count how many
rows passed based on default_validation.passed only.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def _normalize(value: str) -> str:
    return value.strip().lower()


def _load_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _display_type_label(raw_type: str) -> str:
    text = raw_type.strip()
    if not text:
        return "unknown"
    if text.upper().startswith("TYPE-"):
        return f"type-{text[5:]}"
    return text.lower()


def _find_summary_file(dataset_runs_root: Path, row_index: int, project: str) -> Path | None:
    row_dir = dataset_runs_root / f"row-{row_index}"
    if not row_dir.is_dir():
        return None

    preferred = row_dir / project / "validation_summary.json"
    if preferred.is_file():
        return preferred

    matches = list(row_dir.glob("*/validation_summary.json"))
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    # Prefer a summary with matching project field when multiple exist.
    for match in matches:
        try:
            with match.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if _normalize(str(payload.get("project", ""))) == _normalize(project):
                return match
        except (json.JSONDecodeError, OSError):
            continue

    return None


def _default_validation_passed(summary_path: Path) -> bool:
    try:
        with summary_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return False

    default_validation = payload.get("default_validation", {})
    if not isinstance(default_validation, dict):
        return False
    return bool(default_validation.get("passed", False))


def summarize(csv_path: Path, dataset_runs_root: Path) -> dict:
    rows = _load_csv_rows(csv_path)

    overall_counts: dict[str, dict[str, int]] = {}
    repo_counts: dict[str, dict[str, dict[str, int]]] = {}
    missing_summaries_by_type: dict[str, int] = {}
    missing_summaries_total = 0

    for row_index, row in enumerate(rows):
        patch_type = _display_type_label(str(row.get("Type", "")))

        project = str(row.get("Project", "")).strip()
        if not project:
            project = "unknown"

        overall_entry = overall_counts.setdefault(patch_type, {"total": 0, "passed": 0})
        overall_entry["total"] += 1

        repo_entry = repo_counts.setdefault(project, {})
        repo_type_entry = repo_entry.setdefault(patch_type, {"total": 0, "passed": 0})
        repo_type_entry["total"] += 1

        summary_path = _find_summary_file(dataset_runs_root, row_index, project)
        if summary_path is None:
            missing_summaries_total += 1
            missing_summaries_by_type[patch_type] = missing_summaries_by_type.get(patch_type, 0) + 1
            continue

        if _default_validation_passed(summary_path):
            overall_entry["passed"] += 1
            repo_type_entry["passed"] += 1

    overall_ratios = {
        patch_type: f"{entry['passed']}/{entry['total']}"
        for patch_type, entry in sorted(overall_counts.items(), key=lambda item: item[0])
    }

    repo_wise = {}
    for project, project_types in sorted(repo_counts.items(), key=lambda item: item[0].lower()):
        repo_wise[project] = {
            patch_type: f"{entry['passed']}/{entry['total']}"
            for patch_type, entry in sorted(project_types.items(), key=lambda item: item[0])
        }

    return {
        "source": {
            "csv": str(csv_path),
            "dataset_runs_root": str(dataset_runs_root),
            "validation_considered": "default_validation",
        },
        "overall": overall_ratios,
        "repo_wise": repo_wise,
        "missing_validation_summaries": {
            "total": missing_summaries_total,
            "by_type": dict(sorted(missing_summaries_by_type.items(), key=lambda item: item[0])),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate per-type default-validation pass ratios overall and per repository"
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("my_java_dataset/java_backports.csv"),
        help="Path to java_backports.csv (default: my_java_dataset/java_backports.csv)",
    )
    parser.add_argument(
        "--dataset-runs-root",
        type=Path,
        default=Path("dataset_runs"),
        help="Path to dataset_runs root (default: dataset_runs)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("dataset_runs/type_i_default_validation_summary.json"),
        help=(
            "Output JSON path "
            "(default: dataset_runs/type_i_default_validation_summary.json)"
        ),
    )
    args = parser.parse_args()

    result = summarize(args.csv, args.dataset_runs_root)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")

    print(f"Wrote summary: {args.out}")
    print(json.dumps(result["overall"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
