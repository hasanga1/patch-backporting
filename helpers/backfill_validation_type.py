#!/usr/bin/env python3
"""Backfill missing `type` fields in validation summary JSON files.

The script scans rows in dataset_runs, reads each validation_summary.json, and
adds a top-level "type" field only when it is missing. The value is resolved by
matching commits.original_commit against the "Original Commit" column in the CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def _normalize(value: str) -> str:
    return value.strip().lower()


def _load_csv_rows(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows.extend(reader)
    return rows


def _iter_validation_files(dataset_runs_root: Path, start_row: int, end_row: int):
    for row_index in range(start_row, end_row + 1):
        row_dir = dataset_runs_root / f"row-{row_index}"
        if not row_dir.is_dir():
            continue

        for project_dir in row_dir.iterdir():
            if not project_dir.is_dir():
                continue
            summary = project_dir / "validation_summary.json"
            if summary.is_file():
                yield row_index, summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill missing 'type' in validation_summary.json files using "
            "Original Commit -> Type mapping from java_backports.csv"
        )
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
    parser.add_argument("--start-row", type=int, default=21, help="Start row, inclusive (default: 21)")
    parser.add_argument("--end-row", type=int, default=45, help="End row, inclusive (default: 45)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned changes without writing files",
    )
    args = parser.parse_args()

    if args.start_row > args.end_row:
        raise ValueError("start-row must be <= end-row")

    csv_rows = _load_csv_rows(args.csv)

    scanned = 0
    updated = 0
    already_had_type = 0
    missing_commit = 0
    commit_not_found = 0
    ambiguous_type = 0

    for row_index, summary_path in _iter_validation_files(
        args.dataset_runs_root, args.start_row, args.end_row
    ):
        scanned += 1
        with summary_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)

        if "type" in data and str(data["type"]).strip():
            already_had_type += 1
            continue

        original_commit = (
            data.get("commits", {}).get("original_commit", "")
            if isinstance(data.get("commits"), dict)
            else ""
        )
        original_commit = _normalize(str(original_commit))
        project = _normalize(str(data.get("project", "")))

        if not original_commit:
            missing_commit += 1
            print(f"[missing original_commit] row-{row_index}: {summary_path}")
            continue

        matching_rows = [
            r for r in csv_rows
            if _normalize(str(r.get("Original Commit", ""))) == original_commit
        ]
        if project:
            matching_rows = [
                r for r in matching_rows
                if _normalize(str(r.get("Project", ""))) == project
            ]

        if not matching_rows:
            commit_not_found += 1
            print(
                f"[commit not in csv] row-{row_index}: {summary_path} "
                f"(original_commit={original_commit})"
            )
            continue

        types = {str(r.get("Type", "")).strip() for r in matching_rows if str(r.get("Type", "")).strip()}
        patch_type = ""
        if len(types) == 1:
            patch_type = next(iter(types))
        else:
            # Disambiguate using CSV row index, which aligns with dataset row numbering.
            index_match = ""
            if 0 <= row_index < len(csv_rows):
                indexed = csv_rows[row_index]
                idx_commit = _normalize(str(indexed.get("Original Commit", "")))
                idx_project = _normalize(str(indexed.get("Project", "")))
                idx_type = str(indexed.get("Type", "")).strip()
                if idx_commit == original_commit and (not project or idx_project == project):
                    index_match = idx_type
            if index_match:
                patch_type = index_match

        if not patch_type:
            ambiguous_type += 1
            print(
                f"[ambiguous type] row-{row_index}: {summary_path} "
                f"(original_commit={original_commit}, candidate_types={sorted(types)})"
            )
            continue

        data["type"] = patch_type
        updated += 1
        print(f"[update] row-{row_index}: set type={patch_type} in {summary_path}")

        if not args.dry_run:
            with summary_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.write("\n")

    print("\nSummary")
    print(f"- scanned: {scanned}")
    print(f"- updated: {updated}")
    print(f"- already had type: {already_had_type}")
    print(f"- missing original_commit: {missing_commit}")
    print(f"- original_commit not in csv: {commit_not_found}")
    print(f"- ambiguous type: {ambiguous_type}")
    print(f"- dry-run: {args.dry_run}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
