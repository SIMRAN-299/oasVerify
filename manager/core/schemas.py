"""Declarative Pydantic schemas for test suites, scenarios, steps, variable captures, and assertions."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class HTTPMethod(str, Enum):
    """Supported HTTP methods for API test steps."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class ParameterLocation(str, Enum):
    """Locations where an OpenAPI parameter may reside."""

    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"


class ParameterSchema(BaseModel):
    """Schema metadata for a single operation parameter."""

    name: str
    location: ParameterLocation
    required: bool = False
    schema_def: dict[str, Any] = Field(default_factory=dict, alias="schema")
    description: str | None = None


class OperationNode(BaseModel):
    """Node in the endpoint dependency graph."""

    operation_id: str
    method: HTTPMethod
    path: str
    parameters: list[ParameterSchema] = Field(default_factory=list)
    produces_keys: list[str] = Field(default_factory=list)


class AssertionOperator(str, Enum):
    """Semantic comparison operators for response validation."""

    EQUALS = "EQUALS"
    NOT_EQUALS = "NOT_EQUALS"
    CONTAINS = "CONTAINS"
    GREATER_THAN = "GREATER_THAN"
    LESS_THAN = "LESS_THAN"
    NOT_NULL = "NOT_NULL"
    MATCHES_REGEX = "MATCHES_REGEX"


class SemanticAssertion(BaseModel):
    """Declarative assertion evaluated against a JSON response body."""

    target_expression: str
    operator: AssertionOperator
    expected_value: str | None = None


class VariableCapture(BaseModel):
    """Rule extracting a JSONPath value from a response into the scenario context."""

    variable_name: str
    json_path: str


class TestStep(BaseModel):
    """Atomic HTTP request and validation step within a scenario."""

    step_id: str
    operation_id: str
    method: HTTPMethod
    path_template: str
    path_params: dict[str, Any] = Field(default_factory=dict)
    query_params: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    request_body: dict[str, Any] | None = None
    captures: list[VariableCapture] = Field(default_factory=list)
    expected_status: int
    semantic_assertions: list[SemanticAssertion] = Field(default_factory=list)
    timeout_seconds: float = 10.0


class ScenarioType(str, Enum):
    """Classification of an integration test journey."""

    HAPPY_PATH_E2E = "HAPPY_PATH_E2E"
    BOUNDARY_NEGATIVE = "BOUNDARY_NEGATIVE"
    SECURITY_PERMUTATION = "SECURITY_PERMUTATION"
    IDEMPOTENCY = "IDEMPOTENCY"


class TestScenario(BaseModel):
    """Ordered multi-step integration test journey."""

    scenario_id: str
    name: str
    description: str
    scenario_type: ScenarioType
    tags: list[str] = Field(default_factory=list)
    setup_steps: list[TestStep] = Field(default_factory=list)
    test_steps: list[TestStep]
    teardown_steps: list[TestStep] = Field(default_factory=list)


class TestSuite(BaseModel):
    """Top-level container for a generated integration test suite."""

    suite_id: str
    target_base_url: str
    scenarios: list[TestScenario]


class DomainScenarioBatch(BaseModel):
    """Batch of scenarios generated for a specific domain cluster."""

    scenarios: list[TestScenario] = Field(default_factory=list)
