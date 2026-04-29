#!/usr/bin/env python3
"""
Java backport patch generation using the PortGPT methodology via OpenRouter.

For each CSV row in [start_row, end_row], this script:
  1. Extracts the original upstream fix patch          → original.patch
  2. Filters it to core Java source changes only       → original_filtered.patch
     (test files and non-Java changes are removed so the LLM only reasons
      about logic changes, not boilerplate test scaffolding)
  3. Extracts the developer ground-truth backport      → developer.patch
  4. Detects which test classes to run from developer.patch via
     helpers/{project}/get_test_targets.py
  5. Runs the PortGPT agent on the filtered patch      → generated.patch
  6. Compiles and tests the generated patch via
     helpers/{project}/run_build.sh + run_tests.sh (Docker)
  7. Writes per-sample metadata.json and run.log

USAGE
-----
    python java_backport.py \\
        --csv   my_java_dataset/java_backports.csv \\
        --repo-root my_java_dataset/repos \\
        --start-row 1 --end-row 5 \\
        --output output/

    # Skip Docker build/test (patch-apply check only):
    python java_backport.py ... --skip-validation

Credentials are read from .env (OPENROUTER_API_KEY / OPENAI_KEY,
OPENROUTER_MODEL / OPENAI_MODEL).
"""

import argparse
import csv
import datetime
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import git
from dotenv import load_dotenv
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.callbacks import FileCallbackHandler
from langchain_openai import ChatOpenAI

load_dotenv()

# ── PortGPT source modules ────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_SCRIPT_DIR, "src"))

from agent.prompt import SYSTEM_PROMPT, USER_PROMPT_HUNK  # noqa: E402
from tools.logger import logger  # noqa: E402
from tools.project import Project  # noqa: E402
from tools.utils import split_patch  # noqa: E402

# ── helpers/ — build and test runner ─────────────────────────────────────────
sys.path.insert(0, os.path.join(_SCRIPT_DIR, "helpers"))
import build_systems  # noqa: E402

# Fix the helpers root: build_systems.py was written for a different repo layout
# where it was 2 levels deep.  Here helpers/ sits directly in the project root.
build_systems._HELPERS_ROOT = os.path.join(_SCRIPT_DIR, "helpers")
build_systems._REPO_ROOT = _SCRIPT_DIR

# ---------------------------------------------------------------------------
# Known GitHub URLs
# ---------------------------------------------------------------------------
GITHUB_URLS: dict = {
    "crate": "https://github.com/crate/crate",
    "druid": "https://github.com/apache/druid",
    "elasticsearch": "https://github.com/elastic/elasticsearch",
    "graylog": "https://github.com/Graylog2/graylog2-server",
    "grpc-java": "https://github.com/grpc/grpc-java",
    "hadoop": "https://github.com/apache/hadoop",
    "hbase": "https://github.com/apache/hbase",
    "hibernate-orm": "https://github.com/hibernate/hibernate-orm",
    "jdk": "https://github.com/openjdk/jdk",
    "jdk11u-dev": "https://github.com/openjdk/jdk11u-dev",
    "jdk17u-dev": "https://github.com/openjdk/jdk17u-dev",
    "jdk21u-dev": "https://github.com/openjdk/jdk21u-dev",
    "jdk25u-dev": "https://github.com/openjdk/jdk25u-dev",
    "logstash": "https://github.com/elastic/logstash",
    "spring-framework": "https://github.com/spring-projects/spring-framework",
    "sql": "https://github.com/crate/crate",
}

# Some CSV project names differ from their helpers/ subdirectory name.
HELPER_NAME_MAP: dict = {
    "graylog": "graylog2-server",
}

# Some CSV project names differ from the directory name under --repo-root.
# "sql" is a CrateDB SQL-engine subproject cloned from the same crate/crate repo.
REPO_DIR_MAP: dict = {
    "sql": "crate",
}

# ---------------------------------------------------------------------------
# Java source-file classifier
# ---------------------------------------------------------------------------

# Test source directories (any path segment matching these is a test file)
_TEST_SOURCE_DIRS = (
    "/src/test/java/",
    "/src/internalClusterTest/java/",
    "/src/javaRestTest/java/",
    "/src/yamlRestTest/java/",
    "/src/integTest/java/",
    "/src/integrationTest/java/",
)

# File name suffixes that identify test classes
_TEST_SUFFIXES = ("Test.java", "Tests.java", "IT.java", "TestCase.java")


def _is_java_source_file(file_path: str) -> bool:
    """
    Return True only for core Java production source files.

    Excluded:
      • non-Java files (anything not ending in .java)
      • files inside a test source directory
      • files whose name ends with a recognised test suffix
      • files whose name starts with 'test' (case-insensitive)
    """
    if not (file_path or "").endswith(".java"):
        return False
    p = file_path.replace("\\", "/")
    if any(d in p for d in _TEST_SOURCE_DIRS):
        return False
    fname = os.path.basename(p)
    if any(fname.endswith(s) for s in _TEST_SUFFIXES):
        return False
    if fname.lower().startswith("test") and fname.endswith(".java"):
        return False
    return True


def _split_diff_blocks(patch_text: str) -> list[str]:
    """Split a multi-file patch into individual per-file diff blocks."""
    parts = re.split(r"(?=^diff --git )", patch_text, flags=re.MULTILINE)
    return [p for p in parts if p.startswith("diff --git ")]


def _block_file_path(block: str) -> str | None:
    """Return the file path for a diff block, or None if unparseable."""
    m = re.search(r"^--- a/(.+?)(?:\t|\n|$)", block, re.MULTILINE)
    if not m:
        m = re.search(r"^\+\+\+ b/(.+?)(?:\t|\n|$)", block, re.MULTILINE)
    return m.group(1).strip() if m else None


def _filter_source_only_patch(patch_text: str) -> str:
    """
    Strip a git-show patch down to core Java source changes only.

    Removes:
      • the commit-message header
      • all diff blocks for non-Java files (.xml, .rst, .yaml, etc.)
      • all diff blocks for Java test files

    Returns the filtered diff text (may be empty if nothing survives).
    """
    result = []
    for block in _split_diff_blocks(patch_text):
        fp = _block_file_path(block)
        if fp and _is_java_source_file(fp):
            result.append(block)
    return "".join(result)


def _filter_non_source_patch(patch_text: str) -> str:
    """
    Return the parts of a patch that are NOT core Java source files:
    test files and any non-Java files (.xml, .rst, .yaml, resources, etc.).

    This is the complement of _filter_source_only_patch and is used to
    extract the developer's test + non-Java changes so they can be merged
    with the model-generated Java changes before build/test validation.
    """
    result = []
    for block in _split_diff_blocks(patch_text):
        fp = _block_file_path(block)
        if fp and not _is_java_source_file(fp):
            result.append(block)
    return "".join(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_clean(repo: git.Repo) -> None:
    """
    Run 'git clean -fdx', tolerating permission-denied errors on Docker-created
    build artifacts (target/, .gradle/, etc.) that are owned by root inside the
    container but not by the host user.  Source files are already reset to a clean
    state by the preceding 'git reset --hard', so leftover build dirs are harmless.
    """
    try:
        repo.git.clean("-fdx")
    except git.exc.GitCommandError as exc:
        stderr = getattr(exc, "stderr", str(exc))
        if "Permission denied" in stderr or "Directory not empty" in stderr:
            logger.debug(f"git clean -fdx: ignoring permission error on Docker artifacts: {stderr[:300]}")
        else:
            raise


def _resolve_commit(repo: git.Repo, ref: str) -> str:
    try:
        return repo.git.rev_parse(ref)
    except git.exc.GitCommandError as exc:
        raise ValueError(f"Cannot resolve ref '{ref}': {exc}") from exc


def _extract_patch_text(repo: git.Repo, commit_hash: str) -> str:
    return repo.git.show(commit_hash)


def _save_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _ensure_trailing_newline(text: str) -> str:
    if not text:
        return text
    return text if text.endswith("\n") else text + "\n"


def _count_hunks(patch_text: str) -> int:
    return len(list(split_patch(patch_text, True)))


def _hunk_file(hunk: str) -> str:
    m = re.search(r"--- a/(.*)", hunk)
    return m.group(1).strip() if m else "unknown"


def _get_backport_file_entries(repo: git.Repo, commit_hash: str) -> list:
    """
    Return [(status, filepath), ...] for every file changed in commit_hash.

    Status values: 'A' added, 'M' modified, 'D' deleted, 'R' renamed, etc.
    These are passed to build_systems.detect_test_targets() so it can find
    both the test classes to run AND the source modules to build.
    """
    try:
        raw = repo.git.diff_tree(
            "--no-commit-id", "-r", "--name-status", commit_hash
        )
        entries = []
        for line in raw.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                entries.append((parts[0].strip(), parts[1].strip()))
        return entries
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Patch similarity
# ---------------------------------------------------------------------------

def _compute_similarity(generated_patch: str, developer_patch_text: str) -> dict:
    """
    Compare changed lines (+/-) of the generated patch vs the developer
    ground-truth.  Returns precision, recall, F1, and file overlap.

      precision  = matching_lines / generated_lines
        "of what the model changed, how much matches the developer?"
      recall     = matching_lines / developer_lines
        "of what the developer changed, how much did the model capture?"
    """
    def _changes(text: str) -> list:
        return [
            ln.strip()
            for ln in text.splitlines()
            if ln.startswith(("+", "-")) and not ln.startswith(("---", "+++"))
        ]

    gen = _changes(generated_patch)
    dev = _changes(developer_patch_text)
    gen_set, dev_set = set(gen), set(dev)
    overlap = gen_set & dev_set

    precision = len(overlap) / len(gen_set) if gen_set else 0.0
    recall    = len(overlap) / len(dev_set) if dev_set else 0.0
    f1        = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0 else 0.0
    )

    gen_files = sorted(set(re.findall(r"--- a/(.*)", generated_patch)))
    dev_files = sorted(set(re.findall(r"--- a/(.*)", developer_patch_text)))

    return {
        "exact_match":                generated_patch.strip() == developer_patch_text.strip(),
        "changed_lines_precision":    round(precision, 4),
        "changed_lines_recall":       round(recall,    4),
        "changed_lines_f1":           round(f1,        4),
        "generated_changed_lines":    len(gen),
        "developer_changed_lines":    len(dev),
        "generated_files_modified":   gen_files,
        "developer_files_modified":   dev_files,
        "files_match":                gen_files == dev_files,
    }


# ---------------------------------------------------------------------------
# PortGPT agent  (LangChain 0.2.x — portgpt311 environment)
# ---------------------------------------------------------------------------

def _build_agent(
    portgpt_project: Project,
    data: SimpleNamespace,
    debug_mode: bool,
) -> tuple:
    llm = ChatOpenAI(
        temperature=0.5,
        model=data.model,
        api_key=data.openrouter_key,
        openai_api_base="https://openrouter.ai/api/v1",
        verbose=debug_mode,
        model_kwargs={
            "extra_headers": {
                "HTTP-Referer": "https://github.com/portgpt/patch-backporting",
                "X-Title": "PortGPT Java Backporter",
            }
        },
    )

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            ("user", USER_PROMPT_HUNK),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )

    viewcode, locate_symbol, validate, git_history, git_show = (
        portgpt_project.get_tools()
    )
    tools = [viewcode, locate_symbol, validate, git_history, git_show]
    agent = create_tool_calling_agent(llm, tools, prompt)
    executor = AgentExecutor(
        agent=agent, tools=tools, verbose=debug_mode, max_iterations=30
    )
    return executor, llm


# ---------------------------------------------------------------------------
# PortGPT backport loop
# ---------------------------------------------------------------------------

def _do_backport_java(
    portgpt_project: Project,
    data: SimpleNamespace,
    debug_mode: bool,
    logfile: str,
    repo_path: str,
    source_patch_text: str | None = None,
) -> tuple:
    """
    Iterate over hunks in the (filtered) upstream fix patch.

    source_patch_text:
        The already-filtered patch text to use.  Only core Java source hunks
        should be present here — test and non-Java changes are excluded so
        the LLM reasons about logic, not scaffolding.
        If None, falls back to the raw git show of data.new_patch.

    Returns:
        (success, llm_was_called, hunk_details, portgpt_validation, num_hunks_applied)
    """
    log_handler = FileCallbackHandler(logfile)

    patch_text = (
        source_patch_text
        if source_patch_text is not None
        else portgpt_project._get_patch(data.new_patch)
    )
    hunks = list(split_patch(patch_text, True))

    if not hunks:
        # Nothing to backport after filtering — treat as trivially succeeded.
        portgpt_project.all_hunks_applied_succeeded = True
        portgpt_project.poc_succeeded = True
        portgpt_project.compile_succeeded = True
        portgpt_project.testcase_succeeded = True
        portgpt_project.succeeded_patches = [""]
        return True, False, [], {
            "apply_check": {"result": "skipped", "reason": "no source hunks after filtering"},
        }, 0

    agent_executor, _ = _build_agent(portgpt_project, data, debug_mode)

    llm_was_called = False
    hunk_details = []

    for idx, hunk in enumerate(hunks):
        hunk_file = _hunk_file(hunk)
        portgpt_project.round_succeeded = False
        portgpt_project.context_mismatch_times = 0

        # --- try direct git apply first ---
        portgpt_project._apply_hunk(data.target_release, hunk, False)
        if portgpt_project.round_succeeded:
            logger.debug(f"Hunk {idx} ({hunk_file}): direct apply OK")
            hunk_details.append(
                {"index": idx, "file": hunk_file, "outcome": "direct", "llm_iterations": 0}
            )
            continue

        # --- direct apply failed: call LLM ---
        error_msg = portgpt_project._apply_hunk(data.target_release, hunk, False)
        portgpt_project.round_succeeded = False
        block_list = re.findall(
            r"older version.\n(.*?)\nBesides,", error_msg, re.DOTALL
        )
        similar_block = "\n".join(block_list)

        logger.debug(f"Hunk {idx} ({hunk_file}): invoking LLM agent")
        llm_was_called = True
        portgpt_project.now_hunk = hunk
        portgpt_project.now_hunk_num = idx

        try:
            agent_executor.invoke(
                {
                    "project_url": data.project_url,
                    "new_patch_parent": data.new_patch_parent,
                    "new_patch": hunk,
                    "target_release": data.target_release,
                    "similar_block": similar_block,
                },
                {"callbacks": [log_handler]},
            )
        except Exception as exc:
            logger.error(f"Hunk {idx}: agent exception — {exc}", exc_info=True)

        if not portgpt_project.round_succeeded:
            logger.error(f"Hunk {idx}: agent failed (max iterations or error)")
            hunk_details.append(
                {"index": idx, "file": hunk_file, "outcome": "failed", "llm_iterations": -1}
            )
            return False, llm_was_called, hunk_details, {}, 0

        hunk_details.append(
            {
                "index": idx,
                "file": hunk_file,
                "outcome": "llm_generated",
                "llm_iterations": len(portgpt_project.hunk_log_info.get(idx, [])),
            }
        )

    # ── all hunks passed — run PortGPT's lightweight validation stubs ────────
    # capture BEFORE _run_poc resets succeeded_patches to [complete_patch]
    num_hunks_applied = len(portgpt_project.succeeded_patches)

    portgpt_project.all_hunks_applied_succeeded = True
    portgpt_project.now_hunk = "completed"
    complete_patch = "\n".join(portgpt_project.succeeded_patches)

    # patch_dataset_dir is always empty → copy loop is a no-op
    _safe_clean(portgpt_project.repo)
    for filename in os.listdir(data.patch_dataset_dir):
        src = os.path.join(data.patch_dataset_dir, filename)
        dst = os.path.join(data.project_dir, filename)
        if os.path.exists(dst):
            os.remove(dst)
        shutil.copy2(src, dst)

    portgpt_project.context_mismatch_times = 0
    # _validate → _compile_patch (no build.sh → auto-pass, leaves patch applied
    #                              in working tree)
    #           → _run_testcase  (no test.sh  → auto-pass)
    #           → _run_poc       (no poc.sh   → auto-pass)
    portgpt_project._validate(data.target_release, complete_patch)

    has_build = os.path.exists(os.path.join(repo_path, "build.sh"))
    has_test  = os.path.exists(os.path.join(repo_path, "test.sh"))
    has_poc   = os.path.exists(os.path.join(repo_path, "poc.sh"))

    portgpt_validation = {
        "apply_check": {
            "result": "passed",
            "meaning": (
                "git apply succeeded for all source hunks on the target branch. "
                "Patch context lines match the target codebase exactly."
            ),
        },
        "compile_check": {
            "result": "passed" if portgpt_project.compile_succeeded else "failed",
            "has_build_sh": has_build,
            "meaning": "No build.sh in repo — auto-passed. Real compile runs via helpers Docker." if not has_build else "build.sh present.",
        },
        "test_check": {
            "result": "passed" if portgpt_project.testcase_succeeded else "failed",
            "has_test_sh": has_test,
        },
        "poc_check": {
            "result": "passed" if portgpt_project.poc_succeeded else "failed",
            "has_poc_sh": has_poc,
        },
    }

    if portgpt_project.poc_succeeded:
        return True, llm_was_called, hunk_details, portgpt_validation, num_hunks_applied

    return False, llm_was_called, hunk_details, portgpt_validation, num_hunks_applied


# ---------------------------------------------------------------------------
# Post-apply build + test via helpers/
# ---------------------------------------------------------------------------

def _run_build_and_tests(
    repo_path: str,
    project_name: str,
    target_info,
    sample_dir: str = None,
) -> tuple:
    """
    Compile the working tree, then run the targeted tests.

    The working tree must already have the generated patch applied
    (PortGPT's _compile_patch leaves it in this state).

    Args:
        repo_path: Path to the repository
        project_name: Name of the project
        target_info: Test target information
        sample_dir: Optional directory to save build/test log files

    Returns (build_meta, test_meta) dicts suitable for metadata.json.
    Saves build.log and test.log to sample_dir if provided.
    """
    helper_project = HELPER_NAME_MAP.get(project_name, project_name)

    # If no Docker helper exists for this project, skip validation entirely.
    # This covers jdk* repos (which use make, not Maven) and any future
    # projects that don't yet have a helpers/ entry.
    if not build_systems._has_helper(helper_project):
        logger.info(
            f"  No helper found for '{helper_project}' — skipping build/test validation"
        )
        skipped = {"success": None, "mode": "skipped_no_helper", "output_tail": ""}
        return skipped, {**skipped, "compile_error": False, "test_state": {}}

    # ── build ────────────────────────────────────────────────────────────────
    logger.info(f"  Running build for {project_name} (helper: {helper_project})")
    try:
        br = build_systems.run_build(repo_path, helper_project)
        build_meta = {
            "success": br.success,
            "mode": br.mode,
            "output_tail": (br.output or "")[-3000:],
        }
        # Save full build log to file
        if sample_dir and br.output:
            build_log_path = os.path.join(sample_dir, "build.log")
            _save_text(build_log_path, br.output)
            logger.debug(f"  Build log saved to {build_log_path}")
    except Exception as exc:
        logger.warning(f"  build_systems.run_build raised: {exc}")
        build_meta = {"success": False, "mode": "error", "output_tail": str(exc)}

    if not build_meta["success"]:
        logger.warning(f"  Build failed — skipping test run")
        return build_meta, {
            "success": False,
            "mode": "skipped_build_failed",
            "compile_error": True,
            "test_state": {},
            "output_tail": "",
        }

    # ── tests ────────────────────────────────────────────────────────────────
    if target_info is None or (
        not target_info.test_targets and not target_info.source_modules
    ):
        logger.info("  No test targets detected — skipping test run")
        return build_meta, {
            "success": True,
            "mode": "skipped_no_targets",
            "compile_error": False,
            "test_state": {
                "summary": {"passed": 0, "failed": 0, "skipped": 0, "total": 0}
            },
            "output_tail": "",
        }

    logger.info(
        f"  Running {len(target_info.test_targets)} test target(s) "
        f"for {project_name}"
    )
    try:
        tr = build_systems.run_tests(repo_path, helper_project, target_info=target_info)
        test_meta = {
            "success": tr.success,
            "mode": tr.mode,
            "compile_error": tr.compile_error,
            "test_state": tr.test_state,
            "output_tail": (tr.output or "")[-3000:],
        }
        # Save full test log to file
        if sample_dir and tr.output:
            test_log_path = os.path.join(sample_dir, "test.log")
            _save_text(test_log_path, tr.output)
            logger.debug(f"  Test log saved to {test_log_path}")
    except Exception as exc:
        logger.warning(f"  build_systems.run_tests raised: {exc}")
        test_meta = {
            "success": False,
            "mode": "error",
            "compile_error": False,
            "test_state": {},
            "output_tail": str(exc),
        }

    return build_meta, test_meta


# ---------------------------------------------------------------------------
# Per-sample processing
# ---------------------------------------------------------------------------

def process_row(
    row_num: int,
    row: dict,
    repo_root: str,
    output_dir: str,
    openrouter_key: str,
    model: str,
    debug_mode: bool,
    skip_validation: bool,
) -> dict:
    """Process one CSV row: filter patch, detect tests, run PortGPT, validate."""

    project_name   = row["Project"].strip()
    original_commit = row["Original Commit"].strip()
    backport_commit = row["Backport Commit"].strip()

    # Some CSV project names use a different directory name under --repo-root
    # (e.g. "sql" shares the crate/crate clone stored as "crate/").
    repo_dir_name = REPO_DIR_MAP.get(project_name, project_name)
    repo_path = os.path.join(repo_root, repo_dir_name)
    if not os.path.isdir(repo_path):
        msg = f"Repository directory not found: {repo_path}"
        logger.error(f"Row {row_num}: {msg}")
        return {
            "row": row_num, "project": project_name,
            "success": False, "patch_type": "error", "notes": msg,
        }

    sample_id  = (
        f"{project_name}_row{row_num:04d}"
        f"_{original_commit[:8]}_{backport_commit[:8]}"
    )
    sample_dir = os.path.join(output_dir, sample_id)
    os.makedirs(sample_dir, exist_ok=True)

    logfile      = os.path.join(sample_dir, "run.log")
    log_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler = logging.FileHandler(logfile)
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)

    metadata: dict = {
        "row":              row_num,
        "project":          project_name,
        "original_version": row.get("Original Version", "").strip(),
        "original_commit":  original_commit,
        "backport_version": row.get("Backport Version", "").strip(),
        "backport_commit":  backport_commit,
        "backport_date":    row.get("Backport Date", "").strip(),
        "type":             row.get("Type", "").strip(),
        "success":          False,
        "patch_type":       "failed",
        "llm_invoked":      False,
        # hunk counts — _total is for the FILTERED (source-only) patch
        "num_hunks_original":  0,   # hunks in full original.patch
        "num_hunks_filtered":  0,   # hunks after removing tests/non-Java
        "num_hunks_succeeded": 0,
        "num_non_source_hunks_developer": 0,  # test+non-Java hunks from developer.patch
        "dev_non_source_applied": False,      # whether those were merged into working tree
        "hunk_details":        [],
        "test_targets":        {},   # detected from developer.patch
        "portgpt_validation":  {},
        "build_validation":    {},
        "test_validation":     {},
        "patch_similarity":    {},
        "model":               model,
        "timestamp":           datetime.datetime.now().isoformat(),
        "duration_seconds":    0,
        "sample_dir":          sample_dir,
        "notes":               "",
    }

    start_time    = time.time()
    original_head = None
    tmp_patch_dir = None
    developer_patch_text = ""

    try:
        repo = git.Repo(repo_path)

        try:
            original_head = repo.active_branch.name
        except TypeError:
            original_head = _resolve_commit(repo, "HEAD")

        # ── 1. Extract full original.patch ──────────────────────────────────
        logger.info(f"Row {row_num} [{project_name}]: extracting original.patch")
        original_patch_text = _extract_patch_text(repo, original_commit)
        _save_text(os.path.join(sample_dir, "original.patch"), original_patch_text)
        metadata["num_hunks_original"] = _count_hunks(original_patch_text)

        # ── 2. Filter to core Java source changes (what the LLM will see) ───
        filtered_patch_text = _filter_source_only_patch(original_patch_text)
        _save_text(os.path.join(sample_dir, "original_filtered.patch"), filtered_patch_text)
        metadata["num_hunks_filtered"] = _count_hunks(filtered_patch_text)
        logger.info(
            f"Row {row_num}: {metadata['num_hunks_original']} total hunks, "
            f"{metadata['num_hunks_filtered']} after filtering "
            f"(test/non-Java removed)"
        )

        # ── 3. Extract developer.patch ───────────────────────────────────────
        logger.info(f"Row {row_num} [{project_name}]: extracting developer.patch")
        developer_patch_text = _extract_patch_text(repo, backport_commit)
        _save_text(os.path.join(sample_dir, "developer.patch"), developer_patch_text)

        # ── 4. Detect test targets from developer.patch ──────────────────────
        bp_file_entries = _get_backport_file_entries(repo, backport_commit)
        helper_project  = HELPER_NAME_MAP.get(project_name, project_name)
        target_info     = None
        if bp_file_entries:
            try:
                target_info = build_systems.detect_test_targets(
                    repo_path, helper_project, file_entries=bp_file_entries
                )
                metadata["test_targets"] = {
                    "test_targets":   target_info.test_targets,
                    "source_modules": target_info.source_modules,
                    "all_modules":    target_info.all_modules,
                }
                logger.info(
                    f"Row {row_num}: detected {len(target_info.test_targets)} "
                    f"test target(s) from developer.patch"
                )
            except Exception as exc:
                logger.warning(f"Row {row_num}: test target detection failed — {exc}")

        # ── 5. Resolve PortGPT refs ──────────────────────────────────────────
        # new_patch        = upstream fix commit
        # new_patch_parent = its parent (state before the fix, used by viewcode)
        # target_release   = parent of the backport (state we must patch)
        new_patch        = _resolve_commit(repo, original_commit)
        new_patch_parent = _resolve_commit(repo, f"{original_commit}^")
        target_release   = _resolve_commit(repo, f"{backport_commit}^")

        # ── 6. Build PortGPT data object ─────────────────────────────────────
        tmp_patch_dir = tempfile.mkdtemp()
        project_url   = GITHUB_URLS.get(
            project_name, f"https://github.com/{project_name}"
        )
        data = SimpleNamespace(
            project=project_name,
            project_url=project_url,
            project_dir=str(Path(repo_path).resolve()) + "/",
            patch_dataset_dir=tmp_patch_dir + "/",
            openrouter_key=openrouter_key,
            model=model,
            new_patch=new_patch,
            new_patch_parent=new_patch_parent,
            target_release=target_release,
            error_message="",
            tag=f"{project_name}-{original_commit[:8]}",
        )

        # ── 7. Run PortGPT on the FILTERED patch ─────────────────────────────
        portgpt_project = Project(data)
        _safe_clean(portgpt_project.repo)

        logger.info(f"Row {row_num}: starting PortGPT backport agent")
        portgpt_success, llm_was_called, hunk_details, portgpt_validation, num_applied = (
            _do_backport_java(
                portgpt_project,
                data,
                debug_mode,
                logfile,
                repo_path,
                source_patch_text=filtered_patch_text,
            )
        )

        metadata["llm_invoked"]        = llm_was_called
        metadata["num_hunks_succeeded"] = num_applied
        metadata["hunk_details"]        = hunk_details
        metadata["portgpt_validation"]  = portgpt_validation

        # ── 8. Save generated.patch ──────────────────────────────────────────
        generated_text = (
            "\n".join(portgpt_project.succeeded_patches)
            if portgpt_project.succeeded_patches
            else ""
        )
        _save_text(os.path.join(sample_dir, "generated.patch"), generated_text)

        # ── 8.5. Merge: generated (Java source) + developer non-source ───────
        # The model only sees and generates Java source changes.  To run a
        # meaningful build+test we re-attach the developer's test files and
        # non-Java changes on top of the model's output.  Those files are
        # taken verbatim from the ground-truth developer.patch and applied
        # to the working tree after the generated patch is already in place.
        dev_non_source_text = _filter_non_source_patch(developer_patch_text)
        dev_non_source_text = _ensure_trailing_newline(dev_non_source_text)
        _save_text(
            os.path.join(sample_dir, "developer_non_source.patch"),
            dev_non_source_text,
        )
        num_non_source_hunks = _count_hunks(dev_non_source_text)
        metadata["num_non_source_hunks_developer"] = num_non_source_hunks

        # Build the merged artifact (what actually goes into the working tree)
        merged_text = (generated_text.rstrip("\n") + "\n" + dev_non_source_text
                       if generated_text and dev_non_source_text
                       else generated_text or dev_non_source_text)
        _save_text(os.path.join(sample_dir, "merged.patch"), merged_text)

        # Apply developer non-source changes on top of already-applied generated patch
        dev_non_source_applied = False
        if portgpt_success and dev_non_source_text:
            patch_tmp = None
            try:
                import tempfile as _tmpmod
                with _tmpmod.NamedTemporaryFile(
                    mode="w", suffix=".patch", delete=False, encoding="utf-8"
                ) as f:
                    f.write(dev_non_source_text)
                    patch_tmp = f.name
                portgpt_project.repo.git.apply(patch_tmp)
                dev_non_source_applied = True
                logger.info(
                    f"Row {row_num}: merged {num_non_source_hunks} non-source "
                    f"hunk(s) from developer.patch into working tree"
                )
            except Exception as exc:
                logger.warning(
                    f"Row {row_num}: could not apply developer non-source changes "
                    f"— {exc}"
                )
            finally:
                if patch_tmp:
                    try:
                        os.unlink(patch_tmp)
                    except OSError:
                        pass
        metadata["dev_non_source_applied"] = dev_non_source_applied

        # ── 9. Build + test validation via helpers/ ──────────────────────────
        # Working tree now holds: target_release + generated (Java source) +
        # developer non-source (tests + non-Java).  Docker build/test runs
        # against this merged state.
        if portgpt_success and not skip_validation:
            logger.info(f"Row {row_num}: running build/test validation via helpers/")
            build_meta, test_meta = _run_build_and_tests(
                repo_path, project_name, target_info, sample_dir
            )
            metadata["build_validation"] = build_meta
            metadata["test_validation"]  = test_meta

            build_success = build_meta.get("success")
            test_success  = test_meta.get("success")

            if build_success is None:
                # No helper for this project — treat as "not applicable", not failed
                final_success = portgpt_success
            else:
                # Final success = all hunks applied AND compiled AND tests passed
                final_success = (
                    portgpt_success
                    and build_success is True
                    and (test_success is True or test_success is None)
                )
        else:
            if skip_validation:
                metadata["build_validation"] = {"result": "skipped_by_flag"}
                metadata["test_validation"]  = {"result": "skipped_by_flag"}
            final_success = portgpt_success

        # ── 10. Similarity vs developer patch (source changes only) ──────────
        # Compare the model's Java output against the developer's Java source
        # changes only — not against tests/non-Java that the model never sees.
        developer_source_text = _filter_source_only_patch(developer_patch_text)
        if generated_text and developer_source_text:
            metadata["patch_similarity"] = _compute_similarity(
                generated_text, developer_source_text
            )

        # ── 11. Record outcome ───────────────────────────────────────────────
        if final_success:
            metadata["success"]    = True
            metadata["patch_type"] = "direct" if not llm_was_called else "llm_generated"
            logger.info(f"Row {row_num}: SUCCESS ({metadata['patch_type']})")
        elif metadata["num_hunks_filtered"] == 0:
            # No source hunks to backport — counts as success
            metadata["success"]    = True
            metadata["patch_type"] = "no_source_changes"
            logger.info(f"Row {row_num}: SUCCESS (no source hunks to backport)")
        elif portgpt_project.succeeded_patches:
            metadata["patch_type"] = "partial"
            metadata["notes"] = (
                f"{num_applied}/{metadata['num_hunks_filtered']} hunk(s) succeeded"
            )
            logger.warning(f"Row {row_num}: PARTIAL — {metadata['notes']}")
        else:
            metadata["patch_type"] = "failed"
            metadata["notes"]      = "No hunks could be backported"
            logger.error(f"Row {row_num}: FAILED")

    except Exception as exc:
        logger.error(f"Row {row_num}: unhandled exception — {exc}", exc_info=True)
        metadata["notes"] = str(exc)
        generated_path = os.path.join(sample_dir, "generated.patch")
        if not os.path.exists(generated_path):
            _save_text(generated_path, "")

    finally:
        if original_head:
            try:
                restore_repo = git.Repo(repo_path)
                # Reset any uncommitted changes first so the checkout can't be
                # blocked by "your changes would be overwritten" errors.
                restore_repo.git.reset("--hard")
                restore_repo.git.checkout("-f", original_head)
                _safe_clean(restore_repo)
            except Exception as exc:
                logger.warning(
                    f"Row {row_num}: could not restore repo to "
                    f"'{original_head}' — {exc}"
                )

        if tmp_patch_dir and os.path.isdir(tmp_patch_dir):
            shutil.rmtree(tmp_patch_dir, ignore_errors=True)

        logger.removeHandler(file_handler)
        file_handler.close()

    metadata["duration_seconds"] = int(time.time() - start_time)

    json_path = os.path.join(sample_dir, "metadata.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, default=str)

    logger.info(
        f"Row {row_num}: artefacts in {sample_dir} [{metadata['duration_seconds']}s]"
    )
    return metadata


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _write_summary(results: list, output_dir: str) -> None:
    total   = len(results)
    success = sum(1 for r in results if r.get("success"))
    partial = sum(1 for r in results if r.get("patch_type") == "partial")
    direct  = sum(1 for r in results if r.get("patch_type") == "direct")
    llm_gen = sum(1 for r in results if r.get("patch_type") == "llm_generated")
    failed  = sum(1 for r in results if r.get("patch_type") in ("failed", "error"))
    no_src  = sum(1 for r in results if r.get("patch_type") == "no_source_changes")

    f1_scores = [
        r["patch_similarity"]["changed_lines_f1"]
        for r in results
        if r.get("patch_similarity") and "changed_lines_f1" in r["patch_similarity"]
    ]
    avg_f1 = round(sum(f1_scores) / len(f1_scores), 4) if f1_scores else None

    build_pass = sum(
        1 for r in results
        if r.get("build_validation", {}).get("success") is True
    )
    test_pass = sum(
        1 for r in results
        if r.get("test_validation", {}).get("success") is True
    )

    summary = {
        "timestamp":           datetime.datetime.now().isoformat(),
        "total_rows":          total,
        "succeeded":           success,
        "partial":             partial,
        "direct_cherry_pick":  direct,
        "llm_generated":       llm_gen,
        "no_source_changes":   no_src,
        "failed":              failed,
        "build_passed":        build_pass,
        "tests_passed":        test_pass,
        "avg_similarity_f1":   avg_f1,
        "results":             results,
    }
    summary_path = os.path.join(output_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)

    logger.info(f"Summary → {summary_path}")
    logger.info(
        f"  {success}/{total} succeeded  "
        f"({direct} direct, {llm_gen} LLM)  "
        f"{partial} partial  {no_src} no-source  {failed} failed  "
        f"build={build_pass}  tests={test_pass}  avg_f1={avg_f1}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Java backport patches using PortGPT via OpenRouter.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--csv",        required=True, metavar="PATH",
                        help="Path to java_backports.csv")
    parser.add_argument("--repo-root",  required=True, metavar="DIR",
                        help="Root dir containing one subdirectory per project")
    parser.add_argument("--start-row",  type=int, default=1,   metavar="N",
                        help="First data row (1-indexed, inclusive; default: 1)")
    parser.add_argument("--end-row",    type=int, default=None, metavar="N",
                        help="Last data row  (1-indexed, inclusive; default: last)")
    parser.add_argument("--output",     default="output",      metavar="DIR",
                        help="Output directory (default: output/)")
    parser.add_argument("--model",      default=None,          metavar="MODEL",
                        help="OpenRouter model, e.g. anthropic/claude-3.5-sonnet")
    parser.add_argument("--skip-validation", action="store_true",
                        help="Skip Docker build/test; rely on git-apply check only")
    parser.add_argument("--skip-types", nargs="+", metavar="TYPE", default=[],
                        help="Skip rows whose Type column matches any of these values "
                             "(case-insensitive). E.g. --skip-types TYPE-I TYPE-II")
    parser.add_argument("--debug",      action="store_true",
                        help="Enable DEBUG logging and verbose agent output")
    args = parser.parse_args()

    logger.setLevel(logging.DEBUG if args.debug else logging.INFO)

    openrouter_key = (
        os.getenv("OPENROUTER_API_KEY", "").strip()
        or os.getenv("OPENAI_KEY",      "").strip()
    )
    if not openrouter_key:
        parser.error("No API key. Set OPENROUTER_API_KEY or OPENAI_KEY in .env.")

    model = (
        args.model
        or os.getenv("OPENROUTER_MODEL", "").strip()
        or os.getenv("OPENAI_MODEL",     "").strip()
        or "anthropic/claude-3.5-sonnet"
    )

    if not os.path.isfile(args.csv):
        parser.error(f"CSV not found: {args.csv}")
    if not os.path.isdir(args.repo_root):
        parser.error(f"Repo root not found: {args.repo_root}")

    os.makedirs(args.output, exist_ok=True)

    with open(args.csv, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    total = len(rows)
    start = max(1, args.start_row)
    end   = min(args.end_row if args.end_row is not None else total, total)

    if start > end:
        parser.error(f"--start-row ({start}) > --end-row ({end})")

    skip_types = {t.upper() for t in args.skip_types}

    logger.info(
        f"Processing rows {start}–{end} of {total} "
        f"| model: {model} | output: {args.output}"
        + (" | validation: SKIPPED" if args.skip_validation else "")
        + (f" | skipping types: {sorted(skip_types)}" if skip_types else "")
    )

    results = []
    for row_num in range(start, end + 1):
        row = rows[row_num - 1]
        row_type = row.get("Type", "").strip().upper()
        if skip_types and row_type in skip_types:
            logger.info(
                f"=== Row {row_num}/{end}  project={row.get('Project','?')}  "
                f"type={row_type} — SKIPPED (--skip-types) ==="
            )
            continue
        logger.info(
            f"=== Row {row_num}/{end}  project={row.get('Project','?')}  "
            f"orig={row.get('Original Commit','?')[:8]}  "
            f"bp={row.get('Backport Commit','?')[:8]} ==="
        )
        result = process_row(
            row_num=row_num,
            row=row,
            repo_root=args.repo_root,
            output_dir=args.output,
            openrouter_key=openrouter_key,
            model=model,
            debug_mode=args.debug,
            skip_validation=args.skip_validation,
        )
        results.append(result)

    _write_summary(results, args.output)


if __name__ == "__main__":
    main()
