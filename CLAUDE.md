# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PortGPT is a research system for automated security patch backporting using LLMs (published at IEEE S&P 2026). It takes a CVE fix from a newer version of a project and automatically adapts it to an older, vulnerable version, handling context mismatches, file renames, and code structure changes via an agentic LLM loop.

The tag `sp26-submission` marks the exact code state used for the paper submission.

## Setup

```shell
curl -sSL https://pdm-project.org/install-pdm.py | python3 -
pdm install
source .venv/bin/activate
```

Requires Python >=3.10. External dependency: `ctags` must be installed on the system (used for symbol indexing via `_prepare` in `src/tools/project.py`).

## Running

All commands run from `src/`.

**Original CVE YAML-based entry point:**
```shell
cd src
python backporting.py --config example.yml        # normal run
python backporting.py --config example.yml --debug # verbose LLM output
```

**Java dataset CSV entry point:**
```shell
cd src
# All repos live under one root directory named after the project
python run_from_csv.py --csv ../my_java_dataset/java_backports.csv --index 0 \
    --repo-root /path/to/clones \
    --openai-key sk-... \
    --llm-provider openrouter \
    --model openai/gpt-4o-mini

# Or map individual projects to arbitrary paths
python run_from_csv.py --csv ../my_java_dataset/java_backports.csv --index 0 \
    --repo-map-file repos.yml \   # YAML/JSON: {projectname: /abs/path}
    --openai-key sk-...

# API key can also be set via env vars: OPENROUTER_API_KEY or OPENAI_API_KEY
```

Key CSV flags:
- `--index` — 0-based row index in the CSV (default: 0)
- `--llm-provider` — `openrouter` (default), `openai`, or `azure`
- `--model` — model name for the chosen provider (default: `openai/gpt-4o-mini`)
- `--backport-parent-index` — which parent of Backport Commit to use as `target_release` (default: 0)
- `--dataset-root` — where per-row runtime assets are stored (default: `../dataset_runs`)

To manually test a patch or hunk against a target release (without running the full LLM loop):

```shell
cd test
python test_patch.py --config ../src/example.yml   # validate a pre-written patch
python test_hunk.py  --config ../src/example.yml   # test applying a single hunk
```

In both test files, the patch/hunk to test is hardcoded at the bottom of the file — edit in place before running.

## Linting

```shell
pylint src/ --fail-under=7.0
```

CI runs pylint on every pull request (`.github/workflows/pylint.yml`).

## Architecture

### Data flow

1. `src/backporting.py` — entry point. Loads YAML config into a `SimpleNamespace`, validates commit IDs, creates a `Project`, then calls `initial_agent` → `do_backport`.
2. `src/agent/invoke_llm.py` — builds the LangChain `AgentExecutor` with the five LangChain tools, runs two phases:
   - **Hunk phase**: processes each hunk individually. Hunks that apply cleanly are skipped; failing hunks enter the LLM loop (max 30 iterations).
   - **Patch phase**: once all hunks succeed, the joined patch is compiled/tested/PoC'd. If validation fails, a second agent (max 20 iterations, 3 tools) revises the whole patch.
3. `src/tools/project.py` — `Project` class. Wraps GitPython and exposes the five LangChain tools (`viewcode`, `locate_symbol`, `validate`, `git_history`, `git_show`). Also handles patch application, compilation (via Docker container `build-kernel-ubuntu-16.04`), testcase execution, and PoC execution.
4. `src/tools/utils.py` — patch manipulation: `split_patch`, `revise_patch` (auto-fixes line numbers, indentation, context mismatches), `find_most_similar_block` (Levenshtein-based), `extract_context`.
5. `src/agent/prompt.py` — all LLM system/user prompts. Two system prompts (`SYSTEM_PROMPT` for hunk phase, `SYSTEM_PROMPT_PTACH` for patch phase) and two user prompts.

### LangChain agent tools (exposed to the LLM)

| Tool | Purpose |
|---|---|
| `viewcode(ref, path, startline, endline)` | Read file lines at a git ref |
| `locate_symbol(ref, symbol)` | Find function/variable via ctags |
| `git_history()` | `git log -L` for the current hunk's lines |
| `git_show()` | Show the last commit from `git_history` with an auto-generated abstract |
| `validate(ref, patch)` | Apply patch, compile, run testcase, run PoC; returns structured feedback |

### Config file (`example.yml` as template)

Key fields: `project_dir` (local git repo of the target project), `patch_dataset_dir` (directory with `build.sh`, `test.sh`, `poc.sh`), `new_patch` + `new_patch_parent` (commits in fixed version), `target_release` (vulnerable commit to patch), `openai_key`, optional Azure fields (`use_azure`, `azure_endpoint`, `azure_deployment`, `azure_api_version`).

### Patch dataset directory

Each entry in `patch_dataset_dir` must contain shell scripts used by the validation chain:
- `build.sh` — builds the project (run inside Docker container `build-kernel-ubuntu-16.04`)
- `test.sh` — runs the test suite
- `poc.sh` — runs the PoC; success means the bug is no longer triggered

If any script is absent, that validation step is considered passed automatically.

### Validation chain (in `Project._validate`)

`_apply_hunk` → `_compile_patch` → `_run_testcase` → `_run_poc`. Each stage provides structured feedback to the LLM when it fails. `revise_patch` in `utils.py` auto-corrects patch line numbers and indentation before every `git apply`.

### Result evaluation

Results are judged manually against ground truth: (1) logical block match, (2) location equivalence, (3) semantic equivalence. There is no automated correctness metric.
