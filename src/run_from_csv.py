import argparse
import csv
import json
import logging
import os
import tempfile

import git
import yaml
from dotenv import load_dotenv

from backporting import load_yml, run_pipeline
from tools.logger import logger


def _load_repo_map(repo_map_file: str) -> dict:
    with open(repo_map_file, "r", encoding="utf-8") as f:
        content = f.read()
    parsed = json.loads(content) if repo_map_file.endswith(".json") else yaml.safe_load(content)
    if not isinstance(parsed, dict):
        raise ValueError("repo-map-file must contain a key-value mapping")
    return {str(k): str(v) for k, v in parsed.items()}


def _resolve_project_dir(project: str, repo_root: str, repo_map: dict) -> str:
    if repo_map and project in repo_map:
        return os.path.expanduser(repo_map[project])
    if repo_root:
        return os.path.expanduser(os.path.join(repo_root, project))
    raise ValueError("Unable to resolve project_dir. Provide --repo-root or --repo-map-file.")


def _get_remote_url(repo: git.Repo) -> str:
    try:
        return next(repo.remote("origin").urls)
    except Exception:
        return ""


def _get_parent_hexsha(repo: git.Repo, commit_id: str, parent_index: int = 0) -> str:
    commit = repo.commit(commit_id)
    if len(commit.parents) <= parent_index:
        raise ValueError(f"Commit {commit_id} does not have parent index {parent_index}.")
    return commit.parents[parent_index].hexsha


def _get_patch_against_parent(repo: git.Repo, commit_id: str, parent_index: int = 0) -> str:
    """
    Return unified diff between commit and one of its parents.

    For merge commits (e.g., backport PR merge commits), this captures the net
    change introduced on the chosen parent side.
    """
    commit = repo.commit(commit_id)
    if not commit.parents:
        return repo.git.show(commit_id)

    idx = parent_index if len(commit.parents) > parent_index else 0
    parent = commit.parents[idx].hexsha
    return repo.git.diff(parent, commit.hexsha)


def _process_row(
    row: dict,
    row_index: int,
    args,
    api_key: str,
    api_base: str,
    llm_provider: str,
    model: str,
    repo_map: dict,
) -> str:
    """
    Process a single CSV row. Returns 'ok', 'skipped', or 'error'.
    Raises nothing — all exceptions are caught and logged.
    """
    patch_type = row.get("Type", "").strip()

    project = row["Project"]
    original_commit = row["Original Commit"].strip()
    backport_commit = row["Backport Commit"].strip()

    # Resolve local repo
    try:
        project_dir = _resolve_project_dir(project, args.repo_root, repo_map)
    except ValueError as exc:
        logger.error(f"Row {row_index}: cannot resolve project dir for '{project}': {exc}")
        return "error"

    if not os.path.isdir(project_dir):
        logger.error(f"Row {row_index}: project directory does not exist: {project_dir}")
        return "error"

    try:
        repo = git.Repo(project_dir)

        # target_release always comes from the project repo (backport commit lives there)
        target_release = _get_parent_hexsha(repo, backport_commit, args.backport_parent_index)
        project_url = _get_remote_url(repo)

        # new_patch_parent: try the project repo first; fall back to a separate mainline
        # repo when the original commit doesn't exist there (e.g. JDK cross-repo backports
        # where Original Commit is in the 'jdk' mainline repo, not in 'jdk17u-dev').
        cross_repo_config = None
        new_patch_for_config = original_commit
        try:
            new_patch_parent = _get_parent_hexsha(repo, original_commit, args.original_parent_index)
        except Exception:
            original_repo_name = getattr(args, "original_repo_name", "").strip()
            if not original_repo_name:
                raise
            logger.info(
                f"Row {row_index}: original commit {original_commit[:8]} not found in "
                f"'{project}' repo — looking up in '{original_repo_name}'"
            )
            original_repo_dir = _resolve_project_dir(original_repo_name, args.repo_root, repo_map)
            original_repo = git.Repo(original_repo_dir)
            # Pre-compute the real patch from the mainline repo
            original_patch_text = _get_patch_against_parent(
                original_repo, original_commit, args.original_parent_index
            )
            cross_repo_config = {"original_patch": original_patch_text}
            # new_patch and new_patch_parent must resolve in project_dir for pipeline
            # validation; use target_release as a stable anchor that always exists there.
            new_patch_parent = target_release
            new_patch_for_config = target_release

        patch_dataset_dir = os.path.expanduser(
            os.path.join(args.dataset_root, f"row-{row_index}", project)
        )
        os.makedirs(patch_dataset_dir, exist_ok=True)

        # Save ground-truth developer backport patch
        developer_backport_patch_path = os.path.join(patch_dataset_dir, "developer_backport.patch")
        try:
            developer_backport_patch = _get_patch_against_parent(
                repo, backport_commit, args.backport_parent_index
            )
            with open(developer_backport_patch_path, "w", encoding="utf-8") as f:
                f.write(developer_backport_patch)
            logger.info(f"Saved developer backport patch to {developer_backport_patch_path}")
        except Exception as exc:
            logger.warning(
                f"Row {row_index}: unable to export developer backport patch for {backport_commit}: {exc}"
            )

        tag = f"{args.tag_prefix}-{row_index}"

        generated_config = {
            "project": project,
            "type": patch_type,
            "project_url": project_url,
            "new_patch": new_patch_for_config,
            "new_patch_parent": new_patch_parent,
            "target_release": target_release,
            "error_message": "",
            "tag": tag,
            "openai_key": api_key,
            "project_dir": project_dir,
            "patch_dataset_dir": patch_dataset_dir,
            "llm_provider": llm_provider,
            "model": model,
            "openai_api_base": api_base,
            "use_azure": llm_provider == "azure",
            "azure_endpoint": "",
            "azure_deployment": "gpt-4",
            "azure_api_version": "2024-12-01-preview",
            "build_use_docker": args.build_use_docker,
            "build_docker_image": args.build_docker_image,
            "build_command": args.build_command,
            "backport_commit": backport_commit,
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as tmp:
            yaml.safe_dump(generated_config, tmp)
            config_path = tmp.name

        logger.info(
            f"Running row {row_index}: {project} {original_commit[:8]}"
            f" -> {row.get('Backport Version', '?')} (type={patch_type or 'unknown'})"
        )
        logger.info(f"Generated config at {config_path}")

        extra_validation_config = None
        if args.run_extra_validation:
            extra_validation_config = {
                "enabled": True,
                "row_index": row_index,
                "helpers_root": os.path.abspath(os.path.expanduser(args.helpers_root)) if args.helpers_root else "",
                "builder_image_tag": args.builder_image_tag,
                "build_timeout": args.build_timeout,
                "tests_timeout": args.tests_timeout,
            }

        java_preprocessing_config = None
        if args.filter_java_patch:
            java_preprocessing_config = {"filter_java_source": True}

        try:
            data = load_yml(config_path)
            run_pipeline(data, args.debug, extra_validation_config, java_preprocessing_config, cross_repo_config)
        finally:
            try:
                os.remove(config_path)
            except Exception:
                pass

        return "ok"

    except Exception as exc:  # pylint: disable=broad-except
        logger.error(f"Row {row_index}: unhandled error — {exc}", exc_info=args.debug)
        return "error"


def main():
    parser = argparse.ArgumentParser(
        description="Run PortGPT from a CSV dataset row index or range",
        usage=(
            "%(prog)s --csv path/to/java_backports.csv "
            "--start-index 0 --end-index 10 --repo-root /path/to/clones"
        ),
    )
    parser.add_argument("--csv", type=str, required=True, help="Path to dataset CSV")
    # Single-row shorthand (kept for backward compatibility)
    parser.add_argument("--index", type=int, default=None, help="0-based CSV row index (single-row shorthand)")
    # Batch range
    parser.add_argument("--start-index", type=int, default=0, help="First row to process (inclusive, default: 0)")
    parser.add_argument(
        "--end-index", type=int, default=-1,
        help="Last row to process (inclusive; -1 means last row, default: -1)",
    )
    # Type filter
    parser.add_argument(
        "--types", type=str, default="",
        help=(
            "Comma-separated list of patch types to process (case-insensitive). "
            "E.g. 'TYPE-I,TYPE-II'. Empty means all types."
        ),
    )
    parser.add_argument(
        "--repo-root", type=str, default="",
        help="Root directory containing project clones as subfolders",
    )
    parser.add_argument(
        "--original-repo-name", type=str, default="",
        help=(
            "Name of the mainline/source repo folder (under --repo-root or in --repo-map-file) "
            "to use when the Original Commit does not exist in the project repo. "
            "E.g. 'jdk' for JDK cross-repo backports where fixes land in the mainline 'jdk' "
            "repo before being cherry-picked into 'jdk17u-dev' etc."
        ),
    )
    parser.add_argument(
        "--repo-map-file", type=str, default="",
        help="YAML/JSON file mapping project name to local repo path",
    )
    parser.add_argument(
        "--dataset-root", type=str, default="../dataset_runs",
        help="Root folder for per-row runtime assets (build.sh/test.sh/poc.sh)",
    )
    parser.add_argument("--openai-key", type=str, default="", help="API key for OpenAI/Azure/OpenRouter")
    parser.add_argument(
        "--env-file", type=str, default="",
        help="Path to .env file (defaults to loading .env from current/parent directories)",
    )
    parser.add_argument(
        "--llm-provider", type=str, default="",
        help="LLM provider: openai | openrouter | azure",
    )
    parser.add_argument("--model", type=str, default="", help="Model name for the provider")
    parser.add_argument("--api-base", type=str, default="", help="OpenAI-compatible API base URL (auto-selected if omitted)")
    parser.add_argument("--tag-prefix", type=str, default="csv-row", help="Tag prefix used in output log filenames")
    parser.add_argument(
        "--original-parent-index", type=int, default=0,
        help="Which parent of the Original Commit to use as new_patch_parent (default: 0)",
    )
    parser.add_argument(
        "--backport-parent-index", type=int, default=0,
        help="Which parent of the Backport Commit to use as target_release (default: 0)",
    )
    parser.add_argument("--build-use-docker", action="store_true", help="Use Docker for build validation")
    parser.add_argument(
        "--build-docker-image", type=str, default="build-kernel-ubuntu-16.04",
        help="Docker image used when --build-use-docker is set",
    )
    parser.add_argument("--build-command", type=str, default="bash build.sh", help="Build command to run")
    parser.add_argument(
        "--run-extra-validation", action="store_true",
        help="Run extra Java validation using per-project helper scripts after default validation passes",
    )
    parser.add_argument(
        "--helpers-root", type=str, default="",
        help="Path to helpers directory containing per-project subdirectories (e.g. helpers/hbase/)",
    )
    parser.add_argument(
        "--builder-image-tag", type=str, default="",
        help="Docker image tag passed as BUILDER_IMAGE_TAG to helper scripts",
    )
    parser.add_argument(
        "--build-timeout", type=int, default=3600,
        help="Timeout in seconds for the build helper script (default: 3600)",
    )
    parser.add_argument(
        "--tests-timeout", type=int, default=3600,
        help="Timeout in seconds for the tests helper script (default: 3600)",
    )
    parser.add_argument(
        "--filter-java-patch", action="store_true",
        help="Filter the mainline patch to agent-eligible Java source files before passing to PortGPT "
             "(strips test files, non-Java files, and auto-generated Java files)",
    )
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    # Load .env variables so OPENROUTER_API_KEY/OPENAI_API_KEY work without shell export.
    if args.env_file:
        load_dotenv(dotenv_path=os.path.expanduser(args.env_file), override=False)
    else:
        load_dotenv(override=False)

    if args.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    # Resolve provider/model/key from CLI first, then .env.
    llm_provider = (
        args.llm_provider
        or os.getenv("PROVIDER", "")
        or os.getenv("LLM_PROVIDER", "")
        or "openrouter"
    )
    model = args.model or os.getenv("OPENAI_MODEL", "") or "openai/gpt-4o-mini"

    # Resolve API key
    api_key = (
        args.openai_key
        or os.getenv("OPENAI_KEY", "")
        or os.getenv("OPENROUTER_API_KEY", "")
        or os.getenv("OPENAI_API_KEY", "")
    )
    if not api_key:
        raise ValueError("Missing API key. Pass --openai-key or set OPENAI_KEY/OPENROUTER_API_KEY/OPENAI_API_KEY.")

    # Auto-select API base URL
    api_base = args.api_base or os.getenv("OPENAI_BASE_URL", "") or os.getenv("OPENAI_API_BASE", "")
    if not api_base:
        api_base = "https://openrouter.ai/api/v1" if llm_provider == "openrouter" else "https://api.openai.com/v1"

    # Load optional repo map
    repo_map = _load_repo_map(args.repo_map_file) if args.repo_map_file else {}

    # Read CSV
    with open(args.csv, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Resolve row range — --index takes priority as a single-row shorthand
    if args.index is not None:
        start = args.index
        end = args.index
    else:
        start = args.start_index
        end = args.end_index if args.end_index >= 0 else len(rows) - 1

    if start < 0 or start >= len(rows):
        raise IndexError(f"start-index {start} out of range for {len(rows)} rows")
    end = min(end, len(rows) - 1)

    # Resolve type filter (normalise to upper-case, strip whitespace)
    type_filter = set()
    if args.types.strip():
        type_filter = {t.strip().upper() for t in args.types.split(",") if t.strip()}

    total = end - start + 1
    logger.info(
        f"Batch: rows {start}–{end} ({total} rows)"
        + (f", types filter: {sorted(type_filter)}" if type_filter else ", all types")
    )

    counts = {"ok": 0, "skipped": 0, "error": 0}

    for i in range(start, end + 1):
        row = rows[i]
        patch_type = row.get("Type", "").strip().upper()

        if type_filter and patch_type not in type_filter:
            logger.info(f"Row {i}: skipping type '{patch_type}' (not in filter)")
            counts["skipped"] += 1
            continue

        result = _process_row(row, i, args, api_key, api_base, llm_provider, model, repo_map)
        counts[result] += 1

    logger.info(
        f"Batch complete — processed: {counts['ok']}, "
        f"skipped: {counts['skipped']}, errors: {counts['error']} "
        f"(out of {total} rows in range)"
    )


if __name__ == "__main__":
    main()

