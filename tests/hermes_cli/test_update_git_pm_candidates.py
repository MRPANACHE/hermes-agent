"""Portable Git discovery follows the selected PM facts, not a fixed install root."""
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
