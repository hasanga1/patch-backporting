import os
import re
import shutil

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.callbacks import FileCallbackHandler
from langchain_openai import ChatOpenAI, AzureChatOpenAI

from agent.prompt import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_PTACH,
    USER_PROMPT_HUNK,
    USER_PROMPT_PATCH,
)
from tools.logger import logger
from tools.project import Project
from tools.utils import split_patch


def _write_patch_files(output_dirs, filename: str, content: str):
    for out_dir in output_dirs:
        if not out_dir:
            continue
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
            f.write(content)


def initial_agent(project: Project, data, debug_mode: bool):
    llm_provider = getattr(data, "llm_provider", "openai")

    if llm_provider == "azure":
        logger.info(f"Using Azure OpenAI: {data.azure_endpoint} (deployment: {data.azure_deployment})")
        llm = AzureChatOpenAI(
            temperature=1.0,
            azure_deployment=data.azure_deployment,
            api_key=data.openai_key,
            azure_endpoint=data.azure_endpoint,
            api_version=data.azure_api_version,
            verbose=True,
        )
    else:
        model = getattr(data, "model", "gpt-4-turbo")
        api_base = getattr(data, "openai_api_base", "https://api.openai.com/v1")
        logger.info(f"Using {llm_provider} API: {api_base} (model: {model})")
        llm = ChatOpenAI(
            temperature=0.5,
            model=model,
            api_key=data.openai_key,
            openai_api_base=api_base,
            verbose=True,
        )

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            ("user", USER_PROMPT_HUNK),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )
    viewcode, locate_symbol, validate, git_history, git_show = project.get_tools()
    tools = [viewcode, locate_symbol, validate, git_history, git_show]
    agent = create_tool_calling_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(
        agent=agent, tools=tools, verbose=debug_mode, max_iterations=30
    )
    return agent_executor, llm


def do_backport(
    agent_executor: AgentExecutor, project: Project, data, llm: ChatOpenAI, logfile: str
):
    log_handler = FileCallbackHandler(logfile)
    artifact_dirs = [os.path.dirname(logfile), data.patch_dataset_dir]
    project.patch_output_dirs = artifact_dirs

    patch = project._get_patch(data.new_patch)
    _write_patch_files(artifact_dirs, "original.patch", patch)
    pps = split_patch(patch, True)
    for idx, pp in enumerate(pps):
        project.round_succeeded = False
        project.context_mismatch_times = 0
        ret = project._apply_hunk(data.target_release, pp, False)
        if project.round_succeeded:
            logger.debug(f"Hunk {idx} can be applied without any conflicts")
            continue
        else:
            block_list = re.findall(r"older version.\n(.*?)\nBesides,", ret, re.DOTALL)
            similar_block = "\n".join(block_list)
            logger.debug(f"Hunk {idx} can not be applied, using LLM to generate a fix")
            project.now_hunk = pp
            project.now_hunk_num = idx
            agent_executor.invoke(
                {
                    "project_url": data.project_url,
                    "new_patch_parent": data.new_patch_parent,
                    "new_patch": pp,
                    "target_release": data.target_release,
                    "similar_block": similar_block,
                },
                {"callbacks": [log_handler]},
            )
            if not project.round_succeeded:
                logger.debug(
                    f"Failed to backport the hunk {idx} \n----------------------------------\n{pp}\n----------------------------------\n"
                )
                logger.error(f"Reach max_iterations for hunk {idx}")
                return

    project.all_hunks_applied_succeeded = True
    logger.info(f"Aplly all hunks in the patch      PASS")
    project.now_hunk = "completed"
    complete_patch = "\n".join(project.succeeded_patches)
    project.repo.git.clean("-fdx")
    for file in os.listdir(data.patch_dataset_dir):
        if os.path.exists(f"{data.project_dir}{file}"):
            os.remove(f"{data.project_dir}{file}")
        shutil.copy2(f"{data.patch_dataset_dir}{file}", f"{data.project_dir}{file}")
    project.context_mismatch_times = 0
    validate_ret = project._validate(data.target_release, complete_patch)
    if project.poc_succeeded:
        applied_patch = project.succeeded_patches[-1] if project.succeeded_patches else complete_patch
        _write_patch_files(artifact_dirs, "backport_applied.patch", applied_patch)
        logger.info(
            f"Successfully backport the patch to the target release {data.target_release}"
        )
        for patch in project.succeeded_patches:
            logger.info(patch)
        return

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT_PTACH),
            ("user", USER_PROMPT_PATCH),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )
    # XXX maybe refactor initial_agent function to cover
    viewcode, locate_symbol, validate, _, _ = project.get_tools()
    tools = [viewcode, locate_symbol, validate]
    agent = create_tool_calling_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(
        agent=agent, tools=tools, verbose=True, max_iterations=20
    )
    agent_executor.invoke(
        {
            "project_url": data.project_url,
            "new_patch_parent": data.new_patch_parent,
            "target_release": data.target_release,
            "new_patch": patch,
            "complete_patch": complete_patch,
            "compile_ret": validate_ret,
        },
        {"callbacks": [log_handler]},
    )
    if project.poc_succeeded:
        applied_patch = project.succeeded_patches[-1] if project.succeeded_patches else complete_patch
        _write_patch_files(artifact_dirs, "backport_applied.patch", applied_patch)
        logger.info(
            f"Successfully backport the patch to the target release {data.target_release}"
        )
        for patch in project.succeeded_patches:
            logger.info(patch)
    else:
        logger.error(
            f"Failed backport the patch to the target release {data.target_release}"
        )
