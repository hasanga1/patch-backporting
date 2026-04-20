#!/usr/bin/env python3

import argparse
import json
from collections import defaultdict
from pathlib import Path


def load_records(path: Path):
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if "results" in data and isinstance(data["results"], list):
            return data["results"]
        raise ValueError("JSON object found, but no 'results' list key was present.")

    raise ValueError("Unsupported JSON structure. Expected a list of run objects.")


def to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "pass"}
    return bool(value)


def summarize(records):
    group_counts = defaultdict(lambda: {"runs": 0, "success": 0, "failed": 0})
    project_counts = defaultdict(lambda: {"runs": 0, "success": 0, "failed": 0})
    type_counts = defaultdict(lambda: {"runs": 0, "success": 0, "failed": 0})

    for row in records:
        project = str(row.get("project", "UNKNOWN"))
        run_type = str(row.get("type", "UNKNOWN"))
        success = to_bool(row.get("success", False))

        group_key = (project, run_type)

        for bucket, key in (
            (group_counts, group_key),
            (project_counts, project),
            (type_counts, run_type),
        ):
            bucket[key]["runs"] += 1
            if success:
                bucket[key]["success"] += 1
            else:
                bucket[key]["failed"] += 1

    return group_counts, project_counts, type_counts


def format_rate(success, runs):
    if runs == 0:
        return "0.00%"
    return f"{(success / runs) * 100:.2f}%"


def print_table(title, rows):
    print(title)
    print("-" * len(title))
    if not rows:
        print("No data")
        print()
        return

    headers = ["Project", "Type", "Runs", "Success", "Failed", "Success %"]
    widths = [
        max(len(str(row[i])) for row in [headers] + rows)
        for i in range(len(headers))
    ]

    def fmt(values):
        return " | ".join(str(v).ljust(widths[i]) for i, v in enumerate(values))

    print(fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))
    print()


def print_simple_table(title, key_header, counts_dict):
    print(title)
    print("-" * len(title))
    if not counts_dict:
        print("No data")
        print()
        return

    rows = []
    for key in sorted(counts_dict):
        c = counts_dict[key]
        rows.append((key, c["runs"], c["success"], c["failed"], format_rate(c["success"], c["runs"])))

    headers = [key_header, "Runs", "Success", "Failed", "Success %"]
    widths = [
        max(len(str(row[i])) for row in [headers] + rows)
        for i in range(len(headers))
    ]

    def fmt(values):
        return " | ".join(str(v).ljust(widths[i]) for i, v in enumerate(values))

    print(fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))
    print()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Summarize backport run results by project and type, "
            "including success and failure counts."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="java_results_with_retrofit/summary.json",
        help="Path to summary.json (default: java_results_with_retrofit/summary.json)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    records = load_records(input_path)
    group_counts, project_counts, type_counts = summarize(records)

    total_runs = len(records)
    total_success = sum(1 for r in records if to_bool(r.get("success", False)))
    total_failed = total_runs - total_success

    print(f"Input file   : {input_path}")
    print(f"Total runs   : {total_runs}")
    print(f"Total success: {total_success}")
    print(f"Total failed : {total_failed}")
    print(f"Success rate : {format_rate(total_success, total_runs)}")
    print()

    grouped_rows = []
    for (project, run_type) in sorted(group_counts):
        c = group_counts[(project, run_type)]
        grouped_rows.append(
            (
                project,
                run_type,
                c["runs"],
                c["success"],
                c["failed"],
                format_rate(c["success"], c["runs"]),
            )
        )

    print_table("Per project + type summary", grouped_rows)
    print_simple_table("Per project summary", "Project", project_counts)
    print_simple_table("Per type summary", "Type", type_counts)


if __name__ == "__main__":
    main()