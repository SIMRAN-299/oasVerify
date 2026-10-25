"""Google ADK (Agent Development Kit) Workflow Definition.

Defines the graph execution pipeline with function nodes, TestGeneratorAgent,
and deterministic quality gates for OpenAPI integration test generation.
"""

import logging
from typing import Any

from manager.agents.test_generator_agent import TestGeneratorAgent
from manager.core.github_deployer import GitHubDeployer
from manager.core.schemas import TestSuite
from manager.core.suite_scaffolder import TestSuiteScaffolder
from manager.models import OASMetadata
from manager.tools.parsing_tools import extract_oas_metadata, parse_and_resolve_oas
from manager.tools.review_tools import review_test_file

logger = logging.getLogger(__name__)


def parse_oas_node(oas_path: str) -> dict[str, Any]:
    """Node 1: Resolves OpenAPI Spec and extracts strongly-typed metadata."""
    logger.info("Executing parse_oas_node for: %s", oas_path)
    spec = parse_and_resolve_oas(oas_path)
    metadata: OASMetadata = extract_oas_metadata(spec)
    return metadata.model_dump()


def generate_scenarios_node(oas_metadata_dict: dict[str, Any]) -> dict[str, Any]:
    """Node 2: Synthesizes modular integration test scenarios via TestGeneratorAgent."""
    logger.info("Executing generate_scenarios_node...")
    metadata = OASMetadata(**oas_metadata_dict)
    generator = TestGeneratorAgent(session_id="adk_session")
    suite: TestSuite = generator.execute(metadata)
    return suite.model_dump()


def scaffold_tests_node(test_suite_dict: dict[str, Any]) -> str:
    """Node 3: Writes executable PyTest code and suite.json from the structured TestSuite."""
    logger.info("Executing scaffold_tests_node...")
    suite = TestSuite(**test_suite_dict)
    scaffolder = TestSuiteScaffolder(session_id="adk_session")
    test_files = scaffolder.execute(suite)
    return test_files[0] if test_files else ""


def review_tests_node(test_file_path: str) -> dict[str, Any]:
    """Node 4: Evaluates generated test file with black, flake8, and pytest discovery."""
    logger.info("Executing review_tests_node for: %s", test_file_path)
    review_res = review_test_file(test_file_path)
    route = "deploy" if review_res.passed else "repair"
    return {
        "file_path": test_file_path,
        "review_result": review_res.model_dump(),
        "route": route,
    }


def deploy_tests_node(review_output: dict[str, Any]) -> str:
    """Node 5: Commits test files and opens a Pull Request on GitHub."""
    logger.info("Executing deploy_tests_node...")
    test_file_path = review_output.get("file_path", "")
    deployer = GitHubDeployer(session_id="adk_session")
    return deployer.execute([test_file_path])


def create_adk_workflow() -> Any:
    """Constructs the Google ADK Workflow graph."""
    try:
        from google.adk import Workflow

        workflow = Workflow(
            name="oas_test_generator_workflow",
            edges=[
                (
                    "START",
                    parse_oas_node,
                    generate_scenarios_node,
                    scaffold_tests_node,
                    review_tests_node,
                ),
                (
                    review_tests_node,
                    {
                        "repair": scaffold_tests_node,
                        "deploy": deploy_tests_node,
                    },
                ),
            ],
        )
        return workflow
    except ImportError:
        logger.info(
            "google.adk not installed in current environment; workflow ready for ADK runtime."
        )
        return None


root_agent = create_adk_workflow()
