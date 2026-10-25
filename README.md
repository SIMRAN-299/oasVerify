# OASVerify — Autonomous, AI-Powered API QA Agent

**OASVerify** is an autonomous, AI-powered QA agent built on Google’s Gemini models to modernize API testing. Instead of writing test scripts, engineers simply provide an OpenAPI Specification (OAS). OASVerify acts as a cognitive tester — autonomously analyzing specifications, planning comprehensive coverage, and exercising live APIs with boundary, negative and security cases to uncover edge-case failures before they reach production.

By replacing manual API QA with zero-script, spec-driven validation, OASVerify eliminates delivery drag and accelerates timelines:

- **For PSO & Delivery Teams**: Eliminates repetitive integration test authoring, freeing engineers to focus on high-value architecture while ensuring 100% adherence to the OpenAPI contract prior to project handover.
- **For Customers**: Hardens enterprise gateways and production APIs against edge-case failures. Clients gain immediate access to AI-optimized, maintainable regression suites natively injected into their codebases for lasting pipeline agility.

Ultimately, by automating low-level debugging and reducing production defects, OASVerify maximizes operational efficiency, lowers downstream maintenance costs, and reinforces customer confidence in Google Cloud delivery.

Under the hood, OASVerify ingests OpenAPI 3.0+ specifications, synthesizes multi-step integration test journeys, streams live execution with real-time progress tracking, self-heals failing tests, and exports clean, standalone **Python PyTest** test suites.

The platform is built on a **Hybrid Declarative Architecture**, explicitly dividing the system between **Deterministic Infrastructure** (enforcing execution order, type safety, and schema contracts) and **AI Synthesis** (discovering domain workflows and diagnosing server rejections).

---

## 🌟 Key Capabilities

- **⚙️ Topological DAG Ordering**: Computes resource dependencies using Kahn's algorithm so operations execute in natural REST order (`POST` $\rightarrow$ `GET` $\rightarrow$ `PUT`/`PATCH` $\rightarrow$ `DELETE`).
- **🤖 Domain-Aware Journey Planning**: Clusters operations by resource paths into modular, realistic scenarios (happy path, boundary validation, and negative testing) with 100% endpoint coverage.
- **📊 Real-Time Execution Dashboard**: Interactive web dashboard featuring live per-step progress bars (`1/N` $\rightarrow$ `N/N`), collapsible request/response JSON viewers, and master suite progress tracking.
- **⚡ Parallel Execution Streaming**: Dispatches independent journeys concurrently over Server-Sent Events (SSE) with bounded concurrency (`asyncio.Semaphore(4)`), achieving a **~4x execution speedup**.
- **🔄 Autonomous Dual-Mode Self-Healing**: Diagnoses API failures (validation errors, enum/regex mismatches, expected status code differences) using Gemini 3.8 Flash with a generic deterministic fallback. Patches tests on disk and verifies fixes with live re-runs.
- **🐍 Standalone Python Test Export**: 1-click download of an idiomatic, executable PyTest file (`test_suite.py`) containing individual `@pytest.mark.asyncio async def test_<scenario>()` functions. No proprietary runners required.
- **🐙 GitHub Sync & Push**: Automated synchronization and 1-click manual push to GitHub. Organizes test artifacts into logical timestamped folders (`tests/generated/{api_slug}/{timestamp}/`) via Git CLI (SSH) or GitHub REST API, supporting direct commits and Pull Request creation.

---

## 🚀 Quickstart

### 1. Installation

```bash
# Clone the repository
cd oas-spec-test

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configuration (Optional)

The platform runs out-of-the-box with deterministic planning and heuristic self-healing even without AI credentials. To enable Gemini 3.8 Flash AI planning and advanced reasoning:

```bash
# Option A: Gemini Developer API Key
export GOOGLE_API_KEY="AIzaSy..."

# Option B: Vertex AI (Google Cloud)
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT="your-project-id"
export GOOGLE_CLOUD_LOCATION="global"
```

### 3. Start the Web Server

```bash
python server.py
```
Open **`http://localhost:8081`** in your browser.

---

## 🛠️ Generating Test Suites

### Via the Web UI
1. Navigate to `http://localhost:8081`.
2. Upload your OpenAPI Specification (`.yaml` or `.json`).
3. *(Optional)* Upload a custom `test_config.json` for global path parameters or payload overrides.
4. Click **Generate Test Suite** to open the interactive dashboard.

### Via the Command Line
```bash
python main.py --oas-file sample_oas/weather.yaml --config-file test_config.json --session-id my_session
```

---

## 🧪 Running & Exporting Tests

### Option 1: Standalone Python PyTest Suite (Recommended)

You can download pure Python test code directly from the Execution Dashboard by clicking **`🐍 Download Python Tests (test_suite.py)`** or fetch it via the API:

```bash
curl http://localhost:8081/download_tests/<SESSION_ID> -o test_suite.py
```

Run the generated Python tests natively with `pytest`:
```bash
# Execute with PyTest
pytest test_suite.py -v

# Override the target API base URL at runtime
API_BASE_URL="http://staging.example.com" pytest test_suite.py -v

# Run directly with Python
python test_suite.py
```

Each scenario is generated as an independent, readable async test function:
```python
@pytest.mark.asyncio
async def test_product_full_lifecycle_e2e_flow():
    """Validates creation, retrieval, mutation, and deletion of products."""
    context = {}
    async with httpx.AsyncClient(base_url=BASE_URL, follow_redirects=True) as client:
        # Step 1: POST /v1/products
        resp_1 = await client.request("POST", "/v1/products", json={"name": "Widget", "price": 29.99})
        assert resp_1.status_code == 201
        context["created_id"] = resp_1.json()["id"]

        # Step 2: GET /v1/products/{id}
        resp_2 = await client.request("GET", f"/v1/products/{context['created_id']}")
        assert resp_2.status_code == 200
```

---

### Option 2: CLI Test Suite Runner (`run_suite.py`)

Run exported `suite.json` files directly from the command line with colored terminal output, parallel concurrency, and CI/CD report generation:

```bash
# Basic run
python run_suite.py session_workspaces/<SESSION_ID>/tests/suite.json

# Run against a different environment with 4x concurrency
python run_suite.py suite.json --base-url https://staging.api.com -c 4

# Run a single journey
python run_suite.py suite.json --scenario SCENARIO-PROD-01

# Generate JUnit XML for CI/CD pipelines (Jenkins, GitHub Actions, GitLab CI)
python run_suite.py suite.json --junitxml results.xml --output report.json
```

---

### Option 3: Interactive Dashboard (Browser)

Navigate to `http://localhost:8081/execution_dashboard/<SESSION_ID>`:
- **`▶ Run Journey`**: Runs a single journey with a live progress bar that advances per step in real time.
- **`▶ Run All Journeys`**: Executes all journeys in parallel with live status counters (`✅ X passed | ❌ Y failed`).
- **`🤖 Enable LLM Self-Healing`**: Automatically detects failures, patches parameters or status expectations on disk, updates the request display with a `🤖 HEALED` badge, and re-runs to verify.
- **`< / > Request` & `< / > Response`**: Inspect exact headers, URLs, query parameters, and JSON payloads.

---

## ⚙️ Configuration Overrides (`test_config.json`)

Customize parameters, inject authentication tokens, or provide entity IDs without editing the specification:

```json
{
  "global": {
    "path_parameters": {
      "zoneId": "ILZ014",
      "stationId": "KORD"
    },
    "payload_overrides": {
      "status": "active"
    }
  },
  "endpoints": {
    "/v1/products/{id}": {
      "path_parameters": {
        "id": "prod_12345"
      }
    }
  }
}
```

---

## 🐙 GitHub Integration & Automated Push Implementation

The platform includes a built-in GitHub integration allowing you to synchronize generated test suites directly into your GitHub repository in logical, timestamped folders.

### 🌟 Features & User Workflows
- **"Sync with GitHub" Toggle Switch**:
  - Located on both the initial OpenAPI upload screen and the Test Execution Dashboard header.
  - When enabled, generated tests are automatically committed and pushed to GitHub as soon as the test generation and quality gate reviews complete.
- **"Push to GitHub" Button & Modal**:
  - Located in the Test Execution Dashboard header with the official GitHub Octocat SVG icon.
  - Clicking opens an interactive configuration modal where you can:
    - **Target Repository**: Auto-detected from your local Git remote (e.g. `apigee-agentic-offering/oas-spec-test`) or custom.
    - **Target Branch**: Auto-detected from your current Git branch (e.g. `main` or feature branch).
    - **Logical Folder**: Automatically formatted as `tests/generated/{api_slug}/{YYYY-MM-DD_HH-MM-SS}`.
    - **Push Mode**: **Direct Commit** to the target branch or **Create Pull Request** against `main`.
    - **Token**: Optionally supply a GitHub Personal Access Token (`GITHUB_TOKEN`), or leave blank to use your local SSH credentials.
- **Live GitHub Sync Banner**:
  - Displays directly at the top of the Test Execution Dashboard showing the commit branch, timestamped folder, and a direct clickable link to view the commit or Pull Request on GitHub.

---

### 🏗️ How It Is Implemented

The GitHub integration is implemented across five core layers:

```
┌────────────────────────────────────────────────────────┐
│               Test Execution Dashboard UI              │
│   [Sync with GitHub Toggle]   [Push to GitHub Button]  │
└───────────────────────────┬────────────────────────────┘
                            │ HTTP POST /github/push or /upload?sync_github=true
┌───────────────────────────▼────────────────────────────┐
│                    server.py API                       │
│     Endpoints: /github/config, /github/status, /push   │
└───────────────────────────┬────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────┐
│              OASTestOrchestrator Pipeline              │
│ OASParser ➔ TestGeneratorAgent ➔ TestSuiteScaffolder   │
│                           │                            │
│                           ▼                            │
│                    TestQualityGate                     │
│                           │                            │
│         (if sync enabled) ▼                            │
│                     GitHubDeployer                     │
└───────────────────────────┬────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────┐
│                  manager/tools/github_tools            │
│  ├─ get_detected_github_repo() & get_detected_branch() │
│  ├─ make_timestamped_folder()                          │
│  ├─ PyGithub REST API (Direct Blobs & Trees / PRs)     │
│  └─ Local Git CLI Fallback (Scoped SSH git add/push)   │
└────────────────────────────────────────────────────────┘
```

#### 1. Repository & Branch Auto-Detection (`manager/tools/github_tools.py`)
- **`get_detected_github_repo()`**: Dynamically queries the local git configuration (`git config --get remote.origin.url`) to extract `{owner}/{repo}` across both SSH (`git@github.com:owner/repo.git`) and HTTPS (`https://github.com/owner/repo.git`) remote formats. Falls back to `GITHUB_REPO` environment variable or defaults.
- **`get_detected_git_branch()`**: Inspects `git rev-parse --abbrev-ref HEAD` to detect the currently checked-out branch, falling back to `GITHUB_BRANCH` or `main`.

#### 2. Timestamped Logical Folder Hierarchy
Tests are partitioned into clean, collision-free directories so subsequent test generations never overwrite previous runs:
```
tests/oas-tests-agent/{YYYY-MM-DD_HH-MM-SS}/
├── README.md              # Auto-generated documentation with test execution commands
├── test_suite.py          # Standalone executable PyTest suite
├── suite.json             # Declarative test scenario definitions
└── test_api_runner.py     # Interactive runner script
```
- **`make_timestamped_folder()`**: Generates a dedicated folder `tests/oas-tests-agent/{timestamp}/` and pairs it with an isolated branch `oas-tests-agent/{timestamp}`.

#### 3. Dual-Mode Authentication & Resilient Push Engine
The implementation handles both CI/cloud environments and local developer setups:
- **PyGithub REST API (API Token)**:
  - When `GITHUB_TOKEN` is available, `commit_files()` uses the GitHub Git Data API to construct blobs and trees in-memory.
  - Commits are pushed directly to remote branch `oas-tests-agent/{timestamp}` (never directly to `main`).
  - Supports opening automated Pull Requests via `open_pull_request()`.
- **Local Git CLI Fallback (SSH Keys)**:
  - When no API token is supplied, `commit_and_push_local_git()` automatically uses your existing SSH key (`git@github.com:...`).
  - **Isolated Branch**: Automatically checks out a dedicated branch (`git checkout -B oas-tests-agent/{timestamp}`), commits only the test folder, pushes to `origin oas-tests-agent/{timestamp}`, and restores your original branch.
  - Your main branch and other uncommitted working directory changes remain completely untouched.

#### 4. Automated Orchestration Hook (`manager/core/orchestrator.py`)
- The pipeline's `run()` method accepts `sync_github: bool` (configured per session or via `GITHUB_AUTO_SYNC=true`).
- Once `TestSuiteScaffolder` generates the files and `TestQualityGate` validates quality gates, `GitHubDeployer.push_session_tests()` is invoked.
- Detailed synchronization results are persisted to `session_workspaces/{session_id}/github_sync.json` and returned in the orchestrator status payload.

#### 5. REST Endpoints (`server.py`)
| Endpoint | Method | Description |
|---|---|---|
| `/github/config` | `GET` | Retrieves auto-sync status, detected repo, active branch, and token presence. |
| `/github/config` | `POST` | Updates global auto-sync setting and repository defaults. |
| `/github/status/{session_id}` | `GET` | Returns synchronization status, commit SHA, folder path, and GitHub URL. |
| `/github/push/{session_id}` | `POST` | Triggers a manual push or PR creation for a session with custom options. |

---

### 💻 GitHub Integration API Examples

#### 1. Check Detected Configuration
```bash
curl -s http://localhost:8081/github/config
# Response:
# {"auto_sync": false, "repo": "apigee-agentic-offering/oas-spec-test", "branch": "avishek", "has_token": false}
```

#### 2. Enable Global Auto-Sync
```bash
curl -s -X POST http://localhost:8081/github/config \
  -H "Content-Type: application/json" \
  -d '{"auto_sync": true}'
```

#### 3. Manually Push a Session to GitHub
```bash
curl -s -X POST http://localhost:8081/github/push/<SESSION_ID> \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "apigee-agentic-offering/oas-spec-test",
    "branch": "avishek",
    "mode": "commit"
  }'
```

---

## 🏛️ System Architecture

For complete architectural details, sequence diagrams, and module specifications, see [**`ARCHITECTURE.md`**](ARCHITECTURE.md).

```
oas-spec-test/
├── manager/
│   ├── core/
│   │   ├── schemas.py           # Pydantic contract (TestSuite, TestScenario, TestStep)
│   │   ├── graph.py             # Topological DAG dependency sort (Kahn's algorithm)
│   │   ├── runtime.py           # Async HTTPX execution engine & AssertionEngine
│   │   ├── codegen.py           # Standalone PyTest Python test code generator
│   │   ├── oas_parser.py        # Deterministic OAS normalization & $ref dereferencing (OASParser)
│   │   ├── suite_scaffolder.py  # Session scaffolding & test_suite.py writer (TestSuiteScaffolder)
│   │   ├── quality_gate.py      # Deterministic quality gate validation (TestQualityGate)
│   │   ├── github_deployer.py   # GitHub test suite deployment (GitHubDeployer)
│   │   └── orchestrator.py      # Master pipeline coordinator (OASTestOrchestrator)
│   ├── tools/
│   │   ├── github_tools.py      # Git remote detection, timestamped folders & push helpers
│   │   ├── parsing_tools.py     # OpenAPI dereferencing & endpoint extraction
│   │   ├── generation_tools.py  # PyTest code rendering helpers
│   │   └── review_tools.py      # PyTest dry-run & syntax quality validation
│   └── agents/
│       ├── test_generator_agent.py  # LLM agent: Domain clustering & scenario synthesis (TestGeneratorAgent)
│       └── test_validator_agent.py  # LLM agent: Dual-mode live test validator & self-healer (TestValidatorAgent)
├── server.py                    # FastAPI backend, SSE streams & Execution Dashboard
├── run_suite.py                 # CLI runner with parallel execution & JUnit XML reports
└── tests/
    └── test_pipeline.py         # Automated unit test suite
```

---

## 🧪 Running Automated Tests

Run the platform's test suite:

```bash
pytest tests/ -v
```
