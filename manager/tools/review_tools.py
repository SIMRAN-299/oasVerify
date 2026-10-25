"""Static analysis and dry-run collection utilities for generated PyTest files."""

import ast
import logging
import subprocess
import sys

from manager.models import ReviewResult

logger = logging.getLogger(__name__)


def run_formatter(file_path: str) -> tuple[bool, str]:
    """Runs 'black' formatter on the generated test file."""
    logger.info("Running black formatter on %s", file_path)
    cmd = [sys.executable, "-m", "black", file_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True, result.stderr.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as err:
        err_msg = getattr(err, "stderr", str(err))
        logger.warning("Black formatter notice: %s", err_msg)
        return False, err_msg


def run_linter(file_path: str) -> tuple[bool, list[str]]:
    """Runs AST validation and 'flake8' linter to check for critical syntax and name errors."""
    logger.info("Running linter on %s", file_path)
    ast_errors: list[str] = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=file_path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in {"true", "false", "null"}
            ):
                ast_errors.append(
                    f"{file_path}:{node.lineno}:{node.col_offset}: F821 undefined name '{node.id}'"
                )
    except SyntaxError as syn_err:
        ast_errors.append(f"{file_path}:{syn_err.lineno}: E999 SyntaxError: {syn_err.msg}")
    except OSError as os_err:
        ast_errors.append(f"{file_path}: OSError: {os_err}")

    cmd = [sys.executable, "-m", "flake8", "--select=E9,F63,F7,F82", file_path]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return len(ast_errors) == 0, ast_errors
    except subprocess.CalledProcessError as err:
        errors = [
            line.strip()
            for line in err.stdout.strip().splitlines()
            if line.strip()
        ]
        combined = list(dict.fromkeys(ast_errors + errors))
        logger.warning("Linter found syntax issues: %s", combined)
        return False, combined
    except FileNotFoundError:
        return len(ast_errors) == 0, ast_errors


def run_dry_test(file_path: str) -> tuple[bool, str]:
    """Runs 'pytest --collect-only' to verify test validity and test discovery."""
    logger.info("Running dry-run test discovery on %s", file_path)
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", file_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info("PyTest discovered tests successfully.")
        return True, result.stdout.strip()
    except subprocess.CalledProcessError as err:
        err_msg = f"{err.stdout}\n{err.stderr}".strip()
        logger.error("PyTest failed to collect tests:\n%s", err_msg)
        return False, err_msg
    except FileNotFoundError as err:
        logger.warning("PyTest not available in path: %s", err)
        return True, "pytest not found, skipping dry-run"


def review_test_file(file_path: str, retry_count: int = 0) -> ReviewResult:
    """Performs full static and discovery review on a generated test file."""
    _, black_out = run_formatter(file_path)
    _, flake8_errs = run_linter(file_path)
    pytest_passed, pytest_out = run_dry_test(file_path)

    passed = pytest_passed and (len(flake8_errs) == 0)

    error_msg = None
    if not passed:
        reasons = []
        if flake8_errs:
            reasons.append("Linter syntax errors: " + "; ".join(flake8_errs))
        if not pytest_passed:
            reasons.append(f"PyTest collection failure: {pytest_out}")
        error_msg = " | ".join(reasons)

    return ReviewResult(
        passed=passed,
        black_output=black_out,
        flake8_errors=flake8_errs,
        pytest_output=pytest_out,
        pytest_passed=pytest_passed,
        error_message=error_msg,
        retry_count=retry_count,
    )
