import pytest
import os
import json
import asyncio
from manager.core.schemas import TestSuite
from manager.core.runtime import TestRunner

suite_path = os.path.join(os.path.dirname(__file__), "suite.json")
with open(suite_path, "r") as f:
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
        err_detail = "\n".join(failed_steps)
        assert False, f"Scenario '{scenario.name}' failed on step(s):\n{err_detail}"
