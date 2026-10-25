"""Master orchestrator coordinating OpenAPI parsing, LLM test generation, quality gates, and GitHub sync."""

import json
import logging
import os
import re
from typing import Any

from manager.agents.test_generator_agent import TestGeneratorAgent
from manager.core.github_deployer import GitHubDeployer
from manager.core.oas_parser import OASParser
from manager.core.quality_gate import TestQualityGate
from manager.core.schemas import TestSuite
from manager.core.suite_scaffolder import TestSuiteScaffolder
from manager.models import OASMetadata, ReviewResult
from manager.tools.github_tools import get_detected_github_repo

logger = logging.getLogger(__name__)


class OASTestOrchestrator:
    """Coordinates the end-to-end OpenAPI test generation, validation, and deployment workflow."""

    def __init__(self, session_id: str, repo_name: str | None = None) -> None:
        self.session_id = session_id
        detected = get_detected_github_repo()
        candidate = (repo_name or os.environ.get("GITHUB_REPO") or detected).strip()
        if not candidate or candidate == "your-org/your-repo":
            candidate = detected
        self.repo_name = candidate

        self.oas_parser = OASParser(session_id)
        self.test_generator = TestGeneratorAgent(session_id)
        self.suite_scaffolder = TestSuiteScaffolder(session_id)
        self.quality_gate = TestQualityGate(session_id)
        self.github_deployer = GitHubDeployer(session_id, repo_name=self.repo_name)

    def run(
        self,
        oas_path: str,
        config_path: str | None = None,
        sync_github: bool = False,
    ) -> dict[str, Any]:
        """Executes the complete test parsing, generation, quality check, and deployment pipeline."""
        logger.info(
            "[%s] Starting OAS Test Generation Pipeline for: %s",
            self.session_id,
            oas_path,
        )

        # 1. Parse OAS into typed metadata
        logger.info("[%s] Step 1: Parsing and resolving OAS...", self.session_id)
        oas_metadata: OASMetadata = self.oas_parser.execute(oas_path)
        logger.info(
            "[%s] Spec resolved. Title: '%s', Base URL: '%s', Endpoints: %d",
            self.session_id,
            oas_metadata.title,
            oas_metadata.base_url,
            len(oas_metadata.endpoints),
        )

        # 1.5 Load Test Config if provided
        test_config: dict[str, Any] | None = None
        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    test_config = json.load(f)
                logger.info(
                    "[%s] Successfully loaded custom test_config.json", self.session_id
                )
            except Exception as err:
                logger.warning(
                    "[%s] Failed to load test_config.json: %s", self.session_id, err
                )

        # 2. Generate test suite scenarios via TestGeneratorAgent
        logger.info(
            "[%s] Step 2: Generating integration test scenarios...", self.session_id
        )
        test_suite: TestSuite = self.test_generator.execute(oas_metadata, test_config)
        logger.info(
            "[%s] Test suite generated with %d test steps across %d scenarios.",
            self.session_id,
            sum(len(s.test_steps) for s in test_suite.scenarios),
            len(test_suite.scenarios),
        )

        # 3. Scaffold test files and run deterministic quality gates
        logger.info(
            "[%s] Step 3: Scaffolding test suite and running quality gates...",
            self.session_id,
        )
        test_files = self.suite_scaffolder.execute(
            test_suite,
            generation_report=getattr(self.test_generator, "generation_report", None),
        )
        if not test_files:
            logger.error("[%s] Scaffolding yielded no files.", self.session_id)
            return {"status": "error", "message": "No test files generated."}

        review_result: ReviewResult = self.quality_gate.execute(test_files)
        if not review_result.passed:
            logger.error(
                "[%s] Test code failed quality gate: %s. Aborting deployment.",
                self.session_id,
                review_result.error_message,
            )
            return {
                "status": "review_failed",
                "test_files": test_files,
                "error": review_result.error_message or "Quality gate failed",
            }

        logger.info("[%s] Quality gate passed.", self.session_id)

        # 4. GitHub Deployment (optional / if sync enabled)
        pr_url = ""
        github_sync_result = None
        should_sync = sync_github or os.environ.get("GITHUB_AUTO_SYNC", "").lower() in (
            "true",
            "1",
            "yes",
        )

        clean_spec_filename = re.sub(
            r"^[0-9a-fA-F\-]{36}_", "", os.path.basename(oas_path)
        )
        if should_sync:
            logger.info(
                "[%s] Step 4: Syncing tests to GitHub in logical timestamped folder for '%s'...",
                self.session_id,
                self.repo_name,
            )
            github_sync_result = self.github_deployer.push_session_tests(
                session_id=self.session_id,
                repo_name=self.repo_name,
                spec_file=clean_spec_filename,
            )
            if github_sync_result.get("status") == "success":
                pr_url = github_sync_result.get("url", "")
        else:
            logger.info(
                "[%s] GitHub sync not triggered. Skipping GitHub deployment.",
                self.session_id,
            )

        return {
            "status": "success",
            "session_id": self.session_id,
            "api_title": oas_metadata.title,
            "spec_file": clean_spec_filename,
            "base_url": test_suite.target_base_url,
            "test_files": test_files,
            "test_count": sum(len(s.test_steps) for s in test_suite.scenarios),
            "pr_url": pr_url,
            "github_sync": github_sync_result,
        }
