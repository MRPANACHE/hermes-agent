"""Incomplete review receipts must not release the kanban terminal stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    session_called_kanban_terminal,
)


@pytest.mark.parametrize(
    "receipt",
    [
        pytest.param('{"ok": true,', id="malformed-json"),
        pytest.param("null", id="null-receipt"),
        pytest.param("[]", id="array-receipt"),
        pytest.param('"review"', id="string-receipt"),
        pytest.param("true", id="boolean-receipt"),
        pytest.param("1", id="number-receipt"),
        pytest.param('{"ok": true, "status": "review"}', id="missing-task-id"),
        pytest.param(
            '{"ok": 1, "task_id": "t_review", "status": "review"}',
            id="integer-ok-is-not-true",
        ),
    ],
)
def test_incomplete_review_receipt_keeps_terminal_guard_active(monkeypatch, receipt):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "review-handoff",
                    "type": "function",
                    "function": {
                        "name": "kanban_request_review",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "kanban_request_review",
            "tool_call_id": "review-handoff",
            "content": receipt,
        },
    ]

    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None
