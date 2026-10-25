"""Deterministic test suite artifact writer and Visualize tests snapshot scaffolder."""

import json
import logging
import os
from typing import Any

from manager.core.codegen import generate_python_test_suite
from manager.core.schemas import TestSuite

logger = logging.getLogger(__name__)


class TestSuiteScaffolder:
    """Writes suite.json, standalone test_suite.py, runner wrapper, generation_report.json and visualize.html."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.workspace_dir = os.path.join("session_workspaces", session_id)
        self.tests_dir = os.path.join(self.workspace_dir, "tests")

    def execute(
        self,
        test_suite: TestSuite,
        generation_report: dict[str, Any] | None = None,
    ) -> list[str]:
        """Writes all test artifacts for the given TestSuite (plus its provenance report) to the session workspace."""
        os.makedirs(self.tests_dir, exist_ok=True)

        wrapper_code = """import asyncio
import json
import os
import pytest
from manager.core.runtime import TestRunner
from manager.core.schemas import TestSuite

suite_path = os.path.join(os.path.dirname(__file__), "suite.json")
with open(suite_path, "r", encoding="utf-8") as f:
    suite_data = json.load(f)

suite = TestSuite(**suite_data)


@pytest.mark.parametrize(
    "scenario", suite.scenarios, ids=[s.name for s in suite.scenarios]
)
def test_api_scenario(scenario):
    runner = TestRunner(base_url=suite.target_base_url)
    results = {}
    success = asyncio.run(runner.execute_scenario(scenario, step_results=results))
    if not success:
        failed_steps = [
            f"{k}: {v.get('error')}"
            for k, v in results.items()
            if v.get("status") == "FAILED"
        ]
        err_detail = "\\n".join(failed_steps)
        assert False, f"Scenario '{scenario.name}' failed on step(s):\\n{err_detail}"
"""
        test_file = os.path.join(self.tests_dir, "test_api_runner.py")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write(wrapper_code.strip() + "\n")

        suite_json_path = os.path.join(self.tests_dir, "suite.json")
        with open(suite_json_path, "w", encoding="utf-8") as f:
            f.write(test_suite.model_dump_json(indent=2))

        generated_files = [test_file]
        try:
            py_suite_path = os.path.join(self.tests_dir, "test_suite.py")
            with open(py_suite_path, "w", encoding="utf-8") as f:
                f.write(generate_python_test_suite(test_suite.model_dump()))
            generated_files.append(py_suite_path)
        except Exception as err:
            logger.warning(
                "[%s] Failed to generate standalone test_suite.py: %s",
                self.session_id,
                err,
            )

        # Provenance (which journeys Gemini designed vs. the deterministic baseline, and what the
        # guardrails adjusted) powers the evidence panels on the Visualize tests page.
        report = generation_report if isinstance(generation_report, dict) else None
        if report is not None:
            try:
                report_path = os.path.join(self.tests_dir, "generation_report.json")
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2, default=str)
            except (OSError, TypeError, ValueError) as err:
                logger.warning(
                    "[%s] Failed to write generation_report.json: %s",
                    self.session_id,
                    err,
                )

        self._write_visualizer_snapshot(test_suite, report)
        return generated_files

    def _write_visualizer_snapshot(
        self, test_suite: TestSuite, report: dict[str, Any] | None
    ) -> None:
        """Writes a static visualize.html for CLI runs; the web server renders the same page live per request."""
        try:
            # The renderer lives with the other root-level UI modules (ui_theme, server); import lazily so
            # the core pipeline package has no hard dependency on the web UI layer.
            from ui_visualizer import build_visualizer_page

            html_content = build_visualizer_page(
                self.session_id,
                test_suite.model_dump(mode="json"),
                generation_report=report,
            )
            vis_path = os.path.join(self.workspace_dir, "visualize.html")
            with open(vis_path, "w", encoding="utf-8") as f:
                f.write(html_content)
        except Exception as err:
            logger.warning(
                "[%s] Failed to write visualize.html snapshot: %s",
                self.session_id,
                err,
            )
