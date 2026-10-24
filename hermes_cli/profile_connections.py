"""Reviewed, owner-side profile setup; no provider calls or credential copying.

The authenticated operator transport supplies requests. This module changes only
the named profile's explicitly managed model, SOUL and MCP entries. Ownership and
resumable operation snapshots live in the existing profile.yaml metadata.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from hermes_cli import profiles
from hermes_cli.config import read_user_config_raw, save_config
from hermes_cli.kanban_connections import _binding
from hermes_cli.mcp_config import _remove_mcp_server, _save_mcp_server
from hermes_cli.mcp_security import validate_mcp_server_entry
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from utils import atomic_write_text

REQUEST_SCHEMA = "hermes.profile_connection_request.v1"
SETUP_SCHEMA = "hermes.profile_setup.v1"
RECEIPT_SCHEMA = "hermes.profile_connection_receipt.v1"
OWNER_SCHEMA = "hermes.profile_connection_owner.v1"
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
_ENV = re.compile(r"^(?:Bearer )?\$\{[A-Z][A-Z0-9_]*\}$")
_CREDENTIAL_ENV = re.compile(r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?|BEARER|AUTH|COOKIE)$", re.IGNORECASE)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and not re.search(r"[\x00-\x1f]", value)


def _reviewed_bytes(reference):
    if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
            or not _text(reference.get("path")) or not isinstance(reference.get("sha256"), str)
            or not _SHA.fullmatch(reference["sha256"])):
        raise ValueError("profile_setup_reference_invalid")
    path = Path(reference["path"])
    if (not path.is_absolute() or path.name in {".env", "auth.json", "credentials.json"}
            or path.resolve() != path or not path.is_file()):
        raise ValueError("profile_setup_reference_invalid")
    with path.open("rb") as source:
        value = source.read(1024 * 1024 + 1)
    if len(value) > 1024 * 1024 or hashlib.sha256(value).hexdigest() != reference["sha256"]:
        raise ValueError("profile_setup_reference_changed")
    return value


def _mcp_config(reference):
    try:
        config = json.loads(_reviewed_bytes(reference))
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("profile_setup_mcp_invalid") from None
    allowed = {"command", "args", "url", "transport", "auth", "headers", "env", "public_env", "tools",
               "enabled", "disabled", "timeout", "connect_timeout", "read_timeout"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("profile_setup_mcp_invalid")
    if bool(config.get("command")) == bool(config.get("url")):
        raise ValueError("profile_setup_mcp_invalid")
    if config.get("command") is not None and not _text(config["command"]):
        raise ValueError("profile_setup_mcp_invalid")
    if "args" in config and (not isinstance(config["args"], list) or not all(_text(a) for a in config["args"])):
        raise ValueError("profile_setup_mcp_invalid")
    if config.get("url"):
        if not _text(config["url"]):
            raise ValueError("profile_setup_mcp_invalid")
        url = urlsplit(config["url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("profile_setup_mcp_invalid")
    # Dit pakket bevat alleen bestaande envverwijzingen, nooit credentialwaarden.
    for key in ("env", "headers"):
        values = config.get(key, {})
        if not isinstance(values, dict) or any(not _text(k) or not isinstance(v, str) or not _ENV.fullmatch(v)
                                                for k, v in values.items()):
            raise ValueError("profile_setup_inline_credentials_forbidden")
    public = config.pop("public_env", {})
    if (not isinstance(public, dict) or set(public) & set(config.get("env", {}))
            or any(not _text(k) or _CREDENTIAL_ENV.search(k) or not _text(v) for k, v in public.items())):
        raise ValueError("profile_setup_public_env_invalid")
    if public:
        config["env"] = {**config.get("env", {}), **public}
    if config.get("auth") not in (None, "none", "oauth", "header"):
        raise ValueError("profile_setup_mcp_invalid")
    if validate_mcp_server_entry("reviewed", config):
        raise ValueError("profile_setup_mcp_invalid")
    return config


def _request(request, action):
    allowed = {"schema_version", "action", "request_id", "binding", "setup_ref"}
    if action == "apply":
        allowed.add("expected_plan_digest")
    if (not isinstance(request, dict) or set(request) != allowed
            or request.get("schema_version") != REQUEST_SCHEMA or request.get("action") != action
            or not _text(request.get("request_id")) or len(request["request_id"]) > 200
            or (action == "apply" and (not isinstance(request["expected_plan_digest"], str)
                                      or not _SHA.fullmatch(request["expected_plan_digest"])))):
        raise ValueError("profile_setup_request_invalid")
    binding = _binding(request["binding"])
    profiles.validate_profile_name(binding["profile"])
    if binding["profile"] == "default":
        raise ValueError("profile_setup_default_profile_forbidden")
    try:
        setup = json.loads(_reviewed_bytes(request["setup_ref"]))
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("profile_setup_document_invalid") from None
    fields = {"schema_version", "binding", "provider", "model", "soul_ref", "mcp_servers", "source_owner"}
    if (not isinstance(setup, dict) or set(setup) - fields - {"auth_binding"} or fields - set(setup)
            or setup["schema_version"] != SETUP_SCHEMA or _binding(setup["binding"]) != binding
            or not all(_text(setup.get(k)) for k in ("provider", "model"))
            or not isinstance(setup["source_owner"], dict) or set(setup["source_owner"]) != {"repo", "ref"}
            or setup["source_owner"]["repo"] != binding["repo"] or not _text(setup["source_owner"]["ref"])
            or not isinstance(setup["mcp_servers"], list)):
        raise ValueError("profile_setup_document_invalid")
    auth = setup.get("auth_binding")
    if auth is not None and auth != {"kind": "existing_provider_auth", "provider": setup["provider"]}:
        raise ValueError("profile_setup_auth_binding_invalid")
    try:
        soul = _reviewed_bytes(setup["soul_ref"]).decode("utf-8")
    except UnicodeError:
        raise ValueError("profile_setup_soul_invalid") from None
    if not soul.strip():
        raise ValueError("profile_setup_soul_invalid")
    mcp = {}
    for server in setup["mcp_servers"]:
        if (not isinstance(server, dict) or set(server) != {"name", "config_ref"}
                or not isinstance(server.get("name"), str) or not _NAME.fullmatch(server["name"])
                or server["name"] in mcp):
            raise ValueError("profile_setup_mcp_invalid")
        mcp[server["name"]] = _mcp_config(server["config_ref"])
    return binding, setup, soul, mcp


def _metadata(path):
    import yaml
    source = path / "profile.yaml"
    if not source.exists():
        return {}
    value = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("profile_setup_metadata_invalid")
    return value


@contextmanager
def _scope(path):
    token = set_hermes_home_override(str(path))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _snapshot(path, names):
    # Raw diagnostic hashes preserve env templates and never return their values.
    config = read_user_config_raw(path / "config.yaml") if (path / "config.yaml").exists() else {}
    model = config.get("model", {})
    if not isinstance(model, dict):
        raise ValueError("profile_setup_model_invalid")
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("profile_setup_mcp_invalid")
    soul = path / "SOUL.md"
    terminal = config.get("terminal", {})
    if not isinstance(terminal, dict):
        raise ValueError("profile_setup_terminal_invalid")
    return {"model": _digest({k: model.get(k) for k in ("provider", "default")}),
            "workspace": _digest(terminal.get("cwd")),
            "soul": hashlib.sha256(soul.read_bytes()).hexdigest() if soul.exists() else None,
            "mcp": {name: _digest(servers[name]) if name in servers else None for name in names}}


def _desired(setup, soul, mcp, names):
    return {"model": _digest({"provider": setup["provider"], "default": setup["model"]}),
            "workspace": _digest(setup["binding"]["workspace"]),
            "soul": hashlib.sha256(soul.encode()).hexdigest(),
            "mcp": {name: _digest(mcp[name]) if name in mcp else None for name in names}}


def _compatible(current, before, desired):
    return (all(current[k] in (before[k], desired[k]) for k in ("model", "soul", "workspace"))
            and all(current["mcp"][name] in (before["mcp"][name], desired["mcp"][name])
                    for name in current["mcp"]))


def _plan(request, parsed):
    binding, setup, soul, mcp = parsed
    path = profiles.get_profile_dir(binding["profile"])
    if path.is_symlink():
        raise ValueError("profile_setup_path_invalid")
    owner = _metadata(path).get("connection_setup") if path.exists() else None
    if path.exists() and (not isinstance(owner, dict) or owner.get("schema_version") != OWNER_SCHEMA
                          or owner.get("binding") != binding):
        return path, owner, None, None, "profile_unmanaged"
    names = sorted(set(mcp) | set(owner.get("managed_mcp", []) if owner else []))
    snapshot = _snapshot(path, names)
    wanted = _desired(setup, soul, mcp, names)
    pending = owner.get("pending") if owner else None
    if pending:
        if pending["request_id"] != request["request_id"] or pending["setup_ref"] != request["setup_ref"]:
            raise ValueError("profile_setup_pending_operation")
        before = pending["snapshots"]
        if not pending["profile_exists"]:
            from hermes_cli.default_soul import DEFAULT_SOUL_MD
            if snapshot["soul"] == hashlib.sha256(DEFAULT_SOUL_MD.encode()).hexdigest():
                before = {**before, "soul": snapshot["soul"]}
        if not _compatible(snapshot, before, wanted):
            raise ValueError("profile_setup_drift")
        return path, owner, pending, wanted, None
    if owner:
        saved = owner["snapshots"]
        if (any(snapshot[key] != saved[key] for key in ("model", "soul", "workspace"))
                or any(snapshot["mcp"].get(name) != saved["mcp"].get(name) for name in owner["managed_mcp"])):
            raise ValueError("profile_setup_drift")
        if any(name not in owner["managed_mcp"] and snapshot["mcp"][name] is not None for name in names):
            raise ValueError("profile_setup_mcp_owner_conflict")
        previous = owner.get("last_operation")
        if previous and previous["request_id"] == request["request_id"]:
            if previous["setup_ref"] != request["setup_ref"]:
                raise ValueError("profile_setup_request_conflict")
            return path, owner, previous, wanted, None
    plan = {"binding": binding, "request_id": request["request_id"], "setup_ref": request["setup_ref"],
            "source_owner": setup["source_owner"], "snapshots": snapshot, "desired_snapshots": wanted,
            "managed_mcp": names, "profile_exists": path.exists()}
    plan["plan_digest"] = _digest(plan)
    return path, owner, plan, wanted, None


def _receipt(request, parsed, path, plan, wanted, reason=None):
    binding, setup, _, mcp = parsed
    snapshots = _snapshot(path, sorted(wanted["mcp"])) if wanted else None
    configured = reason is None and snapshots == wanted
    auth = {"status": "unverified", "reason": "existing_provider_resolver_not_invoked"} if setup.get("auth_binding") else {
        "status": "missing", "reason": "existing_provider_auth_binding_not_supplied"}
    return {"schema_version": RECEIPT_SCHEMA, "action": request["action"], "request_id": request["request_id"],
            "binding": binding, "setup_ref": request["setup_ref"], "plan_digest": plan["plan_digest"] if plan else None,
            "status": "incomplete", "reason": reason, "configuration_status": "configured" if configured else "incomplete",
            "snapshots": snapshots, "expected_snapshots": plan["snapshots"] if plan else None,
            "source_owner": setup["source_owner"], "auth": auth, "runtime_activation": "unverified",
            "readback": {"model_matches": bool(wanted and snapshots["model"] == wanted["model"]),
                         "workspace_matches": bool(wanted and snapshots["workspace"] == wanted["workspace"]),
                         "mandate_matches": bool(wanted and snapshots["soul"] == wanted["soul"]),
                         "mcp_matches": bool(wanted and snapshots["mcp"] == wanted["mcp"]),
                         "mcp_names": sorted(mcp)}, "profile_path": str(path)}


def inspect_profile_connection(request):
    """Read reviewed references and actual non-secret managed fields; no writes."""
    parsed = _request(request, "inspect")
    path, _, plan, wanted, reason = _plan(request, parsed)
    return _receipt(request, parsed, path, plan, wanted, reason)


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.parent / f".{path.name}.profile-setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("profile_setup_busy") from None
        yield
    finally:
        os.close(fd)


def _write_model(path, setup):
    with _scope(path):
        config = read_user_config_raw()
        model = dict(config.get("model", {}))
        model.update(provider=setup["provider"], default=setup["model"])
        config["model"] = model
        terminal = dict(config.get("terminal", {}))
        terminal["cwd"] = setup["binding"]["workspace"]
        config["terminal"] = terminal
        save_config(config)


def apply_profile_connection(request):
    """Apply exactly an inspected digest; preserve unrelated profiles/config.

    A pending operation permits each field's before OR intended value after a
    crash. Unexpected values are drift. Reapply resumes the same reviewed bytes.
    Configured does not claim provider authentication or runtime activation.
    """
    parsed = _request(request, "apply")
    binding, setup, soul, mcp = parsed
    path = profiles.get_profile_dir(binding["profile"])
    with _lock(path):
        path, owner, plan, wanted, reason = _plan(request, parsed)
        if reason:
            raise ValueError("profile_setup_" + reason)
        if plan["plan_digest"] != request["expected_plan_digest"]:
            raise ValueError("profile_setup_plan_changed")
        if owner and owner.get("last_operation") == plan:
            return _receipt(request, parsed, path, plan, wanted)
        owner = dict(owner or {"schema_version": OWNER_SCHEMA, "binding": binding, "managed_mcp": []})
        owner["pending"] = plan
        try:
            if not plan["profile_exists"]:
                profiles.create_profile(binding["profile"], no_alias=True, no_skills=True, connection_setup=owner)
            else:
                profiles.write_profile_meta(path, connection_setup=owner)
            _write_model(path, setup)
            atomic_write_text(path / "SOUL.md", soul)
            with _scope(path):
                for name in plan["managed_mcp"]:
                    if name in mcp:
                        if not _save_mcp_server(name, mcp[name], preserve_templates=True):
                            raise ValueError("profile_setup_mcp_write_failed")
                    else:
                        _remove_mcp_server(name, preserve_templates=True)
            receipt = _receipt(request, parsed, path, plan, wanted)
            if receipt["configuration_status"] != "configured":
                return {**receipt, "reason": "profile_setup_readback_incomplete"}
            owner.update(managed_mcp=sorted(mcp), snapshots=_snapshot(path, sorted(mcp)),
                         source_owner=setup["source_owner"], last_operation=plan)
            owner.pop("pending", None)
            profiles.write_profile_meta(path, connection_setup=owner)
            return receipt
        except (OSError, ValueError):
            return _receipt(request, parsed, path, plan, wanted, "profile_setup_partial_apply")
