"""FastAPI web server, SSE execution streaming, and interactive dashboard for OAS test generation."""

import asyncio
import copy
from datetime import datetime, timezone
import html
import json
import logging
import os
import re
from typing import Any, Callable
from urllib.parse import quote
import uuid

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
import httpx
import uvicorn

from manager.agents.test_validator_agent import (
    TestValidatorAgent,
    apply_test_fix_to_step,
)
from manager.core.codegen import (
    filter_suite_for_successful_tests,
    generate_python_test_suite,
)
from manager.core.github_deployer import GitHubDeployer
from manager.core.orchestrator import OASTestOrchestrator
from manager.core.provenance import diff_step_fields
from manager.core.runtime import TestRunner, TestScenario, TestStep
from manager.tools.github_tools import get_detected_github_repo
from ui_theme import (
    APP_NAME,
    icon,
    render_error_page,
    render_page,
    status_icon_markup,
)
from ui_visualizer import build_visualizer_page

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

_session_locks: dict[str, asyncio.Lock] = {}


def get_session_lock(session_id: str) -> asyncio.Lock:
    """Returns an asyncio.Lock scoped to a specific session_id."""
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


def _js_json(value: Any) -> str:
    """Serializes a value as a JSON literal that is safe to embed inside an inline <script> block."""
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def format_step_results(results_dict: dict[str, Any]) -> dict[str, Any]:
    """Formats raw TestRunner step results into JSON-serializable payloads for SSE events."""
    formatted: dict[str, Any] = {}
    for st_id, data in results_dict.items():
        diag = data.get("diagnosis") or data.get("reasoning")
        err = data.get("error")
        if "response" in data and data["response"] is not None:
            resp = data["response"]
            try:
                body_val = resp.json()
            except ValueError:
                body_val = resp.text
            formatted[st_id] = {
                "status": data.get("status", "SUCCESS"),
                "status_code": resp.status_code,
                "request_url": (
                    str(resp.request.url) if hasattr(resp, "request") else ""
                ),
                "request_method": (
                    resp.request.method if hasattr(resp, "request") else ""
                ),
                "body": body_val,
                "error": err,
                "diagnosis": diag,
            }
        else:
            formatted[st_id] = {
                "status": data.get("status", "FAILED"),
                "status_code": "ERROR",
                "body": data.get("error", "Unknown error"),
                "error": err,
                "diagnosis": diag,
            }
    return formatted


class SessionLogHandler(logging.Handler):
    """Captures log lines tagged with a session UUID into in-memory session logs."""

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        match = re.search(r"\[([a-f0-9\-]{32,36})\]", msg)
        if match:
            sess_id = match.group(1)
            if sess_id in session_status_map:
                if "logs" not in session_status_map[sess_id]:
                    session_status_map[sess_id]["logs"] = []
                session_status_map[sess_id]["logs"].append(msg)


handler = SessionLogHandler()
handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
logging.getLogger().addHandler(handler)

app = FastAPI(
    title=f"{APP_NAME} API",
    description=(
        "OASVerify is an autonomous, AI-powered API QA agent built on Google's Gemini models. "
        "Provide an OpenAPI Specification and it plans comprehensive coverage, exercises your live API with boundary, "
        "negative and security cases, repairs failing tests automatically, and exports a standalone PyTest suite — zero test scripts required."
    ),
    version="2.0.0",
)

# Base paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_OAS_DIR = os.path.join(BASE_DIR, "sample_oas")
# Bundled sample specs offered on the home page when no file is uploaded; the first entry is pre-selected.
# The keys double as the whitelist for the ``/sample_oas/{filename}`` download route.
BUNDLED_SAMPLES: dict[str, dict[str, str]] = {
    "ecommerce.yaml": {"label": "E-commerce", "title": "Cloud Run E-Commerce CRUD API"},
    "weather.yaml": {"label": "Weather", "title": "weather.gov API"},
}
DEFAULT_OAS_PATH = os.path.join(SAMPLE_OAS_DIR, next(iter(BUNDLED_SAMPLES)))
UPLOAD_DIR = os.path.join(BASE_DIR, "session_uploads")
WORKSPACE_DIR = os.path.join(BASE_DIR, "session_workspaces")
FRONTEND_DIST_DIR = os.path.join(BASE_DIR, "frontend", "dist")
FRONTEND_ASSETS_DIR = os.path.join(FRONTEND_DIST_DIR, "assets")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(WORKSPACE_DIR, exist_ok=True)

if os.path.isdir(FRONTEND_ASSETS_DIR):
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_ASSETS_DIR),
        name="frontend-assets",
    )


def _try_serve_spa_html() -> HTMLResponse | None:
    """Serves the compiled React SPA index.html when SERVE_REACT_SPA=1 and frontend/dist/index.html exists."""
    if os.environ.get("SERVE_REACT_SPA", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return None
    spa_index = os.path.join(FRONTEND_DIST_DIR, "index.html")
    if not os.path.isfile(spa_index):
        return None
    try:
        with open(spa_index, "r", encoding="utf-8") as f:
            return HTMLResponse(
                content=f.read(),
                status_code=200,
                headers={"Cache-Control": "no-cache"},
            )
    except OSError:
        return None


_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,128}")


def resolve_session_dir(session_id: str) -> str | None:
    """Returns the workspace directory for a session id, or None if the id is malformed or escapes WORKSPACE_DIR."""
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        return None
    root = os.path.realpath(WORKSPACE_DIR)
    candidate = os.path.realpath(os.path.join(root, session_id))
    if candidate == root or os.path.commonpath([root, candidate]) != root:
        return None
    return candidate


# In-memory session tracking and ephemeral per-session GitHub token store
session_status_map: dict[str, dict[str, Any]] = {}
session_github_tokens: dict[str, str] = {}


def save_session_status(session_id: str) -> None:
    """Persists session status to disk so sessions survive server reloads or restarts."""
    try:
        sess_dir = os.path.join(WORKSPACE_DIR, session_id)
        os.makedirs(sess_dir, exist_ok=True)
        status_file = os.path.join(sess_dir, "status.json")
        data = session_status_map.get(session_id)
        if data:
            with open(status_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
    except OSError as err:
        logger.warning("Could not persist session status to disk: %s", err)


def get_execution_results_path(session_id: str) -> str:
    """Returns the file path for persisted test execution results for a session."""
    return os.path.join(
        WORKSPACE_DIR, session_id, "tests", "execution_results.json"
    )


def load_execution_results(session_id: str) -> dict[str, Any]:
    """Loads persisted scenario and step execution statuses for a session."""
    path = get_execution_results_path(session_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("scenarios", {})
                    return data
        except (OSError, ValueError) as err:
            logger.warning("Could not read execution_results.json: %s", err)
    return {"scenarios": {}}


def _save_execution_results(session_id: str, data: dict[str, Any]) -> None:
    """Writes scenario/step execution statuses to execution_results.json."""
    try:
        path = get_execution_results_path(session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as err:
        logger.warning("Could not persist execution_results.json: %s", err)


def record_scenario_execution_results(
    session_id: str,
    scenario_id: str | None,
    step_results: dict[str, Any],
    scenario_status: str,
) -> None:
    """Persists all step execution outcomes for a scenario after a run completes."""
    if not scenario_id:
        return
    data = load_execution_results(session_id)
    scenarios_map = data.setdefault("scenarios", {})
    existing_sc = scenarios_map.get(scenario_id)
    steps_map: dict[str, str] = (
        dict(existing_sc.get("steps", {}))
        if isinstance(existing_sc, dict)
        else {}
    )
    for st_id, res in step_results.items():
        if isinstance(res, dict):
            steps_map[str(st_id)] = str(res.get("status", "FAILED")).upper()
        else:
            steps_map[str(st_id)] = str(res).upper()
    scenarios_map[str(scenario_id)] = {
        "status": scenario_status.upper(),
        "steps": steps_map,
    }
    _save_execution_results(session_id, data)


def record_single_step_execution_result(
    session_id: str,
    scenario_id: str,
    step_statuses: dict[str, str],
) -> None:
    """Persists step execution outcomes when a single step is run from the dashboard."""
    data = load_execution_results(session_id)
    scenarios_map = data.setdefault("scenarios", {})
    sc_entry = scenarios_map.setdefault(
        str(scenario_id), {"status": "PENDING", "steps": {}}
    )
    steps_map = sc_entry.setdefault("steps", {})
    for st_id, st_status in step_statuses.items():
        steps_map[str(st_id)] = str(st_status).upper()
    _save_execution_results(session_id, data)


def load_generation_report(session_id: str) -> dict[str, Any] | None:
    """Loads the provenance report written at generation time (None for sessions generated before provenance tracking)."""
    path = os.path.join(WORKSPACE_DIR, session_id, "tests", "generation_report.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError) as err:
            logger.warning("Could not read generation_report.json: %s", err)
    return None


def get_healing_log_path(session_id: str) -> str:
    """Returns the file path of the per-session self-healing log shown on the Visualize tests page."""
    return os.path.join(WORKSPACE_DIR, session_id, "tests", "healing_log.json")


def load_healing_log(session_id: str) -> list[dict[str, Any]]:
    """Loads the persisted self-healing events for a session (empty when none were recorded)."""
    path = get_healing_log_path(session_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [entry for entry in data if isinstance(entry, dict)]
        except (OSError, ValueError) as err:
            logger.warning("Could not read healing_log.json: %s", err)
    return []


def record_healing_event(
    session_id: str,
    scenario_id: str | None,
    step_id: str | None,
    fix: Any,
    source: str | None,
    model: str | None,
    error: str,
    step_before: dict[str, Any] | None,
    step_after: dict[str, Any] | None,
    re_run_success: bool | None,
) -> None:
    """Appends one self-healing event (who repaired which test, what changed, did the re-run pass) to healing_log.json."""
    try:
        entry = {
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scenario_id": scenario_id,
            "step_id": step_id,
            "source": source or "deterministic",
            "model": model if source == "gemini" else None,
            "error": str(error or "")[:1000],
            "reasoning": str(getattr(fix, "reasoning", "") or ""),
            "changes": diff_step_fields(step_before or {}, step_after or {}),
            "re_run_success": re_run_success,
        }
        log = load_healing_log(session_id)
        log.append(entry)
        path = get_healing_log_path(session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, default=str)
    except (OSError, TypeError, ValueError) as err:
        logger.warning("Could not persist healing_log.json: %s", err)


def normalize_passed_steps_payload(
    raw_passed_steps: Any,
) -> dict[str, list[str]] | None:
    """Normalizes an optional client-provided {scenario_id: [step_id, ...]} mapping."""
    if not isinstance(raw_passed_steps, dict):
        return None
    normalized: dict[str, list[str]] = {}
    for sc_id, step_ids in raw_passed_steps.items():
        if isinstance(step_ids, list):
            normalized[str(sc_id)] = [str(s) for s in step_ids if s is not None]
    return normalized


GITHUB_CONFIG_FILE = os.path.join(WORKSPACE_DIR, "github_config.json")


def get_github_config(session_id: str | None = None) -> dict[str, Any]:
    """Loads non-sensitive GitHub repository settings without sharing tokens across sessions."""
    detected_repo = get_detected_github_repo()
    env_repo = os.environ.get("GITHUB_REPO", "").strip()
    default_repo = (
        env_repo
        if (env_repo and env_repo != "your-org/your-repo")
        else detected_repo
    )
    cfg: dict[str, Any] = {
        "auto_sync": False,
        "repo": default_repo,
        "branch": "",
        "has_token": bool(
            session_id and session_github_tokens.get(session_id)
        ),
    }
    if os.path.exists(GITHUB_CONFIG_FILE):
        try:
            with open(GITHUB_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                saved.pop("token", None)
                if saved.get("repo") == "your-org/your-repo":
                    saved["repo"] = default_repo
                cfg.update(saved)
        except (OSError, ValueError) as err:
            logger.warning("Could not read github_config.json: %s", err)
    cfg["auto_sync"] = False
    cfg["has_token"] = bool(
        session_id and session_github_tokens.get(session_id)
    )
    return cfg


def save_github_config(data: dict[str, Any]) -> dict[str, Any]:
    """Updates and saves non-sensitive GitHub repo settings (never persists tokens globally)."""
    cfg = get_github_config()
    for k, v in data.items():
        if k in ("token", "auto_sync", "has_token"):
            continue
        cfg[k] = v
    cfg.pop("token", None)
    cfg["auto_sync"] = False
    try:
        with open(GITHUB_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {k: v for k, v in cfg.items() if k not in ("token", "has_token")},
                f,
                indent=2,
            )
    except OSError as err:
        logger.warning("Could not write github_config.json: %s", err)
    return cfg


def run_agent_task(
    session_id: str,
    file_path: str,
    config_path: str | None = None,
    sync_github: bool = False,
    spec_name: str | None = None,
) -> None:
    """Background task running the full test generation workflow."""
    clean_spec = spec_name or re.sub(
        r"^[0-9a-fA-F\-]{36}_", "", os.path.basename(file_path)
    )
    logger.info(
        "Starting generation task for session %s with file %s (sync_github=%s, spec=%s)",
        session_id,
        file_path,
        sync_github,
        clean_spec,
    )
    try:
        session_status_map[session_id] = {
            "status": "running",
            "message": "Parsing OpenAPI Spec and generating test scenarios...",
            "result": None,
            "spec_file": clean_spec,
        }
        save_session_status(session_id)

        gh_cfg = get_github_config(session_id)
        target_repo = gh_cfg.get("repo")
        if not target_repo or target_repo == "your-org/your-repo":
            target_repo = get_detected_github_repo()

        orchestrator = OASTestOrchestrator(
            session_id=session_id, repo_name=target_repo
        )
        result = orchestrator.run(
            file_path, config_path, sync_github=bool(sync_github)
        )
        result["spec_file"] = clean_spec

        if result.get("status") == "success":
            msg = f"Successfully generated {result.get('test_count', 0)} integration tests!"
            if (
                result.get("github_sync")
                and result["github_sync"].get("status") == "success"
            ):
                gh_info = result["github_sync"]
                msg += f" Synced to GitHub ({gh_info.get('folder', '')})."
            session_status_map[session_id] = {
                "status": "completed",
                "message": msg,
                "result": result,
                "spec_file": clean_spec,
            }
        else:
            session_status_map[session_id] = {
                "status": "failed",
                "message": f"Validation failed: {result.get('error', 'Unknown error')}",
                "result": result,
                "spec_file": clean_spec,
            }
        save_session_status(session_id)
    except Exception as err:
        logger.exception(
            "Pipeline failed for session %s: %s", session_id, err
        )
        session_status_map[session_id] = {
            "status": "error",
            "message": str(err),
            "result": None,
        }
        save_session_status(session_id)


HOME_EXTRA_CSS = """
body.narrow { --container: 820px; }
.how { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 28px; }
.how__tile { display: flex; gap: 12px; align-items: flex-start; padding: 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.how__num { width: 26px; height: 26px; border-radius: 50%; background: var(--primary); color: #fff; font-size: 13px; font-weight: 700; display: grid; place-items: center; flex: none; box-shadow: var(--shadow-sm); }
.how__title { font-weight: 700; font-size: 14px; color: var(--primary-text); }
.how__desc { font-size: 13px; color: var(--text-2); margin-top: 3px; line-height: 1.5; }
.flow { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; }
.flow__step { font-size: 13px; font-weight: 600; color: var(--text); background: rgba(255,255,255,.85); border: 1px solid var(--note-border); border-radius: 4px; padding: 2px 8px; white-space: nowrap; }
.flow__arrow { color: var(--note-text); font-weight: 700; font-size: 13px; }
.flow__note { margin-top: 8px; font-size: 13px; }
.samples { display: flex; flex-direction: column; gap: 8px; padding: 12px 16px; border: 1px solid var(--primary-border); border-radius: var(--radius); background: var(--surface); transition: opacity .15s; }
.samples__label { font-size: 12px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; color: var(--text-3); }
.samples__hint { font-size: 13px; line-height: 1.5; color: var(--text-3); margin-top: -2px; }
.samples__hint code { font-size: 12px; }
.samples__hint[hidden] { display: none; }
.samples__list { display: flex; flex-direction: column; gap: 6px; }
.samples.is-disabled { opacity: .55; }
.samples.is-disabled .sample__pick { cursor: not-allowed; }
.sample { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.sample__pick { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; font-size: 14px; min-width: 0; }
.sample__pick input { width: 15px; height: 15px; margin: 0; accent-color: var(--primary); flex: none; }
.sample__name { font-weight: 600; color: var(--text); }
.sample__title { font-size: 13px; color: var(--text-3); }
.sample__title::before { content: "\\00b7"; margin-right: 8px; }
.sample__file { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; color: var(--primary-text); white-space: nowrap; }
.sample__file svg { width: 14px; height: 14px; }
.sample__file code { background: transparent; border: 0; padding: 0; color: inherit; font-size: 13px; }
.sample__file span { font-weight: 500; color: var(--text-3); }
#status-box { border-left: 4px solid var(--border-strong); }
#status-box[data-state="running"] { border-color: var(--primary-border); border-left-color: var(--primary); }
#status-box[data-state="running"] > .card__header { background: var(--primary-soft); border-bottom-color: var(--primary-border); }
#status-box[data-state="running"] > .card__header h2 { color: var(--primary-text); }
#status-box[data-state="completed"] { border-color: var(--success-border); border-left-color: var(--success); }
#status-box[data-state="completed"] > .card__header { background: var(--success-soft); border-bottom-color: var(--success-border); }
#status-box[data-state="completed"] > .card__header h2 { color: var(--success-text); }
#status-box[data-state="failed"] { border-color: var(--danger-border); border-left-color: var(--danger); }
#status-box[data-state="failed"] > .card__header { background: var(--danger-soft); border-bottom-color: var(--danger-border); }
#status-box[data-state="failed"] > .card__header h2 { color: var(--danger-text); }
.progress--indeterminate .progress__bar { width: 40%; animation: indeterminate 1.4s ease-in-out infinite; }
@keyframes indeterminate { 0% { transform: translateX(-100%); } 100% { transform: translateX(350%); } }
#status-msg { font-size: 15px; }
@media (max-width: 640px) { .how { grid-template-columns: 1fr; } }
"""

HOME_JS = r"""
let currentSessionId = null;
let pollInterval = null;
const ACTIVE_SESSION_KEY = 'oas_active_session_id';

function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function formatBytes(bytes) {
    if (!bytes && bytes !== 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function setBadge(state, text) {
    const badge = document.getElementById('status-badge');
    badge.className = 'badge badge-' + state;
    badge.innerHTML = (state === 'running' ? '<span class="dot"></span>' : '') + esc(text);
    const box = document.getElementById('status-box');
    if (box) box.dataset.state = state;
}

function setProgressVisible(visible) {
    const bar = document.getElementById('status-progress');
    if (bar) bar.style.display = visible ? 'block' : 'none';
}

function setSubmitting(isSubmitting) {
    const btn = document.getElementById('submitBtn');
    if (isSubmitting) {
        if (!btn.dataset.idle) btn.dataset.idle = btn.innerHTML;
        btn.innerHTML = '<span class="spinner"></span><span>Generating…</span>';
        btn.disabled = true;
    } else {
        if (btn.dataset.idle) btn.innerHTML = btn.dataset.idle;
        btn.disabled = false;
    }
}

function bindDropzone(zoneId, inputId, nameId) {
    const zone = document.getElementById(zoneId);
    const input = document.getElementById(inputId);
    const nameEl = document.getElementById(nameId);
    if (!zone || !input) return;
    const refresh = () => {
        const file = input.files && input.files[0];
        zone.classList.toggle('has-file', !!file);
        if (nameEl) nameEl.textContent = file ? `${file.name} · ${formatBytes(file.size)}` : '';
        zone.dispatchEvent(new CustomEvent('dropzone:change', { detail: { file: file || null } }));
    };
    input.addEventListener('change', refresh);
    ['dragenter', 'dragover'].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('is-dragover'); }));
    ['dragleave', 'drop'].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove('is-dragover'); }));
    zone.addEventListener('drop', (e) => {
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
            input.files = e.dataTransfer.files;
            refresh();
        }
    });
    const clearBtn = zone.querySelector('.dropzone__clear');
    if (clearBtn) {
        clearBtn.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); input.value = ''; refresh(); });
    }
}

function bindSampleChooser() {
    const zone = document.getElementById('specDropzone');
    const chooser = document.getElementById('sampleChooser');
    if (!zone || !chooser) return;
    const label = document.getElementById('sampleChooserLabel');
    const hint = document.getElementById('sampleChooserHint');
    const idleText = label ? label.textContent : '';
    zone.addEventListener('dropzone:change', (e) => {
        const hasFile = !!(e.detail && e.detail.file);
        chooser.classList.toggle('is-disabled', hasFile);
        chooser.querySelectorAll('input[name="sample_spec"]').forEach((r) => { r.disabled = hasFile; });
        if (label) label.textContent = hasFile ? 'Bundled samples are ignored while a file is attached' : idleText;
        if (hint) hint.hidden = hasFile;
    });
}

function toggleGenerationLogs() {
    const panel = document.getElementById('generation-logs-panel');
    const label = document.getElementById('toggleGenLogsLabel');
    const logContainer = document.getElementById('generation-logs');
    const isHidden = panel.style.display === 'none' || !panel.style.display;
    panel.style.display = isHidden ? 'block' : 'none';
    if (label) label.innerText = isHidden ? 'Hide generation logs' : 'Show generation logs';
    if (isHidden && logContainer) logContainer.scrollTop = logContainer.scrollHeight;
}

function resetStatusPanel() {
    const statusBox = document.getElementById('status-box');
    const statusDetails = document.getElementById('status-details');
    const logContainer = document.getElementById('generation-logs');
    const panel = document.getElementById('generation-logs-panel');
    const logBtn = document.getElementById('toggleGenLogsBtn');
    const label = document.getElementById('toggleGenLogsLabel');
    statusBox.style.display = 'block';
    statusDetails.innerHTML = '';
    if (logContainer) logContainer.innerHTML = '';
    if (panel) panel.style.display = 'none';
    if (logBtn) logBtn.style.display = 'none';
    if (label) label.innerText = 'Show generation logs';
}

document.getElementById('uploadForm').onsubmit = async (e) => {
    e.preventDefault();
    const fileInput = document.getElementById('fileInput');
    const configFile = document.getElementById('configFile');
    const statusMsg = document.getElementById('status-msg');

    const formData = new FormData();
    if (fileInput.files.length > 0) {
        formData.append("file", fileInput.files[0]);
    } else {
        const sample = document.querySelector('input[name="sample_spec"]:checked');
        if (sample) formData.append("sample_spec", sample.value);
    }
    if (configFile && configFile.files.length > 0) {
        formData.append("config_file", configFile.files[0]);
    }

    setSubmitting(true);
    resetStatusPanel();
    setBadge('running', 'Initializing');
    setProgressVisible(true);
    statusMsg.innerText = 'Submitting specification to the test generation pipeline…';

    try {
        const response = await fetch('/upload', { method: 'POST', body: formData });
        const res = await response.json();
        currentSessionId = res.session_id;
        try {
            localStorage.setItem(ACTIVE_SESSION_KEY, currentSessionId);
        } catch (storageErr) {}

        setBadge('running', 'In progress');
        statusMsg.innerHTML = `Session <code>${esc(currentSessionId)}</code> started for <strong>${esc(res.spec_used)}</strong>.`;

        clearInterval(pollInterval);
        pollInterval = setInterval(() => checkStatus(false), 1500);
    } catch (err) {
        setBadge('failed', 'Error');
        setProgressVisible(false);
        statusMsg.innerText = 'Failed to submit: ' + err;
        setSubmitting(false);
    }
};

function renderCompleted(data) {
    const statusMsg = document.getElementById('status-msg');
    const statusDetails = document.getElementById('status-details');
    const resObj = data.result || {};
    statusMsg.innerHTML = `<strong>${esc(data.message)}</strong>`;

    let ghBanner = '';
    if (resObj.github_sync && resObj.github_sync.status === 'success') {
        const gh = resObj.github_sync;
        ghBanner = `
            <div class="alert alert-success alert--between" style="margin-top:14px;">
                ${ICONS.github}
                <div><strong>Synced with GitHub.</strong> Committed to <code>${esc(gh.folder)}</code></div>
                <a href="${esc(gh.url)}" target="_blank" rel="noopener" class="btn btn-sm btn-secondary">View on GitHub ${ICONS.external}</a>
            </div>`;
    }

    const testCount = (resObj.test_count === undefined || resObj.test_count === null) ? '—' : resObj.test_count;
    statusDetails.innerHTML = `
        <div class="kv">
            <div><div class="kv__label">API</div><div class="kv__value">${esc(resObj.api_title || 'API')}</div></div>
            <div><div class="kv__label">Base URL</div><div class="kv__value mono">${esc(resObj.base_url || 'N/A')}</div></div>
            <div><div class="kv__label">Tests generated</div><div class="kv__value">${esc(testCount)}</div></div>
            <div><div class="kv__label">Session</div><div class="kv__value mono">${esc(currentSessionId)}</div></div>
        </div>
        <div class="alert alert-note" style="margin-top:16px;">
            ${ICONS.lightbulb}
            <div>
                <div class="flow">
                    <span class="flow__step">Open Execution Dashboard</span><span class="flow__arrow">&rarr;</span>
                    <span class="flow__step">Run specific/all journeys</span><span class="flow__arrow">&rarr;</span>
                    <span class="flow__step">View/export generated test code</span>
                </div>
                <div class="flow__note"><strong>Validate before you export.</strong> Running journeys lets self-healing fix failures against the live API ahead of download or GitHub push.</div>
            </div>
        </div>
        ${ghBanner}
        <div class="row" style="margin-top:16px;">
            <a class="btn btn-primary" href="/execution_dashboard/${encodeURIComponent(currentSessionId)}" target="_blank" rel="noopener">${ICONS.play} Open Execution Dashboard</a>
        </div>`;
}

async function checkStatus(isInitialRestore = false) {
    if (!currentSessionId) return;
    try {
        const res = await fetch(`/status/${currentSessionId}`);
        const data = await res.json();
        const statusBox = document.getElementById('status-box');
        const statusMsg = document.getElementById('status-msg');

        if (data.status === 'not_found') {
            clearInterval(pollInterval);
            try {
                localStorage.removeItem(ACTIVE_SESSION_KEY);
            } catch (storageErr) {}
            if (isInitialRestore) {
                currentSessionId = null;
                statusBox.style.display = 'none';
                setSubmitting(false);
                return;
            }
        }

        statusBox.style.display = 'block';

        const logContainer = document.getElementById('generation-logs');
        const panel = document.getElementById('generation-logs-panel');
        const logBtn = document.getElementById('toggleGenLogsBtn');
        if (data.logs && data.logs.length > 0) {
            if (logBtn && logBtn.style.display === 'none') {
                logBtn.style.display = 'inline-flex';
            }
            logContainer.innerHTML = data.logs.map(esc).join('\n');
            if (panel && panel.style.display === 'block') {
                logContainer.scrollTop = logContainer.scrollHeight;
            }
        }

        if (data.status === 'completed') {
            clearInterval(pollInterval);
            setBadge('completed', 'Completed');
            setProgressVisible(false);
            renderCompleted(data);
            setSubmitting(false);
        } else if (data.status === 'failed' || data.status === 'error' || data.status === 'not_found') {
            clearInterval(pollInterval);
            setBadge('failed', 'Failed');
            setProgressVisible(false);
            statusMsg.innerText = data.message;
            setSubmitting(false);
        } else {
            setBadge('running', 'Running');
            setProgressVisible(true);
            statusMsg.innerText = data.message || 'Processing specification…';
            if (isInitialRestore) {
                setSubmitting(true);
                clearInterval(pollInterval);
                pollInterval = setInterval(() => checkStatus(false), 1500);
            }
        }
    } catch (e) {
        console.error("Polling error:", e);
    }
}

window.addEventListener('DOMContentLoaded', () => {
    bindDropzone('specDropzone', 'fileInput', 'fileInputName');
    bindDropzone('configDropzone', 'configFile', 'configFileName');
    bindSampleChooser();
    try {
        const savedSessionId = localStorage.getItem(ACTIVE_SESSION_KEY);
        if (savedSessionId) {
            currentSessionId = savedSessionId;
            checkStatus(true);
        }
    } catch (e) {}
});
"""


def _format_size(num_bytes: int) -> str:
    """Formats a byte count as a short human-readable string (e.g. '142 KB')."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _render_sample_option(fname: str, meta: dict[str, str], checked: bool) -> str:
    """Renders one bundled-sample row: a radio to generate from it plus a download link for the file."""
    path = os.path.join(SAMPLE_OAS_DIR, fname)
    size_html = f"<span>{_format_size(os.path.getsize(path))}</span>" if os.path.exists(path) else ""
    href = f"/sample_oas/{quote(fname, safe='')}"
    return (
        '<div class="sample">'
        f'<label class="sample__pick"><input type="radio" name="sample_spec" value="{html.escape(fname)}"{" checked" if checked else ""}>'
        f'<span class="sample__name">{html.escape(meta["label"])}</span>'
        f'<span class="sample__title">{html.escape(meta["title"])}</span></label>'
        f'<a class="sample__file" href="{html.escape(href)}" download title="Download {html.escape(fname)}">'
        f'{icon("download")}<code>{html.escape(fname)}</code>{size_html}</a>'
        "</div>"
    )


@app.get("/", response_class=HTMLResponse)
async def serve_ui() -> HTMLResponse:
    """Renders the interactive Web UI for uploading OAS specs and monitoring test generation."""
    spa = _try_serve_spa_html()
    if spa is not None:
        return spa
    default_sample = os.path.basename(DEFAULT_OAS_PATH)
    samples_html = "".join(
        _render_sample_option(fname, meta, checked=(fname == default_sample))
        for fname, meta in BUNDLED_SAMPLES.items()
    )
    body = f"""
    <main class="page">
        <section class="hero">
            <div class="hero__kicker">{icon("shield")}<span>Autonomous API QA agent &middot; Powered by Gemini</span></div>
            <h1>OASVerify: zero-script API QA from your OpenAPI spec</h1>
            <p>Provide an OpenAPI Specification instead of writing test scripts. OASVerify analyzes the contract, plans comprehensive coverage, and exercises your live API with boundary, negative and security cases &mdash; automatically repairing failing tests and exporting a standalone PyTest suite you can commit straight to your repository. The result: hardened APIs and fewer production defects, with no integration-test authoring.</p>
        </section>

        <h3 class="section-title">How it works</h3>
        <div class="how">
            <div class="how__tile"><span class="how__num">1</span><div><div class="how__title">Analyze the specification</div><div class="how__desc">OASVerify dereferences the OpenAPI contract and orders every endpoint into a resource dependency graph, so journeys run in natural REST order (create &rarr; read &rarr; update &rarr; delete).</div></div></div>
            <div class="how__tile"><span class="how__num">2</span><div><div class="how__title">Plan test coverage</div><div class="how__desc">Gemini agents cluster operations by domain and design happy-path, boundary, negative, security and idempotency journeys across every endpoint.</div></div></div>
            <div class="how__tile"><span class="how__num">3</span><div><div class="how__title">Execute &amp; self-heal</div><div class="how__desc">Journeys run live against your API. When a request is rejected, the validator agent repairs the <em>test</em> (never your API), re-runs it, and exports a PyTest suite ready to push to GitHub.</div></div></div>
        </div>

        <form id="uploadForm" enctype="multipart/form-data" class="card card--accent">
            <div class="card__header">
                <h2>{icon("upload")} New generation run</h2>
                <span class="badge badge-info">Runs in background</span>
            </div>
            <div class="card__body stack">
                <label class="dropzone" id="specDropzone">
                    <input type="file" name="file" id="fileInput" accept=".yaml,.yml,.json">
                    <span class="dropzone__icon">{icon("file")}</span>
                    <span class="dropzone__text">
                        <span class="dropzone__title">OpenAPI specification <span class="badge badge-neutral">YAML · JSON</span></span>
                        <span class="dropzone__hint">Click to browse or drag a file here, or pick a bundled sample below.</span>
                        <span class="dropzone__file" id="fileInputName"></span>
                    </span>
                    <button type="button" class="btn-icon dropzone__clear" title="Remove file" aria-label="Remove file">{icon("x")}</button>
                </label>
                <div class="samples" id="sampleChooser" role="radiogroup" aria-labelledby="sampleChooserLabel" aria-describedby="sampleChooserHint">
                    <div class="samples__label" id="sampleChooserLabel">No file? Simply click Generate tests</div>
                    <div class="samples__hint" id="sampleChooserHint">The pre-selected <code>{html.escape(default_sample)}</code> sample is used by default &mdash; pick another bundled sample below, or attach your own spec above to override it.</div>
                    <div class="samples__list">{samples_html}</div>
                </div>
                <label class="dropzone" id="configDropzone">
                    <input type="file" name="config_file" id="configFile" accept=".json">
                    <span class="dropzone__icon">{icon("sliders")}</span>
                    <span class="dropzone__text">
                        <span class="dropzone__title">Test configuration <span class="badge badge-neutral">Optional</span></span>
                        <span class="dropzone__hint">JSON overrides for path variables and request payloads (<code>test_config.json</code>).</span>
                        <span class="dropzone__file" id="configFileName"></span>
                    </span>
                    <button type="button" class="btn-icon dropzone__clear" title="Remove file" aria-label="Remove file">{icon("x")}</button>
                </label>
            </div>
            <div class="card__footer">
                <span class="muted">Progress is restored automatically if you reload this page.</span>
                <button type="submit" id="submitBtn" class="btn btn-primary btn-lg">{icon("sparkles")}<span>Generate tests</span></button>
            </div>
        </form>

        <section id="status-box" class="card" style="display:none; margin-top:16px;">
            <div class="card__header">
                <h2>{icon("activity")} Generation status</h2>
                <div id="status-badge" class="badge badge-neutral"></div>
            </div>
            <div class="card__body stack">
                <div id="status-progress" class="progress progress--indeterminate" style="display:none;"><div class="progress__bar"></div></div>
                <div id="status-msg"></div>
                <div id="status-details"></div>
                <div>
                    <button type="button" id="toggleGenLogsBtn" class="btn btn-ghost btn-sm" style="display:none;" onclick="toggleGenerationLogs()">{icon("terminal")}<span id="toggleGenLogsLabel">Show generation logs</span></button>
                    <div id="generation-logs-panel" class="console" style="display:none; margin-top:10px;">
                        <div class="console__header"><span>Generation logs</span></div>
                        <div id="generation-logs" class="console__body"></div>
                    </div>
                </div>
            </div>
        </section>
    </main>
    """
    icons_js = _js_json(
        {
            "github": icon("github"),
            "external": icon("external"),
            "alert": icon("alert"),
            "lightbulb": icon("lightbulb"),
            "play": icon("play"),
            "branch": icon("branch"),
            "code": icon("code"),
        }
    )
    html_content = render_page(
        title=APP_NAME,
        body_html=body,
        header_title="",
        header_actions_html=(
            '<a class="btn btn-ghost btn-sm" href="/docs" target="_blank" rel="noopener">'
            f'{icon("code")} API reference</a>'
        ),
        extra_css=HOME_EXTRA_CSS,
        scripts_js=f"const ICONS = {icons_js};\n{HOME_JS}",
        body_class="narrow",
    )
    return HTMLResponse(content=html_content, status_code=200)


@app.get("/visualize/{session_id}", response_class=HTMLResponse)
async def get_visualizer(session_id: str) -> HTMLResponse:
    """Renders the Visualize tests page live from the session's current suite, provenance report, healing log and results."""
    spa = _try_serve_spa_html()
    if spa is not None:
        return spa
    session_dir = resolve_session_dir(session_id)
    suite_path = os.path.join(session_dir, "tests", "suite.json") if session_dir else ""
    if not session_dir or not os.path.exists(suite_path):
        return HTMLResponse(
            render_error_page(
                404,
                "Visualization not found",
                "No test suite has been generated for this session yet.",
            ),
            status_code=404,
        )

    try:
        with open(suite_path, "r", encoding="utf-8") as f:
            suite = json.load(f)
    except (OSError, ValueError) as err:
        logger.warning("Could not read suite.json for visualization: %s", err)
        return HTMLResponse(
            render_error_page(500, "Suite unreadable", "The generated test suite could not be loaded."),
            status_code=500,
        )
    if not isinstance(suite, dict):
        suite = {}

    api_title = ""
    status_path = os.path.join(session_dir, "status.json")
    if os.path.exists(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as sf:
                sdata = json.load(sf)
            api_title = str((sdata.get("result") or {}).get("api_title") or "")
        except (OSError, ValueError, AttributeError):
            api_title = ""

    page = build_visualizer_page(
        session_id,
        suite,
        generation_report=load_generation_report(session_id),
        healing_log=load_healing_log(session_id),
        execution_results=load_execution_results(session_id),
        api_title=api_title,
    )
    return HTMLResponse(content=page, status_code=200)


def get_session_spec_path(session_id: str) -> str | None:
    """Resolves the OpenAPI specification file path on disk for a given session_id."""
    sinfo = session_status_map.get(session_id)
    if isinstance(sinfo, dict) and sinfo.get("spec_path") and os.path.exists(sinfo["spec_path"]):
        return str(sinfo["spec_path"])

    status_path = os.path.join(WORKSPACE_DIR, session_id, "status.json")
    if os.path.exists(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as sf:
                sdata = json.load(sf)
            sp = sdata.get("spec_path") or (sdata.get("result") or {}).get("spec_path")
            if sp and os.path.exists(sp):
                return str(sp)
            sfile = sdata.get("spec_file") or (sdata.get("result") or {}).get("spec_file")
            if sfile:
                cand = os.path.join(UPLOAD_DIR, f"{session_id}_{sfile}")
                if os.path.exists(cand):
                    return cand
        except (OSError, ValueError):
            pass

    if os.path.exists(UPLOAD_DIR):
        for fname in os.listdir(UPLOAD_DIR):
            if fname.startswith(f"{session_id}_") and fname.lower().endswith((".yaml", ".yml", ".json")):
                return os.path.join(UPLOAD_DIR, fname)

    if os.path.exists(DEFAULT_OAS_PATH):
        return DEFAULT_OAS_PATH
    return None


@app.post("/upload")
async def upload_oas(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(None),
    config_file: UploadFile = File(None),
    sync_github: str | None = Form(None),
    sample_spec: str | None = Form(None),
) -> dict[str, Any]:
    """Uploads an OpenAPI specification file (or picks a bundled sample) and triggers background test generation."""
    session_id = str(uuid.uuid4())
    is_sync = str(sync_github).lower() in ("true", "1", "on", "yes")

    if file and file.filename:
        file_path = os.path.join(UPLOAD_DIR, f"{session_id}_{file.filename}")
        with open(file_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)
        spec_name = file.filename
    else:
        sample = sample_spec if sample_spec in BUNDLED_SAMPLES else os.path.basename(DEFAULT_OAS_PATH)
        file_path = os.path.join(SAMPLE_OAS_DIR, sample)
        spec_name = f"{sample} (sample)"

    config_path = None
    if config_file and config_file.filename:
        config_path = os.path.join(
            UPLOAD_DIR, f"{session_id}_{config_file.filename}"
        )
        with open(config_path, "wb") as buffer:
            c = await config_file.read()
            buffer.write(c)

    session_status_map[session_id] = {
        "status": "queued",
        "message": f"Queued test generation for {spec_name}",
        "result": None,
        "spec_file": spec_name,
        "spec_path": file_path,
    }
    save_session_status(session_id)

    background_tasks.add_task(
        run_agent_task, session_id, file_path, config_path, is_sync, spec_name
    )

    return {
        "message": "Test generation started successfully.",
        "session_id": session_id,
        "spec_used": spec_name,
    }


@app.get("/sample_oas/{filename}")
async def download_sample_oas(filename: str) -> Response:
    """Serves one of the bundled sample OpenAPI specs as a download (whitelisted by BUNDLED_SAMPLES)."""
    if filename not in BUNDLED_SAMPLES:
        return JSONResponse({"message": "Unknown sample specification."}, status_code=404)
    path = os.path.join(SAMPLE_OAS_DIR, filename)
    if not os.path.exists(path):
        return JSONResponse({"message": "Sample specification is missing on the server."}, status_code=404)
    return FileResponse(path, media_type="application/yaml", filename=filename)


@app.get("/github/config")
async def get_gh_config(session_id: str | None = None) -> dict[str, Any]:
    """Returns current non-sensitive GitHub repository settings."""
    cfg = get_github_config(session_id)
    return {k: v for k, v in cfg.items() if k != "token"}


@app.post("/github/config")
async def update_gh_config(request: Request) -> dict[str, Any]:
    """Updates non-sensitive GitHub repository settings."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    saved = save_github_config(data)
    safe_saved = {k: v for k, v in saved.items() if k != "token"}
    return {"status": "success", "config": safe_saved}


@app.get("/github/status/{session_id}")
async def get_gh_status(session_id: str) -> dict[str, Any]:
    """Checks whether this session's generated tests have already been pushed to GitHub."""
    sync_file = os.path.join(WORKSPACE_DIR, session_id, "github_sync.json")
    if os.path.exists(sync_file):
        try:
            with open(sync_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["pushed"] = data.get("status") == "success"
                return data
        except (OSError, ValueError) as err:
            logger.warning("Could not read github_sync.json: %s", err)
    return {"pushed": False}


@app.post("/github/push/{session_id}")
async def push_to_github_endpoint(
    session_id: str, request: Request
) -> Any:
    """Pushes generated tests for session_id to GitHub using the session's mandatory GitHub token."""
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        body = {}

    repo = body.get("repo")
    branch = body.get("branch")
    provided_token = (body.get("token") or "").strip() or None
    token = provided_token or session_github_tokens.get(session_id)
    create_pr = bool(body.get("create_pr", False))
    commit_msg = body.get("commit_message")
    folder = body.get("folder")
    spec_file = body.get("spec_file")
    test_scope = str(body.get("test_scope") or "all").strip().lower()
    only_successful = test_scope == "successful"
    passed_steps_override = normalize_passed_steps_payload(
        body.get("passed_steps")
    )

    if not token:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": (
                    "GitHub Personal Access Token is required. Please enter your GitHub Token "
                    "(ghp_... or github_pat_...) to push tests for this session."
                ),
            },
        )

    # Store token strictly in memory for this session_id only (never persisted to disk or shared globally)
    session_github_tokens[session_id] = token
    if repo:
        save_github_config({"repo": repo})

    deployer = GitHubDeployer(
        session_id=session_id, repo_name=repo, token=token
    )
    return deployer.push_session_tests(
        session_id=session_id,
        repo_name=repo,
        branch=branch,
        token=token,
        create_pr=create_pr,
        commit_message=commit_msg,
        custom_folder=folder,
        spec_file=spec_file,
        require_token=True,
        only_successful=only_successful,
        passed_steps_override=passed_steps_override,
    )


@app.get("/status/{session_id}")
async def get_session_status(session_id: str) -> Any:
    """Checks the real-time execution status of an ongoing or completed test generation session."""
    if session_id not in session_status_map:
        status_file = os.path.join(WORKSPACE_DIR, session_id, "status.json")
        if os.path.exists(status_file):
            try:
                with open(status_file, "r", encoding="utf-8") as f:
                    session_status_map[session_id] = json.load(f)
                    return session_status_map[session_id]
            except (OSError, ValueError) as err:
                logger.warning("Could not read status file: %s", err)

        suite_path = os.path.join(
            WORKSPACE_DIR, session_id, "tests", "suite.json"
        )
        if os.path.exists(suite_path):
            try:
                with open(suite_path, "r", encoding="utf-8") as f:
                    suite_data = json.load(f)
                test_count = sum(
                    len(s.get("test_steps", []))
                    for s in suite_data.get("scenarios", [])
                )
                recovered = {
                    "status": "completed",
                    "message": f"Successfully generated {test_count} integration tests!",
                    "result": {
                        "status": "success",
                        "session_id": session_id,
                        "api_title": suite_data.get("metadata", {}).get(
                            "title", "API"
                        ),
                        "base_url": suite_data.get("target_base_url", ""),
                        "test_count": test_count,
                    },
                }
                session_status_map[session_id] = recovered
                return recovered
            except (OSError, ValueError) as err:
                logger.warning("Could not recover suite data: %s", err)

        return JSONResponse(
            status_code=404,
            content={"status": "not_found", "message": "Session not found."},
        )
    return session_status_map[session_id]


CODE_VIEW_EXTRA_CSS = """
.card__intro { padding: 12px 20px 12px 24px; font-size: 14px; background: var(--note-soft); color: var(--note-text); border-bottom: 1px solid var(--note-border); box-shadow: inset 4px 0 0 var(--note); }
.card__intro code { background: rgba(255, 255, 255, .75); border-color: rgba(0, 0, 0, .08); color: inherit; }
.code-view {
    counter-reset: ln;
    margin: 0;
    padding: 14px 0;
    max-height: 72vh;
    overflow: auto;
    background: var(--code-bg);
    color: var(--code-fg);
    border-radius: 0 0 var(--radius-lg) var(--radius-lg);
    font-family: var(--font-mono);
    font-size: 13.5px;
    line-height: 1.6;
    white-space: pre;
    tab-size: 4;
}
.code-view > span::before {
    counter-increment: ln;
    content: counter(ln);
    display: inline-block;
    width: 4ch;
    margin: 0 1.5em 0 1em;
    text-align: right;
    color: #6e7681;
    user-select: none;
}
"""

CODE_VIEW_JS = r"""
async function copyCode(id, btn) {
    const el = document.getElementById(id);
    if (!el) return;
    try {
        await navigator.clipboard.writeText(el.innerText);
        const original = btn.innerHTML;
        btn.innerHTML = ICONS.check + ' Copied';
        setTimeout(() => { btn.innerHTML = original; }, 1600);
    } catch (e) {
        console.error('Copy failed:', e);
    }
}
"""


def _numbered_code(text: str) -> str:
    """Wraps each source line in a span (HTML-escaped) so CSS counters can render line numbers."""
    return "\n".join(f"<span>{html.escape(line)}</span>" for line in text.split("\n"))


@app.get("/tests/{session_id}", response_class=HTMLResponse)
async def get_generated_test(
    session_id: str, scope: str = "all"
) -> HTMLResponse:
    """Retrieves the generated standalone Python PyTest suite (test_suite.py) for a given session."""
    spa = _try_serve_spa_html()
    if spa is not None:
        return spa
    suite_path = os.path.join(WORKSPACE_DIR, session_id, "tests", "suite.json")
    py_suite_path = os.path.join(
        WORKSPACE_DIR, session_id, "tests", "test_suite.py"
    )

    if not os.path.exists(suite_path) and not os.path.exists(py_suite_path):
        return HTMLResponse(
            render_error_page(
                404,
                "Test files not found",
                "No generated test artifacts exist for this session yet.",
            ),
            status_code=404,
        )

    normalized_scope = (scope or "all").strip().lower()
    only_successful = normalized_scope == "successful"
    total_all_steps = 0
    total_passed_steps = 0

    if os.path.exists(suite_path):
        with open(suite_path, "r", encoding="utf-8") as f:
            suite_data = json.load(f)
        for sc in suite_data.get("scenarios", []):
            total_all_steps += (
                len(sc.get("setup_steps", []))
                + len(sc.get("test_steps", []))
                + len(sc.get("teardown_steps", []))
            )
        exec_results = load_execution_results(session_id)
        filtered_suite, total_passed_steps = filter_suite_for_successful_tests(
            suite_data, exec_results=exec_results
        )
        if only_successful:
            if total_passed_steps > 0:
                py_content = generate_python_test_suite(filtered_suite)
            else:
                py_content = (
                    "# No successful tests recorded yet for this session.\n"
                    "# Execute journeys on the Execution Dashboard first, or switch to 'All tests'."
                )
        else:
            py_content = generate_python_test_suite(suite_data)
            try:
                with open(py_suite_path, "w", encoding="utf-8") as pf:
                    pf.write(py_content)
            except OSError:
                pass
    else:
        with open(py_suite_path, "r", encoding="utf-8") as f:
            py_content = f.read()

    sid = html.escape(session_id)
    sid_short = html.escape(session_id[:8])
    sid_q = html.escape(quote(session_id, safe=""))
    file_label = "test_suite_passed.py" if only_successful else "test_suite.py"
    badge_label = (
        f"Successful tests ({total_passed_steps} passed)"
        if only_successful
        else f"PyTest suite ({total_all_steps} tests)"
    )
    badge_cls = "badge-success" if only_successful else "badge-info"
    all_btn_cls = "btn-secondary" if only_successful else "btn-primary"
    succ_btn_cls = "btn-primary" if only_successful else "btn-secondary"

    body = f"""
    <main class="page">
        <div class="page-title hero">
            <div>
                <h1>Generated code</h1>
                <p>Standalone, executable Python PyTest integration test suite synthesized for this session. Self-healing fixes applied from the dashboard are reflected here.</p>
                <div class="page-title__meta">
                    <span class="chip" title="Session {sid}">{icon("hash")}<code>{sid_short}…</code></span>
                </div>
            </div>
            <div class="row">
                <a class="btn {all_btn_cls} btn-sm" href="/tests/{sid_q}?scope=all">All tests ({total_all_steps})</a>
                <a class="btn {succ_btn_cls} btn-sm" href="/tests/{sid_q}?scope=successful">{icon("check-circle")} Successful tests ({total_passed_steps})</a>
                <a class="btn btn-secondary" href="/visualize/{sid_q}">{icon("branch")} Visualize tests</a>
                <a class="btn btn-primary" href="/execution_dashboard/{sid_q}">{icon("play")} Open dashboard</a>
            </div>
        </div>

        <div class="stack">
            <section class="card card--tint-primary">
                <div class="card__header">
                    <h2>{icon("file")} {file_label} <span class="badge {badge_cls}">{badge_label}</span></h2>
                    <div class="row">
                        <button type="button" class="btn btn-ghost btn-sm" onclick="copyCode('code-py', this)">{icon("copy")} Copy</button>
                        <a class="btn btn-secondary btn-sm" href="/download_tests/{sid_q}?scope=all">{icon("download")} Download all tests</a>
                        <a class="btn btn-secondary btn-sm" href="/download_tests/{sid_q}?scope=successful">{icon("check-circle")} Download successful tests</a>
                    </div>
                </div>
                <p class="card__intro">Standalone executable Python test suite using <code>pytest</code> and <code>httpx</code>. Run locally with <code>pytest {file_label} -v</code>.</p>
                <pre id="code-py" class="code-view">{_numbered_code(py_content)}</pre>
            </section>
        </div>
    </main>
    """
    html_content = render_page(
        title="Generated Code",
        body_html=body,
        header_title="Generated Code",
        extra_css=CODE_VIEW_EXTRA_CSS,
        scripts_js=f"const ICONS = {_js_json({'check': icon('check')})};\n{CODE_VIEW_JS}",
    )
    return HTMLResponse(content=html_content, status_code=200)


@app.get("/download_suite/{session_id}")
async def download_suite(session_id: str, scope: str = "all") -> Response:
    """Downloads the declarative suite.json file (all tests or only successful tests)."""
    suite_path = os.path.join(WORKSPACE_DIR, session_id, "tests", "suite.json")
    if not os.path.exists(suite_path):
        return JSONResponse({"error": "Suite not found"}, status_code=404)
    if scope.strip().lower() == "successful":
        with open(suite_path, "r", encoding="utf-8") as f:
            suite_data = json.load(f)
        exec_results = load_execution_results(session_id)
        filtered_suite, total_passed = filter_suite_for_successful_tests(
            suite_data, exec_results=exec_results
        )
        if total_passed == 0:
            return JSONResponse(
                {
                    "error": (
                        "No successful tests found to download. Please execute journeys on the "
                        "Execution Dashboard first so passing tests are recorded, or choose 'Download all tests'."
                    )
                },
                status_code=400,
            )
        return Response(
            content=json.dumps(filtered_suite, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="suite_{session_id}_passed.json"'
            },
        )
    return FileResponse(
        suite_path,
        media_type="application/json",
        filename=f"suite_{session_id}.json",
    )


def _build_python_tests_download_response(
    session_id: str,
    scope: str = "all",
    passed_steps_override: dict[str, list[str]] | None = None,
) -> Response:
    """Builds the downloadable PyTest suite response for either all tests or only successful tests."""
    suite_path = os.path.join(WORKSPACE_DIR, session_id, "tests", "suite.json")
    if not os.path.exists(suite_path):
        return JSONResponse({"error": "Suite not found"}, status_code=404)
    with open(suite_path, "r", encoding="utf-8") as f:
        suite_data = json.load(f)

    normalized_scope = (scope or "all").strip().lower()
    if normalized_scope == "successful":
        exec_results = load_execution_results(session_id)
        effective_suite_data, total_passed = filter_suite_for_successful_tests(
            suite_data,
            exec_results=exec_results,
            passed_steps_override=passed_steps_override,
        )
        if total_passed == 0:
            return JSONResponse(
                {
                    "error": (
                        "No successful tests found to download. Please execute journeys on the "
                        "Execution Dashboard first so passing tests are recorded, or choose 'Download all tests'."
                    )
                },
                status_code=400,
            )
        filename = f"test_suite_{session_id}_passed.py"
    else:
        effective_suite_data = suite_data
        filename = f"test_suite_{session_id}.py"

    code_str = generate_python_test_suite(effective_suite_data)
    return Response(
        content=code_str,
        media_type="text/x-python",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/download_tests/{session_id}")
async def download_python_tests(
    session_id: str, scope: str = "all"
) -> Response:
    """Generates and returns an idiomatic, executable PyTest test file (all or only successful tests)."""
    return _build_python_tests_download_response(session_id, scope=scope)


@app.post("/download_tests/{session_id}")
async def download_python_tests_post(
    session_id: str, request: Request
) -> Response:
    """Generates and returns an executable PyTest test file, accepting optional live passed_steps from UI."""
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        body = {}
    scope = str(body.get("scope") or "all").strip().lower()
    passed_steps_override = normalize_passed_steps_payload(
        body.get("passed_steps")
    )
    return _build_python_tests_download_response(
        session_id,
        scope=scope,
        passed_steps_override=passed_steps_override,
    )


DASHBOARD_EXTRA_CSS = """
.journey__header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 16px 20px; border-bottom: 1px solid var(--border); border-radius: var(--radius-lg) var(--radius-lg) 0 0; background: var(--surface-2); }
.journey__title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
.journey__name { font-size: 16px; font-weight: 700; }
.journey__desc { color: var(--text-2); font-size: 14px; margin-top: 4px; max-width: 95ch; }
.journey__meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.journey__body { padding: 14px 20px 18px; display: flex; flex-direction: column; gap: 10px; }
.journey__progress { background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 12px; }
.journey__progress-row { display: flex; justify-content: space-between; gap: 12px; font-size: 13px; margin-bottom: 6px; }
.steps-list { display: flex; flex-direction: column; gap: 8px; }
.step { border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface-2); padding: 10px 12px; }
.step__row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.step__idx { width: 22px; height: 22px; border-radius: 6px; background: var(--surface); border: 1px solid var(--border); font-size: 12px; font-weight: 700; color: var(--text-2); display: grid; place-items: center; flex: none; }
.step__main { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; flex-wrap: wrap; }
.step__path { font-family: var(--font-mono); font-size: 13.5px; font-weight: 600; color: var(--text); overflow-wrap: anywhere; }
.step__expect { font-family: var(--font-mono); font-size: 12px; color: var(--text-2); background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 1px 6px; white-space: nowrap; }
.step__phase { font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: var(--text-3); background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px; }
.step__actions { display: flex; align-items: center; gap: 6px; margin-left: auto; }
.step > pre {
    display: none;
    margin-top: 10px;
    padding: 12px 14px;
    max-height: 420px;
    overflow: auto;
    border-radius: var(--radius-sm);
    font-size: 13px;
    line-height: 1.55;
    background: var(--code-bg);
    color: var(--code-fg);
    border: 1px solid var(--code-border);
}
.step > pre.result-pre, .step > pre.result-fail { white-space: pre-wrap; word-break: break-word; }
.type-HAPPY_PATH_E2E { background: var(--success-soft); color: var(--success-text); border-color: var(--success-border); }
.type-BOUNDARY_NEGATIVE { background: var(--warning-soft); color: var(--warning-text); border-color: var(--warning-border); }
.type-SECURITY_PERMUTATION { background: var(--danger-soft); color: var(--danger-text); border-color: var(--danger-border); }
.type-IDEMPOTENCY { background: var(--heal-soft); color: var(--heal-text); border-color: var(--heal-border); }
#suite-progress-pct { font-size: 16px; font-weight: 700; color: var(--primary-text); }
@media (max-width: 720px) {
    .journey__header { flex-direction: column; }
    .step__actions { margin-left: 0; width: 100%; justify-content: flex-end; }
}
"""

DASHBOARD_JS = r"""
let hasValidatedAllInUI = false;
const COLORS = { primary: '#1a73e8', success: '#1e8e3e', danger: '#d93025', warning: '#f9ab00', heal: '#f59e0b' };
const LOG_COLORS = { default: '#e6edf3', fail: '#ff7b72', pass: '#7ee787', heal: '#5fd3bc', info: '#79c0ff' };

function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function setStatus(el, state) {
    if (!el) return;
    el.dataset.state = state;
    el.innerHTML = ICONS[state] || '';
}

function stepLabel(scId, stId) {
    // Human-readable reference for a test row ("#3 GET /products") instead of its raw step id.
    const card = document.getElementById('step_card_' + scId + '_' + stId);
    if (!card) return String(stId || '');
    const parts = [];
    const idx = card.querySelector('.step__idx');
    const method = card.querySelector('.method');
    const path = card.querySelector('.step__path');
    if (idx) parts.push('#' + idx.textContent.trim());
    if (method) parts.push(method.textContent.trim());
    if (path) parts.push(path.textContent.trim());
    return parts.length ? parts.join(' ') : String(stId || '');
}

function setBusy(btn, busy, label) {
    if (!btn) return;
    if (busy) {
        if (!btn.dataset.idle) btn.dataset.idle = btn.innerHTML;
        btn.innerHTML = '<span class="spinner"></span><span>' + esc(label || 'Running…') + '</span>';
        btn.disabled = true;
    } else {
        if (btn.dataset.idle) btn.innerHTML = btn.dataset.idle;
        btn.disabled = false;
    }
}

function setAlert(el, kind, innerHtml, iconName) {
    if (!el) return;
    el.className = 'alert alert-' + kind;
    el.innerHTML = (ICONS[iconName || 'info'] || '') + '<div>' + innerHtml + '</div>';
    el.style.display = 'flex';
}

function markHealed(scId, stId, fixedStep) {
    const codeDiv = document.getElementById('code_' + scId + '_' + stId);
    if (codeDiv && fixedStep) {
        codeDiv.innerText = JSON.stringify(fixedStep, null, 2);
        codeDiv.classList.add('is-healed');
    }
    const badges = document.getElementById('badges_' + scId + '_' + stId);
    if (badges && !document.getElementById('healed_badge_' + scId + '_' + stId)) {
        const badge = document.createElement('span');
        badge.id = 'healed_badge_' + scId + '_' + stId;
        badge.className = 'badge badge-heal';
        badge.title = 'Patched by TestValidatorAgent';
        badge.innerHTML = ICONS.sparkles + 'Healed';
        badges.appendChild(badge);
    }
}

function formatStepResult(data, extraPrefix) {
    const diagLine = data.diagnosis ? `[Diagnosis]: ${data.diagnosis}\n\n` : '';
    const reqLine = data.request_url ? `Request: ${data.request_method || 'GET'} ${data.request_url}\n` : '';
    const bodyStr = (typeof data.body === 'object') ? JSON.stringify(data.body, null, 2) : String(data.body || '');
    const errLine = data.error ? `Error: ${data.error}\n` : '';
    return `${extraPrefix || ''}${diagLine}${reqLine}Status: ${data.status_code}\n${errLine}Body: ${bodyStr}`;
}

function countStates(prefix) {
    let pass = 0;
    let fail = 0;
    document.querySelectorAll('[id^="' + prefix + '"]').forEach((el) => {
        if (el.dataset.state === 'pass') pass++;
        else if (el.dataset.state === 'fail') fail++;
    });
    return { pass, fail, done: pass + fail };
}

function collectPassedStepsByScenario() {
    const map = {};
    document.querySelectorAll('[id^="status_st_"]').forEach((el) => {
        if (el.dataset.state === 'pass') {
            const scId = el.dataset.scenarioId;
            const stId = el.dataset.stepId;
            if (scId && stId) {
                if (!map[scId]) map[scId] = [];
                map[scId].push(stId);
            }
        }
    });
    return map;
}

function getSelectedCommitScope() {
    const checked = document.querySelector('input[name="modalGhTestScope"]:checked');
    return checked ? checked.value : 'all';
}

function updateModalScopeSelection() {
    const scope = getSelectedCommitScope();
    const optAll = document.getElementById('scopeOptAll');
    const optSucc = document.getElementById('scopeOptSuccessful');
    const hintEl = document.getElementById('modalGhScopeHint');
    const passedCount = countStates('status_st_').pass;

    if (optAll) optAll.classList.toggle('is-selected', scope === 'all');
    if (optSucc) optSucc.classList.toggle('is-selected', scope === 'successful');

    if (hintEl) {
        if (scope === 'successful' && passedCount === 0) {
            hintEl.style.display = 'block';
            hintEl.className = 'field__hint is-danger';
            hintEl.innerHTML = '<strong>No successful tests recorded yet.</strong> Run journeys on the dashboard first so passing tests are captured, or choose <em>Commit all tests</em>.';
        } else if (scope === 'successful') {
            hintEl.style.display = 'block';
            hintEl.className = 'field__hint is-success';
            hintEl.innerHTML = `Only the <strong>${passedCount}</strong> executed &amp; passing test${passedCount === 1 ? '' : 's'} will be committed to GitHub.`;
        } else {
            hintEl.style.display = 'none';
            hintEl.innerHTML = '';
        }
    }
}

function refreshStats() {
    const journeys = countStates('status_sc_');
    const tests = countStates('status_st_');
    const setText = (id, value) => { const el = document.getElementById(id); if (el) el.innerText = value; };
    const pct = (c) => `${Math.round((c.pass / c.done) * 100)}%`;
    setText('stat-passed', journeys.done ? journeys.pass : '–');
    setText('stat-passed-tests', tests.done ? tests.pass : '–');
    setText('stat-failed', journeys.done ? journeys.fail : '–');
    setText('stat-failed-tests', tests.done ? tests.fail : '–');
    setText('stat-score', journeys.done ? pct(journeys) : '–');
    setText('stat-score-tests', tests.done ? pct(tests) : '–');
    setText('dl-passed-badge', `${tests.pass} passed`);
    setText('modalScopePassedBadge', `${tests.pass} passed`);
    const jm = document.getElementById('stat-score-metric');
    const tm = document.getElementById('stat-score-tests-metric');
    if (jm) jm.title = journeys.done ? `${journeys.pass} of ${journeys.done} journeys run passed` : 'No journeys run yet';
    if (tm) tm.title = tests.done ? `${tests.pass} of ${tests.done} tests run passed` : 'No tests run yet';
    updateModalScopeSelection();
}

function restoreInitialExecutionResults() {
    if (typeof INITIAL_EXEC_RESULTS !== 'object' || !INITIAL_EXEC_RESULTS) return;
    const scMap = INITIAL_EXEC_RESULTS.scenarios || {};
    let anyRestored = false;
    for (const [scId, scInfo] of Object.entries(scMap)) {
        if (!scInfo || typeof scInfo !== 'object') continue;
        const scIcon = document.getElementById('status_sc_' + scId);
        if (scIcon && (scInfo.status === 'SUCCESS' || scInfo.status === 'FAILED')) {
            setStatus(scIcon, scInfo.status === 'SUCCESS' ? 'pass' : 'fail');
            anyRestored = true;
        }
        const steps = scInfo.steps || {};
        for (const [stId, stStatus] of Object.entries(steps)) {
            const stIcon = document.getElementById('status_st_' + scId + '_' + stId);
            if (stIcon && (stStatus === 'SUCCESS' || stStatus === 'FAILED')) {
                setStatus(stIcon, stStatus === 'SUCCESS' ? 'pass' : 'fail');
                anyRestored = true;
            }
        }
    }
    if (anyRestored) {
        refreshStats();
    }
}

function toggleDownloadMenu(e) {
    if (e) e.stopPropagation();
    const dd = document.getElementById('downloadTestsDropdown');
    const btn = document.getElementById('btnDownloadTests');
    if (!dd) return;
    const isOpen = dd.classList.toggle('is-open');
    if (btn) btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
}

function closeDownloadMenu() {
    const dd = document.getElementById('downloadTestsDropdown');
    const btn = document.getElementById('btnDownloadTests');
    if (dd) dd.classList.remove('is-open');
    if (btn) btn.setAttribute('aria-expanded', 'false');
}

async function downloadTests(scope) {
    closeDownloadMenu();
    const normalizedScope = (scope === 'successful') ? 'successful' : 'all';
    const passedSteps = collectPassedStepsByScenario();
    const passedCount = countStates('status_st_').pass;

    if (normalizedScope === 'successful' && passedCount === 0) {
        showToast("No passed tests yet. Run journeys first to download successful tests, or choose 'Download all tests'.", 'danger');
        return;
    }

    try {
        const res = await fetch(`/download_tests/${encodeURIComponent(SESSION_ID)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                scope: normalizedScope,
                passed_steps: passedSteps
            })
        });
        if (!res.ok) {
            let errMsg = `Download failed (HTTP ${res.status})`;
            try {
                const errData = await res.json();
                if (errData && errData.error) errMsg = errData.error;
            } catch (_) {}
            showToast(errMsg, 'danger');
            return;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = normalizedScope === 'successful'
            ? `test_suite_${SESSION_ID}_passed.py`
            : `test_suite_${SESSION_ID}.py`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        showToast(
            normalizedScope === 'successful'
                ? `Downloaded ${passedCount} successful test${passedCount === 1 ? '' : 's'} (test_suite_${SESSION_ID}_passed.py)`
                : `Downloaded all tests (test_suite_${SESSION_ID}.py)`,
            'success'
        );
    } catch (err) {
        showToast('Failed to download tests: ' + (err.message || err), 'danger');
    }
}

async function consumeSSE(url, onEvent) {
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
            const trimmed = line.trim();
            if (trimmed.startsWith('data: ')) {
                try {
                    const data = JSON.parse(trimmed.slice(6));
                    let logColor = LOG_COLORS.default;
                    if (data.status === 'FAILED' || data.event === 'HEALING_FAILED') logColor = LOG_COLORS.fail;
                    else if (data.status === 'SUCCESS' || data.event === 'HEALING_APPLIED') logColor = LOG_COLORS.pass;
                    else if (data.event === 'HEALING_START') logColor = LOG_COLORS.heal;
                    appendLog(`[${data.event}] ${data.name || data.step_id || data.scenario_id || ''} ${data.error || ''}`, logColor);
                    onEvent(data);
                } catch (e) {
                    console.error("Failed to parse SSE event:", e, trimmed);
                }
            }
        }
    }
}

function renderHealingDiagnosis(healingBox, scId, info, isPartialFix) {
    if (!healingBox) return;
    const diags = (info && info.diagnoses) || [];
    if (diags.length > 1) {
        const title = isPartialFix
            ? `<strong>Self-healing patch applied, but ${diags.length} tests still failed:</strong>`
            : `<strong>Self-healing diagnosis (${diags.length} failed tests):</strong>`;
        const listHtml = diags.map(d => {
            const label = stepLabel(scId, d.step_id);
            return `<div style="margin-top: 6px; padding: 8px 12px; background: rgba(0,0,0,0.03); border-radius: 6px; border-left: 3px solid var(--danger, #ef4444); font-size: 0.9em; line-height: 1.4;">
                <div><strong>Failed test:</strong> <code>${esc(label)}</code> · <span style="opacity: 0.85;">No automated patch applied</span></div>
                <div style="margin-top: 3px;">${esc(d.reasoning)}</div>
            </div>`;
        }).join('');
        setAlert(healingBox, isPartialFix ? 'warning' : 'danger', `${title}<div class="healing-diagnoses-list" style="margin-top: 6px; display: flex; flex-direction: column; gap: 4px;">${listHtml}</div>`, 'bot');
    } else if (diags.length === 1) {
        const d = diags[0];
        const failTarget = `<br><small><strong>Failed test:</strong> <code>${esc(stepLabel(scId, d.step_id))}</code> · No automated patch applied</small>`;
        const prefix = isPartialFix ? '<strong>Self-healing patch applied, but re-run failed.</strong> ' : '<strong>Self-healing diagnosis.</strong> ';
        setAlert(healingBox, isPartialFix ? 'warning' : 'danger', `${prefix}${esc(d.reasoning)}${failTarget}`, 'bot');
    } else {
        const failTarget = (info && info.target_step)
            ? `<br><small><strong>Failed test:</strong> <code>${esc(stepLabel(scId, info.target_step))}</code> · No automated patch applied</small>`
            : '';
        const prefix = isPartialFix ? '<strong>Self-healing patch applied, but re-run failed.</strong> ' : '<strong>Self-healing diagnosis.</strong> ';
        setAlert(healingBox, isPartialFix ? 'warning' : 'danger', `${prefix}${esc((info && info.reasoning) || 'No safe patch found.')}${failTarget}`, 'bot');
    }
}

function handleScenarioEvent(data, scenarioId) {
    const scId = data.scenario_id || scenarioId;
    const icon = document.getElementById('status_sc_' + scId);
    const pBox = document.getElementById('progress_box_' + scId);
    const pBar = document.getElementById('progress_bar_' + scId);
    const pLabel = document.getElementById('progress_label_' + scId);
    const pVal = document.getElementById('progress_val_' + scId);
    const healingBox = document.getElementById('healing_box_' + scId);

    if (pBox) pBox.style.display = 'block';

    if (data.event === 'JOURNEY_START') {
        setStatus(icon, 'running');
        if (pBar) {
            pBar.style.width = '0%';
            pBar.style.background = COLORS.primary;
            pBar.classList.remove('healing-active');
        }
        if (pVal) pVal.innerText = '0%';
        if (pLabel) pLabel.innerText = `Starting ${data.name || 'journey'} (${data.total_steps || 0} tests)…`;
    } else if (data.event === 'STEP_START') {
        const stIcon = document.getElementById('status_st_' + scId + '_' + data.step_id);
        setStatus(stIcon, 'running');
        if (pLabel) {
            pLabel.innerText = `Test ${data.step_index}/${data.total_steps}: ${data.method} ${data.path_template}`;
        }
    } else if (data.event === 'STEP_PROGRESS') {
        const stId = data.step_id;
        const stIcon = document.getElementById('status_st_' + scId + '_' + stId);
        const stDiv = document.getElementById('result_' + scId + '_' + stId);
        const stBtn = document.getElementById('btn_resp_' + scId + '_' + stId);
        const passed = data.status === 'SUCCESS';

        const pct = Math.round((data.step_index / data.total_steps) * 100);
        if (pBar) {
            pBar.style.width = pct + '%';
            pBar.classList.remove('healing-active');
            pBar.style.background = COLORS.primary;
        }
        if (pVal) pVal.innerText = pct + '%';
        if (pLabel) {
            pLabel.innerText = `Completed test ${data.step_index}/${data.total_steps} (${data.method} ${data.path_template}) — ${passed ? 'passed' : 'failed'}`;
        }

        setStatus(stIcon, passed ? 'pass' : 'fail');
        if (stDiv) {
            stDiv.className = passed ? "result-pre" : "result-fail";
            stDiv.innerText = formatStepResult(data);
            stDiv.style.display = 'none';
        }
        if (stBtn) stBtn.style.display = 'inline-flex';
        refreshStats();
    } else if (data.event === 'HEALING_START') {
        if (pBar) {
            pBar.classList.add('healing-active');
            pBar.style.background = COLORS.heal;
        }
        if (pLabel) {
            pLabel.innerHTML = `<strong>Self-healing:</strong> TestValidatorAgent is analyzing the failure on test <code>${esc(stepLabel(scId, data.step_id))}</code>…`;
        }
        setAlert(healingBox, 'heal', `<strong>Attempting self-healing.</strong> TestValidatorAgent is analyzing the failure on test <code>${esc(stepLabel(scId, data.step_id))}</code>…`, 'sparkles');
    } else if (data.event === 'HEALING_APPLIED') {
        if (pBar) {
            pBar.classList.remove('healing-active');
            pBar.style.background = COLORS.primary;
        }
        if (pLabel) {
            pLabel.innerHTML = `<strong>Fix applied.</strong> Re-running journey to verify the patch…`;
        }
        setAlert(healingBox, 'success', `<strong>Self-healing fix applied.</strong> ${esc(data.reasoning)}<br><small><strong>Target test:</strong> <code>${esc(stepLabel(scId, data.target_step))}</code> · re-running verification…</small>`, 'sparkles');
        if (data.target_step) {
            markHealed(scId, data.target_step, data.fixed_step);
        }
    } else if (data.event === 'HEALING_FAILED') {
        if (pBar) {
            pBar.classList.remove('healing-active');
            pBar.style.background = COLORS.danger;
        }
        renderHealingDiagnosis(healingBox, scId, data, false);
    } else if (data.event === 'JOURNEY_COMPLETE') {
        const passed = data.status === 'SUCCESS';
        setStatus(icon, passed ? 'pass' : 'fail');
        if (pBar) {
            pBar.classList.remove('healing-active');
            pBar.style.width = '100%';
            pBar.style.background = passed ? COLORS.success : COLORS.danger;
        }
        if (pVal) pVal.innerText = '100%';
        if (pLabel) {
            pLabel.innerText = passed ? 'Journey completed successfully' : (data.error ? `Journey failed: ${data.error}` : 'Journey failed');
        }

        if (data.healing && healingBox) {
            if (data.healing.applied) {
                const ok = data.healing.re_run_success;
                if (ok) {
                    setAlert(healingBox, 'success', `<strong>Self-healing fix applied.</strong> ${esc(data.healing.reasoning)}<br><small><strong>Target test:</strong> <code>${esc(stepLabel(scId, data.healing.target_step))}</code> · <strong>Outcome:</strong> Fixed and passed</small>`, 'sparkles');
                } else {
                    renderHealingDiagnosis(healingBox, scId, data.healing, true);
                }
            } else if (data.healing.attempted) {
                renderHealingDiagnosis(healingBox, scId, data.healing, false);
            }
        }

        if (data.steps) {
            for (const [st_id, st_data] of Object.entries(data.steps)) {
                const st_icon = document.getElementById('status_st_' + scId + '_' + st_id);
                const st_div = document.getElementById('result_' + scId + '_' + st_id);
                const st_btn = document.getElementById('btn_resp_' + scId + '_' + st_id);
                const stepPassed = st_data.status === 'SUCCESS';
                setStatus(st_icon, stepPassed ? 'pass' : 'fail');
                if (st_div) {
                    st_div.className = stepPassed ? "result-pre" : "result-fail";
                    st_div.innerText = formatStepResult(st_data);
                    st_div.style.display = 'none';
                }
                if (st_btn) st_btn.style.display = 'inline-flex';
            }
        }
        refreshStats();
    }
}

async function runAll() {
    const btn = document.getElementById('runAllBtn');
    const suiteBox = document.getElementById('suite-progress-container');
    const suiteBar = document.getElementById('suite-progress-bar');
    const suiteText = document.getElementById('suite-progress-text');
    const suitePct = document.getElementById('suite-progress-pct');
    const suiteBadge = document.getElementById('suite-stats-badge');
    const statsDiv = document.getElementById('global-stats');
    const isHealing = document.getElementById('toggleSelfHealing').checked;

    setBusy(btn, true, 'Running (4× parallel)…');

    suiteBox.style.display = 'block';
    suiteBar.style.width = '0%';
    suiteBar.style.background = COLORS.primary;
    suitePct.innerText = '0%';
    suiteText.innerText = 'Initializing parallel runner…';
    suiteBadge.className = 'badge badge-neutral';
    suiteBadge.innerText = '0 completed';
    statsDiv.style.display = 'none';

    try {
        await consumeSSE(`/stream_suite/${encodeURIComponent(SESSION_ID)}?healing=${isHealing}&concurrency=4`, (data) => {
            if (data.event === 'SUITE_COMPLETE') {
                hasValidatedAllInUI = true;
                const noteBanner = document.getElementById('ui-workflow-note');
                setAlert(noteBanner, 'success', '<strong>All journeys executed from the dashboard.</strong> Validated and self-healed updates are saved — you can now safely download <code>test_suite.py</code> or push to GitHub.', 'shield');

                const isFullPass = data.score === 100;
                const kind = isFullPass ? 'success' : (data.passed > 0 ? 'warning' : 'danger');
                setAlert(statsDiv, kind, `<strong>Suite execution complete.</strong> ${data.passed} passed · ${data.failed} failed · ${data.score}% pass rate across ${data.total} journeys.`, isFullPass ? 'check-circle' : 'alert');

                suiteBar.style.width = '100%';
                suitePct.innerText = '100%';
                suiteBar.style.background = isFullPass ? COLORS.success : (data.passed > 0 ? COLORS.warning : COLORS.danger);
                suiteText.innerText = `Finished all ${data.total} journeys (${data.score}% pass rate)`;
                suiteBadge.className = 'badge ' + (data.failed === 0 ? 'badge-success' : 'badge-danger');
                suiteBadge.innerText = `${data.passed} passed · ${data.failed} failed`;
                refreshStats();
            } else {
                handleScenarioEvent(data, data.scenario_id);

                if (data.event === 'JOURNEY_COMPLETE' && data.total_journeys) {
                    const completed = data.completed_journeys;
                    const total = data.total_journeys;
                    const pct = Math.round((completed / total) * 100);
                    suiteBar.style.width = pct + '%';
                    suitePct.innerText = pct + '%';
                    suiteText.innerText = `Completed ${completed} of ${total} journeys…`;
                    suiteBadge.className = 'badge ' + (data.failed > 0 ? 'badge-danger' : 'badge-success');
                    suiteBadge.innerText = `${data.passed} passed · ${data.failed} failed`;
                }
            }
        });
    } catch (e) {
        showToast("Error executing test suite: " + (e.message || e), 'danger');
    } finally {
        setBusy(btn, false);
    }
}

async function runScenario(scenarioId) {
    const icon = document.getElementById('status_sc_' + scenarioId);
    const btn = document.getElementById('btn_sc_' + scenarioId);
    const pBox = document.getElementById('progress_box_' + scenarioId);
    const pBar = document.getElementById('progress_bar_' + scenarioId);
    const pLabel = document.getElementById('progress_label_' + scenarioId);
    const pVal = document.getElementById('progress_val_' + scenarioId);
    const healingBox = document.getElementById('healing_box_' + scenarioId);
    const isHealing = document.getElementById('toggleSelfHealing').checked;

    setStatus(icon, 'running');
    setBusy(btn, true, 'Running…');
    if (pBox) {
        pBox.style.display = 'block';
        pBar.style.width = '0%';
        pBar.style.background = COLORS.primary;
        pBar.classList.remove('healing-active');
        pVal.innerText = '0%';
        pLabel.innerText = 'Starting journey…';
    }
    if (healingBox) {
        healingBox.style.display = 'none';
        healingBox.innerHTML = '';
    }

    try {
        await consumeSSE(`/stream_scenario/${encodeURIComponent(SESSION_ID)}/${encodeURIComponent(scenarioId)}?healing=${isHealing}`, (data) => {
            handleScenarioEvent(data, scenarioId);
        });
    } catch (e) {
        setStatus(icon, 'fail');
        setAlert(healingBox, 'danger', `<strong>Execution error.</strong> ${esc(e.message || e)}`, 'alert');
        refreshStats();
    } finally {
        setBusy(btn, false);
    }
}

async function runStep(scenarioId, stepId) {
    const icon = document.getElementById('status_st_' + scenarioId + '_' + stepId);
    const resultDiv = document.getElementById('result_' + scenarioId + '_' + stepId);
    const respBtn = document.getElementById('btn_resp_' + scenarioId + '_' + stepId);

    setStatus(icon, 'running');
    if (resultDiv) resultDiv.style.display = 'none';

    const isHealing = document.getElementById('toggleSelfHealing').checked;
    appendLog(`[STEP_START] Executing single step: ${stepId}…`, LOG_COLORS.info);

    let data;
    try {
        const res = await fetch(`/execute_single/${encodeURIComponent(SESSION_ID)}/step/${encodeURIComponent(scenarioId)}/${encodeURIComponent(stepId)}?healing=${isHealing}`, { method: 'POST' });
        data = await res.json();
    } catch (e) {
        setStatus(icon, 'fail');
        appendLog(`[STEP_FAILED] ${stepId} request error: ${e.message || e}`, LOG_COLORS.fail);
        showToast('Test execution failed: ' + (e.message || e), 'danger');
        refreshStats();
        return;
    }

    if (resultDiv) resultDiv.style.display = 'block';
    if (respBtn) respBtn.style.display = 'inline-flex';

    if (data.healing && data.healing.applied && data.fixed_step) {
        appendLog(`[HEALING_APPLIED] Fixed step: ${stepId} - ${data.healing.reasoning}`, LOG_COLORS.heal);
        markHealed(scenarioId, stepId, data.fixed_step);
    } else if (data.healing && data.healing.attempted && !data.healing.applied && data.healing.reasoning) {
        appendLog(`[HEALING_FAILED] ${stepId} - ${data.healing.reasoning}`, LOG_COLORS.warn);
    }

    const reqLine = data.request_url ? `Request: ${data.request_method || 'GET'} ${data.request_url}\n` : '';
    const healLine = (data.healing && data.healing.applied)
        ? `[Self-healed]: ${data.healing.reasoning}\n\n`
        : ((data.healing && data.healing.attempted && !data.healing.applied && data.healing.reasoning)
            ? `[Self-healing diagnosis]: ${data.healing.reasoning}\n\n`
            : '');
    if (data.status === 'SUCCESS') {
        appendLog(`[STEP_SUCCESS] ${stepId} passed. Status: ${data.response_status}`, LOG_COLORS.pass);
        setStatus(icon, 'pass');
        if (resultDiv) {
            resultDiv.className = "result-pre";
            resultDiv.innerText = `${healLine}${reqLine}Status: ${data.response_status}\nBody: ${JSON.stringify(data.response_body, null, 2)}`;
        }
    } else {
        appendLog(`[STEP_FAILED] ${stepId} failed. Error: ${data.error}`, LOG_COLORS.fail);
        setStatus(icon, 'fail');
        if (resultDiv) {
            resultDiv.className = "result-fail";
            resultDiv.innerText = `${healLine}${reqLine}Error: ${data.error}`;
        }
    }
    refreshStats();
}

function toggleCode(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = el.style.display === 'block' ? 'none' : 'block';
}

function toggleConsoleLogs() {
    const container = document.getElementById('consoleLogsContainer');
    const label = document.getElementById('btnToggleLogsLabel');
    const isHidden = container.style.display === 'none' || !container.style.display;
    container.style.display = isHidden ? 'block' : 'none';
    if (label) label.innerText = isHidden ? 'Hide logs' : 'Show logs';
}

function clearConsoleLogs() {
    const logsText = document.getElementById('consoleLogsText');
    if (logsText) logsText.innerHTML = '';
}

function appendLog(msg, color) {
    const logsText = document.getElementById('consoleLogsText');
    if (!logsText) return;
    const time = new Date().toLocaleTimeString();
    const line = document.createElement('div');
    line.innerHTML = `<span class="console__time">[${esc(time)}]</span> <span style="color:${color || LOG_COLORS.default}">${esc(msg)}</span>`;
    logsText.appendChild(line);
    logsText.scrollTop = logsText.scrollHeight;
}

let toastTimer = null;
function showToast(msg, kind) {
    const t = document.getElementById('dashToast');
    if (!t) return;
    t.className = 'toast' + (kind ? ' toast-' + kind : '');
    t.innerText = msg;
    t.style.display = 'flex';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.style.display = 'none'; }, 3500);
}

const SESSION_GH_TOKEN_KEY = 'oas_gh_token_' + SESSION_ID;

function updateModalTokenHint() {
    const modeEl = document.getElementById('modalGhMode');
    const tokenEl = document.getElementById('modalGhToken');
    const hintEl = document.getElementById('modalGhTokenHint');
    if (!modeEl || !tokenEl || !hintEl) return;
    const isPr = modeEl.value === 'pr';
    const hasTypedToken = tokenEl.value.trim().length > 0;
    tokenEl.placeholder = 'ghp_… or github_pat_… (required)';
    if (!hasTypedToken) {
        hintEl.className = 'field__hint is-danger';
        hintEl.innerHTML = isPr
            ? '<strong>Required:</strong> a GitHub token with <code>Contents: Read &amp; write</code> and <code>Pull requests: Read &amp; write</code> permissions. Isolated strictly to this session.'
            : '<strong>Required:</strong> a GitHub token with <code>Contents: Read &amp; write</code> (or classic <code>repo</code>) permissions. Isolated strictly to this session.';
    } else {
        tokenEl.classList.remove('is-invalid');
        hintEl.className = 'field__hint is-success';
        hintEl.innerHTML = 'Token provided for this session only (kept in browser <code>sessionStorage</code> for session <code>' + esc(SESSION_ID.slice(0, 8)) + '…</code>; never shared across users).';
    }
}

async function checkGithubSyncStatus() {
    try {
        const res = await fetch(`/github/status/${encodeURIComponent(SESSION_ID)}`);
        const data = await res.json();
        const banner = document.getElementById('github-sync-banner');
        const btnText = document.getElementById('btnPushGithubText');
        const btn = document.getElementById('btnPushGithub');

        if (data && data.pushed) {
            if (banner) {
                banner.style.display = 'flex';
                document.getElementById('gh-sync-folder-badge').innerText = data.folder || 'tests/oas-tests-agent/...';
                document.getElementById('gh-sync-branch-badge').innerText = data.branch || 'main';
                const link = document.getElementById('gh-sync-view-link');
                link.href = data.url || '#';
                let label = 'View commit on GitHub';
                if (data.type === 'pr') label = 'View pull request';
                else if (data.type === 'pr_compare') label = 'Open pull request on GitHub';
                link.innerHTML = esc(label) + ' ' + ICONS.external;
            }
            if (btnText) btnText.innerText = 'Push updated tests';
            if (btn) {
                btn.classList.remove('btn-dark');
                btn.classList.add('btn-success');
                btn.title = 'Tests previously pushed. Click to re-sync or push healed updates to GitHub.';
            }
        }
    } catch (e) {
        console.error('Failed to check GitHub sync status:', e);
    }
}

window.addEventListener('DOMContentLoaded', () => {
    restoreInitialExecutionResults();
    checkGithubSyncStatus();
    const modal = document.getElementById('githubModal');
    if (modal) {
        modal.addEventListener('click', (e) => { if (e.target === modal) closeGithubModal(); });
    }
    document.addEventListener('click', (e) => {
        const dd = document.getElementById('downloadTestsDropdown');
        if (dd && dd.classList.contains('is-open') && !dd.contains(e.target)) {
            closeDownloadMenu();
        }
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeDownloadMenu();
            if (modal && modal.style.display === 'flex') closeGithubModal();
        }
    });
});

async function openGithubModal() {
    const modal = document.getElementById('githubModal');
    const repoInput = document.getElementById('modalGhRepo');
    const branchInput = document.getElementById('modalGhBranch');
    const folderInput = document.getElementById('modalGhFolder');
    const tokenInput = document.getElementById('modalGhToken');
    const resultDiv = document.getElementById('modalGhResult');
    const btn = document.getElementById('modalBtnConfirmPush');
    const btnText = document.getElementById('modalBtnText');
    const modalPreTestWarning = document.getElementById('modalPreTestWarning');

    resultDiv.style.display = 'none';
    if (modalPreTestWarning) {
        modalPreTestWarning.style.display = hasValidatedAllInUI ? 'none' : 'flex';
    }

    if (btn) btn.disabled = false;
    if (btnText) btnText.innerText = 'Push to GitHub';

    if (tokenInput && !tokenInput.value.trim()) {
        const sessionTok = sessionStorage.getItem(SESSION_GH_TOKEN_KEY) || '';
        if (sessionTok) {
            tokenInput.value = sessionTok;
        }
    }

    try {
        const cfgRes = await fetch(`/github/config?session_id=${encodeURIComponent(SESSION_ID)}`);
        const cfg = await cfgRes.json();
        if (cfg) {
            if (repoInput && !repoInput.value) repoInput.value = cfg.repo || DEFAULT_REPO;
        }
    } catch (e) {}

    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const ts = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}_${pad(now.getHours())}-${pad(now.getMinutes())}-${pad(now.getSeconds())}`;
    folderInput.value = `tests/oas-tests-agent/${ts}`;
    branchInput.value = `oas-tests-agent/${ts}`;
    refreshStats();
    updateModalScopeSelection();
    updateModalTokenHint();

    modal.style.display = 'flex';
    setTimeout(() => { if (tokenInput && !tokenInput.value) tokenInput.focus(); }, 50);
}

function closeGithubModal() {
    document.getElementById('githubModal').style.display = 'none';
}

async function executeGithubPush() {
    const repo = document.getElementById('modalGhRepo').value.trim();
    const branch = document.getElementById('modalGhBranch').value.trim();
    const tokenEl = document.getElementById('modalGhToken');
    const token = tokenEl.value.trim();
    const folder = document.getElementById('modalGhFolder').value.trim();
    const mode = document.getElementById('modalGhMode').value;
    const testScope = getSelectedCommitScope();
    const passedSteps = collectPassedStepsByScenario();
    const passedCount = countStates('status_st_').pass;
    const btn = document.getElementById('modalBtnConfirmPush');
    const btnText = document.getElementById('modalBtnText');
    const resultDiv = document.getElementById('modalGhResult');

    if (testScope === 'successful' && passedCount === 0) {
        setAlert(resultDiv, 'danger', '<strong>No successful tests to commit yet.</strong> Run journeys on the Execution Dashboard first so passing tests are recorded, or select <em>Commit all tests</em>.', 'alert');
        return;
    }

    if (!token) {
        tokenEl.classList.add('is-invalid');
        tokenEl.focus();
        setAlert(resultDiv, 'danger', '<strong>GitHub token required.</strong> Enter your GitHub Personal Access Token (<code>ghp_…</code> or <code>github_pat_…</code>) before pushing.', 'lock');
        updateModalTokenHint();
        return;
    }

    sessionStorage.setItem(SESSION_GH_TOKEN_KEY, token);

    btn.disabled = true;
    btnText.innerText = mode === 'pr' ? 'Pushing & creating PR…' : 'Pushing to GitHub…';
    const scopeLabel = testScope === 'successful' ? `successful tests (${passedCount} passed)` : 'all generated tests';
    setAlert(resultDiv, 'info', `<strong>Committing and pushing ${esc(scopeLabel)} to GitHub.</strong><br><code>${esc(folder)}</code>`, 'loader');

    try {
        const res = await fetch(`/github/push/${encodeURIComponent(SESSION_ID)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                repo: repo,
                branch: branch,
                token: token,
                folder: folder,
                create_pr: mode === 'pr',
                spec_file: SPEC_FILE,
                test_scope: testScope,
                passed_steps: passedSteps
            })
        });
        const data = await res.json();

        if (data.status === 'success') {
            const committedScopeDesc = data.test_scope === 'successful'
                ? `Successful tests only (${esc(data.tests_committed ?? passedCount)} passed)`
                : `All tests (${esc(data.tests_committed ?? '')})`;
            const details = `<div style="margin-top:6px;"><strong>Scope:</strong> ${committedScopeDesc}<br><strong>Folder:</strong> <code>${esc(data.folder)}</code><br><strong>Branch:</strong> <code>${esc(data.branch)}</code></div>`;
            if (data.type === 'pr') {
                setAlert(resultDiv, 'success', `<strong>Pull request created on GitHub.</strong>${details}<a href="${esc(data.url)}" target="_blank" rel="noopener" class="btn btn-success btn-sm" style="margin-top:10px;">View pull request ${ICONS.external}</a>`, 'pull-request');
            } else if (data.type === 'pr_compare') {
                const noteHtml = data.pr_note ? `<div style="margin-top:6px; font-size:13px; opacity:.9;">${esc(data.pr_note)}</div>` : '';
                const commitLinkHtml = data.commit_url ? `<a href="${esc(data.commit_url)}" target="_blank" rel="noopener" class="btn btn-secondary btn-sm">View commit ${ICONS.external}</a>` : '';
                setAlert(resultDiv, 'success', `<strong>Branch pushed. Open the pre-filled pull request:</strong>${details}${noteHtml}<div class="row" style="margin-top:10px;"><a href="${esc(data.url)}" target="_blank" rel="noopener" class="btn btn-success btn-sm">Create pull request on GitHub ${ICONS.external}</a>${commitLinkHtml}</div>`, 'pull-request');
                try { window.open(data.url, '_blank'); } catch (e) {}
            } else {
                setAlert(resultDiv, 'success', `<strong>Successfully pushed tests to GitHub.</strong>${details}<a href="${esc(data.url)}" target="_blank" rel="noopener" class="btn btn-secondary btn-sm" style="margin-top:10px;">View commit on GitHub ${ICONS.external}</a>`, 'check-circle');
            }
            btn.disabled = false;
            btnText.innerText = 'Push again to GitHub';
            checkGithubSyncStatus();
        } else {
            setAlert(resultDiv, 'danger', `<strong>GitHub push failed.</strong><br>${esc(data.message || 'Unknown error occurred')}`, 'alert');
            btn.disabled = false;
            btnText.innerText = 'Retry push to GitHub';
        }
    } catch (e) {
        setAlert(resultDiv, 'danger', `<strong>Network error.</strong><br>${esc(e.message || e)}`, 'alert');
        btn.disabled = false;
        btnText.innerText = 'Retry push to GitHub';
    }
}
"""

_SCENARIO_TYPE_LABELS = {
    "HAPPY_PATH_E2E": "Happy path",
    "BOUNDARY_NEGATIVE": "Negative",
    "SECURITY_PERMUTATION": "Security",
    "IDEMPOTENCY": "Idempotency",
}


def _esc(value: Any) -> str:
    """HTML-escapes any value (None renders as an empty string) for safe template interpolation."""
    return html.escape("" if value is None else str(value))


def _is_fallback_scenario(sc: dict[str, Any]) -> bool:
    """True when the journey came from the deterministic fallback builder (Gemini synthesis degraded for its domain)."""
    sc_id = str(sc.get("scenario_id") or "")
    if sc_id.startswith("fallback_"):
        return True
    step_ids = [str(s.get("step_id") or "") for s in (sc.get("test_steps") or [])]
    return sc_id.startswith("neg_") and bool(step_ids) and all(s.startswith(("fallback_", "neg_")) for s in step_ids)


def _render_step_card(sc_id: str, index: int, step: dict[str, Any], phase: str) -> str:
    """Renders a single step row (request/response toggles, run action) for the Execution Dashboard."""
    st_id = step.get("step_id", str(index))
    st_json = html.escape(json.dumps(step, indent=2), quote=False)
    code_div_id = f"code_{sc_id}_{st_id}"
    method = str(step.get("method") or "").upper()
    phase_html = f'<span class="step__phase">{_esc(phase)}</span>' if phase != "test" else ""
    expected = step.get("expected_status")
    expect_html = (
        f'<span class="step__expect" title="Expected HTTP status">&rarr; {_esc(expected)}</span>'
        if expected is not None
        else ""
    )
    return f"""
            <div class="step" id="step_card_{sc_id}_{st_id}">
                <div class="step__row">
                    <span class="step__idx" title="Test ID: {_esc(st_id)}">{index + 1}</span>
                    <span id="status_st_{sc_id}_{st_id}" class="status-icon" data-scenario-id="{_esc(sc_id)}" data-step-id="{_esc(st_id)}"></span>
                    <span class="method method-{_esc(method)}">{_esc(method)}</span>
                    <div class="step__main">
                        <span class="step__path">{_esc(step.get("path_template"))}</span>
                        {expect_html}
                        {phase_html}
                        <span id="badges_{sc_id}_{st_id}" class="row" style="gap:4px;"></span>
                    </div>
                    <div class="step__actions">
                        <button type="button" class="btn btn-ghost btn-sm" onclick="toggleCode('{code_div_id}')">{icon("code")} Request</button>
                        <button type="button" id="btn_resp_{sc_id}_{st_id}" class="btn btn-ghost btn-sm" style="display:none;" onclick="toggleCode('result_{sc_id}_{st_id}')">{icon("terminal")} Response</button>
                        <button type="button" class="btn btn-primary btn-sm" onclick="runStep('{sc_id}', '{st_id}')">{icon("play")} Run</button>
                    </div>
                </div>
                <pre id="{code_div_id}">{st_json}</pre>
                <pre id="result_{sc_id}_{st_id}"></pre>
            </div>
    """


def _render_journey_card(index: int, sc: dict[str, Any]) -> str:
    """Renders a journey (scenario) card with its progress, healing status, and ordered steps."""
    sc_id = sc.get("scenario_id", str(index))
    sc_type = str(sc.get("scenario_type") or "")
    type_label = _SCENARIO_TYPE_LABELS.get(sc_type, sc_type.replace("_", " ").title() if sc_type else "")
    type_html = (
        f'<span class="badge type-{_esc(sc_type)}">{_esc(type_label)}</span>' if type_label else ""
    )
    tags_html = "".join(
        f'<span class="chip">{_esc(tag)}</span>' for tag in (sc.get("tags") or [])[:6]
    )
    fallback_html = (
        '<span class="badge badge-warning" title="Deterministic fallback: Gemini synthesis was unavailable for this domain, so the journey was built directly from the spec">Fallback</span>'
        if _is_fallback_scenario(sc)
        else ""
    )
    phased_steps = (
        [("setup", s) for s in sc.get("setup_steps", [])]
        + [("test", s) for s in sc.get("test_steps", [])]
        + [("teardown", s) for s in sc.get("teardown_steps", [])]
    )
    steps_html = "".join(
        _render_step_card(sc_id, j, st, phase) for j, (phase, st) in enumerate(phased_steps)
    )
    step_count = len(phased_steps)
    return f"""
        <section class="card journey journey--{_esc(sc_type)}" id="card_{sc_id}">
            <div class="journey__header">
                <div style="min-width:0; flex:1;">
                    <div class="journey__title">
                        <span id="status_sc_{sc_id}" class="status-icon"></span>
                        <span class="journey__name">{_esc(sc.get("name"))}</span>
                        {type_html}
                        {fallback_html}
                    </div>
                    <p class="journey__desc">{_esc(sc.get("description", ""))}</p>
                    <div class="journey__meta">
                        <span class="chip">{icon("list")}{step_count} test{"s" if step_count != 1 else ""}</span>
                        {tags_html}
                    </div>
                </div>
                <button type="button" id="btn_sc_{sc_id}" class="btn btn-secondary" onclick="runScenario('{sc_id}')">{icon("play")}<span>Run journey</span></button>
            </div>
            <div class="journey__body">
                <div id="progress_box_{sc_id}" class="journey__progress" style="display:none;">
                    <div class="journey__progress-row">
                        <span id="progress_label_{sc_id}" class="text-2" style="font-weight:600;">Ready</span>
                        <span id="progress_val_{sc_id}" style="font-weight:700; color:var(--primary-text);">0%</span>
                    </div>
                    <div class="progress"><div id="progress_bar_{sc_id}" class="progress__bar"></div></div>
                </div>
                <div id="healing_box_{sc_id}" class="alert alert-heal" style="display:none;"></div>
                <div class="steps-list">
                    {steps_html}
                </div>
            </div>
        </section>
    """


@app.get("/execution_dashboard/{session_id}", response_class=HTMLResponse)
async def execution_dashboard(session_id: str) -> HTMLResponse:
    """Renders the interactive Execution Dashboard for running, validating, and exporting test cases."""
    spa = _try_serve_spa_html()
    if spa is not None:
        return spa
    suite_path = os.path.join(WORKSPACE_DIR, session_id, "tests", "suite.json")
    if not os.path.exists(suite_path):
        return HTMLResponse(
            render_error_page(
                404,
                "Suite not found",
                "No generated test suite exists for this session. Generate tests first, then open the dashboard.",
            ),
            status_code=404,
        )

    with open(suite_path, "r", encoding="utf-8") as f:
        suite = json.load(f)

    status_path = os.path.join(WORKSPACE_DIR, session_id, "status.json")
    spec_file = "weather.yaml"
    api_title = ""
    if os.path.exists(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as sf:
                sdata = json.load(sf)
                result = sdata.get("result") or {}
                spec_file = (
                    sdata.get("spec_file")
                    or result.get("spec_file")
                    or spec_file
                )
                api_title = str(result.get("api_title") or "")
        except (OSError, ValueError):
            pass
    if spec_file == "weather.yaml" and os.path.exists(UPLOAD_DIR):
        for fname in os.listdir(UPLOAD_DIR):
            if fname.startswith(f"{session_id}_"):
                spec_file = fname[len(session_id) + 1:]
                break

    scenarios = suite.get("scenarios", [])
    total_steps = sum(
        len(sc.get("setup_steps", []))
        + len(sc.get("test_steps", []))
        + len(sc.get("teardown_steps", []))
        for sc in scenarios
    )
    base_url = suite.get("target_base_url") or ""
    sid_q = html.escape(quote(session_id, safe=""))
    sid_short = html.escape(session_id[:8])
    journeys_html = "".join(
        _render_journey_card(i, sc) for i, sc in enumerate(scenarios)
    )

    header_actions = f"""
        <label class="switch" title="When enabled, failing tests are analyzed by TestValidatorAgent and patched automatically">
            <input type="checkbox" id="toggleSelfHealing" checked>
            <span class="switch__track"></span>
            {icon("sparkles")}<span>AI self-healing</span>
        </label>
        <button type="button" id="btnToggleLogs" class="btn btn-ghost" onclick="toggleConsoleLogs()">{icon("terminal")}<span id="btnToggleLogsLabel">Show logs</span></button>
        <div class="dropdown" id="downloadTestsDropdown">
            <button type="button" id="btnDownloadTests" class="btn btn-secondary" onclick="toggleDownloadMenu(event)" aria-haspopup="true" aria-expanded="false" title="Download Python Tests (test_suite.py)">
                {icon("download")}<span>Download tests</span>{icon("chevron-down")}
            </button>
            <div class="dropdown__menu" id="downloadTestsMenu" role="menu">
                <button type="button" class="dropdown__item" role="menuitem" onclick="downloadTests('all')">
                    <span class="dropdown__item-title">{icon("download")}<span>Download all tests</span><span class="badge badge-neutral">{total_steps} tests</span></span>
                </button>
                <button type="button" class="dropdown__item" role="menuitem" onclick="downloadTests('successful')">
                    <span class="dropdown__item-title">{icon("check-circle")}<span>Download successful tests</span><span id="dl-passed-badge" class="badge badge-success">0 passed</span></span>
                </button>
            </div>
        </div>
        <button type="button" id="btnPushGithub" class="btn btn-dark" onclick="openGithubModal()">{icon("github")}<span id="btnPushGithubText">Push to GitHub</span></button>
        <button type="button" id="runAllBtn" class="btn btn-primary" onclick="runAll()">{icon("play")}<span>Run all journeys</span></button>
    """

    body = f"""
    <main class="page">
        <div class="page-title hero">
            <div style="min-width:0;">
                <h1>{_esc(api_title) if api_title else "Execution Dashboard"}</h1>
                <p>Run generated journeys against the live API, inspect every request and response, and let the validator agent self-heal failures before you export the suite.</p>
                <div class="page-title__meta">
                    <span class="chip" title="Specification file">{icon("file")}<code>{_esc(spec_file)}</code></span>
                    <span class="chip" title="Target base URL">{icon("globe")}<code>{_esc(base_url) or "—"}</code></span>
                    <span class="chip" title="Session {_esc(session_id)}">{icon("hash")}<code>{sid_short}…</code></span>
                </div>
            </div>
            <div class="row">
                <a class="btn btn-ghost btn-sm" href="/tests/{sid_q}" target="_blank" rel="noopener">{icon("code")} View generated test code</a>
                <a class="btn btn-ghost btn-sm" href="/visualize/{sid_q}" target="_blank" rel="noopener">{icon("branch")} Visualize tests</a>
            </div>
        </div>

        <div class="stats" style="margin-bottom:16px;">
            <div class="stat"><div class="stat__label">Journeys</div><div class="stat__value">{len(scenarios)}</div></div>
            <div class="stat"><div class="stat__label">Tests</div><div class="stat__value">{total_steps}</div></div>
            <div class="stat stat--success"><div class="stat__label">Passed</div>
                <div class="stat__pair">
                    <div class="stat__metric"><div class="stat__value" id="stat-passed">–</div><div class="stat__caption">journeys</div></div>
                    <div class="stat__metric"><div class="stat__value" id="stat-passed-tests">–</div><div class="stat__caption">tests</div></div>
                </div>
            </div>
            <div class="stat stat--danger"><div class="stat__label">Failed</div>
                <div class="stat__pair">
                    <div class="stat__metric"><div class="stat__value" id="stat-failed">–</div><div class="stat__caption">journeys</div></div>
                    <div class="stat__metric"><div class="stat__value" id="stat-failed-tests">–</div><div class="stat__caption">tests</div></div>
                </div>
            </div>
            <div class="stat"><div class="stat__label">Pass rate</div>
                <div class="stat__pair">
                    <div class="stat__metric" id="stat-score-metric"><div class="stat__value" id="stat-score">–</div><div class="stat__caption">journeys</div></div>
                    <div class="stat__metric" id="stat-score-tests-metric"><div class="stat__value" id="stat-score-tests">–</div><div class="stat__caption">tests</div></div>
                </div>
            </div>
        </div>

        <!-- Global Console Logs Pane -->
        <div id="consoleLogsContainer" class="console" style="display:none; margin-bottom:16px;">
            <div class="console__header">
                <span>Execution logs</span>
                <button type="button" class="btn btn-ghost btn-sm" onclick="clearConsoleLogs()">{icon("x")} Clear</button>
            </div>
            <div id="consoleLogsText" class="console__body"></div>
        </div>

        <!-- Recommended UI Validation Workflow Note -->
        <div id="ui-workflow-note" class="alert alert-note" style="margin-bottom:16px;">
            {icon("lightbulb")}
            <div><strong>Validate before you download or push.</strong> Click <strong>Run all journeys</strong> to execute every generated case from the dashboard first. Live execution validates each test and lets <code>TestValidatorAgent</code> self-heal contract mismatches in <code>suite.json</code> and <code>test_suite.py</code> before you export or commit.</div>
        </div>

        <!-- GitHub Sync Banner -->
        <div id="github-sync-banner" class="alert alert-success alert--between" style="display:none; margin-bottom:16px;">
            {icon("github")}
            <div><strong>Synced with GitHub.</strong> Committed to <code id="gh-sync-folder-badge"></code> on branch <code id="gh-sync-branch-badge"></code></div>
            <a id="gh-sync-view-link" href="#" target="_blank" rel="noopener" class="btn btn-sm btn-secondary">View on GitHub {icon("external")}</a>
        </div>

        <div id="suite-progress-container" class="card card--accent" style="display:none; margin-bottom:16px;">
            <div class="card__body" style="padding:16px 20px;">
                <div class="row" style="justify-content:space-between; margin-bottom:10px;">
                    <div class="row" style="gap:10px;">
                        <strong>Suite execution</strong>
                        <span id="suite-progress-text" class="text-2">Executing journeys in parallel…</span>
                    </div>
                    <div class="row" style="gap:12px;">
                        <span id="suite-stats-badge" class="badge badge-neutral">0 completed</span>
                        <span id="suite-progress-pct">0%</span>
                    </div>
                </div>
                <div class="progress progress--lg"><div id="suite-progress-bar" class="progress__bar"></div></div>
            </div>
        </div>

        <div id="global-stats" class="alert alert-success" style="display:none; margin-bottom:16px;"></div>

        <div class="row" style="justify-content:space-between; margin: 8px 0 12px;">
            <h2 class="section-title" style="margin:0;">Journeys ({len(scenarios)})</h2>
            <span class="text-2" style="font-size:13px;">Run all executes journeys with up to 4× concurrency.</span>
        </div>

        <div class="stack">
            {journeys_html}
        </div>
    </main>

    <!-- Toast container -->
    <div id="dashToast" class="toast"></div>

    <!-- GitHub Modal -->
    <div id="githubModal" class="modal-backdrop">
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="githubModalTitle">
            <div class="modal__header">
                <div class="modal__title" id="githubModalTitle">{icon("github")} Push generated tests to GitHub</div>
                <button type="button" class="btn-icon" onclick="closeGithubModal()" aria-label="Close">{icon("x")}</button>
            </div>
            <div class="modal__body">
                <div id="modalPreTestWarning" class="alert alert-warning">
                    {icon("alert")}
                    <div><strong>Have you run all journeys yet?</strong> Running <strong>Run all journeys</strong> first validates and self-heals test cases before they are committed.</div>
                </div>
                <div class="alert alert-info" style="align-items:center;">
                    {icon("file")}
                    <div><strong>Specification:</strong> <code>{_esc(spec_file)}</code> <span style="margin-left:8px; opacity:.8;">Commit subject: <em>OAS tests for {_esc(spec_file)}</em></span></div>
                </div>
                <div class="field">
                    <label class="field__label">Tests to commit</label>
                    <div class="scope-choice" id="modalGhScopeGroup">
                        <label class="scope-choice__option is-selected" id="scopeOptAll">
                            <input type="radio" name="modalGhTestScope" value="all" checked onchange="updateModalScopeSelection()">
                            <div>
                                <div class="scope-choice__head">
                                    <span class="scope-choice__title">Commit all tests</span>
                                    <span class="badge badge-neutral" id="modalScopeAllBadge">{total_steps} tests</span>
                                </div>
                                <div class="scope-choice__desc">Commit every generated journey and test step in the suite.</div>
                            </div>
                        </label>
                        <label class="scope-choice__option" id="scopeOptSuccessful">
                            <input type="radio" name="modalGhTestScope" value="successful" onchange="updateModalScopeSelection()">
                            <div>
                                <div class="scope-choice__head">
                                    <span class="scope-choice__title">Commit successful tests</span>
                                    <span class="badge badge-success" id="modalScopePassedBadge">0 passed</span>
                                </div>
                                <div class="scope-choice__desc">Commit only the tests that have been executed and passed.</div>
                            </div>
                        </label>
                    </div>
                    <span id="modalGhScopeHint" class="field__hint" style="display:none;"></span>
                </div>
                <div class="field">
                    <label class="field__label" for="modalGhRepo">Target repository</label>
                    <input type="text" id="modalGhRepo" class="input" placeholder="owner/repository" autocomplete="off" spellcheck="false">
                </div>
                <div class="grid-2">
                    <div class="field">
                        <label class="field__label" for="modalGhBranch">Target branch</label>
                        <input type="text" id="modalGhBranch" class="input input--mono" placeholder="oas-tests-agent/YYYY-MM-DD_HH-MM-SS" autocomplete="off" spellcheck="false">
                    </div>
                    <div class="field">
                        <label class="field__label" for="modalGhMode">Push mode</label>
                        <select id="modalGhMode" class="select" onchange="updateModalTokenHint()">
                            <option value="commit">Direct commit to branch</option>
                            <option value="pr">Create pull request</option>
                        </select>
                    </div>
                </div>
                <div class="field">
                    <label class="field__label" for="modalGhFolder">Destination folder (timestamped)</label>
                    <input type="text" id="modalGhFolder" class="input input--mono" readonly>
                    <span class="field__hint">Organized under <code>tests/oas-tests-agent/{{timestamp}}/</code> so previous runs are never overwritten.</span>
                </div>
                <div class="field">
                    <label class="field__label" for="modalGhToken">GitHub token <span class="req">*</span></label>
                    <input type="password" id="modalGhToken" class="input" required oninput="updateModalTokenHint()" placeholder="ghp_… or github_pat_… (required)" autocomplete="off">
                    <span id="modalGhTokenHint" class="field__hint is-danger">Required. Enter a GitHub token; it is isolated strictly to this session.</span>
                </div>
                <div id="modalGhResult" class="alert alert-info" style="display:none;"></div>
            </div>
            <div class="modal__footer">
                <button type="button" class="btn btn-secondary" onclick="closeGithubModal()">Cancel</button>
                <button type="button" id="modalBtnConfirmPush" class="btn btn-success" onclick="executeGithubPush()">{icon("upload")}<span id="modalBtnText">Push to GitHub</span></button>
            </div>
        </div>
    </div>
    """

    js_icons = {
        **status_icon_markup(),
        "sparkles": icon("sparkles"),
        "bot": icon("bot"),
        "alert": icon("alert"),
        "info": icon("info"),
        "shield": icon("shield"),
        "check-circle": icon("check-circle"),
        "external": icon("external"),
        "github": icon("github"),
        "lock": icon("lock"),
        "loader": icon("loader"),
        "pull-request": icon("pull-request"),
    }
    initial_exec_results = load_execution_results(session_id)
    prelude = (
        f"const SESSION_ID = {_js_json(session_id)};\n"
        f"const SPEC_FILE = {_js_json(spec_file)};\n"
        f"const DEFAULT_REPO = {_js_json(get_detected_github_repo() or '')};\n"
        f"const INITIAL_EXEC_RESULTS = {_js_json(initial_exec_results)};\n"
        f"const ICONS = {_js_json(js_icons)};\n"
    )
    html_content = render_page(
        title="Execution Dashboard",
        body_html=body,
        header_title="Execution Dashboard",
        header_actions_html=header_actions,
        extra_css=DASHBOARD_EXTRA_CSS,
        scripts_js=prelude + DASHBOARD_JS,
    )
    return HTMLResponse(content=html_content, status_code=200)


def _extract_step_error_info(
    st_id: str, res: dict[str, Any], context: dict[str, Any] | None = None
) -> str:
    """Builds a comprehensive error message string for a failed step."""
    resp_obj = res.get("response")
    error_msg = res.get("error", "")
    if not error_msg and resp_obj is not None:
        error_msg = f"HTTP {resp_obj.status_code}: {resp_obj.text}"
    if resp_obj is not None and hasattr(resp_obj, "request"):
        error_msg += f" | Request: {resp_obj.request.method} {resp_obj.request.url}"
    elif res.get("request_url"):
        error_msg += f" | Request: {res.get('request_method', 'GET')} {res['request_url']}"
    if context:
        try:
            error_msg += f" | Captured Context: {json.dumps(context)}"
        except TypeError:
            pass
    return f"Step {st_id} failed: {error_msg}"


async def _execute_and_heal_journey(
    session_id: str,
    target_sc: dict[str, Any],
    suite: dict[str, Any],
    suite_path: str,
    healing: bool,
    step_callback: Callable[[dict[str, Any]], Any],
    event_queue: asyncio.Queue[dict[str, Any] | None],
    lock: Any,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any]]:
    """Executes a scenario, attempts self-healing across any failed steps, and collects detailed diagnoses for all failures."""
    runner = TestRunner(base_url=suite.get("target_base_url"))
    sc_obj = TestScenario(**target_sc)
    step_results: dict[str, Any] = {}
    success = await runner.execute_scenario(
        sc_obj, step_results, on_step_progress=step_callback
    )

    sc_id = target_sc.get("scenario_id", "")
    healing_info = None

    if not success and healing:
        # 1. Collect all initial failed steps
        failed_steps: list[tuple[str, str]] = []
        for st_id, res in step_results.items():
            if res.get("status") == "FAILED":
                st_err = _extract_step_error_info(st_id, res, runner.context)
                failed_steps.append((st_id, st_err))

        validator = TestValidatorAgent()
        spec_path = get_session_spec_path(session_id)
        applied_fixes: list[tuple[str, Any]] = []
        applied_fix_records: list[dict[str, Any]] = []

        # 2. Attempt self-healing across all failed steps
        for failing_st_id, error_msg in failed_steps:
            await event_queue.put(
                {
                    "event": "HEALING_START",
                    "scenario_id": sc_id,
                    "step_id": failing_st_id,
                    "message": f"Analyzing failure on Step {failing_st_id} with TestValidatorAgent...",
                }
            )
            fix = await validator.heal_scenario_async(
                target_sc,
                error_msg,
                spec_path=spec_path,
            )
            if fix:
                applied_fixes.append((failing_st_id, fix))
                target_step_id = fix.fixed_step_id or failing_st_id
                all_steps = (
                    target_sc.get("setup_steps", [])
                    + target_sc.get("test_steps", [])
                    + target_sc.get("teardown_steps", [])
                )
                target_steps = [
                    s for s in all_steps if s.get("step_id") == target_step_id
                ]
                if not target_steps:
                    target_steps = all_steps

                step_before = (
                    copy.deepcopy(target_steps[0]) if target_steps else None
                )
                for step in target_steps:
                    apply_test_fix_to_step(
                        step,
                        fix,
                        scenario_type=target_sc.get("scenario_type"),
                    )
                updated_step_dict = next(
                    (s for s in all_steps if s.get("step_id") == target_step_id),
                    None,
                )
                applied_fix_records.append(
                    {
                        "target_step_id": target_step_id,
                        "fix": fix,
                        "source": getattr(validator, "last_fix_source", None),
                        "model": getattr(validator, "model_id", None),
                        "error_msg": error_msg,
                        "step_before": step_before,
                        "step_after": updated_step_dict
                        or (target_steps[0] if target_steps else None),
                    }
                )
                await event_queue.put(
                    {
                        "event": "HEALING_APPLIED",
                        "scenario_id": sc_id,
                        "target_step": target_step_id,
                        "reasoning": fix.reasoning,
                        "fixed_step": updated_step_dict,
                        "fix": fix.model_dump(),
                    }
                )

        # 3. If any fixes were applied, persist to suite.json and re-run verification
        re_runner = None
        if applied_fixes:
            async with lock:
                with open(suite_path, "w", encoding="utf-8") as f:
                    json.dump(suite, f, indent=2)

                try:
                    py_suite_path = os.path.join(
                        os.path.dirname(suite_path), "test_suite.py"
                    )
                    with open(py_suite_path, "w", encoding="utf-8") as pf:
                        pf.write(generate_python_test_suite(suite))
                except OSError as py_err:
                    logger.warning(
                        "Could not refresh test_suite.py after healing: %s", py_err
                    )

            sc_obj = TestScenario(**target_sc)
            step_results = {}
            re_runner = TestRunner(base_url=suite.get("target_base_url"))
            success = await re_runner.execute_scenario(
                sc_obj,
                step_results,
                on_step_progress=step_callback,
            )
            async with lock:
                for rec in applied_fix_records:
                    record_healing_event(
                        session_id,
                        sc_id,
                        rec["target_step_id"],
                        rec["fix"],
                        rec["source"],
                        rec["model"],
                        rec["error_msg"],
                        rec["step_before"],
                        rec["step_after"],
                        success,
                    )

        # 4. If all tests now pass after re-running
        if success and applied_fixes:
            healing_info = {
                "applied": True,
                "attempted": True,
                "target_step": applied_fixes[0][0] if len(applied_fixes) == 1 else None,
                "target_steps": [st_id for st_id, _ in applied_fixes],
                "reasoning": (
                    applied_fixes[0][1].reasoning
                    if len(applied_fixes) == 1
                    else "; ".join(f"Step {st_id}: {f.reasoning}" for st_id, f in applied_fixes)
                ),
                "re_run_success": True,
            }
            return True, healing_info, step_results

        # 5. If tests still fail or healing was not applied: collect reasoning for ALL failed steps!
        failed_diagnoses: list[dict[str, Any]] = []
        active_context = re_runner.context if re_runner else runner.context

        for st_id, res in step_results.items():
            if res.get("status") == "FAILED":
                st_err = _extract_step_error_info(st_id, res, active_context)
                unhealed_reason = validator.explain_unhealed_failure(
                    target_sc,
                    st_err,
                    failing_step_id=st_id,
                )
                failed_diagnoses.append(
                    {
                        "step_id": st_id,
                        "reasoning": unhealed_reason,
                        "error": st_err,
                    }
                )
                res["diagnosis"] = unhealed_reason
                res["reasoning"] = unhealed_reason

        primary_reason = (
            failed_diagnoses[0]["reasoning"]
            if len(failed_diagnoses) == 1
            else f"{len(failed_diagnoses)} tests failed in this journey without automated patches."
        )

        await event_queue.put(
            {
                "event": "HEALING_FAILED",
                "scenario_id": sc_id,
                "target_step": (
                    failed_diagnoses[0]["step_id"]
                    if len(failed_diagnoses) == 1
                    else None
                ),
                "target_steps": [d["step_id"] for d in failed_diagnoses],
                "reasoning": primary_reason,
                "diagnoses": failed_diagnoses,
            }
        )

        healing_info = {
            "applied": bool(applied_fixes),
            "attempted": True,
            "target_step": (
                applied_fixes[0][0]
                if applied_fixes
                else (failed_diagnoses[0]["step_id"] if len(failed_diagnoses) == 1 else None)
            ),
            "target_steps": (
                [st_id for st_id, _ in applied_fixes]
                if applied_fixes
                else [d["step_id"] for d in failed_diagnoses]
            ),
            "reasoning": (
                applied_fixes[0][1].reasoning
                if applied_fixes and len(applied_fixes) == 1
                else primary_reason
            ),
            "re_run_success": False,
            "diagnoses": failed_diagnoses,
        }
        return False, healing_info, step_results

    elif not success and not healing:
        validator = TestValidatorAgent()
        for st_id, res in step_results.items():
            if res.get("status") == "FAILED":
                st_err = _extract_step_error_info(st_id, res, runner.context)
                unhealed_reason = validator.explain_unhealed_failure(
                    target_sc,
                    st_err,
                    failing_step_id=st_id,
                )
                res["diagnosis"] = unhealed_reason
                res["reasoning"] = unhealed_reason

    return success, healing_info, step_results


@app.get("/stream_scenario/{session_id}/{scenario_id}")
async def stream_scenario(
    session_id: str, scenario_id: str, healing: bool = True
) -> Response:
    """Executes a single scenario and streams progress events and self-healing status in real time via SSE."""
    suite_path = os.path.join(WORKSPACE_DIR, session_id, "tests", "suite.json")
    if not os.path.exists(suite_path):
        return JSONResponse({"error": "Suite not found"}, status_code=404)

    with open(suite_path, "r", encoding="utf-8") as f:
        suite = json.load(f)

    target_sc = next(
        (
            s
            for s in suite.get("scenarios", [])
            if s.get("scenario_id") == scenario_id
        ),
        None,
    )
    if not target_sc:
        return JSONResponse({"error": "Scenario not found"}, status_code=404)

    async def event_generator():
        event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def step_callback(info: dict[str, Any]) -> None:
            await event_queue.put({"scenario_id": scenario_id, **info})

        async def run_worker() -> None:
            try:
                lock = get_session_lock(session_id)
                success, healing_info, step_results = await _execute_and_heal_journey(
                    session_id=session_id,
                    target_sc=target_sc,
                    suite=suite,
                    suite_path=suite_path,
                    healing=healing,
                    step_callback=step_callback,
                    event_queue=event_queue,
                    lock=lock,
                )

                async with get_session_lock(session_id):
                    record_scenario_execution_results(
                        session_id,
                        scenario_id,
                        step_results,
                        "SUCCESS" if success else "FAILED",
                    )
                formatted_steps = format_step_results(step_results)
                await event_queue.put(
                    {
                        "event": "JOURNEY_COMPLETE",
                        "scenario_id": scenario_id,
                        "status": "SUCCESS" if success else "FAILED",
                        "healing": healing_info,
                        "steps": formatted_steps,
                    }
                )
            except Exception as err:
                logger.error(
                    "Error executing scenario %s: %s", scenario_id, err
                )
                await event_queue.put(
                    {
                        "event": "JOURNEY_COMPLETE",
                        "scenario_id": scenario_id,
                        "status": "FAILED",
                        "error": str(err),
                        "steps": {},
                    }
                )
            finally:
                await event_queue.put(None)

        worker_task = asyncio.create_task(run_worker())

        try:
            while True:
                item = await event_queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            if not worker_task.done():
                worker_task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/stream_suite/{session_id}")
async def stream_suite(
    session_id: str, healing: bool = True, concurrency: int = 4
) -> Response:
    """Executes all scenarios in parallel (bounded concurrency) and streams live progress and healing events via SSE."""
    suite_path = os.path.join(WORKSPACE_DIR, session_id, "tests", "suite.json")
    if not os.path.exists(suite_path):
        return JSONResponse({"error": "Suite not found"}, status_code=404)

    with open(suite_path, "r", encoding="utf-8") as f:
        suite = json.load(f)

    scenarios = suite.get("scenarios", [])
    total_journeys = len(scenarios)

    async def event_generator():
        event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        semaphore = asyncio.Semaphore(concurrency)
        suite_lock = get_session_lock(session_id)

        completed_count = 0
        passed_count = 0
        failed_count = 0
        counter_lock = asyncio.Lock()

        async def run_single_journey(sc_data: dict[str, Any]) -> None:
            nonlocal completed_count, passed_count, failed_count
            sc_id = sc_data.get("scenario_id")
            sc_name = sc_data.get("name")

            async with semaphore:
                all_steps = (
                    sc_data.get("setup_steps", [])
                    + sc_data.get("test_steps", [])
                    + sc_data.get("teardown_steps", [])
                )
                await event_queue.put(
                    {
                        "event": "JOURNEY_START",
                        "scenario_id": sc_id,
                        "name": sc_name,
                        "total_steps": len(all_steps),
                    }
                )

                async def step_callback(info: dict[str, Any]) -> None:
                    await event_queue.put({"scenario_id": sc_id, **info})

                step_results: dict[str, Any] = {}
                success = False
                healing_info = None

                try:
                    success, healing_info, step_results = (
                        await _execute_and_heal_journey(
                            session_id=session_id,
                            target_sc=sc_data,
                            suite=suite,
                            suite_path=suite_path,
                            healing=healing,
                            step_callback=step_callback,
                            event_queue=event_queue,
                            lock=suite_lock,
                        )
                    )
                except Exception as sc_err:
                    logger.error(
                        "Error executing journey %s: %s", sc_id, sc_err
                    )
                    success = False
                finally:
                    async with suite_lock:
                        record_scenario_execution_results(
                            session_id,
                            sc_id,
                            step_results,
                            "SUCCESS" if success else "FAILED",
                        )
                    async with counter_lock:
                        completed_count += 1
                        if success:
                            passed_count += 1
                        else:
                            failed_count += 1
                        cur_completed = completed_count
                        cur_passed = passed_count
                        cur_failed = failed_count

                    formatted_steps = format_step_results(step_results)
                    await event_queue.put(
                        {
                            "event": "JOURNEY_COMPLETE",
                            "scenario_id": sc_id,
                            "status": "SUCCESS" if success else "FAILED",
                            "completed_journeys": cur_completed,
                            "total_journeys": total_journeys,
                            "passed": cur_passed,
                            "failed": cur_failed,
                            "steps": formatted_steps,
                            "healing": healing_info,
                        }
                    )

        async def master_worker() -> None:
            tasks = [
                asyncio.create_task(run_single_journey(sc)) for sc in scenarios
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            score = (
                int((passed_count / total_journeys) * 100)
                if total_journeys > 0
                else 0
            )
            await event_queue.put(
                {
                    "event": "SUITE_COMPLETE",
                    "total": total_journeys,
                    "passed": passed_count,
                    "failed": failed_count,
                    "score": score,
                }
            )
            await event_queue.put(None)

        master_task = asyncio.create_task(master_worker())

        try:
            while True:
                item = await event_queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            if not master_task.done():
                master_task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/execute_single/{session_id}/{target_type}/{scenario_id}/{step_id}")
async def execute_single(
    session_id: str,
    target_type: str,
    scenario_id: str,
    step_id: str,
    healing: bool = False,
) -> Any:
    """Executes a single step within a scenario (including prior context steps) with optional self-healing."""
    if target_type != "step":
        return JSONResponse(
            {"error": f"Unsupported target_type '{target_type}'"},
            status_code=400,
        )

    suite_path = os.path.join(WORKSPACE_DIR, session_id, "tests", "suite.json")
    if not os.path.exists(suite_path):
        return JSONResponse({"error": "Suite not found"}, status_code=404)

    with open(suite_path, "r", encoding="utf-8") as f:
        suite = json.load(f)

    target_sc = next(
        (
            s
            for s in suite.get("scenarios", [])
            if s.get("scenario_id") == scenario_id
        ),
        None,
    )
    if not target_sc:
        return JSONResponse({"error": "Scenario not found"}, status_code=404)

    runner = TestRunner(base_url=suite.get("target_base_url"))
    all_steps = (
        target_sc.get("setup_steps", [])
        + target_sc.get("test_steps", [])
        + target_sc.get("teardown_steps", [])
    )
    target_st = next(
        (s for s in all_steps if s.get("step_id") == step_id), None
    )
    if not target_st:
        return JSONResponse({"error": "Step not found"}, status_code=404)

    st_obj = TestStep(**target_st)
    prior_step_statuses: dict[str, str] = {}
    async with httpx.AsyncClient(
        base_url=runner.base_url, follow_redirects=True
    ) as client:
        try:
            prior_steps = []
            for s in all_steps:
                if s.get("step_id") == step_id:
                    break
                prior_steps.append(TestStep(**s))
            for ps in prior_steps:
                try:
                    await runner._execute_step(client, ps)
                    prior_step_statuses[ps.step_id] = "SUCCESS"
                except Exception:
                    prior_step_statuses[ps.step_id] = "FAILED"

            resp = await runner._execute_step(client, st_obj)
            async with get_session_lock(session_id):
                record_single_step_execution_result(
                    session_id,
                    scenario_id,
                    {**prior_step_statuses, step_id: "SUCCESS"},
                )
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text
            return {
                "status": "SUCCESS",
                "step": step_id,
                "response_status": resp.status_code,
                "request_url": (
                    str(resp.request.url) if hasattr(resp, "request") else ""
                ),
                "request_method": (
                    resp.request.method if hasattr(resp, "request") else ""
                ),
                "response_body": body,
            }
        except Exception as err:
            resp = getattr(err, "response", None)
            req_url = (
                str(resp.request.url)
                if resp and hasattr(resp, "request")
                else ""
            )
            req_method = (
                resp.request.method
                if resp and hasattr(resp, "request")
                else ""
            )
            err_text = str(err)
            if resp and hasattr(resp, "text"):
                err_text += f" | Response: {resp.text}"
            if req_url:
                err_text += f" | Request: {req_method or 'GET'} {req_url}"
            if runner.context:
                try:
                    err_text += f" | Captured Context: {json.dumps(runner.context)}"
                except TypeError:
                    pass

            unhealed_info: dict[str, Any] | None = None
            if healing:
                validator = TestValidatorAgent()
                full_error_msg = f"Step {step_id} failed: {err_text}"
                fix = await validator.heal_scenario_async(
                    target_sc,
                    full_error_msg,
                    spec_path=get_session_spec_path(session_id),
                )
                if fix:
                    step_before = copy.deepcopy(target_st)
                    apply_test_fix_to_step(
                        target_st,
                        fix,
                        scenario_type=target_sc.get("scenario_type"),
                    )
                    with open(suite_path, "w", encoding="utf-8") as f:
                        json.dump(suite, f, indent=2)

                    try:
                        py_suite_path = os.path.join(
                            os.path.dirname(suite_path), "test_suite.py"
                        )
                        with open(py_suite_path, "w", encoding="utf-8") as pf:
                            pf.write(generate_python_test_suite(suite))
                    except OSError as py_err:
                        logger.warning(
                            "Could not refresh test_suite.py after healing: %s",
                            py_err,
                        )
                    st_obj = TestStep(**target_st)
                    try:
                        resp = await runner._execute_step(client, st_obj)
                        async with get_session_lock(session_id):
                            record_single_step_execution_result(
                                session_id,
                                scenario_id,
                                {**prior_step_statuses, step_id: "SUCCESS"},
                            )
                            record_healing_event(
                                session_id,
                                scenario_id,
                                step_id,
                                fix,
                                getattr(validator, "last_fix_source", None),
                                getattr(validator, "model_id", None),
                                err_text,
                                step_before,
                                target_st,
                                True,
                            )
                        try:
                            body = resp.json()
                        except ValueError:
                            body = resp.text
                        return {
                            "status": "SUCCESS",
                            "step": step_id,
                            "response_status": resp.status_code,
                            "request_url": (
                                str(resp.request.url)
                                if hasattr(resp, "request")
                                else req_url
                            ),
                            "request_method": (
                                resp.request.method
                                if hasattr(resp, "request")
                                else req_method
                            ),
                            "response_body": body,
                            "fixed_step": target_st,
                            "healing": {
                                "applied": True,
                                "attempted": True,
                                "target_step": step_id,
                                "reasoning": fix.reasoning,
                            },
                        }
                    except Exception as retry_e:
                        async with get_session_lock(session_id):
                            record_single_step_execution_result(
                                session_id,
                                scenario_id,
                                {**prior_step_statuses, step_id: "FAILED"},
                            )
                            record_healing_event(
                                session_id,
                                scenario_id,
                                step_id,
                                fix,
                                getattr(validator, "last_fix_source", None),
                                getattr(validator, "model_id", None),
                                err_text,
                                step_before,
                                target_st,
                                False,
                            )
                        return {
                            "status": "FAILED",
                            "error": str(retry_e),
                            "request_url": req_url,
                            "request_method": req_method,
                            "fixed_step": target_st,
                            "healing": {
                                "applied": True,
                                "attempted": True,
                                "target_step": step_id,
                                "reasoning": fix.reasoning,
                            },
                        }
                else:
                    unhealed_info = {
                        "applied": False,
                        "attempted": True,
                        "target_step": step_id,
                        "reasoning": validator.explain_unhealed_failure(
                            target_sc,
                            full_error_msg,
                            failing_step_id=step_id,
                        ),
                    }

            async with get_session_lock(session_id):
                record_single_step_execution_result(
                    session_id,
                    scenario_id,
                    {**prior_step_statuses, step_id: "FAILED"},
                )
            failed_resp: dict[str, Any] = {
                "status": "FAILED",
                "error": str(err),
                "request_url": req_url,
                "request_method": req_method,
            }
            if unhealed_info is not None:
                failed_resp["healing"] = unhealed_info
            return failed_resp


# ===========================================================================
# Additive JSON API Endpoints for React Frontend (Zero Impact on Existing SSR)
# ===========================================================================
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/samples")
async def api_list_samples() -> dict[str, Any]:
    """Returns the bundled sample OpenAPI specs and their metadata for the React frontend."""
    default_sample = os.path.basename(DEFAULT_OAS_PATH)
    items = []
    for fname, meta in BUNDLED_SAMPLES.items():
        path = os.path.join(SAMPLE_OAS_DIR, fname)
        size_bytes = os.path.getsize(path) if os.path.exists(path) else 0
        items.append(
            {
                "filename": fname,
                "label": meta.get("label", fname),
                "title": meta.get("title", ""),
                "size_bytes": size_bytes,
                "size_formatted": _format_size(size_bytes) if size_bytes else "",
                "default": fname == default_sample,
                "download_url": f"/sample_oas/{quote(fname, safe='')}",
            }
        )
    return {"default_sample": default_sample, "samples": items}


@app.get("/api/session/{session_id}/suite")
async def api_get_session_suite(session_id: str) -> Response:
    """Returns the full declarative suite, metadata, execution results, and GitHub config for the React dashboard."""
    session_dir = resolve_session_dir(session_id)
    suite_path = (
        os.path.join(session_dir, "tests", "suite.json") if session_dir else ""
    )
    if not session_dir or not os.path.exists(suite_path):
        return JSONResponse(
            {"error": "Suite not found for this session."}, status_code=404
        )

    with open(suite_path, "r", encoding="utf-8") as f:
        suite = json.load(f)

    status_path = os.path.join(session_dir, "status.json")
    spec_file = "ecommerce.yaml"
    api_title = ""
    if os.path.exists(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as sf:
                sdata = json.load(sf)
                result = sdata.get("result") or {}
                spec_file = (
                    sdata.get("spec_file")
                    or result.get("spec_file")
                    or spec_file
                )
                api_title = str(result.get("api_title") or "")
        except (OSError, ValueError):
            pass

    scenarios = suite.get("scenarios", [])
    total_steps = sum(
        len(sc.get("setup_steps", []))
        + len(sc.get("test_steps", []))
        + len(sc.get("teardown_steps", []))
        for sc in scenarios
    )
    return JSONResponse(
        {
            "session_id": session_id,
            "spec_file": spec_file,
            "api_title": api_title or "API Test Suite",
            "base_url": suite.get("target_base_url") or "",
            "total_journeys": len(scenarios),
            "total_steps": total_steps,
            "suite": suite,
            "execution_results": load_execution_results(session_id),
            "default_repo": get_detected_github_repo() or "",
        }
    )


@app.get("/api/session/{session_id}/code")
async def api_get_session_code(
    session_id: str, scope: str = "all"
) -> Response:
    """Returns the generated standalone PyTest code and counts as JSON for the React Code Viewer."""
    session_dir = resolve_session_dir(session_id)
    suite_path = (
        os.path.join(session_dir, "tests", "suite.json") if session_dir else ""
    )
    py_suite_path = (
        os.path.join(session_dir, "tests", "test_suite.py")
        if session_dir
        else ""
    )
    if not session_dir or (
        not os.path.exists(suite_path) and not os.path.exists(py_suite_path)
    ):
        return JSONResponse(
            {"error": "Generated test files not found."}, status_code=404
        )

    normalized_scope = (scope or "all").strip().lower()
    only_successful = normalized_scope == "successful"
    total_all_steps = 0
    total_passed_steps = 0

    if os.path.exists(suite_path):
        with open(suite_path, "r", encoding="utf-8") as f:
            suite_data = json.load(f)
        for sc in suite_data.get("scenarios", []):
            total_all_steps += (
                len(sc.get("setup_steps", []))
                + len(sc.get("test_steps", []))
                + len(sc.get("teardown_steps", []))
            )
        exec_results = load_execution_results(session_id)
        filtered_suite, total_passed_steps = filter_suite_for_successful_tests(
            suite_data, exec_results=exec_results
        )
        if only_successful:
            if total_passed_steps > 0:
                py_content = generate_python_test_suite(filtered_suite)
            else:
                py_content = (
                    "# No successful tests recorded yet for this session.\n"
                    "# Execute journeys on the Execution Dashboard first, or switch to 'All tests'."
                )
        else:
            py_content = generate_python_test_suite(suite_data)
    else:
        with open(py_suite_path, "r", encoding="utf-8") as f:
            py_content = f.read()

    return JSONResponse(
        {
            "session_id": session_id,
            "scope": "successful" if only_successful else "all",
            "filename": (
                "test_suite_passed.py" if only_successful else "test_suite.py"
            ),
            "total_all_steps": total_all_steps,
            "total_passed_steps": total_passed_steps,
            "code": py_content,
        }
    )


@app.get("/api/session/{session_id}/visualize")
async def api_get_session_visualize(session_id: str) -> Response:
    """Returns provenance report, healing log, Mermaid graphs, and execution results as JSON for the React Visualizer."""
    from ui_visualizer import build_mermaid_graph, resolve_provenance
    from manager.core.provenance import scenario_steps

    session_dir = resolve_session_dir(session_id)
    suite_path = (
        os.path.join(session_dir, "tests", "suite.json") if session_dir else ""
    )
    if not session_dir or not os.path.exists(suite_path):
        return JSONResponse(
            {"error": "Visualization data not found."}, status_code=404
        )

    with open(suite_path, "r", encoding="utf-8") as f:
        suite = json.load(f)

    api_title = ""
    status_path = os.path.join(session_dir, "status.json")
    if os.path.exists(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as sf:
                sdata = json.load(sf)
            api_title = str((sdata.get("result") or {}).get("api_title") or "")
        except (OSError, ValueError, AttributeError):
            api_title = ""

    report = load_generation_report(session_id)
    heals = load_healing_log(session_id)
    exec_results = load_execution_results(session_id)

    heals_by_sc: dict[str, list[dict[str, Any]]] = {}
    for entry in heals:
        heals_by_sc.setdefault(str(entry.get("scenario_id") or ""), []).append(
            entry
        )

    scenarios = [
        sc for sc in (suite.get("scenarios") or []) if isinstance(sc, dict)
    ]
    journeys_data = []
    counts = {"gemini": 0, "fallback": 0, "template": 0}
    adjustments_total = 0

    for sc in scenarios:
        sc_id = str(sc.get("scenario_id") or "")
        prov = resolve_provenance(sc, report)
        src = str(prov.get("source") or "gemini")
        counts[src] = counts.get(src, 0) + 1
        adjs = prov.get("adjustments") or []
        adjustments_total += len(adjs)
        sc_heals = heals_by_sc.get(sc_id, [])
        steps = scenario_steps(sc)
        healed_ids = {
            str(e.get("step_id")) for e in sc_heals if e.get("step_id")
        }
        adjusted_ids = {
            str(a.get("step_id")) for a in adjs if a.get("step_id")
        }
        mermaid_graph = build_mermaid_graph(
            steps, src, healed_ids, adjusted_ids
        )
        journeys_data.append(
            {
                "scenario": sc,
                "steps": steps,
                "provenance": prov,
                "healing_entries": sc_heals,
                "mermaid_graph": mermaid_graph,
                "last_run": (exec_results.get("scenarios") or {}).get(sc_id),
            }
        )

    return JSONResponse(
        {
            "session_id": session_id,
            "api_title": api_title,
            "base_url": suite.get("target_base_url") or "",
            "generation_report": report,
            "healing_log": heals,
            "execution_results": exec_results,
            "summary_counts": {
                "total_journeys": len(scenarios),
                "gemini": counts.get("gemini", 0),
                "fallback": counts.get("fallback", 0),
                "template": counts.get("template", 0),
                "adjustments_total": adjustments_total,
                "heals_total": len(heals),
                "gemini_heals": sum(
                    1 for e in heals if e.get("source") == "gemini"
                ),
            },
            "journeys": journeys_data,
        }
    )


# ---------------------------------------------------------------------------
# Signed-in user identity (Cloud Run behind Identity-Aware Proxy)
# ---------------------------------------------------------------------------
IAP_EMAIL_HEADER = "x-goog-authenticated-user-email"
IAP_USER_ID_HEADER = "x-goog-authenticated-user-id"
IAP_JWT_HEADER = "x-goog-iap-jwt-assertion"
IAP_IDENTITY_PREFIX_RE = re.compile(r"^accounts\.google\.com:", re.IGNORECASE)
DEFAULT_DISPLAY_NAME = "User"


def _strip_iap_prefix(value: str | None) -> str:
    """IAP prefixes identity values with 'accounts.google.com:'; return the bare value."""
    if not value:
        return ""
    return IAP_IDENTITY_PREFIX_RE.sub("", value.strip())


def resolve_user_identity(headers: Any) -> dict[str, Any]:
    """Builds the display identity for the UI from IAP headers.

    Resolution order: IAP email header -> LOCAL_USER_EMAIL (local development) -> "User".
    The username is the part of the email before '@'. The IAP user id is passed through
    (prefix stripped) so the UI has a stable identifier without exposing the full email.
    """
    get = headers.get if hasattr(headers, "get") else (lambda _key: None)
    email = _strip_iap_prefix(get(IAP_EMAIL_HEADER))
    user_id = _strip_iap_prefix(get(IAP_USER_ID_HEADER))
    source = "iap" if email else ""

    if not email:
        local_email = (os.environ.get("LOCAL_USER_EMAIL") or "").strip()
        if local_email:
            email = local_email
            source = "local"

    username = DEFAULT_DISPLAY_NAME
    if email:
        local_part = email.split("@", 1)[0].strip()
        username = local_part or DEFAULT_DISPLAY_NAME
    else:
        logger.debug(
            "No %s header and no LOCAL_USER_EMAIL set; defaulting to '%s'.",
            IAP_EMAIL_HEADER,
            DEFAULT_DISPLAY_NAME,
        )

    return {
        "username": username,
        "email": email,
        "user_id": user_id,
        "authenticated": source == "iap",
        "source": source or "anonymous",
    }


@app.get("/api/user-info")
async def api_user_info(request: Request) -> Response:
    """Returns the signed-in user's display name derived from the IAP identity headers."""
    try:
        identity = resolve_user_identity(request.headers)
    except Exception as err:  # pragma: no cover - defensive: identity must never break the UI
        logger.warning("Could not resolve user identity: %s", err)
        identity = {
            "username": DEFAULT_DISPLAY_NAME,
            "email": "",
            "user_id": "",
            "authenticated": False,
            "source": "anonymous",
        }
    return JSONResponse(identity, headers={"Cache-Control": "no-store"})


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Lightweight Cloud Run health and readiness probe."""
    return {"status": "ok"}


@app.get("/guide", response_class=HTMLResponse)
async def serve_guide() -> HTMLResponse:
    """Serves the React SPA for the /guide client route (or falls back to the home UI)."""
    spa = _try_serve_spa_html()
    if spa is not None:
        return spa
    return await serve_ui()


if __name__ == "__main__":
    reload_flag = os.environ.get("UVICORN_RELOAD", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8081,
        reload=reload_flag,
        reload_excludes=[
            "session_workspaces/*",
            "session_uploads/*",
            "*.json",
            "*.html",
            "*.pyc",
            "__pycache__/*",
            "*.db",
            "*.log",
        ],
    )

