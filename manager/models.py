"""Pydantic data models for parsed OpenAPI metadata, test plans, and quality gate results."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SecurityScheme(BaseModel):
    """Represents an OpenAPI security scheme definition."""

    scheme_name: str
    type: str
    scheme: str | None = None
    in_location: str | None = None
    name: str | None = None
    bearer_format: str | None = None
    description: str | None = None


class ParameterMetadata(BaseModel):
    """Represents an endpoint parameter (path, query, header, or cookie)."""

    name: str
    in_location: str = Field(description="'path', 'query', 'header', or 'cookie'")
    required: bool = False
    param_type: str = "string"
    format: str | None = None
    pattern: str | None = None
    enum: list[Any] | None = None
    example: Any | None = None
    default: Any | None = None
    description: str | None = None


class RequestBodyMetadata(BaseModel):
    """Represents an endpoint request body schema and constraints."""

    required: bool = False
    content_type: str = "application/json"
    schema_type: str = "object"
    properties: dict[str, Any] = Field(default_factory=dict)
    required_properties: list[str] = Field(default_factory=list)
    raw_schema: dict[str, Any] = Field(default_factory=dict)


class EndpointMetadata(BaseModel):
    """Represents a fully resolved API operation."""

    path: str
    method: str
    operation_id: str
    summary: str | None = ""
    description: str | None = ""
    parameters: list[ParameterMetadata] = Field(default_factory=list)
    request_body: RequestBodyMetadata | None = None
    responses: dict[str, Any] = Field(default_factory=dict)
    security: list[dict[str, list[str]]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class OASMetadata(BaseModel):
    """Structured, validated specification metadata extracted from OAS."""

    title: str
    version: str
    description: str | None = ""
    base_url: str = "http://localhost:8080"
    servers: list[str] = Field(default_factory=list)
    security_schemes: dict[str, SecurityScheme] = Field(default_factory=dict)
    global_security: list[dict[str, list[str]]] = Field(default_factory=list)
    endpoints: list[EndpointMetadata] = Field(default_factory=list)


class TestCasePlan(BaseModel):
    """Defines a single test scenario planned by the heuristic planner."""

    test_id: str = Field(description="Unique identifier for the test case")
    operation_id: str = Field(description="Corresponding operationId from OAS")
    method: str = Field(description="HTTP Method: GET, POST, PUT, DELETE, PATCH")
    resolved_path: str = Field(
        description="Path with all {path_param} replaced with concrete dummy values"
    )
    query_params: dict[str, Any] = Field(
        default_factory=dict, description="Concrete query parameters"
    )
    headers: dict[str, str] = Field(
        default_factory=dict, description="Custom headers for the test"
    )
    request_body: dict[str, Any] | None = Field(
        default=None, description="Request payload for write methods"
    )
    expected_status: int = Field(
        description="Expected HTTP status code, e.g. 200, 201, 204, 400, 422"
    )
    test_type: str = Field(description="'happy_path' or 'negative_400'")
    scenario_description: str = Field(
        description="Human readable docstring explaining the test purpose"
    )
    auth_required: bool = Field(
        default=False, description="Whether this endpoint requires auth headers"
    )


class GeminiTestCase(BaseModel):
    """Schema-compliant model for Gemini structured edge-case output."""

    test_id: str = Field(
        description="Unique snake_case identifier, e.g. test_create_product_negative_price"
    )
    operation_id: str = Field(description="Corresponding operationId from OAS")
    method: str = Field(description="HTTP Method: POST, PUT, PATCH")
    resolved_path: str = Field(description="Concrete path, e.g. /products")
    request_body_json: str | None = Field(
        default=None,
        description="JSON-encoded string of the payload containing the boundary violation",
    )
    expected_status: int = Field(
        default=400, description="Expected HTTP status code, usually 400 or 422"
    )
    scenario_description: str = Field(
        description="Explanation of the edge case or boundary tested"
    )


class GeminiEdgeCaseResponse(BaseModel):
    """Container for semantic edge cases proposed by Gemini."""

    tests: list[GeminiTestCase] = Field(
        default_factory=list, description="List of proposed edge case tests"
    )


EdgeCaseSuggestions = GeminiEdgeCaseResponse


class WorkflowStep(BaseModel):
    """Represents a single step in a multi-endpoint stateful CRUD workflow."""

    step_name: str = Field(
        description="Descriptive step identifier, e.g. 'create_item'"
    )
    method: str = Field(description="HTTP method: POST, GET, PUT, DELETE")
    path_template: str = Field(
        description="Path string with {var} substitutions, e.g. '/products' or '/products/{created_id}'"
    )
    payload: dict[str, Any] | None = Field(
        default=None, description="Request JSON payload"
    )
    capture_var: str | None = Field(
        default=None,
        description="Variable name to store captured response value, e.g. 'created_id'",
    )
    capture_json_key: str | None = Field(
        default=None, description="Key in JSON response to capture, e.g. 'id'"
    )
    expected_status: int = Field(
        default=200, description="Expected HTTP response status code"
    )
    auth_required: bool = Field(
        default=False, description="Whether authentication is required"
    )


class WorkflowPlan(BaseModel):
    """Represents a multi-step integration test scenario (Create -> Get -> Delete)."""

    workflow_id: str = Field(
        description="Unique function suffix for test, e.g. 'product_crud_lifecycle'"
    )
    title: str = Field(description="Short title for workflow")
    description: str = Field(
        description="Docstring detailing what this workflow verifies"
    )
    steps: list[WorkflowStep] = Field(default_factory=list)


class TestPlan(BaseModel):
    """Baseline test plan container."""

    api_title: str
    base_url: str
    auth_header_name: str = "Authorization"
    auth_header_env_var: str = "API_AUTH_TOKEN"
    auth_prefix: str = "Bearer "
    mode: str = Field(
        default="deterministic", description="'deterministic' or 'hybrid'"
    )
    tests: list[TestCasePlan] = Field(default_factory=list)
    workflows: list[WorkflowPlan] = Field(default_factory=list)


class ReviewResult(BaseModel):
    """Validation result produced by TestQualityGate."""

    passed: bool
    black_output: str = ""
    flake8_errors: list[str] = Field(default_factory=list)
    pytest_output: str = ""
    pytest_passed: bool = False
    error_message: str | None = None
    retry_count: int = 0
