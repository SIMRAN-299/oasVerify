#!/usr/bin/env python3
"""CLI Test Suite Runner for exported OAS test suites (suite.json).

Executes API test scenarios with live step progress, concurrency, and CI/CD reporting.

Usage:
    python run_suite.py path/to/suite.json
    python run_suite.py path/to/suite.json --base-url http://localhost:8080
    python run_suite.py path/to/suite.json --concurrency 4
    python run_suite.py path/to/suite.json --scenario SCENARIO-PROD-01
    python run_suite.py path/to/suite.json --junitxml results.xml
"""

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any
import xml.etree.ElementTree as ET

from manager.agents.test_validator_agent import (
    TestValidatorAgent,
    apply_test_fix_to_step,
)
from manager.core.runtime import TestRunner
from manager.core.schemas import TestScenario


class Colors:
    """ANSI terminal color escape sequences."""

    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def print_colored(text: str, color: str = "") -> None:
    """Prints text with ANSI color if stdout is a TTY."""
    if sys.stdout.isatty():
        print(f"{color}{text}{Colors.RESET}")
    else:
        print(text)


async def execute_journey(
    sc_data: dict[str, Any],
    base_url: str,
    healing: bool,
    semaphore: asyncio.Semaphore,
    verbose: bool = True,
) -> dict[str, Any]:
    """Executes a single scenario journey with optional self-healing."""
    sc_id = sc_data.get("scenario_id", "UNKNOWN")
    sc_name = sc_data.get("name", sc_id)
    steps = (
        sc_data.get("setup_steps", [])
        + sc_data.get("test_steps", [])
        + sc_data.get("teardown_steps", [])
    )

    async with semaphore:
        if verbose:
            print_colored(
                f"\n▶ [{sc_id}] {sc_name} ({len(steps)} steps)",
                Colors.BOLD + Colors.CYAN,
            )

        runner = TestRunner(base_url=base_url)
        sc_obj = TestScenario(**sc_data)
        step_results: dict[str, Any] = {}

        def on_step(info: dict[str, Any]) -> None:
            if not verbose:
                return
            ev = info.get("event")
            if ev == "STEP_PROGRESS":
                st_id = info.get("step_id")
                idx = info.get("step_index")
                total = info.get("total_steps")
                method = info.get("method")
                path = info.get("path_template")
                status = info.get("status")
                code = info.get("status_code")
                if status == "SUCCESS":
                    print_colored(
                        f"   ✔ [{idx}/{total}] {st_id}: {method} {path} (HTTP {code})",
                        Colors.GREEN,
                    )
                else:
                    err = info.get("error", "Error")
                    print_colored(
                        f"   ✖ [{idx}/{total}] {st_id}: {method} {path} (HTTP {code}) -> {err}",
                        Colors.RED,
                    )

        start_time = time.time()
        success = False
        healing_applied = False
        try:
            success = await runner.execute_scenario(
                sc_obj, step_results, on_step_progress=on_step
            )

            if not success and healing:
                error_msg = "Unknown error"
                for _, res in step_results.items():
                    if res.get("status") == "FAILED":
                        error_msg = res.get("error") or str(
                            res.get("response", "Error")
                        )
                        break
                print_colored(
                    f"   🤖 [{sc_id}] Attempting Self-Healing...", Colors.YELLOW
                )
                validator = TestValidatorAgent()
                fix = await validator.heal_scenario_async(sc_data, error_msg)
                if fix:
                    healing_applied = True
                    print_colored(
                        f"   🤖 [{sc_id}] Fix: {fix.reasoning}", Colors.YELLOW
                    )
                    all_s = (
                        sc_data.get("setup_steps", [])
                        + sc_data.get("test_steps", [])
                        + sc_data.get("teardown_steps", [])
                    )
                    target_id = fix.fixed_step_id or next(iter(step_results), None)
                    target_steps = (
                        [s for s in all_s if s.get("step_id") == target_id]
                        or all_s
                    )
                    for step_dict in target_steps:
                        apply_test_fix_to_step(step_dict, fix)

                    # Re-run
                    sc_obj = TestScenario(**sc_data)
                    step_results = {}
                    runner = TestRunner(base_url=base_url)
                    success = await runner.execute_scenario(
                        sc_obj, step_results, on_step_progress=on_step
                    )
        except Exception as err:
            success = False
            step_results["error"] = {"status": "FAILED", "error": str(err)}

        duration = time.time() - start_time
        if success:
            healed_suffix = " (Healed)" if healing_applied else ""
            print_colored(
                f"  ✅ [{sc_id}] PASSED ({duration:.2f}s){healed_suffix}",
                Colors.BOLD + Colors.GREEN,
            )
        else:
            print_colored(
                f"  ❌ [{sc_id}] FAILED ({duration:.2f}s)",
                Colors.BOLD + Colors.RED,
            )

        return {
            "scenario_id": sc_id,
            "name": sc_name,
            "status": "PASSED" if success else "FAILED",
            "duration": duration,
            "healing_applied": healing_applied,
            "steps": step_results,
        }


def generate_junit_xml(
    results: list[dict[str, Any]], suite_name: str, output_path: str
) -> None:
    """Writes a standard JUnit XML report for CI/CD integration."""
    testsuites = ET.Element("testsuites")
    suite_el = ET.SubElement(
        testsuites,
        "testsuite",
        {
            "name": suite_name,
            "tests": str(len(results)),
            "failures": str(sum(1 for r in results if r["status"] == "FAILED")),
            "time": f"{sum(r['duration'] for r in results):.3f}",
        },
    )
    for r in results:
        tc = ET.SubElement(
            suite_el,
            "testcase",
            {
                "name": r["name"],
                "classname": r["scenario_id"],
                "time": f"{r['duration']:.3f}",
            },
        )
        if r["status"] == "FAILED":
            err_msg = ""
            for st_id, st_data in r.get("steps", {}).items():
                if (
                    isinstance(st_data, dict)
                    and st_data.get("status") == "FAILED"
                ):
                    err_msg += f"Step {st_id}: {st_data.get('error', 'Failed')}\n"
            failure = ET.SubElement(
                tc, "failure", {"message": "Scenario execution failed"}
            )
            failure.text = err_msg or "Test failed"

    tree = ET.ElementTree(testsuites)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)


async def async_main() -> None:
    """Async CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Run OpenAPI integration test suite directly from suite.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("suite_file", help="Path to suite.json")
    parser.add_argument(
        "--base-url",
        default=None,
        help="Override target base URL (e.g. http://localhost:8080)",
    )
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=4,
        help="Parallel execution concurrency (default: 4)",
    )
    parser.add_argument(
        "--scenario",
        "-s",
        default=None,
        help="Filter to run only a specific scenario ID or name",
    )
    parser.add_argument(
        "--healing",
        action="store_true",
        help="Enable automatic self-healing on failure",
    )
    parser.add_argument(
        "--junitxml",
        default=None,
        help="Generate JUnit XML report file for CI/CD",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Save execution results JSON report",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Minimal console output",
    )

    args = parser.parse_args()

    if not os.path.exists(args.suite_file):
        print_colored(
            f"Error: Suite file not found at '{args.suite_file}'", Colors.RED
        )
        sys.exit(1)

    with open(args.suite_file, "r", encoding="utf-8") as f:
        suite_data = json.load(f)

    target_base_url = args.base_url or suite_data.get("target_base_url")
    if not target_base_url:
        print_colored(
            "Error: Target base URL not specified in suite.json and no --base-url provided.",
            Colors.RED,
        )
        sys.exit(1)

    scenarios = suite_data.get("scenarios", [])
    if args.scenario:
        scenarios = [
            s
            for s in scenarios
            if args.scenario in (s.get("scenario_id"), s.get("name"))
        ]
        if not scenarios:
            print_colored(
                f"Error: No scenario matching '{args.scenario}' found in suite.",
                Colors.RED,
            )
            sys.exit(1)

    print_colored(
        "\n=======================================================", Colors.BOLD
    )
    print_colored("  OAS Test Suite Runner", Colors.BOLD + Colors.BLUE)
    print_colored(f"  Target: {target_base_url}", Colors.DIM)
    print_colored(
        f"  Scenarios: {len(scenarios)} | Concurrency: {args.concurrency} | Healing: {args.healing}",
        Colors.DIM,
    )
    print_colored(
        "=======================================================", Colors.BOLD
    )

    semaphore = asyncio.Semaphore(args.concurrency)
    start_total = time.time()

    tasks = [
        execute_journey(
            sc,
            target_base_url,
            args.healing,
            semaphore,
            verbose=not args.quiet,
        )
        for sc in scenarios
    ]
    results = await asyncio.gather(*tasks)

    total_duration = time.time() - start_total
    passed = sum(1 for r in results if r["status"] == "PASSED")
    failed = sum(1 for r in results if r["status"] == "FAILED")
    total = len(results)
    score = int((passed / total) * 100) if total > 0 else 0

    print_colored(
        "\n=======================================================", Colors.BOLD
    )
    print_colored("  Execution Summary", Colors.BOLD)
    print_colored(
        f"  Passed: {passed}/{total} ({score}%) | Failed: {failed} | Time: {total_duration:.2f}s",
        Colors.GREEN if failed == 0 else Colors.YELLOW,
    )
    print_colored(
        "=======================================================\n", Colors.BOLD
    )

    if args.junitxml:
        generate_junit_xml(
            results, suite_data.get("suite_id", "suite"), args.junitxml
        )
        print_colored(
            f"📄 JUnit XML report saved to: {args.junitxml}", Colors.CYAN
        )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "total": total,
                    "passed": passed,
                    "failed": failed,
                    "score": score,
                    "results": results,
                },
                f,
                indent=2,
            )
        print_colored(f"📊 Results JSON saved to: {args.output}", Colors.CYAN)

    sys.exit(0 if failed == 0 else 1)


def main() -> None:
    """Runs the async test suite runner."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
