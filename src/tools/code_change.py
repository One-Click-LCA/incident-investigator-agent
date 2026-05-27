"""GitHub API tools for code change analysis — no local clone required."""

import base64
import json
from datetime import datetime, timezone, timedelta
from langchain_core.tools import tool
import requests

from src.config import get_github_token, get_github_org
from src.utils import safe_call, tool_result, truncate

_RISKY_PATTERNS = (
    "migrations/", ".sql", "flyway", "liquibase",
    "application.properties", "application.yml", "application.yaml",
    "bootstrap.yml", "bootstrap.yaml",
    "security", "auth", "oauth", "jwt",
    "pool", "datasource", "connection",
    "scheduler", "scheduled", "cron",
    "pom.xml", "build.gradle", "requirements.txt",
)

_GITHUB_API = "https://api.github.com"


def _gh_headers() -> dict:
    token = get_github_token()
    if not token:
        raise RuntimeError("GITHUB_TOKEN not found in secrets. Add it to Secrets Manager.")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _classify_risk(path: str) -> str:
    p = path.lower()
    if any(x in p for x in ("migration", ".sql", "flyway", "liquibase")):
        return "high"
    if any(x in p for x in ("security", "auth", "oauth", "jwt", "password", "secret", "credential")):
        return "high"
    if any(x in p for x in ("application.yml", "application.yaml", "application.properties", "bootstrap")):
        return "medium"
    if any(x in p for x in ("pool", "datasource", "connection", "timeout", "scheduler", "cron")):
        return "medium"
    if any(x in p for x in ("pom.xml", "build.gradle", "package.json", "requirements")):
        return "medium"
    return "low"


@tool
def get_recent_commits(service_name: str, since_hours: int = 4) -> str:
    """
    Get recent commits from GitHub for the service repo within the last N hours.
    Returns commit SHA, author, message, timestamp, files changed count, and risky path flags.
    service_name: base service name without env suffix (e.g. 'supply-chain-service').
    """
    def _run():
        org = get_github_org()
        since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        url = f"{_GITHUB_API}/repos/{org}/{service_name}/commits"
        resp = requests.get(
            url,
            headers=_gh_headers(),
            params={"since": since, "per_page": 20},
            timeout=15,
        )
        resp.raise_for_status()
        raw_commits = resp.json()

        commits = []
        for c in raw_commits[:10]:
            commit_data = c.get("commit", {})
            sha = c.get("sha", "")[:12]
            # Fetch file details for each commit (up to 5 commits to stay within rate limits)
            files_changed = []
            risky_paths = []
            if len(commits) < 5:
                detail_resp = requests.get(
                    f"{_GITHUB_API}/repos/{org}/{service_name}/commits/{c['sha']}",
                    headers=_gh_headers(),
                    timeout=15,
                )
                if detail_resp.ok:
                    detail = detail_resp.json()
                    files_changed = [f["filename"] for f in detail.get("files", [])]
                    risky_paths = [
                        {"path": f, "risk": _classify_risk(f)}
                        for f in files_changed
                        if any(p in f.lower() for p in _RISKY_PATTERNS)
                    ]

            commits.append({
                "sha": sha,
                "full_sha": c.get("sha", ""),
                "author": commit_data.get("author", {}).get("email", ""),
                "message": truncate(commit_data.get("message", "").split("\n")[0], 200),
                "timestamp": commit_data.get("author", {}).get("date", ""),
                "files_count": len(files_changed),
                "files_changed": files_changed[:20],
                "risky_paths": risky_paths,
                "has_migration": any(
                    "migration" in f.lower() or f.endswith(".sql") for f in files_changed
                ),
            })

        return {
            "repo": f"{org}/{service_name}",
            "since_hours": since_hours,
            "commit_count": len(commits),
            "commits": commits,
        }

    return tool_result(safe_call("get_recent_commits", _run))


@tool
def get_commit_diff(service_name: str, base_sha: str, head_sha: str) -> str:
    """
    Compare two commits and return files changed with risk classification.
    Use this to see exactly what changed between the previous deploy and current deploy.
    service_name: base service name (e.g. 'supply-chain-service').
    base_sha: previous commit/tag. head_sha: current deployed commit.
    """
    def _run():
        org = get_github_org()
        url = f"{_GITHUB_API}/repos/{org}/{service_name}/compare/{base_sha}...{head_sha}"
        resp = requests.get(url, headers=_gh_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()

        files = data.get("files", [])
        classified = []
        for f in files:
            filename = f.get("filename", "")
            classified.append({
                "path": filename,
                "status": f.get("status", ""),   # added / modified / removed
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "risk": _classify_risk(filename),
                # Short patch preview (first 300 chars) for risky files only
                "patch_preview": truncate(f.get("patch", ""), 300) if _classify_risk(filename) != "low" else "",
            })

        risky = [f for f in classified if f["risk"] in ("high", "medium")]
        return {
            "repo": f"{org}/{service_name}",
            "base_sha": base_sha[:12],
            "head_sha": head_sha[:12],
            "commits_between": data.get("total_commits", 0),
            "files_changed": len(files),
            "risky_files": risky,
            "all_files": classified[:40],
            "has_migration": any(
                f["risk"] == "high" and ("migration" in f["path"].lower() or f["path"].endswith(".sql"))
                for f in classified
            ),
        }

    return tool_result(safe_call("get_commit_diff", _run))


@tool
def get_file_at_commit(service_name: str, file_path: str, commit_sha: str) -> str:
    """
    Fetch file content at a specific commit. Use this to inspect config files (application.yaml,
    pom.xml) or to map stack trace line numbers to source code.
    service_name: base service name. file_path: repo-relative path. commit_sha: git SHA.
    Returns decoded file content (truncated to 3000 chars).
    """
    def _run():
        org = get_github_org()
        url = f"{_GITHUB_API}/repos/{org}/{service_name}/contents/{file_path}"
        resp = requests.get(
            url,
            headers=_gh_headers(),
            params={"ref": commit_sha},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        # GitHub returns base64-encoded content
        raw_b64 = data.get("content", "").replace("\n", "")
        content = base64.b64decode(raw_b64).decode("utf-8", errors="replace") if raw_b64 else ""

        return {
            "repo": f"{org}/{service_name}",
            "path": file_path,
            "commit": commit_sha[:12],
            "size_bytes": data.get("size", 0),
            "content": truncate(content, 3000),
            "line_count": content.count("\n"),
        }

    return tool_result(safe_call("get_file_at_commit", _run))


@tool
def get_repo_file_tree(service_name: str, commit_sha: str) -> str:
    """
    Get the full file path list for a repo at a specific commit.
    Use this to discover entity/model files, migration files, and config files
    without a local clone.
    service_name: base service name. commit_sha: git SHA to inspect.
    Returns filtered path list grouped by type (entities, migrations, config).
    """
    def _run():
        org = get_github_org()
        url = f"{_GITHUB_API}/repos/{org}/{service_name}/git/trees/{commit_sha}"
        resp = requests.get(
            url,
            headers=_gh_headers(),
            params={"recursive": "1"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("truncated"):
            print(f"  [warn] get_repo_file_tree: tree truncated for {service_name} (large repo)")

        all_paths = [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]

        entity_paths = [
            p for p in all_paths
            if any(x in p.lower() for x in (
                "/domain/", "/model/", "/entity/", "/entities/",
                "domain.groovy", "domain.java", ".model.", ".entity.",
            ))
        ]

        migration_paths = [
            p for p in all_paths
            if any(x in p.lower() for x in ("migration", "flyway", "liquibase"))
            or p.endswith(".sql")
        ]

        config_paths = [
            p for p in all_paths
            if any(x in p.lower() for x in (
                "application.yml", "application.yaml", "application.properties",
                "bootstrap.yml", "bootstrap.yaml", "pom.xml", "build.gradle",
            ))
        ]

        return {
            "repo": f"{org}/{service_name}",
            "commit": commit_sha[:12],
            "total_files": len(all_paths),
            "entity_model_paths": entity_paths[:30],
            "migration_paths": migration_paths[:20],
            "config_paths": config_paths[:10],
            "suggested_read_first": (entity_paths[:3] + migration_paths[:2])[:5],
        }

    return tool_result(safe_call("get_repo_file_tree", _run))
