"""Real local-shell identity regression; no live board or dispatch calls."""
import json
import os
import shlex
import sys
from unittest.mock import MagicMock

import pytest

from agent.delegation_context import (
    KANBAN_ENV_KEYS, DELEGATED_CHILD_ENV_MARKER, delegated_child_context,
    is_delegated_child_context,
)
from tools.environments.local import LocalEnvironment


def probe(env):
    code = ('import os,json; print(json.dumps({k:v for k,v in os.environ.items() '
            'if k.startswith("HERMES_KANBAN_") or k=="HERMES_DELEGATED_CHILD_CONTEXT"}))')
    result = env.execute(shlex.quote(sys.executable) + ' -c ' + shlex.quote(code), timeout=15)
    assert result['returncode'] == 0, result
    return json.loads(result['output'].strip())


@pytest.fixture
def parent_env(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    expected = {key: 'fixture-' + key for key in KANBAN_ENV_KEYS}
    for key, value in expected.items():
        monkeypatch.setenv(key, value)
    # Avoid loading host shell init/config and passthrough credentials.
    monkeypatch.setattr('tools.environments.local._resolve_shell_init_files', lambda: [])
    monkeypatch.setattr('tools.environments.base.BaseEnvironment._snapshot_excluded_passthrough_names', lambda self: ())
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    yield env, expected
    env.cleanup()


@pytest.mark.parametrize('child_first', [False, True])
def test_shared_shell_parent_child_parent(parent_env, child_first):
    env, expected = parent_env
    if not child_first:
        assert probe(env) == expected
    with delegated_child_context('fixture-child'):
        assert probe(env) == {DELEGATED_CHILD_ENV_MARKER: '1'}
    assert not is_delegated_child_context()
    assert probe(env) == expected
    assert probe(env) == expected


def test_parent_marker_after_child_terminal(parent_env):
    env, expected = parent_env
    assert probe(env) == expected
    with delegated_child_context('fixture-child'):
        observed_child = probe(env)
    assert observed_child[DELEGATED_CHILD_ENV_MARKER] == '1'
    assert probe(env) == expected


def test_stale_snapshot_never_overrides_current_identity(parent_env):
    env, expected = parent_env
    # Model the exact historical snapshot corruption, in this fixture only.
    with open(env._snapshot_path, 'a') as f:
        f.write('\nexport HERMES_DELEGATED_CHILD_CONTEXT=1\n')
        f.write('export HERMES_KANBAN_TASK=foreign-owner\n')
    assert probe(env) == expected
    with delegated_child_context('fixture-child'):
        assert probe(env) == {DELEGATED_CHILD_ENV_MARKER: '1'}
    assert probe(env) == expected


def test_child_process_marker_stays_fail_closed(parent_env, monkeypatch):
    env, _ = parent_env
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, '1')
    assert probe(env) == {DELEGATED_CHILD_ENV_MARKER: '1'}
    assert probe(env) == {DELEGATED_CHILD_ENV_MARKER: '1'}


@pytest.mark.parametrize('schema_retry', [False, True])
def test_synchronous_native_child_terminal_and_guard(parent_env, monkeypatch, schema_retry):
    from tools import delegate_tool, kanban_tools
    env, expected = parent_env
    assert probe(env) == expected
    attempts = []
    class Child:
        session_id = 'fixture-child'
        tool_progress_callback = None
        _delegate_saved_tool_names = []
        _credential_pool = None
        _subagent_id = 'fixture-child'
        _delegate_depth = 1
        _parent_subagent_id = None
        model = 'fixture'
        session_prompt_tokens = session_completion_tokens = 0
        session_estimated_cost_usd = session_reasoning_tokens = 0
        def get_activity_summary(self):
            return {'api_call_count': 0, 'max_iterations': 1, 'current_tool': None}
        def run_conversation(self, **kwargs):
            assert is_delegated_child_context()
            assert probe(env) == {DELEGATED_CHILD_ENV_MARKER: '1'}
            denied = json.loads(kanban_tools._handle_complete({'summary': 'forbidden'}))
            assert 'delegate_task child' in denied['error']
            attempts.append(denied)
            response = 'bad-json' if schema_retry and len(attempts) == 1 else '{"ok": true}'
            return {'final_response': response, 'completed': True, 'api_calls': 0, 'messages': []}
        def close(self):
            pass
    parent = MagicMock()
    parent._current_task_id = 'fixture-parent'
    # Failing before the board resolver proves no persistence/dispatch occurs.
    monkeypatch.setattr(kanban_tools, '_default_task_id', lambda *a, **k: pytest.fail('board identity resolver called'))
    child = Child()
    child._delegate_output_schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']} if schema_retry else None
    result = delegate_tool._run_single_child(0, 'fixture', child, parent)
    assert result['status'] == 'completed', result
    assert len(attempts) == (2 if schema_retry else 1)
    if schema_retry:
        assert result['schema_valid'] is True
    assert not is_delegated_child_context()
    assert probe(env) == expected
