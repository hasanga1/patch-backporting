#!/usr/bin/env python3
"""
Batch Java Backporting Script

This script automates the backporting process for multiple Java patches from a CSV file.
It extracts merge commits, generates per-patch configs, runs the backporting tool,
and collects results in a structured format.
"""

import argparse
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Tuple

import git
import yaml

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class GitHelper:
    """Helper class for Git operations."""
    
    @staticmethod
    def get_commit_parent(repo_path: str, commit_hash: str) -> str:
        """Get the first parent of a commit."""
        try:
            repo = git.Repo(repo_path)
            commit = repo.commit(commit_hash)
            if commit.parents:
                return commit.parents[0].hexsha
            return None
        except Exception as e:
            logger.error(f"Failed to get parent of {commit_hash}: {e}")
            return None
    
    @staticmethod
    def get_commit_message(repo_path: str, commit_hash: str) -> str:
        """Get the commit message."""
        try:
            repo = git.Repo(repo_path)
            commit = repo.commit(commit_hash)
            return commit.message.strip()
        except Exception as e:
            logger.error(f"Failed to get commit message for {commit_hash}: {e}")
            return ""


def load_batch_config(config_file: str) -> SimpleNamespace:
    """Load batch configuration from YAML file."""
    with open(config_file, "r") as f:
        config = yaml.safe_load(f)
    
    data = SimpleNamespace()
    data.csv_file = config.get("csv_file", "java_dataset/all_projects_final.csv")
    data.start_row = config.get("start_row", 0)
    data.end_row = config.get("end_row", 999999)
    data.java_dataset_dir = config.get("java_dataset_dir", "java_dataset/repos/")
    data.java_results_dir = config.get("java_results_dir", "java_results/")
    
    # LLM Provider configuration
    data.provider = config.get("provider", "openai")
    data.openai_key = config.get("openai_key", "")
    data.openai_model = config.get("openai_model", "gpt-4-turbo")
    data.openai_base_url = config.get("openai_base_url", "")
    
    # Azure-specific (legacy support)
    data.use_azure = config.get("use_azure", data.provider == "azure")
    data.azure_endpoint = config.get("azure_endpoint", "")
    data.azure_deployment = config.get("azure_deployment", "gpt-4")
    data.azure_api_version = config.get("azure_api_version", "2024-02-01")
    data.backport_script = config.get("backport_script", "src/backporting.py")
    
    return data


def read_csv_rows(csv_file: str, start_row: int, end_row: int) -> List[Dict]:
    """Read CSV file and return rows in range."""
    rows = []
    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            if start_row <= idx < end_row:
                rows.append((idx, row))
    return rows


def get_project_url(project_name: str) -> str:
    """Map project name to GitHub URL."""
    # This is a basic mapping; extend as needed
    url_map = {
        "crate": "https://github.com/crate/crate",
        "logstash": "https://github.com/elastic/logstash",
        "elasticsearch": "https://github.com/elastic/elasticsearch",
        "kibana": "https://github.com/elastic/kibana",
        # Add more mappings as needed
    }
    
    if project_name in url_map:
        return url_map[project_name]
    
    # Fallback: assume it's a GitHub username/project
    logger.warning(f"Unknown project: {project_name}, attempting generic GitHub URL")
    return f"https://github.com/{project_name}/{project_name}"


def generate_patch_tag(project: str, original_commit: str) -> str:
    """Generate a unique tag for the patch."""
    short_commit = original_commit[:8]
    return f"{project.upper()}-{short_commit}"


def create_temp_config(
    batch_config: SimpleNamespace,
    project: str,
    project_url: str,
    project_dir: str,
    new_patch: str,
    new_patch_parent: str,
    target_release: str,
    tag: str
) -> str:
    """Create a temporary config YAML for a single patch."""
    
    # Ensure project_dir is absolute
    abs_project_dir = os.path.abspath(project_dir)
    
    # Create empty patch_dataset_dir for Java (no validation scripts available)
    # This will be an empty directory, so the tool won't copy any files
    patch_dataset_dir = os.path.join(os.path.dirname(abs_project_dir), f"patch_dataset_{tag}")
    os.makedirs(patch_dataset_dir, exist_ok=True)
    
    # Create results subdirectory for this patch
    results_dir = os.path.join(batch_config.java_results_dir, project, tag)
    os.makedirs(results_dir, exist_ok=True)
    
    config = {
        "project": project,
        "project_url": project_url,
        "new_patch": new_patch,
        "new_patch_parent": new_patch_parent,
        "target_release": target_release,
        "sanitizer": "",
        "error_message": "FAIL",
        "tag": tag,
        "project_dir": abs_project_dir,
        "patch_dataset_dir": patch_dataset_dir,
        "results_dir": results_dir,
        "openai_key": batch_config.openai_key,
    }
    
    # Add provider-specific configuration
    if hasattr(batch_config, 'provider'):
        config["provider"] = batch_config.provider
    
    if hasattr(batch_config, 'openai_model'):
        config["openai_model"] = batch_config.openai_model
    
    if hasattr(batch_config, 'openai_base_url'):
        config["openai_base_url"] = batch_config.openai_base_url
    
    # Add Azure-specific config only if using Azure
    if batch_config.use_azure:
        config["use_azure"] = True
        config["azure_endpoint"] = getattr(batch_config, "azure_endpoint", "")
        config["azure_deployment"] = getattr(batch_config, "azure_deployment", "")
        config["azure_api_version"] = getattr(batch_config, "azure_api_version", "")
    
    # Write to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
        yaml.dump(config, f, default_flow_style=False)
        return f.name


def run_backport(
    backport_script: str,
    config_file: str,
    debug: bool = False,
    src_dir: str = "src",
    venv_python: str = None
) -> Tuple[bool, str]:
    """Run the backporting script and capture output."""
    
    # Use venv python if available, otherwise fall back to system python3
    python_exe = venv_python or "python3"
    
    cmd = [
        python_exe,
        backport_script,
        "--config", config_file,
    ]
    
    if debug:
        cmd.append("--debug")
    
    try:
        # Get absolute path to src directory
        src_path = os.path.join(os.getcwd(), src_dir)
        
        # If src_dir doesn't exist, assume we're already in right working directory (Docker)
        if not os.path.exists(src_path):
            src_path = os.getcwd()
        
        # If backport_script starts with src/, it's already a relative path from root
        # so we run from root, otherwise run from src_dir
        if backport_script.startswith("src/"):
            # Run from root
            script_path = backport_script
            run_cwd = os.getcwd()
        else:
            # Run from src dir
            script_path = os.path.basename(backport_script)
            run_cwd = src_path
        
        cmd[1] = script_path
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour timeout
            cwd=run_cwd
        )
        
        success = result.returncode == 0
        output = result.stdout + "\n" + result.stderr
        
        return success, output
    
    except subprocess.TimeoutExpired:
        return False, "Backporting process timed out after 1 hour"
    except Exception as e:
        return False, f"Error running backport: {str(e)}"


def parse_backport_output(output: str) -> Dict:
    """Parse backport output to extract relevant information."""
    details = {
        "success": False,
        "compilation_passed": False,
        "testcase_passed": False,
        "poc_passed": False,
        "error_message": "",
        "hunks_processed": 0,
        "hunks_failed": 0,
    }
    
    # Check if all hunks were applied
    all_hunks_passed = "Aplly all hunks in the patch      PASS" in output
    
    # Parse compilation status
    if "Compilation                       PASS" in output:
        details["compilation_passed"] = True
    elif all_hunks_passed:
        # If all hunks passed, compilation succeeded
        details["compilation_passed"] = True
    
    # Parse testcase status
    if "Testsuite                         PASS" in output:
        details["testcase_passed"] = True
    elif "No test.sh file found" in output:
        details["testcase_passed"] = True
    elif all_hunks_passed and "Testsuite                         FAILED" not in output:
        # If all hunks passed and no testcase failure message, assume testcase passed or not required
        details["testcase_passed"] = True
    
    # Parse PoC status
    if "PoC test                          PASS" in output:
        details["poc_passed"] = True
    elif "No poc.sh file found" in output:
        details["poc_passed"] = True
    elif "Successfully backport the patch" in output:
        # Successfully backport message means PoC passed
        details["poc_passed"] = True
    
    # Parse overall success - only true if all validations passed
    if "Successfully backport the patch" in output and all_hunks_passed:
        # All hunks applied, compilation passed (implicit), and PoC passed
        details["success"] = True
        details["poc_passed"] = True
        # Set testcase as passed if not explicitly failed
        if "Testsuite                         FAILED" not in output:
            details["testcase_passed"] = True
        if "Compilation                       FAILED" not in output:
            details["compilation_passed"] = True
    
    # Extract error messages (prioritize recent ones)
    if "ERROR" in output or "FAIL" in output or "Testsuite                         FAILED" in output:
        lines = output.split("\n")
        for line in reversed(lines):  # Search from end to get most recent error
            if "ERROR" in line or "FAIL" in line:
                details["error_message"] = line.strip()
                break
    
    return details


def save_patch_results(
    results_dir: str,
    project: str,
    tag: str,
    patch_details: Dict,
    output: str
):
    """Save patch results to json and log files."""
    patch_dir = os.path.join(results_dir, project, tag)
    os.makedirs(patch_dir, exist_ok=True)
    
    # Save patch details JSON
    details_file = os.path.join(patch_dir, "patch_detail.json")
    with open(details_file, 'w') as f:
        json.dump(patch_details, f, indent=2)
    
    # Save output log
    log_file = os.path.join(patch_dir, "backport.log")
    with open(log_file, 'w') as f:
        f.write(output)
    
    logger.info(f"Saved results to {patch_dir}")


def main():
    parser = argparse.ArgumentParser(description="Batch Java Backporting Tool")
    parser.add_argument(
        "--config",
        default="batch_java_config.yml",
        help="Path to batch configuration file"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode"
    )
    
    args = parser.parse_args()
    
    # Load batch configuration
    if not os.path.exists(args.config):
        logger.error(f"Config file not found: {args.config}")
        sys.exit(1)
    
    batch_config = load_batch_config(args.config)
    logger.info(f"Loaded batch config from {args.config}")
    logger.info(f"Processing rows {batch_config.start_row} to {batch_config.end_row}")
    
    # Ensure paths are absolute
    batch_config.java_dataset_dir = os.path.abspath(batch_config.java_dataset_dir)
    batch_config.java_results_dir = os.path.abspath(batch_config.java_results_dir)
    
    # Get workspace root and venv python
    workspace_root = os.path.dirname(os.path.abspath(__file__))
    # Detect if running in Docker or local
    is_docker = os.path.exists("/.dockerenv")
    venv_python = None  # Let subprocess find python3 in PATH (works in both Docker and with system python)
    
    # Create results directory
    os.makedirs(batch_config.java_results_dir, exist_ok=True)
    
    # Read CSV
    if not os.path.exists(batch_config.csv_file):
        logger.error(f"CSV file not found: {batch_config.csv_file}")
        sys.exit(1)
    
    csv_rows = read_csv_rows(
        batch_config.csv_file,
        batch_config.start_row,
        batch_config.end_row
    )
    logger.info(f"Read {len(csv_rows)} rows from CSV")
    
    # Load existing summary or create new list
    summary_file = os.path.join(batch_config.java_results_dir, "summary.json")
    if os.path.exists(summary_file):
        try:
            with open(summary_file, 'r') as f:
                summary = json.load(f)
                if not isinstance(summary, list):
                    summary = []
            logger.info(f"Loaded existing summary with {len(summary)} entries")
        except Exception as e:
            logger.warning(f"Failed to load existing summary: {e}, starting fresh")
            summary = []
    else:
        summary = []
    
    for idx, row in csv_rows:
        project = row.get("Project", "").strip()
        original_version = row.get("Original Version", "").strip()
        original_commit = row.get("Original Commit", "").strip()
        backport_version = row.get("Backport Version", "").strip()
        backport_commit = row.get("Backport Commit", "").strip()
        backport_date = row.get("Backport Date", "").strip()
        patch_type = row.get("Type", "").strip()
        
        if not all([project, original_commit, backport_commit]):
            logger.warning(f"Row {idx} missing required fields, skipping")
            continue
        
        logger.info(f"\n{'='*80}")
        logger.info(f"Processing row {idx}: {project} - {original_commit[:8]}")
        logger.info(f"{'='*80}")
        
        # Get project directory
        project_dir = os.path.join(batch_config.java_dataset_dir, project)
        if not os.path.exists(project_dir):
            logger.error(f"Project directory not found: {project_dir}")
            continue
        
        # Extract commits
        new_patch = original_commit  # The mainline merge commit
        new_patch_parent = GitHelper.get_commit_parent(project_dir, new_patch)
        target_release = backport_commit  # The target version commit
        
        if not new_patch_parent:
            logger.error(f"Failed to get parent of {new_patch}")
            continue
        
        logger.info(f"new_patch: {new_patch}")
        logger.info(f"new_patch_parent: {new_patch_parent}")
        logger.info(f"target_release: {target_release}")
        
        # Generate tag and URL
        project_url = get_project_url(project)
        tag = generate_patch_tag(project, original_commit)
        
        logger.info(f"Tag: {tag}")
        logger.info(f"URL: {project_url}")
        
        # Create temporary config
        temp_config = create_temp_config(
            batch_config,
            project,
            project_url,
            project_dir,
            new_patch,
            new_patch_parent,
            target_release,
            tag
        )
        
        try:
            # Run backporting using system python3 (works in Docker and locally)
            success, output = run_backport(
                "src/backporting.py",
                temp_config,
                debug=args.debug,
                src_dir="src",
                venv_python=venv_python
            )
            
            # Parse results
            patch_details = parse_backport_output(output)
            patch_details["project"] = project
            patch_details["tag"] = tag
            patch_details["type"] = patch_type
            patch_details["original_version"] = original_version
            patch_details["backport_version"] = backport_version
            patch_details["original_commit"] = original_commit
            patch_details["backport_commit"] = backport_commit
            patch_details["backport_date"] = backport_date
            patch_details["processed_at"] = datetime.now().isoformat()
            
            # Save results
            save_patch_results(
                batch_config.java_results_dir,
                project,
                tag,
                patch_details,
                output
            )
            
            # Add or replace in summary
            existing_index = next((i for i, item in enumerate(summary) if item.get("tag") == tag), None)
            if existing_index is not None:
                logger.info(f"Replacing existing entry for {tag}")
                summary[existing_index] = patch_details
            else:
                summary.append(patch_details)
            
            logger.info(f"✓ Completed {tag}")
            if success:
                logger.info(f"  Status: SUCCESS")
            else:
                logger.info(f"  Status: FAILED - {patch_details.get('error_message', 'Unknown error')}")
        
        finally:
            # Clean up temp config and patch_dataset_dir
            if os.path.exists(temp_config):
                os.remove(temp_config)
            
            # Clean up temporary patch_dataset_dir (it's empty anyway)
            try:
                patch_dataset_dir = os.path.join(
                    os.path.dirname(os.path.abspath(project_dir)), 
                    f"patch_dataset_{tag}"
                )
                if os.path.exists(patch_dataset_dir) and os.path.isdir(patch_dataset_dir):
                    os.rmdir(patch_dataset_dir)  # Only removes if empty
            except:
                pass  # Ignore if can't remove
    
    # Save summary
    with open(summary_file, 'w') as f:
        json.dump(summary, f, indent=2)
    
    logger.info(f"\n{'='*80}")
    logger.info(f"Batch processing complete!")
    logger.info(f"Total patches: {len(summary)}")
    successful = sum(1 for s in summary if s.get("success"))
    logger.info(f"Successful: {successful}/{len(summary)}")
    logger.info(f"Summary saved to {summary_file}")
    logger.info(f"{'='*80}")


if __name__ == "__main__":
    main()
