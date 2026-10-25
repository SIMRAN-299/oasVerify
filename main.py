"""CLI entry point for the OASVerify pipeline."""

import argparse
import logging
import os

from manager.core.orchestrator import OASTestOrchestrator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")


def main() -> None:
    """Parses CLI arguments and runs the OASTestOrchestrator pipeline."""
    parser = argparse.ArgumentParser(description="OASVerify pipeline")
    parser.add_argument(
        "--oas-file",
        required=True,
        help="Path to the OpenAPI Specification file",
    )
    parser.add_argument(
        "--session-id",
        default="session_default",
        help="Unique session ID for workspace",
    )
    parser.add_argument(
        "--config-file",
        default=None,
        help="Path to test_config.json overrides",
    )
    args = parser.parse_args()
    config_path = os.path.abspath(args.config_file) if args.config_file else None
    oas_path = os.path.abspath(args.oas_file)

    logger.info("Starting OASVerify for session: %s", args.session_id)
    orchestrator = OASTestOrchestrator(session_id=args.session_id)
    orchestrator.run(oas_path, config_path=config_path)


if __name__ == "__main__":
    main()
