"""OpenAPI specification dereferencing and typed metadata extraction utilities."""

import logging
from typing import Any

import prance

from manager.models import (
    EndpointMetadata,
    OASMetadata,
    ParameterMetadata,
    RequestBodyMetadata,
    SecurityScheme,
)

logger = logging.getLogger(__name__)


def parse_and_resolve_oas(file_path: str) -> dict[str, Any]:
    """Parses an OpenAPI Specification file (YAML/JSON) and resolves all nested $ref references."""
    try:
        logger.info("Parsing and resolving OAS from %s using prance...", file_path)
        parser = prance.ResolvingParser(file_path, backend="openapi-spec-validator")
        return parser.specification
    except Exception as err:
        logger.error("Failed to parse OAS file %s: %s", file_path, err)
        raise


def extract_parameter_metadata(param_dict: dict[str, Any]) -> ParameterMetadata:
    """Extracts typed ParameterMetadata from raw parameter dict."""
    schema = param_dict.get("schema", {})
    items_schema = (
        schema.get("items", {})
        if isinstance(schema.get("items"), dict)
        else {}
    )
    sub_schemas: list[dict[str, Any]] = []
    for comp_key in ("oneOf", "anyOf", "allOf"):
        for s in (schema.get(comp_key) or []) + (items_schema.get(comp_key) or []):
            if isinstance(s, dict):
                sub_schemas.append(s)

    param_type = schema.get("type", param_dict.get("type", "string"))
    fmt = schema.get("format") or items_schema.get("format")
    pat = (
        schema.get("pattern")
        or items_schema.get("pattern")
        or param_dict.get("pattern")
    )
    enum_vals = (
        schema.get("enum")
        or items_schema.get("enum")
        or param_dict.get("enum")
    )
    example_val = (
        param_dict.get("example")
        if param_dict.get("example") is not None
        else (
            schema.get("example")
            if schema.get("example") is not None
            else items_schema.get("example")
        )
    )
    default_val = (
        param_dict.get("default")
        if param_dict.get("default") is not None
        else (
            schema.get("default")
            if schema.get("default") is not None
            else items_schema.get("default")
        )
    )

    for sub in sub_schemas:
        if not fmt and sub.get("format"):
            fmt = sub.get("format")
        if not pat and sub.get("pattern"):
            pat = sub.get("pattern")
        if not enum_vals and sub.get("enum"):
            enum_vals = sub.get("enum")
        if example_val is None and sub.get("example") is not None:
            example_val = sub.get("example")
        if default_val is None and sub.get("default") is not None:
            default_val = sub.get("default")

    return ParameterMetadata(
        name=param_dict.get("name", "param"),
        in_location=param_dict.get("in", "query"),
        required=param_dict.get("required", False) or (param_dict.get("in") == "path"),
        param_type=param_type,
        format=fmt,
        pattern=pat,
        enum=enum_vals,
        example=example_val,
        default=default_val,
        description=param_dict.get("description", ""),
    )


def extract_request_body_metadata(
    request_body: dict[str, Any],
) -> RequestBodyMetadata | None:
    """Extracts typed RequestBodyMetadata from raw requestBody dict."""
    if not request_body:
        return None

    content = request_body.get("content", {})
    json_content = content.get("application/json") or content.get("*/*")

    content_type = "application/json"
    if not json_content and content:
        content_type = next(iter(content.keys()))
        json_content = content[content_type]

    if not json_content:
        return None

    schema = json_content.get("schema", {})
    properties = schema.get("properties", {})
    required_props = schema.get("required", [])

    return RequestBodyMetadata(
        required=request_body.get("required", False),
        content_type=content_type,
        schema_type=schema.get("type", "object"),
        properties=properties,
        required_properties=required_props,
        raw_schema=schema,
    )


def extract_oas_metadata(resolved_spec: dict[str, Any]) -> OASMetadata:
    """Extracts comprehensive, strongly-typed OASMetadata from resolved specification."""
    info = resolved_spec.get("info", {})
    title = info.get("title", "API_Tests")
    version = info.get("version", "1.0.0")
    description = info.get("description", "")

    servers = [
        s.get("url", "") for s in resolved_spec.get("servers", []) if s.get("url")
    ]
    base_url = servers[0] if servers else "http://localhost:8080"
    if base_url.endswith("/"):
        base_url = base_url[:-1]

    components = resolved_spec.get("components", {})
    raw_security_schemes = components.get("securitySchemes", {})
    security_schemes: dict[str, SecurityScheme] = {}

    for name, s_def in raw_security_schemes.items():
        security_schemes[name] = SecurityScheme(
            scheme_name=name,
            type=s_def.get("type", "apiKey"),
            scheme=s_def.get("scheme"),
            in_location=s_def.get("in"),
            name=s_def.get("name"),
            bearer_format=s_def.get("bearerFormat"),
            description=s_def.get("description"),
        )

    global_security = resolved_spec.get("security", [])

    endpoints: list[EndpointMetadata] = []
    paths = resolved_spec.get("paths", {})

    for path, path_item in paths.items():
        path_level_params = path_item.get("parameters", [])

        for method, details in path_item.items():
            if method.lower() not in [
                "get",
                "post",
                "put",
                "delete",
                "patch",
                "options",
                "head",
            ]:
                continue

            op_params = details.get("parameters", [])
            merged_param_map = {}
            for p in path_level_params:
                merged_param_map[(p.get("name"), p.get("in"))] = p
            for p in op_params:
                merged_param_map[(p.get("name"), p.get("in"))] = p

            parsed_params = [
                extract_parameter_metadata(p) for p in merged_param_map.values()
            ]
            parsed_body = extract_request_body_metadata(details.get("requestBody", {}))

            operation_id = details.get(
                "operationId",
                f"{method}_{path.replace('/', '_').replace('{', '').replace('}', '')}",
            )

            endpoints.append(
                EndpointMetadata(
                    path=path,
                    method=method.upper(),
                    operation_id=operation_id,
                    summary=details.get("summary", ""),
                    description=details.get("description", ""),
                    parameters=parsed_params,
                    request_body=parsed_body,
                    responses=details.get("responses", {}),
                    security=details.get("security", global_security),
                    tags=details.get("tags", []),
                )
            )

    logger.info(
        "Extracted %d endpoints from specification '%s'.", len(endpoints), title
    )

    return OASMetadata(
        title=title,
        version=version,
        description=description,
        base_url=base_url,
        servers=servers,
        security_schemes=security_schemes,
        global_security=global_security,
        endpoints=endpoints,
    )
