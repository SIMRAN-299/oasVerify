"""Deterministic GitHub publisher for archiving generated OAS integration test suites."""

from datetime import datetime
import json
import logging
import os
import re
import shutil
from typing import Any

from github import Github, GithubException

from manager.core.codegen import (
    filter_suite_for_successful_tests,
    generate_python_test_suite,
)
from manager.tools.github_tools import (
    build_github_pr_compare_url,
    commit_and_push_local_git,
    commit_files,
    create_branch,
    get_detected_github_repo,
    make_timestamped_folder,
    open_pr_via_gh_cli,
    open_pull_request,
)

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_WORKSPACE_DIR = os.path.join(REPO_ROOT, "session_workspaces")


def _clean_github_token(raw_token: str | None) -> str | None:
    """Strips whitespace, surrounding quotes, or Bearer/token prefixes from a GitHub token."""
    if not raw_token:
        return None
    cleaned = raw_token.strip().strip("'\"")
    cleaned = re.sub(r"^(?:Bearer|token)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned or None


def _format_github_api_error(err: Exception, target_repo: str) -> str:
    """Formats a GithubException or API error into an actionable user-facing message."""
    status = getattr(err, "status", None)
    data = getattr(err, "data", None)
    detail = ""
    if isinstance(data, dict) and data.get("message"):
        detail = str(data["message"])
    else:
        detail = str(err)

    if status == 401 or "Bad credentials" in detail:
        return (
            f"GitHub Authentication Failed (401 Bad credentials): {detail}. "
            "Please verify that your GitHub Personal Access Token (ghp_... or github_pat_...) is valid and not expired."
        )
    if status == 403 or "Resource not accessible by personal access token" in detail:
        return (
            f"GitHub Permission Denied (403 Forbidden) on '{target_repo}': {detail}. "
            "Ensure your token has 'Contents: Read and write' (and 'Pull requests: Read and write' for PRs) "
            f"permissions on '{target_repo}', and that SAML SSO is authorized if required by the organization."
        )
    if status == 404:
        repo_owner = target_repo.split("/")[0] if "/" in target_repo else target_repo
        return (
            f"GitHub Repository '{target_repo}' Not Found or Token Lacks Access (404): {detail}. "
            f"If '{target_repo}' is a private/organization repository under '{repo_owner}': "
            f"(1) For a Fine-Grained PAT (github_pat_...), you must set 'Resource owner' to '{repo_owner}' "
            "(not your personal user account) and grant 'Contents: Read and write', OR "
            "(2) Use a Classic PAT (ghp_...) with the full 'repo' scope checked."
        )
    return f"GitHub API Error on '{target_repo}': {detail}"


class GitHubDeployer:
    """Commits and pushes generated test artifacts to GitHub via PyGithub or Git CLI."""

    def __init__(
        self,
        session_id: str,
        repo_name: str | None = None,
        token: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.token = _clean_github_token(token or os.environ.get("GITHUB_TOKEN"))
        detected = get_detected_github_repo()
        candidate = (repo_name or os.environ.get("GITHUB_REPO") or detected).strip()
        if not candidate or candidate == "your-org/your-repo":
            candidate = detected
        self.repo_name = candidate

    def execute(self, test_file_paths: list[str]) -> str:
        """Commits test files and opens a Pull Request on GitHub."""
        logger.info("[%s] GitHubDeployer started (PR flow).", self.session_id)
        if not self.token:
            logger.warning(
                "GITHUB_TOKEN not found in environment. Skipping PR flow."
            )
            return ""

        logger.info("Authenticating to GitHub and targeting repo: %s", self.repo_name)
        gh_client = Github(self.token)
        try:
            repo = gh_client.get_repo(self.repo_name)
        except Exception as err:
            logger.error("Could not fetch repo %s: %s", self.repo_name, err)
            return ""

        branch_name = f"test-gen/session-{self.session_id}"
        if not create_branch(repo, base_branch="main", new_branch=branch_name):
            return ""

        commit_msg = f"Add auto-generated OAS tests for session {self.session_id}"
        commit_res = commit_files(
            repo,
            branch=branch_name,
            file_paths=test_file_paths,
            commit_message=commit_msg,
        )
        if not commit_res:
            return ""

        pr_title = f"Automated OAS Integration Tests ({self.session_id})"
        pr_body = (
            "This PR contains auto-generated PyTest integration tests "
            "based on the latest OAS spec."
        )
        pr_url = open_pull_request(
            repo, branch=branch_name, title=pr_title, body=pr_body
        )
        logger.info("[%s] GitHub deployment complete. PR: %s", self.session_id, pr_url)
        return pr_url

    def push_session_tests(
        self,
        session_id: str | None = None,
        repo_name: str | None = None,
        branch: str | None = None,
        token: str | None = None,
        create_pr: bool = False,
        commit_message: str | None = None,
        custom_folder: str | None = None,
        workspace_dir: str | None = None,
        spec_file: str | None = None,
        require_token: bool = False,
        only_successful: bool = False,
        passed_steps_override: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Commits generated tests (all or only successful) for a session into a timestamped folder on GitHub."""
        sess_id = session_id or self.session_id
        target_repo = (
            repo_name or self.repo_name or get_detected_github_repo()
        ).strip()
        if not target_repo or target_repo == "your-org/your-repo":
            target_repo = get_detected_github_repo()
        ws_dir = workspace_dir or DEFAULT_WORKSPACE_DIR
        auth_token = _clean_github_token(
            token or self.token or os.environ.get("GITHUB_TOKEN")
        )
        if require_token and not auth_token:
            return {
                "status": "error",
                "message": (
                    "GitHub Personal Access Token is required. Please enter your GitHub Token "
                    "(ghp_... or github_pat_...) to push tests for this session."
                ),
            }

        sess_dir = os.path.join(ws_dir, sess_id)
        test_dir = os.path.join(sess_dir, "tests")

        if not os.path.exists(test_dir):
            return {
                "status": "error",
                "message": f"Test directory not found for session {sess_id}: {test_dir}",
            }

        suite_path = os.path.join(test_dir, "suite.json")
        suite_data: dict[str, Any] = {}
        api_title = "oas_api"
        base_url = "N/A"

        if os.path.exists(suite_path):
            try:
                with open(suite_path, "r", encoding="utf-8") as f:
                    suite_data = json.load(f)
                api_title = suite_data.get("metadata", {}).get("title") or "oas_api"
                base_url = suite_data.get("target_base_url", "N/A")
            except Exception as err:
                logger.warning("Could not read suite.json: %s", err)

        effective_suite_data = suite_data
        if only_successful:
            exec_results: dict[str, Any] = {}
            exec_results_path = os.path.join(test_dir, "execution_results.json")
            if os.path.exists(exec_results_path):
                try:
                    with open(exec_results_path, "r", encoding="utf-8") as ef:
                        exec_results = json.load(ef)
                except Exception as err:
                    logger.warning("Could not read execution_results.json: %s", err)
            effective_suite_data, total_passed_steps = filter_suite_for_successful_tests(
                suite_data,
                exec_results=exec_results,
                passed_steps_override=passed_steps_override,
            )
            if total_passed_steps == 0:
                return {
                    "status": "error",
                    "message": (
                        "No successful tests found to commit. Please execute journeys on the "
                        "Execution Dashboard first so passing tests are recorded, or choose 'Commit all tests'."
                    ),
                }

        scenarios: list[dict[str, Any]] = effective_suite_data.get("scenarios", [])
        total_steps = sum(len(s.get("test_steps", [])) for s in scenarios)
        if total_steps == 0 and scenarios:
            total_steps = sum(
                len(s.get("setup_steps", []))
                + len(s.get("test_steps", []))
                + len(s.get("teardown_steps", []))
                for s in scenarios
            )
        unique_endpoints: set[str] = set()
        for sc in scenarios:
            all_steps = (
                sc.get("setup_steps", [])
                + sc.get("test_steps", [])
                + sc.get("teardown_steps", [])
            )
            for st in all_steps:
                path_val = st.get("path_template") or st.get("path")
                if path_val:
                    unique_endpoints.add(path_val)
        endpoint_count = len(unique_endpoints)

        py_test_path = os.path.join(test_dir, "test_suite.py")
        generated_code_str: str | None = None
        if effective_suite_data:
            try:
                generated_code_str = generate_python_test_suite(effective_suite_data)
                if not only_successful:
                    with open(py_test_path, "w", encoding="utf-8") as f:
                        f.write(generated_code_str)
            except Exception as err:
                logger.warning("Could not auto-generate test_suite.py: %s", err)

        if custom_folder:
            dest_folder = custom_folder
            parts = dest_folder.strip("/").split("/")
            timestamp_str = (
                parts[-1]
                if parts
                else datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            )
        else:
            dest_folder, timestamp_str = make_timestamped_folder()

        if not timestamp_str:
            timestamp_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        resolved_spec_file = spec_file
        if not resolved_spec_file:
            status_path = os.path.join(sess_dir, "status.json")
            if os.path.exists(status_path):
                try:
                    with open(status_path, "r", encoding="utf-8") as sf:
                        sdata = json.load(sf)
                        resolved_spec_file = sdata.get("spec_file") or (
                            sdata.get("result") or {}
                        ).get("spec_file")
                except Exception as err:
                    logger.debug("Could not read status.json: %s", err)
        if not resolved_spec_file:
            upload_dir = os.path.join(REPO_ROOT, "uploaded_oas")
            if os.path.exists(upload_dir):
                for fname in os.listdir(upload_dir):
                    if fname.startswith(f"{sess_id}_"):
                        resolved_spec_file = fname[len(sess_id) + 1:]
                        break
        if not resolved_spec_file:
            resolved_spec_file = suite_data.get("metadata", {}).get("spec_file") or (
                f"{api_title}.yaml"
                if api_title and api_title != "oas_api"
                else "weather.yaml"
            )

        scope_desc = "Successful (Passed) Tests Only" if only_successful else "All Generated Tests"
        if generated_code_str is not None:
            py_suite_content = generated_code_str
        elif os.path.exists(py_test_path):
            with open(py_test_path, "r", encoding="utf-8") as f:
                py_suite_content = f.read()
        else:
            py_suite_content = generate_python_test_suite(effective_suite_data or {})

        file_dict: dict[str, str] = {"test_suite.py": py_suite_content}

        scope_word = "passed tests" if only_successful else "tests"
        default_commit_msg = (
            f"OAS {scope_word} for {resolved_spec_file}: "
            f"{total_steps} {scope_word} across {endpoint_count} endpoints ({timestamp_str})"
        )
        commit_msg = commit_message or default_commit_msg
        pr_title = (
            f"OAS {scope_word} for {resolved_spec_file}: "
            f"{total_steps} {scope_word} across {endpoint_count} endpoints"
        )
        pr_body = (
            f"Generated {total_steps} integration test steps ({scope_desc}) across "
            f"{endpoint_count} unique endpoints ({len(scenarios)} journeys) "
            f"in `{dest_folder}`.\n\n"
            f"- Specification File: `{resolved_spec_file}`\n"
            f"- API Title: {api_title}\n"
            f"- Test Scope: {scope_desc}\n"
            f"- Target Base URL: {base_url}\n"
            f"- Timestamp: {timestamp_str}\n"
            f"- Session ID: {sess_id}"
        )
        result: dict[str, Any] = {}
        pygh_error_msg: str | None = None

        if auth_token:
            logger.info("Attempting GitHub sync via PyGithub API for %s", target_repo)
            try:
                gh_client = Github(auth_token)
                repo = gh_client.get_repo(target_repo)
                default_base_branch = repo.default_branch

                target_branch = (
                    branch
                    if (branch and branch != default_base_branch)
                    else f"oas-tests-agent/{timestamp_str}"
                )
                create_branch(
                    repo,
                    base_branch=default_base_branch,
                    new_branch=target_branch,
                )

                commit_info = commit_files(
                    repo,
                    branch=target_branch,
                    file_paths=[],
                    commit_message=commit_msg,
                    dest_folder=dest_folder,
                    extra_files=file_dict,
                    raise_on_error=True,
                )
                if not commit_info:
                    raise RuntimeError("Failed to commit files via PyGithub")

                branch_url = f"https://github.com/{target_repo}/tree/{target_branch}"
                commit_url = commit_info.get("url") or branch_url

                if create_pr:
                    pr_url = open_pull_request(
                        repo,
                        branch=target_branch,
                        title=pr_title,
                        body=pr_body,
                        base=default_base_branch,
                    )
                    if not pr_url:
                        pr_url = open_pr_via_gh_cli(
                            target_repo,
                            target_branch,
                            pr_title,
                            pr_body,
                            base=default_base_branch,
                            token=auth_token,
                        )
                    compare_url = build_github_pr_compare_url(
                        target_repo,
                        target_branch,
                        pr_title,
                        pr_body,
                        base=default_base_branch,
                    )
                    if pr_url:
                        result = {
                            "status": "success",
                            "type": "pr",
                            "pr_created": True,
                            "url": pr_url,
                            "compare_url": compare_url,
                            "commit_url": commit_url,
                            "branch_url": branch_url,
                            "branch": target_branch,
                            "folder": dest_folder,
                            "repo": target_repo,
                            "timestamp": timestamp_str,
                            "files": list(file_dict.keys()),
                        }
                    else:
                        result = {
                            "status": "success",
                            "type": "pr_compare",
                            "pr_created": False,
                            "url": compare_url,
                            "compare_url": compare_url,
                            "commit_url": commit_url,
                            "branch_url": branch_url,
                            "branch": target_branch,
                            "folder": dest_folder,
                            "repo": target_repo,
                            "timestamp": timestamp_str,
                            "files": list(file_dict.keys()),
                            "pr_note": (
                                "Branch pushed to GitHub, but token lacked Pull Requests write permission. "
                                "Click 'Open Pull Request on GitHub' below (pre-filled)."
                            ),
                        }
                else:
                    result = {
                        "status": "success",
                        "type": "commit",
                        "url": commit_url,
                        "commit_url": commit_url,
                        "branch_url": branch_url,
                        "sha": commit_info.get("sha", ""),
                        "branch": target_branch,
                        "folder": dest_folder,
                        "repo": target_repo,
                        "timestamp": timestamp_str,
                        "files": list(file_dict.keys()),
                    }
            except Exception as pygh_err:
                pygh_error_msg = _format_github_api_error(pygh_err, target_repo)
                logger.warning(
                    "PyGithub sync failed: %s",
                    pygh_error_msg,
                )
                if require_token or not shutil.which("git"):
                    return {
                        "status": "error",
                        "message": pygh_error_msg,
                    }

        if not result or result.get("status") != "success":
            if not shutil.which("git"):
                return {
                    "status": "error",
                    "message": pygh_error_msg
                    or (
                        "GitHub push failed: Git CLI is not installed in this environment. "
                        "Please provide a valid GitHub Personal Access Token (ghp_... or github_pat_...)."
                    ),
                }
            logger.info("Attempting GitHub sync via local Git CLI...")
            try:
                target_branch = (
                    branch
                    if (branch and branch not in ("main", "master"))
                    else f"oas-tests-agent/{timestamp_str}"
                )
                cli_info = commit_and_push_local_git(
                    dest_folder=dest_folder,
                    file_dict=file_dict,
                    commit_message=commit_msg,
                    branch=target_branch,
                    repo_name=target_repo,
                    timestamp_str=timestamp_str,
                )
                pushed_branch = cli_info["branch"]
                commit_url = cli_info["url"]
                branch_url = cli_info.get("branch_url", "")

                if create_pr:
                    pr_url = ""
                    if auth_token:
                        try:
                            gh_client = Github(auth_token)
                            repo = gh_client.get_repo(target_repo)
                            pr_url = open_pull_request(
                                repo,
                                branch=pushed_branch,
                                title=pr_title,
                                body=pr_body,
                                base=repo.default_branch,
                            )
                        except Exception as pr_err:
                            logger.warning(
                                "Could not open PR via PyGithub after Git CLI push: %s",
                                pr_err,
                            )
                    if not pr_url:
                        pr_url = open_pr_via_gh_cli(
                            target_repo,
                            pushed_branch,
                            pr_title,
                            pr_body,
                            base="main",
                            token=auth_token,
                        )
                    compare_url = build_github_pr_compare_url(
                        target_repo,
                        pushed_branch,
                        pr_title,
                        pr_body,
                        base="main",
                    )
                    if pr_url:
                        result = {
                            "status": "success",
                            "type": "pr",
                            "pr_created": True,
                            "url": pr_url,
                            "compare_url": compare_url,
                            "commit_url": commit_url,
                            "branch_url": branch_url,
                            "sha": cli_info["sha"],
                            "branch": pushed_branch,
                            "folder": dest_folder,
                            "repo": target_repo,
                            "timestamp": timestamp_str,
                            "files": list(file_dict.keys()),
                        }
                    else:
                        result = {
                            "status": "success",
                            "type": "pr_compare",
                            "pr_created": False,
                            "url": compare_url,
                            "compare_url": compare_url,
                            "commit_url": commit_url,
                            "branch_url": branch_url,
                            "sha": cli_info["sha"],
                            "branch": pushed_branch,
                            "folder": dest_folder,
                            "repo": target_repo,
                            "timestamp": timestamp_str,
                            "files": list(file_dict.keys()),
                            "pr_note": (
                                "Branch pushed via Git SSH. Because Git SSH cannot call the GitHub REST API "
                                "without a GITHUB_TOKEN, click 'Open Pull Request on GitHub' below (pre-filled) "
                                "or enter a GitHub Token (ghp_...) to auto-create PRs."
                            ),
                        }
                else:
                    result = {
                        "status": "success",
                        "type": "commit",
                        "url": commit_url,
                        "commit_url": commit_url,
                        "branch_url": branch_url,
                        "sha": cli_info["sha"],
                        "branch": pushed_branch,
                        "folder": dest_folder,
                        "repo": target_repo,
                        "timestamp": timestamp_str,
                        "files": list(file_dict.keys()),
                    }
            except Exception as cli_err:
                logger.error("Git CLI push failed: %s", cli_err)
                if pygh_error_msg:
                    return {
                        "status": "error",
                        "message": pygh_error_msg,
                    }
                return {
                    "status": "error",
                    "message": (
                        f"GitHub push failed: {cli_err}. Please provide a valid GitHub Personal Access Token."
                    ),
                }

        if result and result.get("status") == "success":
            result["test_scope"] = "successful" if only_successful else "all"
            result["tests_committed"] = total_steps
            result["scenarios_committed"] = len(scenarios)

        try:
            sync_file = os.path.join(sess_dir, "github_sync.json")
            with open(sync_file, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
        except Exception as err:
            logger.warning("Could not persist github_sync.json: %s", err)

        return result
