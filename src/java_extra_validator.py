import json
import os
import subprocess
import sys

from tools.logger import logger


def _run_script(cmd: list, env: dict, cwd: str, timeout: int, log_path: str) -> bool:
    """Run a shell command, write combined stdout+stderr to log_path. Returns True on exit code 0."""
    try:
        result = subprocess.run(
            cmd,
            env=env,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(result.stdout or "")
        return result.returncode == 0
    except subprocess.TimeoutExpired as exc:
        partial = ""
        if exc.output:
            partial = exc.output.decode("utf-8", errors="replace") if isinstance(exc.output, bytes) else exc.output
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"TIMEOUT after {timeout}s\n{partial}")
        return False
    except Exception as exc:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"ERROR running {cmd[0]}: {exc}\n")
        return False


def _discover_test_targets(
    script: str,
    project_dir: str,
    commit_sha: str,
    python_exe: str,
    timeout: int = 60,
) -> list:
    """Run get_test_targets.py for commit_sha; return merged list of modified+added test targets."""
    if not os.path.isfile(script):
        return []
    try:
        result = subprocess.run(
            [python_exe, script, "--repo", project_dir, "--commit", commit_sha],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        data = json.loads(result.stdout.strip())
        return data.get("modified", []) + data.get("added", [])
    except Exception as exc:
        logger.warning(f"get_test_targets failed: {exc}")
        return []


def run_extra_validation(
    project_name: str,
    project_dir: str,
    new_patch_commit: str,
    output_dir: str,
    helpers_root: str,
    builder_image_tag: str = "",
    build_timeout: int = 3600,
    tests_timeout: int = 3600,
    python_exe: str = "",
) -> dict:
    """
    Run per-project helper scripts (run_build.sh, get_test_targets.py, run_tests.sh)
    against the current working tree state (WORKTREE_MODE=1).

    new_patch_commit is the original fix commit used to discover affected test targets.

    Returns a dict matching the extra_validation section of validation_summary.json.
    """
    if not python_exe:
        python_exe = sys.executable

    project_dir = os.path.abspath(os.path.expanduser(project_dir))
    helpers_root = os.path.abspath(os.path.expanduser(helpers_root))
    helpers_dir = os.path.abspath(os.path.join(helpers_root, project_name))

    if not os.path.isdir(helpers_dir):
        logger.info(f"No helpers for '{project_name}' at {helpers_dir} — extra validation skipped")
        return {
            "ran": False,
            "passed": None,
            "build_passed": None,
            "tests_passed": None,
            "selected_test_targets": [],
            "message": f"No helpers directory: {helpers_dir}",
            "build_logs": None,
            "test_logs": None,
        }

    os.makedirs(output_dir, exist_ok=True)
    build_log = os.path.join(output_dir, "extra_build.log")
    tests_log = os.path.join(output_dir, "extra_tests.log")

    base_env = os.environ.copy()
    base_env["PROJECT_DIR"] = project_dir.rstrip("/")
    base_env["COMMIT_SHA"] = new_patch_commit
    base_env["WORKTREE_MODE"] = "1"
    effective_image_tag = builder_image_tag.strip() if builder_image_tag else ""
    if not effective_image_tag:
        effective_image_tag = f"retrofit-{project_name}-builder:local"
    # Some helper scripts use IMAGE_TAG directly, others read BUILDER_IMAGE_TAG first.
    base_env["IMAGE_TAG"] = effective_image_tag
    base_env["BUILDER_IMAGE_TAG"] = effective_image_tag

    # ── Build ──────────────────────────────────────────────────────────────
    build_script = os.path.abspath(os.path.join(helpers_dir, "run_build.sh"))
    build_passed = None
    if not os.path.isfile(build_script):
        with open(build_log, "w", encoding="utf-8") as f:
            f.write(f"No run_build.sh at {build_script}\n")
        logger.info(f"No run_build.sh for '{project_name}'")
    else:
        logger.info(f"Extra build starting for '{project_name}'")
        build_passed = _run_script(
            ["/bin/bash", build_script], base_env, project_dir, build_timeout, build_log
        )
        logger.info(f"Extra build: {'PASS' if build_passed else 'FAIL'}")

    # ── Test target discovery ──────────────────────────────────────────────
    test_targets = _discover_test_targets(
        os.path.abspath(os.path.join(helpers_dir, "get_test_targets.py")),
        project_dir,
        new_patch_commit,
        python_exe,
    )

    # ── Tests ──────────────────────────────────────────────────────────────
    tests_script = os.path.abspath(os.path.join(helpers_dir, "run_tests.sh"))
    tests_passed = None
    if not os.path.isfile(tests_script):
        with open(tests_log, "w", encoding="utf-8") as f:
            f.write(f"No run_tests.sh at {tests_script}\n")
        logger.info(f"No run_tests.sh for '{project_name}'")
    else:
        test_env = base_env.copy()
        test_env["TEST_TARGETS"] = " ".join(test_targets) if test_targets else "NONE"
        logger.info(f"Extra tests starting for '{project_name}', targets={test_targets or 'NONE'}")
        tests_passed = _run_script(
            ["/bin/bash", tests_script], test_env, project_dir, tests_timeout, tests_log
        )
        logger.info(f"Extra tests: {'PASS' if tests_passed else 'FAIL'}")

    definite = [r for r in (build_passed, tests_passed) if r is not None]
    overall = all(definite) if definite else None

    parts = []
    if build_passed is True:
        parts.append("build passed")
    elif build_passed is False:
        parts.append("build failed")
    if tests_passed is True:
        parts.append("tests passed")
    elif tests_passed is False:
        parts.append("tests failed")
    message = "; ".join(parts) if parts else "no helper scripts ran"

    return {
        "ran": True,
        "passed": overall,
        "build_passed": build_passed,
        "tests_passed": tests_passed,
        "selected_test_targets": test_targets,
        "message": message,
        "build_logs": build_log,
        "test_logs": tests_log,
    }
