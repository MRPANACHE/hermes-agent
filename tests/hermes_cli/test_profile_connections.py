"""Real profile/config primitives, isolated roots; no model or credentials."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from hermes_cli import profile_connections as owner
from hermes_cli import profiles


def reviewed(path, value):
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(profiles, "_maybe_register_gateway_service", lambda _: None)
    # Existing source identity is deliberately populated, never cloned.
    (root / "config.yaml").write_text("model:\n  provider: anthropic\n  default: existing-model\n")
    (root / ".env").write_text("DO_NOT_COPY=existing-private-value\n")
    (root / "auth.json").write_text('{"do_not_copy":"existing-private-auth"}')
    binding = {"agent_id": "new-agent", "profile": "new-profile", "repo": "new-repo",
               "workspace": str(tmp_path / "workspace")}
    mcp_ref = reviewed(tmp_path / "pb-mcp.json", {"command": "python3", "args": ["/approved/pb.py"],
                                               "env": {"PB_TOKEN": "${PB_TOKEN}"},
                                               "public_env": {"PB_GBRAIN_AGENT_ID": "new-agent",
                                                              "PB_AGENT_CONNECTIONS_FILE": str(tmp_path / "connections.json"),
                                                              "PB_OOS_PROVIDER_COMMIT": "a" * 40,
                                                              "PB_OOS_READ_TRANSPORT": "/approved/pb-read.mjs"}})
    document = {"schema_version": owner.SETUP_SCHEMA, "binding": binding,
                "provider": "openai-codex", "model": "approved-model",
                "soul_ref": reviewed(tmp_path / "mandate.md", "# Bounded mandate\nUse owner evidence.\n"),
                "mcp_servers": [{"name": "pb", "config_ref": mcp_ref}],
                "source_owner": {"repo": binding["repo"], "ref": "github:example/new-repo@reviewed"}}
    request = {"schema_version": owner.REQUEST_SCHEMA, "action": "inspect", "request_id": "setup-1",
               "binding": binding, "setup_ref": reviewed(tmp_path / "setup.json", document)}
    return root, document, request


def apply(request):
    plan = owner.inspect_profile_connection(request)
    return owner.apply_profile_connection({**request, "action": "apply", "expected_plan_digest": plan["plan_digest"]})


def change(request, document, request_id="setup-2"):
    return {**request, "request_id": request_id,
            "setup_ref": reviewed(Path(request["setup_ref"]["path"]), document)}


def config(path):
    return yaml.safe_load((path / "config.yaml").read_text())


def test_fresh_readback_replay_isolated_and_no_credential_access(setup, monkeypatch):
    root, document, request = setup
    original = {name: (root / name).read_bytes() for name in ("config.yaml", ".env", "auth.json")}
    before = owner.inspect_profile_connection(request)
    assert not (root / "profiles").exists()  # Inspect does not create locks/directories.
    assert before == owner.inspect_profile_connection(request)
    other = root / "profiles/other-profile"
    other.mkdir(parents=True)
    (other / "config.yaml").write_text("model: other-existing-model\n")
    (other / "SOUL.md").write_text("Existing unrelated identity")
    other_before = {p.name: p.read_bytes() for p in other.iterdir()}
    with monkeypatch.context() as guarded:
        original_open = Path.open

        def no_credential_read(path, mode="r", *args, **kwargs):
            if path.name in {".env", "auth.json"} and "r" in mode:
                pytest.fail("provisioning read credentials")
            return original_open(path, mode, *args, **kwargs)

        guarded.setattr(Path, "open", no_credential_read)
        result = owner.apply_profile_connection({**request, "action": "apply",
                                                 "expected_plan_digest": before["plan_digest"]})
    profile = root / "profiles" / request["binding"]["profile"]
    assert result["configuration_status"] == "configured"
    assert all(result["readback"][key] for key in ("model_matches", "mandate_matches", "mcp_matches", "workspace_matches"))
    assert result["status"] == "incomplete" and result["runtime_activation"] == "unverified"
    assert result["auth"] == {"status": "missing", "reason": "existing_provider_auth_binding_not_supplied"}
    assert config(profile)["model"] == {"provider": document["provider"], "default": document["model"]}
    pb_config = config(profile)["mcp_servers"]["pb"]
    assert pb_config["env"] == {"PB_TOKEN": "${PB_TOKEN}", "PB_GBRAIN_AGENT_ID": "new-agent",
                                "PB_AGENT_CONNECTIONS_FILE": str(root.parent / "connections.json"),
                                "PB_OOS_PROVIDER_COMMIT": "a" * 40,
                                "PB_OOS_READ_TRANSPORT": "/approved/pb-read.mjs"}
    assert "public_env" not in pb_config
    assert config(profile)["terminal"]["cwd"] == request["binding"]["workspace"]
    assert (profile / "SOUL.md").read_bytes() == Path(document["soul_ref"]["path"]).read_bytes()
    assert not (profile / "auth.json").exists()
    assert "DO_NOT_COPY" not in (profile / ".env").read_text()
    assert {name: (root / name).read_bytes() for name in original} == original
    assert {p.name: p.read_bytes() for p in other.iterdir()} == other_before
    assert owner.apply_profile_connection({**request, "action": "apply", "expected_plan_digest": before["plan_digest"]}) == result
    assert list(root.glob("profiles/new-profile")) == [profile]


def test_existing_central_auth_binding_stays_unverified_without_reading_credentials(setup):
    root, document, request = setup
    document["auth_binding"] = {"kind": "existing_provider_auth", "provider": document["provider"]}
    request = change(request, document)
    result = apply(request)
    assert result["configuration_status"] == "configured"
    assert result["auth"] == {"status": "unverified", "reason": "existing_provider_resolver_not_invoked"}
    assert not (root / "profiles/new-profile/auth.json").exists()


def test_unmanaged_profile_is_inspected_but_never_adopted(setup):
    root, _, request = setup
    profile = root / "profiles/new-profile"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: private-existing-model\n")
    result = owner.inspect_profile_connection(request)
    assert result["reason"] == "profile_unmanaged" and result["plan_digest"] is None
    with pytest.raises(ValueError, match="profile_unmanaged"):
        owner.apply_profile_connection({**request, "action": "apply", "expected_plan_digest": "0" * 64})
    assert (profile / "config.yaml").read_text() == "model: private-existing-model\n"


def test_partial_config_write_resumes_original_digest_after_module_reload(setup, monkeypatch):
    root, _, request = setup
    plan = owner.inspect_profile_connection(request)
    mutation = {**request, "action": "apply", "expected_plan_digest": plan["plan_digest"]}
    with monkeypatch.context() as broken:
        broken.setattr(owner, "_save_mcp_server", lambda *a, **k: (_ for _ in ()).throw(OSError("disk error")))
        partial = owner.apply_profile_connection(mutation)
    assert partial["configuration_status"] == "incomplete"
    assert partial["reason"] == "profile_setup_partial_apply"
    assert partial["readback"]["model_matches"] and partial["readback"]["mandate_matches"]
    assert not partial["readback"]["mcp_matches"]
    import importlib
    importlib.reload(owner)
    assert owner.inspect_profile_connection(request)["plan_digest"] == plan["plan_digest"]
    result = owner.apply_profile_connection(mutation)
    assert result["configuration_status"] == "configured"
    marker = yaml.safe_load((root / "profiles/new-profile/profile.yaml").read_text())["connection_setup"]
    assert "pending" not in marker and marker["last_operation"]["plan_digest"] == plan["plan_digest"]


def test_partial_bootstrap_resumes_without_cloning_or_duplicate_profile(setup, monkeypatch):
    root, _, request = setup
    mutation = {**request, "action": "apply", "expected_plan_digest": owner.inspect_profile_connection(request)["plan_digest"]}
    with monkeypatch.context() as broken:
        broken.setattr(owner, "_write_model", lambda *_: (_ for _ in ()).throw(OSError("interrupted")))
        assert owner.apply_profile_connection(mutation)["configuration_status"] == "incomplete"
    assert owner.apply_profile_connection(mutation)["configuration_status"] == "configured"
    assert list((root / "profiles").glob("new-profile*")) == [root / "profiles/new-profile"]


def test_interruption_inside_canonical_bootstrap_finishes_same_owned_profile(setup, monkeypatch):
    root, _, request = setup
    mutation = {**request, "action": "apply", "expected_plan_digest": owner.inspect_profile_connection(request)["plan_digest"]}
    actual_write = profiles.write_profile_meta

    def interrupt_after_ownership(path, **kwargs):
        actual_write(path, **kwargs)
        raise OSError("bootstrap interrupted after owner metadata")

    with monkeypatch.context() as broken:
        broken.setattr(profiles, "write_profile_meta", interrupt_after_ownership)
        assert owner.apply_profile_connection(mutation)["configuration_status"] == "incomplete"
    profile = root / "profiles/new-profile"
    assert not (profile / profiles.NO_BUNDLED_SKILLS_MARKER).exists()
    assert owner.apply_profile_connection(mutation)["configuration_status"] == "configured"
    assert (profile / profiles.NO_BUNDLED_SKILLS_MARKER).exists()
    assert (profile / "skills").is_dir()
    assert not (profile / "auth.json").exists()
    # Ordinary creation still refuses an already-existing managed profile.
    with pytest.raises(FileExistsError):
        profiles.create_profile("new-profile")


def test_managed_update_preserves_unmanaged_fields_and_removes_only_owned_mcp(setup, monkeypatch):
    root, document, request = setup
    assert apply(request)["configuration_status"] == "configured"
    profile = root / "profiles/new-profile"
    value = config(profile)
    value["terminal"] = {"backend": "local", "cwd": request["binding"]["workspace"], "timeout": 17}
    value["mcp_servers"]["unmanaged"] = {"url": "https://example.org/mcp", "headers": {"Authorization": "Bearer ${OTHER_TOKEN}"}}
    (profile / "config.yaml").write_text(yaml.safe_dump(value))
    profiles.write_profile_meta(profile, display_name="Keep this label")
    monkeypatch.setenv("OTHER_TOKEN", "must-not-materialize")
    document["model"] = "new-approved-model"
    document["mcp_servers"] = []
    request = change(request, document)
    result = apply(request)
    assert result["configuration_status"] == "configured"
    updated = config(profile)
    assert updated["terminal"] == value["terminal"]
    assert updated["mcp_servers"] == {"unmanaged": value["mcp_servers"]["unmanaged"]}
    assert updated["model"]["default"] == "new-approved-model"
    assert yaml.safe_load((profile / "profile.yaml").read_text())["display_name"] == "Keep this label"


@pytest.mark.parametrize("field", ["model", "soul", "mcp", "workspace"])
def test_external_managed_drift_blocks_reapply(setup, field):
    root, document, request = setup
    first = apply(request)
    profile = root / "profiles/new-profile"
    if field == "soul":
        (profile / "SOUL.md").write_text("Changed by another owner")
    else:
        value = config(profile)
        if field == "model":
            value["model"]["default"] = "external-model"
        elif field == "workspace":
            value["terminal"]["cwd"] = "/external-workspace"
        else:
            value["mcp_servers"]["pb"]["args"] = ["/external.py"]
        (profile / "config.yaml").write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError, match="profile_setup_drift"):
        owner.apply_profile_connection({**request, "action": "apply", "expected_plan_digest": first["plan_digest"]})


def test_reviewed_bytes_binding_plan_and_request_conflicts_fail_before_mutation(setup):
    root, document, request = setup
    plan = owner.inspect_profile_connection(request)
    with pytest.raises(ValueError, match="profile_setup_plan_changed"):
        owner.apply_profile_connection({**request, "action": "apply", "expected_plan_digest": "0" * 64})
    assert not (root / "profiles/new-profile").exists()
    Path(document["soul_ref"]["path"]).write_text("Changed after review")
    with pytest.raises(ValueError, match="reference_changed"):
        owner.inspect_profile_connection(request)
    document["soul_ref"] = reviewed(Path(document["soul_ref"]["path"]), "Approved mandate")
    request = change(request, document)
    assert apply(request)["configuration_status"] == "configured"
    changed = deepcopy(document)
    changed["model"] = "other-model"
    conflicting = change(request, changed, request_id=request["request_id"])
    with pytest.raises(ValueError, match="profile_setup_request_conflict"):
        owner.inspect_profile_connection(conflicting)
    mismatched = {**conflicting, "binding": {**request["binding"], "repo": "other-repo"}}
    with pytest.raises(ValueError, match="profile_setup_document_invalid"):
        owner.inspect_profile_connection(mismatched)


@pytest.mark.parametrize("config_value", [
    {"url": "https://example.org/mcp", "headers": {"Authorization": "Bearer literal-secret"}},
    {"command": "python", "env": {"PB_TOKEN": "literal-secret"}},
    {"url": "https://user:password@example.org/mcp"},
    {"url": "https://example.org/mcp?token=literal-secret"},
    {"url": "https://example.org/mcp", "api_key": "literal-secret"},
    {"command": "python", "public_env": {"PB_TOKEN": "literal-secret"}},
    {"command": "python", "env": {"PB_TOKEN": "${PB_TOKEN}"}, "public_env": {"PB_TOKEN": "literal-secret"}},
])
def test_inline_credentials_rejected_before_profile_creation(setup, config_value):
    root, document, request = setup
    document["mcp_servers"][0]["config_ref"] = reviewed(root.parent / "bad-mcp.json", config_value)
    with pytest.raises(ValueError, match="profile_setup_"):
        owner.inspect_profile_connection(change(request, document))
    assert not (root / "profiles").exists()


def test_pending_drift_and_replacement_operation_are_refused(setup, monkeypatch):
    root, _, request = setup
    mutation = {**request, "action": "apply", "expected_plan_digest": owner.inspect_profile_connection(request)["plan_digest"]}
    with monkeypatch.context() as broken:
        broken.setattr(owner, "_write_model", lambda *_: (_ for _ in ()).throw(OSError("interrupted")))
        assert owner.apply_profile_connection(mutation)["reason"] == "profile_setup_partial_apply"
    with pytest.raises(ValueError, match="profile_setup_pending_operation"):
        owner.inspect_profile_connection({**request, "request_id": "replace-pending"})
    (root / "profiles/new-profile/SOUL.md").write_text("External change during interruption")
    with pytest.raises(ValueError, match="profile_setup_drift"):
        owner.apply_profile_connection(mutation)
