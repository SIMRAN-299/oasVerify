"""Deterministic code quality gate (Black, Flake8, and PyTest collection check)."""

import logging

from manager.models import ReviewResult
from manager.tools.review_tools import review_test_file

logger = logging.getLogger(__name__)


class TestQualityGate:
    """Runs deterministic static analysis and collection checks on generated test files."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def execute(
        self, test_file_paths: list[str], retry_count: int = 0
    ) -> ReviewResult:
        """Validates a list of generated test files and returns a structured ReviewResult."""
        logger.info(
            "[%s] TestQualityGate reviewing %d files...",
            self.session_id,
            len(test_file_paths),
        )

        if not test_file_paths:
            return ReviewResult(
                passed=False, error_message="No test files provided for review."
            )

        last_result = ReviewResult(passed=True)

        for file_path in test_file_paths:
            logger.info("[%s] Reviewing %s...", self.session_id, file_path)
            res = review_test_file(file_path, retry_count=retry_count)
            if not res.passed:
                logger.error(
                    "[%s] Validation failed for %s: %s",
                    self.session_id,
                    file_path,
                    res.error_message,
                )
                return res
            last_result = res

        logger.info(
            "[%s] TestQualityGate completed. All files passed quality gates.",
            self.session_id,
        )
        return last_result
