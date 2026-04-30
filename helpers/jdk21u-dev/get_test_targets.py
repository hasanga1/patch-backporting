#!/usr/bin/env python3
"""
Test target detector for jdk21u-dev (jtreg).

Returns jtreg test file paths (e.g. test/jdk/java/util/Arrays/Foo.java) so
run_tests.sh can pass them directly to jtreg.

Supported call modes (matching build_systems.py contract):
  --files-json <json>   List of [status, filepath] from target.patch  (primary)
  --worktree            Changed test files from git diff               (fallback)
  --commit <sha>        Changed test files from a specific commit      (legacy)
"""
import argparse
import json
import os
import subprocess
import sys


def _is_jtreg_test(filepath: str) -> bool:
    fp = filepath.replace("\\", "/")
    return "test/" in fp and fp.endswith(".java")


def _collect_from_entries(entries):
    modified, added = [], []
    for entry in entries:
        status, filepath = entry[0], entry[1].replace("\\", "/")
        if _is_jtreg_test(filepath):
            if status == "M":
                modified.append(filepath)
            elif status == "A":
                added.append(filepath)
    return modified, added


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--files-json", default=None,
                        help="JSON list of [status, filepath] from target.patch")
    parser.add_argument("--worktree", action="store_true",
                        help="Detect from current git diff (staged + unstaged)")
    parser.add_argument("--commit", default=None,
                        help="Detect from a specific commit SHA")
    args = parser.parse_args()

    modified, added = [], []

    if args.files_json is not None:
        entries = json.loads(args.files_json)
        modified, added = _collect_from_entries(entries)

    elif args.commit:
        cmd = ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", args.commit]
        try:
            out = subprocess.check_output(cmd, cwd=args.repo, text=True)
            for line in out.strip().splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    status, fp = parts[0], parts[1].replace("\\", "/")
                    if _is_jtreg_test(fp):
                        (modified if status == "M" else added if status == "A" else []).append(fp)
        except subprocess.CalledProcessError:
            pass

    elif args.worktree:
        for diff_filter, target_list in [("M", modified), ("A", added)]:
            seen: set[str] = set()
            for extra in ([], ["--cached"]):
                try:
                    out = subprocess.check_output(
                        ["git", "diff", *extra, "--name-only", f"--diff-filter={diff_filter}"],
                        cwd=args.repo, text=True,
                    )
                    for line in out.strip().splitlines():
                        fp = line.strip().replace("\\", "/")
                        if fp and fp not in seen and _is_jtreg_test(fp):
                            seen.add(fp)
                            target_list.append(fp)
                except subprocess.CalledProcessError:
                    pass

    print(json.dumps({
        "modified": sorted(set(modified)),
        "added": sorted(set(added)),
        "source_modules": [],
        "all_modules": [],
    }))


if __name__ == "__main__":
    main()
