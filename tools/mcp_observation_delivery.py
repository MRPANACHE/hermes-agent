"""Durable, explicitly retried original-observation delivery; no tool execution."""

import asyncio
from contextlib import contextmanager
import hashlib
import inspect
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import urlsplit

import httpx

from tools.mcp_observation import _ID, _json


_KEYS = {"agent_run_id", "runtime_owner", "native_session_id", "execution_id", "invocation_id",
         "tool_name", "intent_id", "arguments", "result"}
_RECEIPT_KEYS = {"observation_id", "arguments_sha256", "result_sha256", "artifact_sha256", "artifact_ref"}
_HASH = re.compile(r"sha256:[a-f0-9]{64}")


class NativeObservationDeliveryError(RuntimeError):
    pass


def _error(suffix):
    return NativeObservationDeliveryError("native_observation_delivery_" + suffix)


def _sha(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _validate_ids(run_id, invocation_id):
    if any(type(value) is not str or not _ID.fullmatch(value) for value in (run_id, invocation_id)):
        raise _error("input_invalid")


def _body(value):
    try:
        text = _json(value)
        copied = json.loads(text)
        if type(copied) is not dict or set(copied) != _KEYS:
            raise ValueError()
        for key in _KEYS - {"arguments", "result"}:
            if type(copied[key]) is not str or not _ID.fullmatch(copied[key]):
                raise ValueError()
        return copied, text.encode("utf-8")
    except Exception:
        raise _error("input_invalid") from None


def _receipt(value, run_id, invocation_id, body_sha):
    if type(value) is not dict or set(value) != {"ok", "request_sha256", "receipt"} or value["ok"] is not True or value["request_sha256"] != body_sha:
        raise _error("unconfirmed")
    receipt = value["receipt"]
    if type(receipt) is not dict or set(receipt) not in (_RECEIPT_KEYS, _RECEIPT_KEYS | {"idempotent"}):
        raise _error("unconfirmed")
    if "idempotent" in receipt and receipt["idempotent"] is not True:
        raise _error("unconfirmed")
    invocation = json.dumps([run_id, invocation_id], separators=(",", ":")).encode("utf-8")
    if receipt["observation_id"] != "native:" + hashlib.sha256(invocation).hexdigest():
        raise _error("unconfirmed")
    for key in ("arguments_sha256", "result_sha256", "artifact_sha256"):
        if type(receipt[key]) is not str or not _HASH.fullmatch(receipt[key]):
            raise _error("unconfirmed")
    if receipt["artifact_ref"] != "oos-native-result:" + receipt["artifact_sha256"]:
        raise _error("unconfirmed")
    return receipt


class NativeObservationDelivery:
    def __init__(self, *, endpoint, token_provider, state_dir, timeout=15.0):
        try:
            if type(endpoint) is not str or len(endpoint.encode("utf-8")) > 4096 or any(ord(char) <= 32 for char in endpoint):
                raise ValueError()
            url = urlsplit(endpoint)
            if not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment or url.path != "/internal/hermes/native-observations":
                raise ValueError()
            if url.scheme != "https" and not (url.scheme == "http" and "%" not in url.hostname and ipaddress.ip_address(url.hostname).is_loopback):
                raise ValueError()
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError()
            if not callable(token_provider) or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
                raise ValueError()
            self._dir = Path(state_dir)
            if not self._dir.is_absolute():
                raise ValueError()
        except Exception:
            raise _error("config_invalid") from None
        self._endpoint, self._token_provider, self._timeout = endpoint, token_provider, timeout
        self._path = self._dir / "native-observations.sqlite3"
        try:
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._connection() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS native_observations (
                    agent_run_id TEXT NOT NULL, invocation_id TEXT NOT NULL, destination TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL, body BLOB, receipt TEXT,
                    PRIMARY KEY (agent_run_id, invocation_id),
                    CHECK ((body IS NOT NULL AND receipt IS NULL) OR (body IS NULL AND receipt IS NOT NULL))
                )""")
        except Exception:
            raise _error("storage_invalid") from None

    @contextmanager
    def _connection(self):
        db = None
        try:
            info = self._dir.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
                raise ValueError()
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                    raise ValueError()
            finally:
                os.close(fd)
            db = sqlite3.connect(self._path, timeout=2, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        except NativeObservationDeliveryError:
            raise
        except Exception:
            raise _error("storage_invalid") from None
        finally:
            if db is not None:
                db.close()

    def _load(self, db, run_id, invocation_id):
        row = db.execute("SELECT * FROM native_observations WHERE agent_run_id=? AND invocation_id=?", (run_id, invocation_id)).fetchone()
        if row is None:
            raise _error("missing")
        if row["destination"] != self._endpoint:
            raise _error("conflict")
        try:
            if type(row["body_sha256"]) is not str or not _HASH.fullmatch(row["body_sha256"]):
                raise ValueError()
            if row["body"] is not None:
                value, body = _body(json.loads(bytes(row["body"]).decode("utf-8")))
                if body != row["body"] or _sha(body) != row["body_sha256"] or value["agent_run_id"] != run_id or value["invocation_id"] != invocation_id or row["receipt"] is not None:
                    raise ValueError()
            else:
                _receipt({"ok": True, "request_sha256": row["body_sha256"], "receipt": json.loads(row["receipt"])}, run_id, invocation_id, row["body_sha256"])
        except Exception:
            raise _error("storage_invalid") from None
        return dict(row)

    async def __call__(self, observation):
        value, body = _body(observation)
        run_id, invocation_id = value["agent_run_id"], value["invocation_id"]
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO native_observations VALUES (?, ?, ?, ?, ?, NULL)",
                       (run_id, invocation_id, self._endpoint, _sha(body), body))
            row = self._load(db, run_id, invocation_id)
            if row["body_sha256"] != _sha(body):
                raise _error("conflict")
            db.commit()
        return await self._deliver(row)

    async def replay(self, agent_run_id, invocation_id):
        _validate_ids(agent_run_id, invocation_id)
        with self._connection() as db:
            row = self._load(db, agent_run_id, invocation_id)
        return await self._deliver(row)

    async def _deliver(self, row):
        if row["receipt"] is not None:
            return json.loads(row["receipt"])
        try:
            async with asyncio.timeout(self._timeout):
                token = self._token_provider()
                if inspect.isawaitable(token):
                    token = await token
                if type(token) is not str or not 1 <= len(token) <= 4096 or any(ord(char) <= 32 or ord(char) >= 127 for char in token):
                    raise ValueError()
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=self._timeout) as client:
                    async with client.stream("POST", self._endpoint, content=row["body"],
                                             headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept-Encoding": "identity"}) as response:
                        if response.status_code != 200:
                            raise ValueError()
                        parts, size = [], 0
                        async for part in response.aiter_raw(chunk_size=16384):
                            size += len(part)
                            if size > 65536:
                                raise ValueError()
                            parts.append(part)
                        ack = json.loads(b"".join(parts).decode("utf-8"))
                receipt = _receipt(ack, row["agent_run_id"], row["invocation_id"], row["body_sha256"])
        except Exception:
            raise _error("unconfirmed") from None
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._load(db, row["agent_run_id"], row["invocation_id"])
            if current["body_sha256"] != row["body_sha256"]:
                raise _error("conflict")
            db.execute("UPDATE native_observations SET body=NULL, receipt=? WHERE agent_run_id=? AND invocation_id=?",
                       (json.dumps(receipt, sort_keys=True, separators=(",", ":")), row["agent_run_id"], row["invocation_id"]))
            db.commit()
        return receipt
