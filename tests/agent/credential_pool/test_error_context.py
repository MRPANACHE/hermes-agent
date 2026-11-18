"""Credential-pool error-context redaction tests."""


def test_normalize_error_context_redacts_api_keys():
    """Error messages containing API keys must be redacted before persisting.

    Upstream providers (Anthropic, OpenAI, xAI) sometimes echo back the key
    in error responses.  _normalize_error_context must scrub these so
    _mark_exhausted / to_dict never writes raw keys to auth.json.
    """
    from agent.credential_pool import _normalize_error_context

    cases = [
        ("Invalid API key: sk-ant-abc123def456ghi789", "sk-ant-abc123def456ghi789"),
        ("Authentication failed: sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef", "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"),
        ("Bad xai-key xai-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234", "xai-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234"),
    ]
    for raw_msg, full_secret in cases:
        ctx = _normalize_error_context({"message": raw_msg})
        result = ctx.get("message", "")
        assert full_secret not in result, (
            f"Full secret not redacted in: {result!r}"
        )
        # Redacted output should still contain some text (not empty)
        assert len(result) > 0, f"Redaction produced empty message for: {raw_msg!r}"


def test_normalize_error_context_preserves_non_secret_messages():
    """Messages without secrets should pass through unchanged."""
    from agent.credential_pool import _normalize_error_context

    msg = "Rate limit exceeded. Retry after 60 seconds."
    ctx = _normalize_error_context({"message": msg})
    assert ctx["message"] == msg
