"""Attempt precedence: real incident evidence, collector and native SQLite lifecycle.

GitHub GET responses are projected in fixtures/pr_acceptance_attempts.json.
Mutations below are explicitly synthetic counterexamples, not live gate evidence.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance as gate
from hermes_cli.kanban_db_connect import connect

REPO = "MRPANACHE/bol-bidbot"
PR = f"https://github.com/{REPO}/pull/3169"
CONTEXT = "commit-knowledge-contract"


@pytest.fixture
def evidence():
    data = json.loads((Path(__file__).parent / "fixtures/pr_acceptance_attempts.json").read_text())
    data["checks"].sort(key=lambda row: row["id"])
    data.update(required=[{"context": CONTEXT, "app_id": None}], statuses=[], calls=[],
                head=data["checks"][0]["head_sha"], base="main", final_head=None,
                final_base=None, pr_state="open", incomplete=False, errors=set())
    return data


def response(data, endpoint):
    data["calls"].append(endpoint)
    if endpoint in data["errors"]:
        raise subprocess.CalledProcessError(1, ["gh", "api", endpoint])
    sha = data["head"]
    if endpoint == "graphql":
        return {"data": {"repository": {"pullRequest": {
            "headRefOid": sha, "baseRefName": data["base"], "state": "OPEN",
            "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                {"context": row["context"], "app": {"databaseId": row["app_id"]}}
                for row in data["required"]]}}}}}}
    if "/rules/branches/" in endpoint:
        return [[]]
    if "/check-runs?" in endpoint:
        # Two pages: the incident genuinely spans two suites on the same head.
        return [{"total_count": len(data["checks"]) + int(data["incomplete"]),
                 "check_runs": data["checks"][:1]},
                {"total_count": len(data["checks"]), "check_runs": data["checks"][1:]}]
    if "/statuses?" in endpoint:
        return [data["statuses"]]
    if "/actions/jobs/" in endpoint:
        return next(row for row in data["jobs"] if endpoint.endswith(f"/{row['id']}"))
    if "/attempts/" in endpoint:
        return next(row for row in data["attempts"] if endpoint.endswith(
            f"/actions/runs/{row['id']}/attempts/{row['run_attempt']}"))
    if "/pulls/" in endpoint:
        if data.get("race"):
            data["race"]()
        return {"head": {"sha": data["final_head"] or sha},
                "base": {"ref": data["final_base"] or data["base"]},
                "state": data["pr_state"], "merged": False}
    raise AssertionError(f"Unexpected API endpoint: {endpoint}")


def collect(monkeypatch, data):
    def api(endpoint, **kwargs):
        assert kwargs.get("profile_home") == "/test/assignee"
        return response(data, endpoint)
    monkeypatch.setattr(gate, "_assignee_profile_home", lambda who: "/test/assignee")
    monkeypatch.setattr(gate, "_api", api)
    return gate.collect_acceptance(PR, PR, assignee="test")


def records(receipt):
    return {row["id"]: row for row in receipt["checks"] if "id" in row}


def newer_outcome(data, outcome):
    data["checks"][0].update(status="completed", conclusion="success")
    data["checks"][1].update(status="in_progress" if outcome == "pending" else "completed",
                              conclusion=None if outcome == "pending" else outcome)


@pytest.mark.parametrize("reverse", [False, True])
def test_incident_new_success_supersedes_old_failure_only_with_proof(monkeypatch, evidence, reverse):
    old, new = evidence["checks"]
    if reverse:
        evidence["checks"].reverse()
    receipt = collect(monkeypatch, evidence)
    assert receipt["ok"]
    observed = records(receipt)
    assert set(observed) == {old["id"], new["id"]}
    assert observed[new["id"]]["selected"] is True
    assert observed[old["id"]]["selected"] is False
    assert observed[old["id"]]["selection"] == "historical"
    assert observed[old["id"]]["superseded_by"] == new["id"]
    assert observed[old["id"]]["classification"] == "failure"
    assert observed[old["id"]]["selection_reason"] == "proven_later_workflow_attempt"
    assert observed[new["id"]]["lineage"]["workflow_id"] == 235269881
    assert any("/attempts/1" in call for call in evidence["calls"])


@pytest.mark.parametrize("outcome", ["failure", "pending", "cancelled", "timed_out", "neutral", "skipped"])
def test_new_failure_or_pending_never_uses_older_success(monkeypatch, evidence, outcome):
    newer_outcome(evidence, outcome)
    receipt = collect(monkeypatch, evidence)
    assert not receipt["ok"]
    assert records(receipt)[evidence["checks"][1]["id"]]["selected"] is True
    assert records(receipt)[evidence["checks"][0]["id"]]["selected"] is False


@pytest.mark.parametrize("field,value", [
    ("workflow_id", 777), ("path", ".github/workflows/other.yml"),
    ("event", "push"), ("head_branch", "independent")])
def test_independent_workflow_producers_keep_their_veto(monkeypatch, evidence, field, value):
    evidence["attempts"][1][field] = value
    receipt = collect(monkeypatch, evidence)
    assert not receipt["ok"]
    assert all(row["selected"] for row in receipt["checks"])
    assert len(receipt["checks"]) == 2


@pytest.mark.parametrize("pin,expected", [(None, False), (-1, False), (15368, True), (999, False)])
def test_apps_never_supersede_each_other(monkeypatch, evidence, pin, expected):
    evidence["checks"][0]["app"] = {"id": 999, "slug": "another-app"}
    evidence["required"][0]["app_id"] = pin
    receipt = collect(monkeypatch, evidence)
    assert receipt["ok"] is expected
    assert all(row.get("selected", True) for row in receipt["checks"])


@pytest.mark.parametrize("fault", [
    "missing_suite", "unknown_app", "missing_job_attempt", "wrong_job_id", "wrong_job_head",
    "wrong_job_check_url", "wrong_job_run", "wrong_html_url", "wrong_run_head",
    "wrong_run_suite", "wrong_repository", "missing_workflow", "missing_run_number",
    "missing_time", "naive_time", "equal_times", "equal_numbers", "reversed_time",
    "reversed_number", "duplicate_same_attempt", "job_api_error", "attempt_api_error"])
def test_ambiguous_lineage_never_suppresses_checks(monkeypatch, evidence, fault):
    check = evidence["checks"][1]
    job = evidence["jobs"][1]
    run = evidence["attempts"][1]
    if fault == "missing_suite": check.pop("check_suite")
    elif fault == "unknown_app": check["app"]["slug"] = "unverified"
    elif fault == "missing_job_attempt": job.pop("run_attempt")
    elif fault == "wrong_job_id":
        # Keep endpoint routing unchanged while returning a contradictory id.
        original_response = response
        def wrong_response(data, endpoint):
            value = original_response(data, endpoint)
            return {**value, "id": 7} if endpoint.endswith(f"/jobs/{job['id']}") else value
        monkeypatch.setattr(sys.modules[__name__], "response", wrong_response)
    elif fault == "wrong_job_head": job["head_sha"] = "b" * 40
    elif fault == "wrong_job_check_url": job["check_run_url"] = job["check_run_url"].replace(REPO, "other/repo")
    elif fault == "wrong_job_run": job["run_id"] = 7
    elif fault == "wrong_html_url": check["html_url"] = check["html_url"].replace(REPO, "other/repo")
    elif fault == "wrong_run_head": run["head_sha"] = "b" * 40
    elif fault == "wrong_run_suite": run["check_suite_id"] = 7
    elif fault == "wrong_repository": run["repository"]["full_name"] = "other/repo"
    elif fault == "missing_workflow": run.pop("workflow_id")
    elif fault == "missing_run_number": run.pop("run_number")
    elif fault == "missing_time": run.pop("created_at")
    elif fault == "naive_time": run["created_at"] = "2026-10-27T18:51:37"
    elif fault == "equal_times": run["created_at"] = evidence["attempts"][0]["created_at"]
    elif fault == "equal_numbers": run["run_number"] = evidence["attempts"][0]["run_number"]
    elif fault == "reversed_time": run["created_at"] = "2026-10-27T18:40:00Z"
    elif fault == "reversed_number": run["run_number"] = 1
    elif fault == "duplicate_same_attempt":
        run.update(copy.deepcopy(evidence["attempts"][0]))
        job["run_id"] = run["id"]
        check["check_suite"]["id"] = run["check_suite_id"]
        check["html_url"] = f"https://github.com/{REPO}/actions/runs/{run['id']}/job/{check['id']}"
    elif fault == "job_api_error": evidence["errors"].add(f"repos/{REPO}/actions/jobs/{job['id']}")
    elif fault == "attempt_api_error": evidence["errors"].add(
        f"repos/{REPO}/actions/runs/{run['id']}/attempts/{run['run_attempt']}")
    receipt = collect(monkeypatch, evidence)
    assert not receipt["ok"]
    assert len(receipt["checks"]) == 2
    assert all(row["selected"] for row in receipt["checks"])
    assert all(row["selection_reason"] for row in receipt["checks"])


def rerun(data):
    old, new = data["attempts"]
    new.update(copy.deepcopy(old))
    new["run_attempt"] = 2
    data["jobs"][1].update(run_id=old["id"], run_attempt=2)
    check = data["checks"][1]
    check["check_suite"]["id"] = old["check_suite_id"]
    check["html_url"] = f"https://github.com/{REPO}/actions/runs/{old['id']}/job/{check['id']}"


@pytest.mark.parametrize("outcome", ["success", "failure", "pending"])
def test_rerun_attempts_are_ordered_within_the_same_run(monkeypatch, evidence, outcome):
    rerun(evidence)
    if outcome != "success": newer_outcome(evidence, outcome)
    receipt = collect(monkeypatch, evidence)
    assert receipt["ok"] is (outcome == "success")
    observed = records(receipt)
    assert not observed[evidence["checks"][0]["id"]]["selected"]
    assert observed[evidence["checks"][1]["id"]]["selected"]
    assert observed[evidence["checks"][1]["id"]]["lineage"]["run_attempt"] == 2


def test_rerun_with_changed_creation_time_is_ambiguous(monkeypatch, evidence):
    rerun(evidence)
    evidence["attempts"][1]["created_at"] = "2026-10-27T19:30:00Z"
    receipt = collect(monkeypatch, evidence)
    assert not receipt["ok"]
    assert all(row["selected"] for row in receipt["checks"])


@pytest.mark.parametrize("legacy_state", ["success", "failure", "pending"])
def test_legacy_latest_status_remains_independent_and_history_retained(monkeypatch, evidence, legacy_state):
    evidence["statuses"] = [{"id": 1, "context": CONTEXT, "state": "failure"},
                            {"id": 2, "context": CONTEXT, "state": legacy_state}]
    receipt = collect(monkeypatch, evidence)
    assert receipt["ok"] is (legacy_state == "success")
    observed = records(receipt)
    assert len(observed) == 4
    assert not observed[1]["selected"] and observed[1]["classification"] == "failure"
    assert observed[2]["selected"]


@pytest.mark.parametrize("fault", ["head_change", "base_change", "closed", "incomplete", "wrong_head", "missing"])
def test_existing_exact_head_and_pagination_guards_survive(monkeypatch, evidence, fault):
    if fault == "head_change": evidence["final_head"] = "b" * 40
    elif fault == "base_change": evidence["final_base"] = "other"
    elif fault == "closed": evidence["pr_state"] = "closed"
    elif fault == "incomplete": evidence["incomplete"] = True
    elif fault == "wrong_head": evidence["checks"][1]["head_sha"] = "b" * 40
    elif fault == "missing": evidence["checks"] = []
    assert not collect(monkeypatch, evidence)["ok"]


@pytest.mark.parametrize("field,value", [
    ("workflow_id", None), ("created_at", "2026-10-27T18:49:44Z"),
    ("run_number", 7724), ("path", None)])
def test_ambiguous_all_green_is_still_failclosed(monkeypatch, evidence, field, value):
    for check in evidence["checks"]:
        check["conclusion"] = "success"
    evidence["attempts"][1][field] = value
    receipt = collect(monkeypatch, evidence)
    assert not receipt["ok"]
    assert all(row["selected"] for row in receipt["checks"])
    assert all(row["classification"] == "success" for row in receipt["checks"])


@pytest.mark.parametrize("field,value", [("workflow_id", 9), ("check_suite_id", 9),
                                         ("path", ".github/workflows/other.yml"), ("run_number", 999)])
def test_rerun_cannot_change_run_identity(monkeypatch, evidence, field, value):
    rerun(evidence)
    evidence["attempts"][1][field] = value
    if field == "check_suite_id":
        evidence["checks"][1]["check_suite"]["id"] = value
    receipt = collect(monkeypatch, evidence)
    assert not receipt["ok"]
    assert all(row["selected"] for row in receipt["checks"])


def test_creation_time_uses_timezone_not_lexical_order(monkeypatch, evidence):
    evidence["attempts"][1]["created_at"] = "2026-10-27T19:51:37+01:00"
    assert collect(monkeypatch, evidence)["ok"]


def test_larger_check_id_and_later_completion_do_not_choose_winner(monkeypatch, evidence):
    # Job ids and completion times are not producer ordering. Old job can end last.
    check, job = evidence["checks"][0], evidence["jobs"][0]
    check["id"] += 100000000
    check["html_url"] = f"https://github.com/{REPO}/actions/runs/{job['run_id']}/job/{check['id']}"
    check["completed_at"] = "2026-10-27T20:30:00Z"
    job["id"] = check["id"]
    job["check_run_url"] = f"https://api.github.com/repos/{REPO}/check-runs/{check['id']}"
    receipt = collect(monkeypatch, evidence)
    assert receipt["ok"]
    assert not records(receipt)[check["id"]]["selected"]


def test_third_suite_in_other_workflow_still_blocks(monkeypatch, evidence):
    check, job, run = (copy.deepcopy(evidence[key][0]) for key in ("checks", "jobs", "attempts"))
    check["id"] += 700000000
    job.update(id=check["id"], run_id=7, check_run_url=f"https://api.github.com/repos/{REPO}/check-runs/{check['id']}")
    check["html_url"] = f"https://github.com/{REPO}/actions/runs/7/job/{check['id']}"
    run.update(id=7, workflow_id=7)
    evidence["checks"].append(check)
    evidence["jobs"].append(job)
    evidence["attempts"].append(run)
    receipt = collect(monkeypatch, evidence)
    assert not receipt["ok"]
    observed = records(receipt)
    assert len(observed) == 3
    assert observed[check["id"]]["selected"]
    assert not observed[evidence["checks"][0]["id"]]["selected"]


@pytest.mark.parametrize("pin,expected", [(None, False), (15368, True)])
def test_other_required_context_and_pinned_legacy_not_masked(monkeypatch, evidence, pin, expected):
    evidence["required"].append({"context": "other-required", "app_id": pin})
    other = {**copy.deepcopy(evidence["checks"][1]), "id": 88, "name": "other-required"}
    evidence["checks"].append(other)
    evidence["statuses"].append({"id": 90, "context": "other-required", "state": "failure"})
    assert collect(monkeypatch, evidence)["ok"] is expected


@pytest.mark.parametrize("state,expected", [("success", True), ("failure", False), ("pending", False)])
def test_legacy_only_retains_existing_latest_id_contract(monkeypatch, evidence, state, expected):
    evidence["checks"] = []
    evidence["statuses"] = [{"id": 1, "context": CONTEXT, "state": "failure"},
                            {"id": 2, "context": CONTEXT, "state": state}]
    receipt = collect(monkeypatch, evidence)
    assert receipt["ok"] is expected
    assert len(receipt["checks"]) == 2
    assert not records(receipt)[1]["selected"]
    assert records(receipt)[2]["selected"]


def test_single_check_does_not_require_actions_lineage(monkeypatch, evidence):
    evidence["checks"] = evidence["checks"][1:]
    assert collect(monkeypatch, evidence)["ok"]
    assert not any("/actions/" in call for call in evidence["calls"])


@pytest.fixture
def http_github(tmp_path, monkeypatch, evidence):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                value = response(evidence, self.path.removeprefix("/"))
            except (subprocess.SubprocessError, StopIteration):
                self.send_error(500)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys, urllib.request\n"
                  f"url='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(url).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield evidence
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("outcome", ["success", "failure", "pending"])
def test_native_completion_keeps_prior_receipt_and_uses_proven_attempt(http_github, outcome):
    data = http_github
    with connect() as conn:
        tid = kb.create_task(conn, title="Incident fixture", completion_contract=PR)
        owner = kb.claim_task(conn, tid)
        run_id = owner.current_run_id
        # First real native refusal; the same task/run then observes the later suite.
        data["checks"][1]["conclusion"] = "failure"
        assert not kb.complete_task(conn, tid, summary="first", expected_run_id=run_id)
        first = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0]
        if outcome == "success": data["checks"][1]["conclusion"] = "success"
        else: newer_outcome(data, outcome)
        assert kb.complete_task(conn, tid, summary="later evidence", expected_run_id=run_id) is (outcome == "success")
        receipts = [row[0] for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance' ORDER BY id", (tid,))]
        assert len(receipts) == 2 and receipts[0] == first
        latest = json.loads(receipts[1])
        assert latest["ok"] is (outcome == "success")
        assert len(latest["checks"]) == 2
        assert any(not row["selected"] for row in latest["checks"])
        assert (kb.get_task(conn, tid).status == "done") is (outcome == "success")


@pytest.mark.platforms("posix")
def test_native_attempt_collection_still_rejects_run_ownership_race(http_github):
    data = http_github
    with connect() as conn:
        tid = kb.create_task(conn, title="Race", completion_contract=PR)
        run_id = kb.claim_task(conn, tid).current_run_id
        def reclaim():
            with connect() as rival:
                assert kb.block_task(rival, tid, reason="replacement fixture")
                assert kb.unblock_task(rival, tid)
                data["replacement"] = kb.claim_task(rival, tid).current_run_id
        data["race"] = reclaim
        assert not kb.complete_task(conn, tid, summary="raced", expected_run_id=run_id)
        assert kb.get_task(conn, tid).current_run_id == data["replacement"] != run_id
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()
