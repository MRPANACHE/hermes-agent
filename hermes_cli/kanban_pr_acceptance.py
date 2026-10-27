"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

``gh`` runs as the card's assignee profile (``profile_home``), not the ambient
login: :func:`_gh_env` resolves that profile's own credentials/config for the
subprocess — a multi-profile host's default ``gh`` login cannot read another
org's private repos (#122689).
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from itertools import combinations
from pathlib import Path
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False,
         profile_home: str | None = None):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                check=True, env=_gh_env(profile_home))
    except subprocess.CalledProcessError as exc:
        # 401/403/404 = the login cannot see this repository (wrong profile identity
        # or missing grant), not a transient API failure. Persist only the status
        # code + endpoint, never gh's stderr (credentials/host details).
        denied = re.search(r"HTTP (40[134])", exc.stderr or "")
        if denied:
            raise _GateAuthError(f"HTTP {denied[1]} on {endpoint.split('?')[0]}") from None
        if exc.returncode == 4:  # gh's authentication-required exit: this profile has no login
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        raise
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


class _GateAuthError(RuntimeError):
    """gh was refused at HTTP 401/403/404 (or GraphQL returned no repository):
    this profile's login cannot see the repo — an identity problem to fix, not
    an infrastructure blip to retry."""


def _gh_env(profile_home: str | None) -> dict[str, str] | None:
    """Child env for ``gh``: the card's profile identity when one is resolvable.

    The completion boundary runs in the worker (assignee), the CLI, or a
    reviewer/dispatcher turn, so an ambient ``gh`` login is whichever process
    happened to call it (#122689). ``served_profile_child_env(inherit_credentials=True)``
    is the seam for "this child acts for that profile": it scrubs the launch
    profile's credential residue and overlays the target profile's own
    ``GH_TOKEN``/``GH_CONFIG_DIR`` (its ``.env`` + external secret sources).
    ``None`` keeps the ambient env — unassigned cards behave exactly as before.
    """
    if not profile_home:
        return None
    from tools.environments.local import _is_routed_home, hermes_subprocess_env, served_profile_child_env
    base = hermes_subprocess_env(inherit_credentials=True)
    routed = _is_routed_home(profile_home)
    if routed:
        # gh's config dir decides which login `gh api` uses, yet it is a path, not a
        # credential, so no scrub list sees it; the target's own value is overlaid from its .env.
        base.pop("GH_CONFIG_DIR", None)
    env = served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=True)
    if routed and not (env.keys() & {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}):
        # HOME/XDG_CONFIG_HOME are still the launch process's: without a login of its own the
        # child would fall through to ~/.config/gh/hosts.yml — the ambient login. Pin gh's config
        # to a profile-owned dir so it fails "not logged in" (exit 4 -> auth) instead.
        env["GH_CONFIG_DIR"] = str(Path(profile_home) / "gh")
    return env


def _assignee_profile_home(assignee: str | None) -> str | None:
    """Home whose ``gh`` login must read the contract repo — the assignee's, resolved
    exactly as the dispatcher resolves the worker's home — or None (unassigned) so the
    ambient login is used. An assigned card whose profile cannot be resolved is an
    identity failure (``auth``), never a silent fall-through to the ambient login."""
    if not assignee:
        return None
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        raise _GateAuthError(f"assignee profile {assignee!r} cannot be resolved") from None


def collect_acceptance(contract: str, published_pr: str | None,
                       assignee: str | None = None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    try:
        profile_home = _assignee_profile_home(assignee)
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        query = f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{pullRequest(number:{number:d}){{headRefOid baseRefName state\n            baseRef{{branchProtectionRule{{requiredStatusChecks{{context app{{databaseId}}}}}}}}}}}}}}'
        repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
        if repository is None:
            # A private repo the login cannot read resolves to null, not an error.
            raise _GateAuthError(f"HTTP 404 on graphql {repo}")
        pr = repository["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
            raise ValueError("PR is closed or current head is unavailable")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
        rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                     paginate=True, profile_home=profile_home)
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required:
            receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
            return receipt
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                     paginate=True, profile_home=profile_home)
        runs = [run for page in pages for run in page["check_runs"]]
        if len({r["id"] for r in runs}) != pages[0]["total_count"]:
            raise ValueError("Incomplete check-run pagination")
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                                                       paginate=True, profile_home=profile_home) for s in page]
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
            legacy = [s for s in statuses if s["context"] == context] if app_id in (None, -1) else []
            selected = _check_attempts(matching, repo, sha, profile_home)
            latest_legacy = max((s["id"] for s in legacy), default=None)
            selected += [(s, {"selected": s["id"] == latest_legacy,
                              "selection": "selected" if s["id"] == latest_legacy else "historical",
                              "selection_reason": "existing_legacy_latest_id",
                              "superseded_by": None if s["id"] == latest_legacy else latest_legacy})
                         for s in legacy]
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha,
                                          "selected": True, "selection": "selected",
                                          "selection_reason": "missing_required_check"})
            for check, selection in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                if selection["selected"]:
                    outcomes.append(selection.get("lineage_error") or classification)
                receipt["checks"].append({"name": context, "id": check["id"],
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome,
                    "app_id": (check.get("app") or {}).get("id"),
                    "check_suite_id": (check.get("check_suite") or {}).get("id"), **selection})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except _GateAuthError as exc:
        login = f"assignee profile {assignee!r}'s gh login" if assignee else "the ambient gh login"
        receipt.update(classification="auth",
                       detail=f"GitHub refused the acceptance read ({exc}) as {login}; "
                              "fix that profile's GitHub credentials/access to the repository, then retry completion.")
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra", detail="GitHub acceptance evidence unavailable or incomplete; check gh authentication/API access and retry.")
        return receipt


def _positive_id(value) -> bool:
    return type(value) is int and value > 0


def _created_at(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("Workflow creation time has no timezone")
    return stamp


def _actions_lineage(check: dict, repo: str, sha: str, read) -> dict:
    """Prove check -> Actions job -> exact run attempt; URLs alone are not proof."""
    if check["app"].get("id") != 15368 or check["app"].get("slug") != "github-actions":
        raise ValueError("No supported producer lineage")
    url = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/actions/runs/([1-9][0-9]*)/job/([1-9][0-9]*)",
                       check.get("html_url", ""))
    if not url or int(url[2]) != check["id"] or check["head_sha"] != sha:
        raise ValueError("Check/job identity differs")
    job = read(f"repos/{repo}/actions/jobs/{check['id']}")
    if (not _positive_id(check["id"]) or not _positive_id(job["id"]) or not _positive_id(job["run_id"])
            or job["id"] != check["id"] or job["run_id"] != int(url[1]) or job["head_sha"] != sha
            or job["check_run_url"] != f"https://api.github.com/repos/{repo}/check-runs/{check['id']}"
            or not _positive_id(job["run_attempt"])):
        raise ValueError("Job does not prove this check's attempt")
    run = read(f"repos/{repo}/actions/runs/{job['run_id']}/attempts/{job['run_attempt']}")
    if (run["id"] != job["run_id"] or run["run_attempt"] != job["run_attempt"]
            or run["head_sha"] != sha or run["check_suite_id"] != check["check_suite"]["id"]
            or run["repository"]["full_name"] != repo
            or not all(_positive_id(run[k]) for k in ("id", "run_attempt", "check_suite_id", "workflow_id", "run_number"))
            or not all(isinstance(run[k], str) and run[k] for k in ("path", "event", "head_branch"))):
        raise ValueError("Run does not prove this producer/head/suite")
    _created_at(run["created_at"])
    return {k: run[k] for k in ("id", "run_attempt", "workflow_id", "path", "event", "head_branch",
                                "run_number", "created_at", "head_sha", "check_suite_id")}


def _later_attempt(a: dict, b: dict) -> bool:
    """Creation time orders new runs, attempt number orders reruns, never job ID/end time."""
    if a["id"] == b["id"]:
        return (a["run_number"] == b["run_number"] and _created_at(a["created_at"]) == _created_at(b["created_at"])
                and a["run_attempt"] > b["run_attempt"])
    return a["run_number"] > b["run_number"] and _created_at(a["created_at"]) > _created_at(b["created_at"])


def _check_attempts(checks: list[dict], repo: str, sha: str, profile_home: str | None) -> list[tuple[dict, dict]]:
    """Keep every observation; suppress only a totally ordered proven producer lineage.

    Different apps/workflows remain independent requirements. Missing/contradictory
    lineage stays selected and fails closed, even if all ambiguous records are green.
    The extra GETs apply only to duplicate context/app/head observations and always
    use the existing assignee identity. No endpoint chosen from untrusted URLs runs.
    """
    entries = [(check, {"selected": True, "selection": "selected",
                        "selection_reason": "independent_check", "superseded_by": None}) for check in checks]
    buckets = {}
    for check, selection in entries:
        buckets.setdefault(((check.get("app") or {}).get("id"), check.get("head_sha")), []).append((check, selection))
    cache = {}
    def read(endpoint):
        if endpoint not in cache:
            cache[endpoint] = _api(endpoint, profile_home=profile_home)
        return cache[endpoint]

    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        groups = {}
        run_identities = {}
        try:
            for check, selection in bucket:
                lineage = _actions_lineage(check, repo, sha, read)
                selection["lineage"] = lineage
                # One run cannot silently change producer/suite/creation identity on a rerun.
                identity = tuple(lineage[k] for k in ("workflow_id", "path", "event", "head_branch",
                                                      "run_number", "head_sha", "check_suite_id")) + (_created_at(lineage["created_at"]),)
                if run_identities.setdefault(lineage["id"], identity) != identity:
                    raise ValueError("Contradictory identity for one workflow run")
                key = tuple(lineage[k] for k in ("workflow_id", "path", "event", "head_branch"))
                groups.setdefault(key, []).append((check, selection))
        except (_GateAuthError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError) as exc:
            for _, selection in bucket:
                selection.update(selection_reason="unproven_producer_lineage",
                                 lineage_error="auth" if isinstance(exc, _GateAuthError) else "infra")
            continue
        for group in groups.values():
            if len(group) < 2:
                continue
            if not all(_later_attempt(a[1]["lineage"], b[1]["lineage"])
                       or _later_attempt(b[1]["lineage"], a[1]["lineage"])
                       for a, b in combinations(group, 2)):
                for _, selection in group:
                    selection.update(selection_reason="ambiguous_attempt_order", lineage_error="infra")
                continue
            newest = next(item for item in group if all(
                item is other or _later_attempt(item[1]["lineage"], other[1]["lineage"]) for other in group))
            for check, selection in group:
                if check is newest[0]:
                    selection["selection_reason"] = "latest_proven_workflow_attempt"
                else:
                    selection.update(selected=False, selection="historical",
                                     selection_reason="proven_later_workflow_attempt", superseded_by=newest[0]["id"])
    return entries


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
