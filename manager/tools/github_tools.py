"""Git and GitHub REST API utility functions for archiving generated test suites."""

from datetime import datetime
import logging
import os
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import quote

import github
from github import GithubException

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def get_detected_github_repo() -> str:
    """Attempts to auto-detect the GitHub owner/repo from environment or git remote."""
    repo = os.environ.get("GITHUB_REPO", "").strip()
    if repo and repo != "your-org/your-repo":
        return repo
    try:
        res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        url = res.stdout.strip()
        match = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", url)
        if match:
            return match.group(1)
    except Exception as err:
        logger.debug("Failed to detect repo from git remote: %s", err)
    return "apigee-agentic-offering/oas-spec-test"


def get_detected_git_branch() -> str:
    """Detects current git branch or defaults to 'main'."""
    try:
        res = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        branch = res.stdout.strip()
        if branch:
            return branch
    except Exception as err:
        logger.debug("Failed to detect git branch: %s", err)
    return "main"


def make_timestamped_folder() -> tuple[str, str]:
    """Generates a logical folder path under tests/oas-tests-agent/{timestamp_str}."""
    now = datetime.now()
    timestamp_str = now.strftime("%Y-%m-%d_%H-%M-%S")
    folder_path = f"tests/oas-tests-agent/{timestamp_str}"
    return folder_path, timestamp_str


def create_branch(repo: Any, base_branch: str, new_branch: str) -> bool:
    """Creates a new branch off the base branch in the given repo."""
    try:
        sb = repo.get_branch(base_branch)
        repo.create_git_ref(ref=f"refs/heads/{new_branch}", sha=sb.commit.sha)
        logger.info("Created branch %s from %s.", new_branch, base_branch)
        return True
    except GithubException as err:
        logger.error("Failed to create branch: %s", err)
        return False


def commit_files(
    repo: Any,
    branch: str,
    file_paths: list[str],
    commit_message: str,
    dest_folder: str = "",
    extra_files: dict[str, str] | None = None,
    raise_on_error: bool = False,
) -> dict[str, Any] | None:
    """Commits local file paths and optional in-memory extra_files to branch in dest_folder."""
    files_to_write: list[tuple[str, str]] = []
    for file_path in file_paths:
        if not os.path.exists(file_path):
            logger.warning("File not found, skipping commit: %s", file_path)
            continue
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        file_name = os.path.basename(file_path)
        rel_path = (
            f"{dest_folder.strip('/')}/{file_name}"
            if dest_folder
            else f"tests/{file_name}"
        )
        files_to_write.append((rel_path, content))

    if extra_files:
        for file_name, content in extra_files.items():
            rel_path = (
                f"{dest_folder.strip('/')}/{file_name}"
                if dest_folder
                else f"tests/{file_name}"
            )
            files_to_write.append((rel_path, content))

    if not files_to_write:
        logger.warning("No files to commit.")
        return None

    try:
        try:
            ref = repo.get_git_ref(f"heads/{branch}")
        except Exception:
            default_branch = repo.default_branch
            sb = repo.get_branch(default_branch)
            ref = repo.create_git_ref(ref=f"refs/heads/{branch}", sha=sb.commit.sha)

        base_tree = repo.get_git_tree(ref.object.sha)
        element_list = []
        committed_files: list[str] = []

        for rel_path, content in files_to_write:
            blob = repo.create_git_blob(content, "utf-8")
            element = github.InputGitTreeElement(
                path=rel_path, mode="100644", type="blob", sha=blob.sha
            )
            element_list.append(element)
            committed_files.append(rel_path)

        tree = repo.create_git_tree(element_list, base_tree)
        parent = repo.get_git_commit(ref.object.sha)
        commit = repo.create_git_commit(commit_message, tree, [parent])
        ref.edit(commit.sha)
        logger.info(
            "Committed %d files to branch %s in '%s'.",
            len(committed_files),
            branch,
            dest_folder,
        )

        commit_url = f"https://github.com/{repo.full_name}/commit/{commit.sha}"
        return {
            "success": True,
            "sha": commit.sha,
            "url": commit_url,
            "branch": branch,
            "folder": dest_folder,
            "files": committed_files,
        }
    except Exception as git_data_err:
        logger.warning(
            "Git Data API commit failed (%s); attempting Contents API fallback...",
            git_data_err,
        )
        try:
            last_sha = ""
            committed_files = []
            for rel_path, content in files_to_write:
                try:
                    existing = repo.get_contents(rel_path, ref=branch)
                    res = repo.update_file(
                        path=rel_path,
                        message=commit_message,
                        content=content,
                        sha=existing.sha,
                        branch=branch,
                    )
                except Exception:
                    res = repo.create_file(
                        path=rel_path,
                        message=commit_message,
                        content=content,
                        branch=branch,
                    )
                commit_obj = res.get("commit") if isinstance(res, dict) else getattr(res, "commit", None)
                if commit_obj and getattr(commit_obj, "sha", None):
                    last_sha = commit_obj.sha
                committed_files.append(rel_path)

            commit_url = (
                f"https://github.com/{repo.full_name}/commit/{last_sha}"
                if last_sha
                else f"https://github.com/{repo.full_name}/tree/{branch}"
            )
            return {
                "success": True,
                "sha": last_sha,
                "url": commit_url,
                "branch": branch,
                "folder": dest_folder,
                "files": committed_files,
            }
        except Exception as err:
            logger.error("Failed to commit files via GitHub API: %s", err)
            if raise_on_error:
                raise git_data_err from err
            return None


def open_pull_request(
    repo: Any, branch: str, title: str, body: str, base: str = "main"
) -> str:
    """Opens a pull request from the new branch to the base branch and returns the PR URL."""
    try:
        pr = repo.create_pull(title=title, body=body, head=branch, base=base)
        logger.info("Pull request created successfully: %s", pr.html_url)
        return pr.html_url
    except GithubException as err:
        logger.warning("create_pull raised GithubException: %s", err)
        try:
            owner_login = getattr(getattr(repo, "owner", None), "login", "")
            head_query = f"{owner_login}:{branch}" if owner_login else branch
            existing_prs = list(repo.get_pulls(state="open", head=head_query, base=base))
            if existing_prs:
                logger.info("Reusing existing open PR: %s", existing_prs[0].html_url)
                return existing_prs[0].html_url
        except Exception as lookup_err:
            logger.debug("Could not query existing PRs: %s", lookup_err)
        return ""


def open_pr_via_gh_cli(
    repo_name: str,
    branch: str,
    title: str,
    body: str,
    base: str = "main",
    token: str | None = None,
) -> str:
    """Attempts to open a Pull Request using the GitHub CLI (gh) if installed and authenticated."""
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return ""
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    try:
        res = subprocess.run(
            [
                gh_bin,
                "pr",
                "create",
                "--repo",
                repo_name,
                "--head",
                branch,
                "--base",
                base,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if res.returncode == 0:
            for line in reversed(res.stdout.strip().splitlines()):
                if line.strip().startswith("http"):
                    return line.strip()
        view_res = subprocess.run(
            [
                gh_bin,
                "pr",
                "view",
                branch,
                "--repo",
                repo_name,
                "--json",
                "url",
                "-q",
                ".url",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if view_res.returncode == 0 and view_res.stdout.strip().startswith("http"):
            return view_res.stdout.strip()
    except Exception as err:
        logger.debug("gh pr create failed: %s", err)
    return ""


def build_github_pr_compare_url(
    repo_name: str,
    branch: str,
    title: str,
    body: str,
    base: str = "main",
) -> str:
    """Constructs a pre-filled GitHub Pull Request creation URL for 1-click PR opening when using Git SSH without a token."""
    encoded_branch = quote(branch, safe="/-._")
    encoded_title = quote(title)
    encoded_body = quote(body)
    return (
        f"https://github.com/{repo_name}/compare/{base}...{encoded_branch}"
        f"?expand=1&title={encoded_title}&body={encoded_body}"
    )


def commit_and_push_local_git(
    dest_folder: str,
    file_dict: dict[str, str],
    commit_message: str,
    branch: str | None = None,
    repo_name: str | None = None,
    timestamp_str: str | None = None,
) -> dict[str, Any]:
    """Commits and pushes files to an isolated branch 'oas-tests-agent/{timestamp}' via local Git CLI."""
    detected_repo = get_detected_github_repo()
    target_repo = (repo_name or detected_repo).strip()
    if not target_repo or target_repo == "your-org/your-repo":
        target_repo = detected_repo

    if detected_repo and target_repo.lower() != detected_repo.lower():
        raise RuntimeError(
            f"Configured target repository '{target_repo}' does not match local git remote '{detected_repo}'. "
            "Local Git CLI push can only push to the current repository clone. "
            f"To push tests to a remote repository ('{target_repo}'), please provide a GITHUB_TOKEN."
        )

    if not timestamp_str:
        parts = dest_folder.strip("/").split("/")
        timestamp_str = (
            parts[-1] if parts else datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        )

    if not branch or branch in ("main", "master"):
        target_branch = f"oas-tests-agent/{timestamp_str}"
    else:
        target_branch = branch

    current_branch = get_detected_git_branch()
    full_dest = os.path.join(REPO_ROOT, dest_folder)
    os.makedirs(full_dest, exist_ok=True)
    committed_files: list[str] = []
    branch_created = False

    try:
        checkout_res = subprocess.run(
            ["git", "checkout", "-B", target_branch],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if checkout_res.returncode != 0:
            raise RuntimeError(
                f"git checkout -B {target_branch} failed: {checkout_res.stderr}"
            )
        branch_created = True

        for file_name, content in file_dict.items():
            file_path = os.path.join(full_dest, file_name)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            committed_files.append(os.path.join(dest_folder, file_name))

        add_res = subprocess.run(
            ["git", "add", dest_folder],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if add_res.returncode != 0:
            raise RuntimeError(f"git add failed: {add_res.stderr}")

        commit_res = subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_res.returncode != 0:
            if (
                "nothing to commit" in commit_res.stdout
                or "nothing to commit" in commit_res.stderr
            ):
                logger.info("Nothing to commit for %s", dest_folder)
            else:
                raise RuntimeError(
                    f"git commit failed: {commit_res.stderr or commit_res.stdout}"
                )

        push_cmd = ["git", "push", "origin", f"{target_branch}:{target_branch}"]
        push_res = subprocess.run(
            push_cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if push_res.returncode != 0:
            raise RuntimeError(
                f"git push failed: {push_res.stderr or push_res.stdout}"
            )

        sha_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        sha = sha_res.stdout.strip()
        commit_url = f"https://github.com/{target_repo}/commit/{sha}"
        branch_url = f"https://github.com/{target_repo}/tree/{target_branch}"

        return {
            "success": True,
            "sha": sha,
            "url": commit_url,
            "branch_url": branch_url,
            "branch": target_branch,
            "folder": dest_folder,
            "files": committed_files,
        }
    finally:
        if branch_created and current_branch:
            subprocess.run(
                ["git", "checkout", current_branch],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
