"""Task-owned skills survive storage and native worker command construction.

The implementation already exists in kanban_db/kanban_db_dispatch; these
regressions exercise the mandatory delivery contract, not a filename alias.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    conn = kb.connect(tmp_path / "board.db")
    try:
        yield conn
    finally:
        conn.close()


def test_task_skills_roundtrip_into_literal_worker_arguments(board, monkeypatch):
    task_id = kb.create_task(
        board, title="synthetic worker", assignee="fixture",
        skills=[" github-code-review ", "plugin:review", "github-code-review", ""],
    )
    task = kb.get_task(board, task_id)
    assert task is not None
    assert task.skills == ["github-code-review", "plugin:review"]
    monkeypatch.setattr(dispatch, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(dispatch, "_resolve_worker_cli_toolsets", lambda home: [])
    argv = dispatch._worker_argv(task, "fixture", None)
    values = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--skills"]
    assert values == task.skills
    assert argv[-3:] == ["chat", "-q", f"work kanban task {task_id}"]


@pytest.mark.parametrize("skills", [None, []])
def test_no_task_skills_does_not_invent_worker_skill(board, monkeypatch, skills):
    task_id = kb.create_task(board, title="defaults", skills=skills)
    task = kb.get_task(board, task_id)
    assert task is not None
    monkeypatch.setattr(dispatch, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(dispatch, "_resolve_worker_cli_toolsets", lambda home: [])
    assert "--skills" not in dispatch._worker_argv(task, "fixture", None)


@pytest.mark.parametrize("skills", [["one,two"], ["terminal"], ["web", "browser"]])
def test_invalid_skill_input_is_refused_without_partial_task(board, skills):
    before = board.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    with pytest.raises(ValueError):
        kb.create_task(board, title="must not persist", skills=skills)
    assert board.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
