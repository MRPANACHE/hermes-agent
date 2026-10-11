"""DrainSecretProvider — shared-bearer-secret auth for the drain-control endpoint.

Non-interactive token capability of the ``DashboardAuthProvider`` ABC (``verify_token`` +
the ``token_auth`` middleware seam): ``nous-account-service`` provisions a per-agent unique
secret (``HERMES_DASHBOARD_DRAIN_SECRET``, env-only — it is a credential); an inbound bearer
is compared constant-time and vouched for as the ``drain-control`` principal. Fail-CLOSED
entropy gate at registration (length, distinct chars, Shannon bits); interactive ABC methods
raise. Knobs ``scope`` / ``min_secret_chars`` live under ``dashboard.drain_auth``.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    Session,
    TokenPrincipal,
)
from hermes_cli.dashboard_auth.secret_strength import (
    _DEFAULT_MIN_SECRET_CHARS,
    _MIN_DISTINCT_CHARS,
    _MIN_SHANNON_BITS,
    _shannon_bits,
    assess_secret_strength,
)
from plugins.dashboard_auth._shared import NonInteractiveMixin, SkipRegistration, load_config_section

logger = logging.getLogger(__name__)
_TAG = "dashboard-auth-drain"

# The path the begin/cancel-drain endpoint lives on. Registered as a
# token-authable route by ``register()`` so the generic seam guards it. Kept
# here (not imported from web_server) to avoid a heavy import at plugin load.
DRAIN_ROUTE_PATH = "/api/gateway/drain"

LAST_SKIP_REASON: str = ""


class DrainSecretProvider(NonInteractiveMixin, DashboardAuthProvider):
    """Non-interactive shared-bearer-secret provider for drain control."""

    name = "drain-secret"
    display_name = "Drain Control (service credential)"
    supports_token = True
    supports_session = False
    _NOT_INTERACTIVE = "DrainSecretProvider is a non-interactive service credential."
    _NO_START_LOGIN = "DrainSecretProvider is a non-interactive service credential; there is no login flow."

    def __init__(self, *, secret: str, scope: str = "drain") -> None:
        # Defence in depth: construction enforces the entropy bar too, so a
        # caller bypassing register() still can't build a weak provider.
        reason = assess_secret_strength(secret)
        if reason is not None:
            raise ValueError(f"drain secret rejected: {reason}")
        self._secret = secret
        self._scope = scope or "drain"

    # ---- token capability (the only thing this provider implements) --------

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        """Constant-time compare; ``drain-control`` principal on match, else
        ``None`` so the generic seam falls through / fails closed."""
        if token and hmac.compare_digest(token.encode("utf-8"), self._secret.encode("utf-8")):
            return TokenPrincipal(principal="drain-control", provider=self.name, scopes=(self._scope,))
        return None

    # ---- interactive methods: unsupported (service credential only) --------

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        # Never mints a Session, so never recognises a cookie. Return None (don't raise)
        # so it stacks harmlessly in the cookie-verify loop.
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError(self._NOT_INTERACTIVE)

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


# ---- Plugin entry point ----

def _load_config_drain_auth_section() -> dict:
    return load_config_section(logger, _TAG, "dashboard", "drain_auth")


def _settings() -> dict:
    """Resolve DrainSecretProvider kwargs from env/config; raises ``SkipRegistration``."""
    secret = os.environ.get("HERMES_DASHBOARD_DRAIN_SECRET", "").strip()
    if not secret:
        raise SkipRegistration(
            "HERMES_DASHBOARD_DRAIN_SECRET is not set. Set a per-agent >=256-bit secret "
            "(e.g. `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`) to enable "
            "NAS-driven drain coordination; leave it unset to disable the drain endpoint.")
    section = _load_config_drain_auth_section()
    scope = str(section.get("scope", "drain") or "drain").strip() or "drain"
    try:
        min_chars = int(section.get("min_secret_chars", _DEFAULT_MIN_SECRET_CHARS))
    except (TypeError, ValueError):
        min_chars = _DEFAULT_MIN_SECRET_CHARS
    reason = assess_secret_strength(secret, min_chars=min_chars)
    if reason is not None:
        raise SkipRegistration(
            f"HERMES_DASHBOARD_DRAIN_SECRET rejected — {reason}. The drain endpoint stays disabled (fail-closed).",
            level="warning")
    return {"secret": secret, "scope": scope}


def register(ctx) -> None:
    """Register ``DrainSecretProvider`` when a strong secret is set; no-op (records a skip
    reason) when ``HERMES_DASHBOARD_DRAIN_SECRET`` is unset or fails the entropy gate. On
    success also registers the drain route as token-authable via the generic seam."""
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""
    try:
        kwargs = _settings()
        provider = DrainSecretProvider(**kwargs)
    except SkipRegistration as skip:
        LAST_SKIP_REASON = skip.reason
        getattr(logger, skip.level)("%s: %s", _TAG, LAST_SKIP_REASON)
        return
    except ValueError as exc:
        LAST_SKIP_REASON = f"DrainSecretProvider construction failed: {exc}"
        logger.warning("dashboard-auth-drain: %s", LAST_SKIP_REASON)
        return

    # Opt the begin/cancel-drain endpoint into the generic token-auth seam so
    # the dashboard's interactive cookie gate doesn't bounce NAS's bearer call.
    try:
        from hermes_cli.dashboard_auth.token_auth import register_token_route

        register_token_route(
            DRAIN_ROUTE_PATH,
            methods=("POST",),
            required_scope=kwargs["scope"],
        )
    except (ImportError, TypeError, ValueError) as exc:
        LAST_SKIP_REASON = (
            f"Drain token-route registration failed: {exc}; provider remains "
            "disabled."
        )
        logger.warning(
            "dashboard-auth-drain: %s", LAST_SKIP_REASON,
        )
        return

    ctx.register_dashboard_auth_provider(provider)

    logger.info(
        "dashboard-auth-drain: registered drain service-credential provider (scope=%s, route=%s)",
        kwargs["scope"], DRAIN_ROUTE_PATH)
