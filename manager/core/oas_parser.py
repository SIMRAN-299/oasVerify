"""Deterministic OpenAPI specification resolver and metadata extractor."""

import logging

from manager.models import OASMetadata
from manager.tools.parsing_tools import extract_oas_metadata, parse_and_resolve_oas

logger = logging.getLogger(__name__)


class OASParser:
    """Reads and resolves OpenAPI Specifications into typed OASMetadata."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def execute(self, oas_file_path: str) -> OASMetadata:
        """Resolves $ref pointers with prance and extracts typed OASMetadata."""
        logger.info(
            "[%s] OASParser started for file: %s", self.session_id, oas_file_path
        )
        resolved_spec = parse_and_resolve_oas(oas_file_path)
        oas_metadata = extract_oas_metadata(resolved_spec)
        logger.info(
            "[%s] OASParser successfully processed %d endpoints for API '%s'.",
            self.session_id,
            len(oas_metadata.endpoints),
            oas_metadata.title,
        )
        return oas_metadata
