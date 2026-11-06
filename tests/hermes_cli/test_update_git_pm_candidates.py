"""Portable Git discovery follows the selected PM facts, not a fixed install root."""
from types import SimpleNamespace

import pytest

from pm import paths
from pm.lock import Facts

from hermes_cli import update_cmd_git as git_update


def test_selected_pm_git_core_is_a_candidate_and_can_be_selected(tmp_path, monkeypatch):
    store = tmp_path / 'tools'
    store.mkdir()
    facts = store / 'facts.json'
    entry = 'git-fixture'
    Facts(facts).record('git', '2.53.0', entry, {}, store)
    monkeypatch.setattr(paths, 'store_root', lambda: store)
    monkeypatch.setattr(paths, 'facts_path', lambda: facts)
    core = store / entry / 'mingw64/libexec/git-core/git.exe'
    core.parent.mkdir(parents=True)
    core.write_text('fixture', encoding='utf-8')
    assert core in git_update._portable_git_candidates()
    monkeypatch.setattr(git_update, '_probe_fork_bomb', lambda argv: False if argv == [str(core)] else True)
    assert git_update._locate_real_git() == core


@pytest.mark.parametrize('layout', ['libexec/git-core', 'bin'])
def test_tool_path_locates_git_when_the_parent_pm_binding_is_absent(tmp_path, monkeypatch, layout):
    from hermes_platform.resolver import core as resolver

    root = tmp_path / 'portable-git'
    command = root / 'cmd/git.exe'
    command.parent.mkdir(parents=True)
    command.write_text('fixture command', encoding='utf-8')
    binary = root / 'mingw64' / layout / 'git.exe'
    binary.parent.mkdir(parents=True)
    binary.write_text('fixture binary', encoding='utf-8')
    monkeypatch.setattr(paths, 'facts_path', lambda: tmp_path / 'missing-facts.json')
    monkeypatch.setattr(resolver, 'locate_command', lambda name: SimpleNamespace(command=(str(command),)))
    monkeypatch.setattr(git_update, '_probe_fork_bomb', lambda argv: False if argv == [str(binary)] else True)
    assert git_update._locate_real_git() == binary
