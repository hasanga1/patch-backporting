import os
import re
import shutil

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.callbacks import FileCallbackHandler
from langchain_openai import ChatOpenAI, AzureChatOpenAI

from agent.prompt import (
    SYSTEM_PROMPT,
    USER_PROMPT_HUNK,
)
from tools.logger import logger
from tools.project import Project
from tools.utils import split_patch


def initial_agent(project: Project, data, debug_mode: bool):
    # Determine provider
    provider = getattr(data, 'provider', 'azure' if data.use_azure else 'openai')

    if provider == "azure" or data.use_azure:
        azure_endpoint = data.azure_endpoint
        azure_deployment = data.azure_deployment
        azure_api_version = data.azure_api_version
        api_key = data.openai_key

        logger.info(f"Using Azure OpenAI: {azure_endpoint} (deployment: {azure_deployment})")

        llm = AzureChatOpenAI(
            temperature=1.0,  # Set to 1.0 for GPT-5 model; can be changed if using other models
            azure_deployment=azure_deployment,
            api_key=api_key,
            azure_endpoint=azure_endpoint,
            api_version=azure_api_version,
            verbose=True,
        )
    elif provider == "openrouter":
        # OpenRouter configuration
        api_key = data.openai_key
        model = getattr(data, 'openai_model', 'openai/gpt-4.1-mini')
        base_url = getattr(data, 'openai_base_url', 'https://openrouter.ai/api/v1')
        
        logger.info(f"Using OpenRouter API: {base_url} (model: {model})")
        
        # OpenRouter requires specific headers
        default_headers = {
            "HTTP-Referer": "https://github.com/patch-backporting",
            "X-Title": "Patch Backporting Agent",
        }
        
        llm = ChatOpenAI(
            temperature=0.5,
            model=model,
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
            verbose=True,
        )
    else:
        # Regular OpenAI configuration
        api_key = data.openai_key
        model = getattr(data, 'openai_model', 'gpt-4.1-mini')
        base_url = getattr(data, 'openai_base_url', 'https://api.openai.com/v1')
        
        logger.info(f"Using OpenAI API (model: {model})")
        
        llm = ChatOpenAI(
            temperature=0.5,
            model=model,
            api_key=api_key,
            base_url=base_url,
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

    patch = project._get_patch(data.new_patch)
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
                return False, None

    project.all_hunks_applied_succeeded = True
    logger.info(f"Aplly all hunks in the patch      PASS")
    project.now_hunk = "completed"
    complete_patch = "\n".join(project.succeeded_patches)
    project.repo.git.clean("-fdx")
    for file in os.listdir(data.patch_dataset_dir):
        if os.path.exists(f"{data.project_dir}{file}"):
            os.remove(f"{data.project_dir}{file}")
        shutil.copy2(f"{data.patch_dataset_dir}{file}", f"{data.project_dir}{file}")

    validation = project._validate_with_retrofit(data.target_release, complete_patch)
    logger.info(
        f"Retrofit validation result       status={validation['status']} compile_status={validation['compile_status']} test_status={validation['test_status']}"
    )

    if validation["status"] == "PASS":
        logger.info(
            f"Successfully backport the patch to the target release {data.target_release}"
        )
        for patch in project.succeeded_patches:
            logger.info(patch)
        return True, complete_patch

    if validation.get("error_logs"):
        logger.error(f"Retrofit validation failed. Logs:\n{validation['error_logs']}")
    logger.error(
        f"Failed backport the patch to the target release {data.target_release}"
    )
    return False, complete_patch
