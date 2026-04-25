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


def main():
    parser = argparse.ArgumentParser(
        description="Run PortGPT from a CSV dataset row index",
        usage="%(prog)s --csv path/to/java_backports.csv --index 0 --repo-root /path/to/clones",
    )
    parser.add_argument("--csv", type=str, required=True, help="Path to dataset CSV")
    parser.add_argument("--index", type=int, default=0, help="0-based CSV row index")
    parser.add_argument(
        "--repo-root", type=str, default="",
        help="Root directory containing project clones as subfolders",
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

    # Read CSV row
    with open(args.csv, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if args.index >= len(rows):
        raise IndexError(f"index {args.index} out of range for {len(rows)} rows")

    row = rows[args.index]
    project = row["Project"]
    original_commit = row["Original Commit"].strip()
    backport_commit = row["Backport Commit"].strip()  # used only to derive target_release

    # Resolve local repo
    project_dir = _resolve_project_dir(project, args.repo_root, repo_map)
    if not os.path.isdir(project_dir):
        raise FileNotFoundError(f"Project directory does not exist: {project_dir}")

    repo = git.Repo(project_dir)

    # Derive the three key commits
    #   new_patch        = original_commit  (the fix on the main branch)
    #   new_patch_parent = parent of original_commit  (pre-fix state on main branch)
    #   target_release   = parent of backport_commit  (pre-backport state on target branch)
    new_patch_parent = _get_parent_hexsha(repo, original_commit, args.original_parent_index)
    target_release = _get_parent_hexsha(repo, backport_commit, args.backport_parent_index)

    project_url = _get_remote_url(repo)

    # Create an empty patch_dataset_dir.
    # Without build.sh / test.sh / poc.sh the validation chain auto-passes,
    # which is the correct behaviour for Java repositories.
    patch_dataset_dir = os.path.expanduser(
        os.path.join(args.dataset_root, f"row-{args.index}", project)
    )
    os.makedirs(patch_dataset_dir, exist_ok=True)

    tag = f"{args.tag_prefix}-{args.index}"

    generated_config = {
        "project": project,
        "project_url": project_url,
        "new_patch": original_commit,
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
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as tmp:
        yaml.safe_dump(generated_config, tmp)
        config_path = tmp.name

    logger.info(
        f"Running row {args.index}: {project} {original_commit[:8]}"
        f" -> {row.get('Backport Version', '?')}"
    )
    logger.info(f"Generated config at {config_path}")

    try:
        data = load_yml(config_path)
        run_pipeline(data, args.debug)
    finally:
        try:
            os.remove(config_path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
