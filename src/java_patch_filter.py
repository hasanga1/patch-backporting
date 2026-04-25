"""
Filter a raw git-show patch to keep only agent-eligible Java source file hunks.

Agent-eligible = .java file that is NOT:
  - a test file  (path contains "test" or name ends with Test/Tests/IT/TestCase)
  - an auto-generated file  (ANTLR/protobuf/gRPC naming patterns)
"""
import re


# ── Classifiers (mirrors omni-port evaluate_full_workflow.py) ──────────────

def _is_java_test_file(path: str) -> bool:
    lower = (path or "").lower()
    return "test" in lower


def _is_auto_generated_java_file(path: str) -> bool:
    lower = (path or "").lower()
    patterns = [
        r"lexer\.java$",
        r"parser\.java$",
        r"baselistener\.java$",
        r"listener\.java$",
        r"basevisitor\.java$",
        r"visitor\.java$",
        r"outerclass\.java$",
        r"pb\.java$",
        r"pborbuilder\.java$",
        r"grpc\.java$",
    ]
    return any(re.search(p, lower) for p in patterns)


def _is_agent_eligible(path: str) -> bool:
    """Return True if this path should be processed by the LLM agent."""
    if not (path or "").lower().endswith(".java"):
        return False
    if _is_java_test_file(path):
        return False
    if _is_auto_generated_java_file(path):
        return False
    return True


# ── Patch-level filter ─────────────────────────────────────────────────────

def filter_java_source_patch(patch: str) -> str:
    """
    Return a copy of *patch* containing only hunks for agent-eligible Java
    source files.  The commit-message header (lines before the first
    'diff --git') is preserved verbatim so PortGPT can still extract the
    commit message context it needs.

    Works on both 'git show' and 'git format-patch --stdout' output.
    Returns the original patch unchanged if parsing yields nothing (safety
    fallback so the pipeline never receives an empty patch unexpectedly).
    """
    if not patch or not patch.strip():
        return patch

    # Split into [header, block1, block2, ...]  on "diff --git" boundaries.
    parts = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)

    header = ""
    file_blocks = []
    for part in parts:
        if part.startswith("diff --git "):
            file_blocks.append(part)
        else:
            header = part  # commit / format-patch header

    if not file_blocks:
        return patch  # nothing to filter

    kept = []
    for block in file_blocks:
        # Extract canonical path from "diff --git a/<path> b/<path>"
        m = re.match(r"diff --git a/(.*?) b/", block)
        path = m.group(1) if m else ""
        if not path:
            # Fallback: try "+++ b/<path>"
            m2 = re.search(r"^\+\+\+ b/(.*)", block, re.MULTILINE)
            path = (m2.group(1) or "").strip() if m2 else ""
        if _is_agent_eligible(path):
            kept.append(block)

    if not kept:
        # Every hunk was filtered out — return original so PortGPT can at
        # least attempt something rather than failing silently.
        return patch

    return header + "".join(kept)


def count_eligible_files(patch: str) -> int:
    """Return the number of agent-eligible files in *patch* (for logging)."""
    parts = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    count = 0
    for part in parts:
        if not part.startswith("diff --git "):
            continue
        m = re.match(r"diff --git a/(.*?) b/", part)
        path = m.group(1) if m else ""
        if _is_agent_eligible(path):
            count += 1
    return count
