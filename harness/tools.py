"""Agent-callable tools. Runbook lookup + git context via GitHub REST or local subprocess."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import requests
import yaml


RUNBOOK_DIR = Path(__file__).parent.parent / "runbooks"
GIT_TIMEOUT_SEC = 5
GITHUB_API = "https://api.github.com"
GITHUB_HTTP_TIMEOUT_SEC = 5


# ----- runbook -----

def load_runbooks() -> list[dict[str, Any]]:
    runbooks = []
    if not RUNBOOK_DIR.exists():
        return runbooks
    for path in RUNBOOK_DIR.glob("*.md"):
        text = path.read_text()
        meta, body = _parse_frontmatter(text)
        meta["id"] = path.stem
        meta["body"] = body
        runbooks.append(meta)
    return runbooks


def lookup_runbook(tags: list[str]) -> dict[str, Any] | None:
    """Tag-intersection match. Returns runbook with highest overlap.

    Accepts both "key:value" and bare "value" tag forms. Splits "key:value"
    so either side matches runbook frontmatter tags.
    """
    tag_set: set[str] = set()
    for t in tags:
        t = t.lower()
        tag_set.add(t)
        if ":" in t:
            k, v = t.split(":", 1)
            tag_set.add(k)
            tag_set.add(v)
    best = None
    best_overlap = 0
    for rb in load_runbooks():
        rb_tags = {t.lower() for t in rb.get("tags", [])}
        overlap = len(tag_set & rb_tags)
        if overlap > best_overlap:
            best, best_overlap = rb, overlap
    return best


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    _, fm, body = text.split("---", 2)
    return yaml.safe_load(fm) or {}, body.strip()


# ----- git -----

class GitTimeout(Exception):
    pass


def _run_git(repo: str, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise GitTimeout(f"git {' '.join(args)} exceeded {GIT_TIMEOUT_SEC}s") from e
    if result.returncode != 0:
        return ""
    return result.stdout


def git_log(repo: str, limit: int = 5, path_filter: str | None = None) -> list[dict[str, Any]]:
    args = ["log", f"-{limit}", "--name-only", "--pretty=format:%H%x09%an%x09%s"]
    if path_filter:
        args.extend(["--", path_filter])
    out = _run_git(repo, args)
    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in out.splitlines():
        if "\t" in line:
            if current:
                commits.append(current)
            sha, author, msg = line.split("\t", 2)
            current = {"sha": sha, "author": author, "msg": msg, "files_changed": []}
        elif line.strip() and current is not None:
            current["files_changed"].append(line.strip())
    if current:
        commits.append(current)
    return commits


def git_blame(repo: str, file: str, line: int | None = None) -> dict[str, Any]:
    args = ["blame", "--line-porcelain"]
    if line:
        args.extend(["-L", f"{line},{line}"])
    args.append(file)
    out = _run_git(repo, args)
    if not out:
        return {}
    author = ""
    summary = ""
    sha = ""
    for ln in out.splitlines():
        if ln.startswith("author "):
            author = ln[len("author "):].strip()
        elif ln.startswith("summary "):
            summary = ln[len("summary "):].strip()
        elif len(ln) == 40 + 1 + 1 or (ln and ln[0].isalnum() and " " in ln and not sha):
            parts = ln.split(" ")
            if len(parts[0]) == 40:
                sha = parts[0]
    return {"file": file, "sha": sha, "author": author, "summary": summary}


def git_diff(repo: str, commit: str) -> str:
    return _run_git(repo, ["show", "--stat", commit])


def _is_github_repo_spec(repo_spec: str) -> bool:
    """Detect `owner/repo` form vs local filesystem path.

    GitHub spec: contains `/`, no leading `/`, no `\\`, exactly one `/` separator
    (single-segment owner + repo), and not an existing directory on disk.
    """
    if not repo_spec:
        return False
    if repo_spec.startswith("/") or repo_spec.startswith("\\"):
        return False
    if "/" not in repo_spec:
        return False
    if os.path.isdir(repo_spec):
        return False
    # owner/repo has exactly 2 segments
    parts = [p for p in repo_spec.split("/") if p]
    return len(parts) == 2


def _gh_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _gh_get(url: str) -> tuple[int, dict[str, str], Any]:
    """GET helper. Returns (status, headers, json-or-None)."""
    resp = requests.get(url, headers=_gh_headers(), timeout=GITHUB_HTTP_TIMEOUT_SEC)
    try:
        body = resp.json()
    except Exception:
        body = None
    return resp.status_code, dict(resp.headers), body


def _gh_fetch_commit_files(repo_spec: str, sha: str) -> list[str]:
    """Per-commit files via GET /repos/{owner}/{repo}/commits/{sha}."""
    try:
        cs, _, cb = _gh_get(f"{GITHUB_API}/repos/{repo_spec}/commits/{sha}")
    except requests.RequestException:
        return []
    if cs != 200 or not isinstance(cb, dict):
        return []
    out = []
    for f in cb.get("files", []) or []:
        fn = f.get("filename")
        if fn:
            out.append(fn)
    return out


def _gh_list_commits(
    repo_spec: str,
    limit: int,
    path: str | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """List commits, optionally scoped to a path. Returns (commits, error_reason)."""
    url = f"{GITHUB_API}/repos/{repo_spec}/commits?per_page={limit}"
    if path:
        url += f"&path={path}"
    try:
        status, headers, body = _gh_get(url)
    except requests.Timeout:
        return None, f"github timeout > {GITHUB_HTTP_TIMEOUT_SEC}s"
    except requests.RequestException as e:
        return None, f"github request error: {e}"
    if status == 404:
        return None, f"github 404: repo {repo_spec} not found"
    if status == 401:
        return None, "github 401: unauthorized (check GITHUB_TOKEN)"
    if status == 403 and headers.get("X-RateLimit-Remaining") == "0":
        return None, "github 403: rate limit exhausted"
    if status >= 400 or not isinstance(body, list):
        return None, f"github status {status}"
    return body, None


def _entry_to_commit(repo_spec: str, entry: dict[str, Any], scope_label: str) -> dict[str, Any]:
    sha = entry.get("sha", "")
    commit_meta = entry.get("commit", {}) or {}
    author = (commit_meta.get("author") or {}).get("name") or (
        entry.get("author") or {}
    ).get("login") or ""
    msg_full = commit_meta.get("message", "") or ""
    msg = msg_full.splitlines()[0] if msg_full else ""
    files_changed = _gh_fetch_commit_files(repo_spec, sha) if sha else []
    return {
        "sha": sha,
        "author": author,
        "msg": msg,
        "files_changed": files_changed,
        "scope": scope_label,
    }


def _github_collect(
    repo_spec: str,
    stack_files: list[str],
    limit_per_path: int = 3,
    fallback_limit: int = 5,
) -> dict[str, Any]:
    """Path-scoped fetch.

    For each file in the stack trace, ask GitHub for the last `limit_per_path`
    commits touching that file. Union them (dedup by SHA). If no stack files
    or all path queries return empty, fall back to recent commits on default branch.
    """
    seen: dict[str, dict[str, Any]] = {}
    queried_paths: list[str] = []
    first_error: str | None = None

    for path in stack_files[:5]:
        queried_paths.append(path)
        commits, err = _gh_list_commits(repo_spec, limit_per_path, path=path)
        if commits is None:
            first_error = first_error or err
            continue
        for entry in commits:
            sha = entry.get("sha")
            if not sha or sha in seen:
                continue
            seen[sha] = _entry_to_commit(repo_spec, entry, scope_label=f"path:{path}")

    if not seen:
        # Fallback: recent commits on default branch (old behavior).
        commits, err = _gh_list_commits(repo_spec, fallback_limit, path=None)
        if commits is None:
            return {"available": False, "reason": err or first_error or "no commits"}
        for entry in commits:
            sha = entry.get("sha")
            if not sha or sha in seen:
                continue
            seen[sha] = _entry_to_commit(repo_spec, entry, scope_label="recent")

    return {
        "available": True,
        "repo": repo_spec,
        "scope": "path-scoped" if queried_paths and any(c["scope"].startswith("path:") for c in seen.values()) else "recent",
        "queried_paths": queried_paths,
        "recent_commits": list(seen.values()),
        "blame": [],
    }


def collect_git_context(repo_spec: str, service: str | None, stack_files: list[str]) -> dict[str, Any]:
    """Aggregate git context for agent.

    `repo_spec` is either an `owner/repo` GitHub slug (REST path) or a local
    filesystem path to a checkout (subprocess path). Detected by shape.
    Returns identical schema in both branches.
    """
    if _is_github_repo_spec(repo_spec):
        return _github_collect(repo_spec, stack_files=stack_files)

    # Local subprocess fallback (back-compat with DEMO_GIT_REPO).
    if not repo_spec or not os.path.isdir(os.path.join(repo_spec, ".git")):
        return {"available": False, "reason": "no git repo at path"}
    try:
        # Path-scoped: log per stack file, dedup by SHA. Fallback to recent log if none.
        seen: dict[str, dict[str, Any]] = {}
        queried: list[str] = []
        for path in stack_files[:5]:
            queried.append(path)
            for c in git_log(repo_spec, limit=3, path_filter=path):
                sha = c["sha"]
                if sha and sha not in seen:
                    c["scope"] = f"path:{path}"
                    seen[sha] = c
        if not seen:
            for c in git_log(repo_spec, limit=5):
                sha = c["sha"]
                if sha and sha not in seen:
                    c["scope"] = "recent"
                    seen[sha] = c
        blame_info = []
        for f in stack_files[:3]:
            try:
                info = git_blame(repo_spec, f)
                if info:
                    blame_info.append(info)
            except GitTimeout:
                continue
        return {
            "available": True,
            "repo": repo_spec,
            "scope": "path-scoped" if any(c.get("scope", "").startswith("path:") for c in seen.values()) else "recent",
            "queried_paths": queried,
            "recent_commits": list(seen.values()),
            "blame": blame_info,
        }
    except GitTimeout as e:
        return {"available": False, "reason": str(e)}
