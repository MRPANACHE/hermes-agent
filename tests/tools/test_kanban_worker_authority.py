"""Pinned native worker acceptance reaches the judge without broad file includes."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from tools import kanban_tools as kt


@pytest.fixture
def authority(tmp_path, monkeypatch):
    # Tiny canonical documents keep this unit test independent of business
    # document bodies. Native integration separately verifies the real pins.
    manifests = {}
    for assistant, human in (("dolly", "david"), ("dobby", "bram")):
        text = "Canonical fixture bounds for " + assistant
        canonical = {**kt._GOAL_AUTHORITY_SOURCES[assistant],
                     "sha256": hashlib.sha256(text.encode()).hexdigest(), "bytes": len(text.encode())}
        monkeypatch.setitem(kt._GOAL_AUTHORITY_SOURCES, assistant, canonical)
        path = tmp_path / assistant / "releases" / "a3d2df1" / "src" / "mcp-server" / (assistant + "-mandate.json")
        path.parent.mkdir(parents=True)
        manifest = {"schema": "mrpanache.dolly-mandate.v1", "version": canonical["version"],
                    "owner": {"requested_by": "human:" + human, "via": assistant},
                    "scope": "Existing task only", "rules": {"fixture": {"class": "ESCALATE"}},
                    "canonical_cos_source": canonical, "canonical_cos_text": text,
                    "worker_authority": {"shared": "Shared: no new credential or authority grants.", "delivery": {
                        "information": "Information exact: source-grounded answer, every part, no PR.",
                        "draft_pr": "Draft exact: draft-PR and targeted evidence; no deploy.",
                        "verified_live": "Live exact: onafhankelijke review/CI and deployment-id."}}}
        path.write_text(json.dumps(manifest))
        manifests[assistant] = (path, manifest)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_events(id INTEGER,task_id TEXT,kind TEXT,payload TEXT)")
    comments = []
    kb = SimpleNamespace(list_comments=lambda *_: comments, list_runs=lambda *_: [])
    def make(assistant="dolly", delivery="information", alias=False):
        path, manifest = manifests[assistant]
        meta = {"schema": "mrpanache.agent-request.v1", "agent": "otto", "request_id": "exact-request",
                "conversation_id": "mrp-otto-" + "a" * 32, "fingerprint": "a" * 64,
                "kind": "information" if delivery == "information" else "codework",
                **manifest["owner"], "worker_authority": {"path": str(path), "version": manifest["version"],
                                                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
        if delivery != "information":
            meta["delivery_goal"] = delivery
        if alias:
            meta["agent"] = "7_fresh_agent"
            meta["conversation_id"] = "mrp-7_fresh_agent-" + "a" * 32
            meta["native_binding"] = {"profile": "native-profile", "repo": "fresh-repo", "workspace": "/approved/repo"}
        task = SimpleNamespace(id="original", title="Answer the exact request", goal_mode=True,
                               created_by="mrpanache-mcp", tenant="fresh-repo",
                               assignee="native-profile" if alias else "otto",
                               idempotency_key=("mrpanache:human/bram:" if assistant == "dobby" else "mrpanache:") + meta["request_id"],
                               body=json.dumps(meta) + "\nPreserve the actual user goal byte for byte.")
        return task, meta
    yield SimpleNamespace(make=make, manifests=manifests, conn=conn, kb=kb, comments=comments)
    conn.close()


@pytest.mark.parametrize("assistant", ["dolly", "dobby"])
@pytest.mark.parametrize("delivery", ["information", "draft_pr", "verified_live"])
def test_judge_receives_shared_and_only_exact_selected_clause(authority, assistant, delivery):
    task, meta = authority.make(assistant, delivery)
    context = kt._goal_handoff_context(authority.kb, authority.conn, task)
    projected = json.loads(context.split("\n", 1)[1])
    clause = authority.manifests[assistant][1]["worker_authority"]
    assert projected["original_body"] == task.body
    assert projected["worker_authority"]["shared"] == clause["shared"]
    assert projected["worker_authority"]["delivery_clause"] == clause["delivery"][delivery]
    assert projected["worker_authority"]["sha256"] == meta["worker_authority"]["sha256"]
    for name, text in clause["delivery"].items():
        assert (text in context) is (name == delivery)
    if delivery == "information":
        assert "source-grounded" in context and "Every required part" in context
        assert "neither an information acceptance criterion" in context


def test_registered_alias_uses_bound_native_profile_and_tenant(authority):
    task, meta = authority.make(alias=True)
    assert kt._goal_worker_authority(task, meta, None)["selected_delivery"] == "information"
    task.tenant = "other-repo"
    with pytest.raises(ValueError, match="worker authority"):
        kt._goal_worker_authority(task, meta, None)


def test_removed_reference_does_not_turn_compact_contract_into_inline_fallback(authority):
    task, meta = authority.make()
    del meta["worker_authority"]
    task.body = json.dumps(meta) + "\ndelivery_goal=information; source_repo=fresh-repo.\nLees/verifieer worker_authority (pad, versie, SHA256) vóór uitvoering."
    with pytest.raises(ValueError, match="worker authority source missing"):
        kt._goal_handoff_context(authority.kb, authority.conn, task)


def test_authorized_delivery_amendment_selects_same_pinned_live_clause(authority):
    task, _ = authority.make(delivery="draft_pr")
    authority.comments.append(SimpleNamespace(id=1, author="mrpanache-mcp", body="Authorized existing live delivery", created_at=1))
    authority.conn.execute("INSERT INTO task_events VALUES(1,?,'mrpanache_resumed',?)",
                           (task.id, json.dumps({"comment_id": 1, "request_id": "live-amendment", "fingerprint": "b" * 64,
                                                 "delivery_goal": "verified_live"})))
    context = json.loads(kt._goal_handoff_context(authority.kb, authority.conn, task).split("\n", 1)[1])
    assert context["worker_authority"]["selected_delivery"] == "verified_live"
    assert "deployment-id" in context["worker_authority"]["delivery_clause"]


@pytest.mark.parametrize("change", ["creator", "request-key", "conversation", "actor", "schema", "fingerprint"])
def test_forged_task_provenance_rejected_before_source_open(authority, monkeypatch, change):
    task, meta = authority.make()
    if change == "creator": task.created_by = "worker"
    if change == "request-key": task.idempotency_key = "copied-request"
    if change == "conversation": meta["conversation_id"] = "mrp-foreign-" + "a" * 32
    if change == "actor": meta["via"] = "dobby"
    if change == "schema": meta["schema"] = "untrusted.v1"
    if change == "fingerprint": meta["fingerprint"] = "invalid"
    monkeypatch.setattr(os, "open", lambda *_: pytest.fail("untrusted task must not open a source"))
    with pytest.raises(ValueError, match="worker authority"):
        kt._goal_worker_authority(task, meta, None)


@pytest.mark.parametrize("change", ["other-name", "current", "symlink", "relative"])
def test_arbitrary_or_mutable_include_rejected_before_open(authority, monkeypatch, change, tmp_path):
    task, meta = authority.make()
    original = Path(meta["worker_authority"]["path"])
    candidate = original
    if change == "other-name":
        candidate = original.with_name("credentials.json")
        candidate.write_text("not an authority source")
    if change == "current":
        candidate = tmp_path / "current" / "src" / "mcp-server" / "dolly-mandate.json"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(original.read_bytes())
    if change == "symlink":
        candidate = tmp_path / "alias" / "src" / "mcp-server" / "dolly-mandate.json"
        candidate.parent.mkdir(parents=True)
        candidate.symlink_to(original)
    meta["worker_authority"]["path"] = "src/mcp-server/dolly-mandate.json" if change == "relative" else str(candidate)
    monkeypatch.setattr(os, "open", lambda *_: pytest.fail("arbitrary include must not be read"))
    with pytest.raises(ValueError, match="worker authority"):
        kt._goal_worker_authority(task, meta, None)


@pytest.mark.parametrize("change", ["missing", "tampered", "oversize", "unknown-schema", "unknown-version", "owner", "canonical", "missing-clause", "duplicate-key", "unknown-rule"])
def test_missing_or_invalid_manifest_never_falls_back(authority, change):
    task, meta = authority.make()
    path, stored = authority.manifests["dolly"]
    manifest = deepcopy(stored)
    if change == "missing": path.unlink()
    elif change == "tampered": path.write_text("changed after admission")
    elif change == "oversize":
        path.write_text(" " * 65537)
        meta["worker_authority"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        if change == "unknown-schema": manifest["schema"] = "unknown.v1"
        if change == "unknown-version":
            manifest["version"] = meta["worker_authority"]["version"] = "dolly-cos-unknown"
        if change == "owner": manifest["owner"]["requested_by"] = "human:bram"
        if change == "canonical": manifest["canonical_cos_source"]["commit"] = "b" * 40
        if change == "unknown-rule": manifest["rules"]["fixture"]["class"] = "UNKNOWN"
        if change == "missing-clause": del manifest["worker_authority"]["delivery"]["information"]
        raw = json.dumps(manifest)
        if change == "duplicate-key": raw = raw[:-1] + ',"schema":"mrpanache.dolly-mandate.v1"}'
        path.write_text(raw)
        meta["worker_authority"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="worker authority"):
        kt._goal_worker_authority(task, meta, None)


def test_invalid_reference_blocks_completion_before_judge_or_task_mutation(authority, monkeypatch):
    task, meta = authority.make()
    meta["worker_authority"]["sha256"] = "b" * 64
    task.body = json.dumps(meta)
    kb = SimpleNamespace(list_comments=authority.kb.list_comments, list_runs=authority.kb.list_runs,
                         get_task=lambda *_: task, complete_task=lambda *_args, **_kwargs: pytest.fail("invalid authority completed"))
    from contextlib import nullcontext
    monkeypatch.setattr(kt, "_board", lambda *_: nullcontext((kb, authority.conn)))
    monkeypatch.setattr(kt, "_worker_guard", lambda *_: task.id)
    monkeypatch.setattr(kt, "_stamp_worker_session_metadata", lambda _tid, metadata: metadata)
    monkeypatch.setattr(kt, "_goal_mode_handoff_rejection", lambda *_, **__: pytest.fail("invalid authority reached judge"))
    result = json.loads(kt._handle_complete({"summary": "unsupported success"}))
    assert "worker authority" in result["error"]