"""Jenkins deployment history tool."""

import base64
from langchain_core.tools import tool
import requests

from src.config import get_secret
from src.utils import safe_call, tool_result, is_prod_env, truncate


def _parse_builds(data: dict) -> list:
    builds = []
    for b in data.get("builds", []):
        sha = ""
        branch = ""
        for action in b.get("actions", []):
            rev = action.get("lastBuiltRevision", {})
            if rev:
                sha = rev.get("SHA1", "")[:12]
                branches = rev.get("branch", [])
                branch = branches[0].get("name", "") if branches else ""
                break
        builds.append({
            "number": b.get("number"),
            "result": b.get("result"),
            "started_at": str(b.get("timestamp")),
            "duration_seconds": int(b.get("duration", 0) / 1000),
            "display_name": b.get("displayName"),
            "git_sha": sha,
            "branch": branch,
        })
    return builds


def _jenkins_creds(env: str) -> tuple[str, str, str]:
    secret = get_secret()
    prefix = "JENKINS_PROD" if is_prod_env(env) else "JENKINS_NONPROD"
    url = secret.get(f"{prefix}_URL", "")
    token = secret.get(f"{prefix}_TOKEN_ENV", "")
    user = secret.get("jenkins_user", "")
    if not url or not token:
        raise RuntimeError(f"Jenkins credentials not found ({prefix}_URL / {prefix}_TOKEN_ENV)")
    return url, user, token


@tool
def get_recent_deployments(service: str, env: str) -> str:
    """
    Get the last 5 Jenkins deployment builds for the service. Returns build number, result
    (SUCCESS/FAILURE/ABORTED), start time, duration, git SHA, and branch.
    Use to correlate incidents with recent deployments — a failed or very recent build
    is a strong signal.
    Job naming convention: deploy-{service}-to-ecs-preprod (nonprod) or
    deploy-{service}-to-ecs-prod (prod Jenkins).
    Returns JSON string.
    """
    def _run():
        jenkins_url, jenkins_user, jenkins_token = _jenkins_creds(env)
        jenkins_url = jenkins_url.rstrip("/")
        auth_b64 = base64.b64encode(f"{jenkins_user}:{jenkins_token}".encode()).decode()
        headers = {"Authorization": f"Basic {auth_b64}"}

        # Strip ECS env suffix before building job name
        # e.g. supply-chain-service-preprod → supply-chain-service
        base = service
        for tag in ("-preprod", "-staging", "-features", "-dev", "-prod", "-qa", "-uat", "-mfg", "-devops", "-internal"):
            if base.endswith(tag):
                base = base[: -len(tag)]
                break

        # Also try with -service suffix stripped: supply-chain-service → supply-chain
        short_base = base[:-len("-service")] if base.endswith("-service") else base

        # Try candidate job names in order
        if is_prod_env(env):
            job_candidates = [
                f"deploy-{base}-to-ecs-prod",
                f"deploy-{short_base}-to-ecs-prod",
                f"deploy-{service}-to-ecs-prod",
            ]
        else:
            job_candidates = [
                f"deploy-{base}-to-ecs-preprod",
                f"deploy-{short_base}-to-ecs-preprod",
                f"deploy-{base}-to-ecs",
                f"deploy-{short_base}-to-ecs",
                f"deploy-{service}-to-ecs-preprod",
            ]

        for job_name in job_candidates:
            try:
                resp = requests.get(
                    f"{jenkins_url}/job/{job_name}/api/json",
                    params={"tree": "builds[number,result,timestamp,duration,displayName,actions[lastBuiltRevision[SHA1,branch[name]]]]{0,5}"},
                    headers=headers,
                    timeout=8,
                )
                if resp.status_code == 404:
                    continue  # job doesn't exist under this name, try next candidate
                if resp.status_code == 401:
                    return {"status": "failed", "error": "Jenkins authentication failed (401) — check JENKINS_NONPROD_TOKEN_ENV and jenkins_user in secret"}
                if resp.status_code == 403:
                    return {"status": "failed", "error": "Jenkins authorization denied (403) — token lacks read permission on this job"}
                resp.raise_for_status()
                data = resp.json()
                builds = _parse_builds(data)
                return {"status": "success", "job_name": job_name, "jenkins_url": jenkins_url, "builds": builds}
            except requests.HTTPError as e:
                if hasattr(e, 'response') and e.response is not None and e.response.status_code not in (404,):
                    return {"status": "failed", "error": f"Jenkins HTTP {e.response.status_code} on job '{job_name}'"}
                continue
            except Exception as e:
                return {"status": "failed", "error": str(e)}

        # Fallback: search all jobs for a name containing the base service name
        try:
            list_resp = requests.get(
                f"{jenkins_url}/api/json",
                params={"tree": "jobs[name]"},
                headers=headers,
                timeout=8,
            )
            if list_resp.status_code == 200:
                all_jobs = [j["name"] for j in list_resp.json().get("jobs", [])]
                # Match jobs whose name contains the base or short_base and "deploy"
                hints = [short_base, base]
                matched = [
                    j for j in all_jobs
                    if "deploy" in j.lower() and any(h in j.lower() for h in hints)
                ]
                if matched:
                    # Try the first matched job
                    job_name = matched[0]
                    resp = requests.get(
                        f"{jenkins_url}/job/{job_name}/api/json",
                        params={"tree": "builds[number,result,timestamp,duration,displayName,actions[lastBuiltRevision[SHA1,branch[name]]]]{0,5}"},
                        headers=headers,
                        timeout=8,
                    )
                    resp.raise_for_status()
                    builds = _parse_builds(resp.json())
                    return {
                        "status": "success",
                        "job_name": job_name,
                        "jenkins_url": jenkins_url,
                        "builds": builds,
                        "note": f"Job discovered via search (candidates tried: {job_candidates})",
                    }
                return {
                    "status": "skipped",
                    "reason": f"No Jenkins job found for service '{service}' in env '{env}'",
                    "candidates_tried": job_candidates,
                    "all_deploy_jobs": [j for j in all_jobs if "deploy" in j.lower()][:20],
                }
        except Exception:
            pass

        return {
            "status": "skipped",
            "reason": f"No Jenkins job found for service '{service}' in env '{env}'",
            "candidates_tried": job_candidates,
        }

    return tool_result(safe_call("get_recent_deployments", _run))
