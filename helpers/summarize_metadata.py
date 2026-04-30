#!/usr/bin/env python3
"""Aggregate patch metadata by type and project.

The script scans a directory tree for `metadata.json` files, groups the
records by `type`, and counts total and successful patches per type and per
project.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_metadata(metadata_path: Path) -> dict[str, Any] | None:
    try:
        with metadata_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        print(f"Skipping {metadata_path}: {exc}", file=sys.stderr)
        return None

    if not isinstance(payload, dict):
        print(f"Skipping {metadata_path}: metadata must be a JSON object", file=sys.stderr)
        return None

    return payload


def summarize_metadata(root_dir: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"types": {}}

    grouped: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"total_patches": 0, "successful_patches": 0})
    )
    type_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total_patches": 0, "successful_patches": 0}
    )

    for metadata_path in root_dir.rglob("metadata.json"):
        payload = load_metadata(metadata_path)
        if payload is None:
            continue

        patch_type = str(payload.get("type", "unknown"))
        project = str(payload.get("project", "unknown"))
        success = bool(payload.get("success", False))

        type_totals[patch_type]["total_patches"] += 1
        if success:
            type_totals[patch_type]["successful_patches"] += 1

        grouped[patch_type][project]["total_patches"] += 1
        if success:
            grouped[patch_type][project]["successful_patches"] += 1

    for patch_type in sorted(grouped):
        type_entry = {
            "total_patches": type_totals[patch_type]["total_patches"],
            "successful_patches": type_totals[patch_type]["successful_patches"],
            "projects": {},
        }

        for project in sorted(grouped[patch_type]):
            type_entry["projects"][project] = grouped[patch_type][project]

        summary["types"][patch_type] = type_entry

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize patch metadata files by type and project."
    )
    parser.add_argument("input_dir", help="Directory containing patch result subfolders")
    parser.add_argument(
        "--output",
        help=(
            "Output JSON file path. Defaults to <input_dir>/metadata_summary.json"
        ),
    )
    args = parser.parse_args()

    root_dir = Path(args.input_dir).expanduser().resolve()
    if not root_dir.is_dir():
        print(f"Input directory does not exist or is not a directory: {root_dir}", file=sys.stderr)
        return 1

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else root_dir / "metadata_summary.json"
    )

    summary = summarize_metadata(root_dir)

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
        handle.write("\n")

    print(output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())