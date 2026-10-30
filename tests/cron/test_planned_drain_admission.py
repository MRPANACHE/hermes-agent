"""Maintenance delays the original fire before submit; it never retries execution."""
from contextlib import asynccontextmanager

import pytest

from cron import scheduler_authority, scheduler_tick
from gateway import drain_control
from hermes_cli import gateway_client, gateway_runtime as runtime
from hermes_cli.gateway_runtime import GatewayDiscovery, GatewayEndpoint


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(runtime, '_update_fenced', lambda homes: False)
    return home


def ready(home):
    return GatewayDiscovery('ready', GatewayEndpoint(
        profile_id=str(home), instance_id='same-owner', authority_epoch=1,
        runtime_protocol=1, api_origin='http://127.0.0.1:1', supervisor='none',
        capabilities=frozenset({'session-authority-v1'})))


@pytest.mark.parametrize('reason', ['update_paused', 'restart_wait', 'stopping'])
def test_cron_ensure_waits_for_drain_without_starting_or_replacing_owner(home, monkeypatch, reason):
    observations = [GatewayDiscovery('draining', reason_code=reason), ready(home)]
    monkeypatch.setattr(runtime, '_observe_replacing_stale', lambda *args: observations.pop(0))
    assert runtime.ensure_gateway_runtime(home, timeout=2, wait_for_drain=True).state == 'ready'
    assert observations == []


def test_durable_marker_is_observed_before_gateway_poller(home, monkeypatch):
    requests = iter([True, False])
    monkeypatch.setattr(drain_control, 'drain_requested', lambda **kwargs: next(requests))
    seen = []
    monkeypatch.setattr(runtime, '_observe_replacing_stale', lambda *args: seen.append(True) or ready(home))
    assert runtime.ensure_gateway_runtime(home, timeout=2, wait_for_drain=True).state == 'ready'
    assert seen == [True]


def test_update_fence_wait_is_bounded_and_never_spawns(home, monkeypatch):
    monkeypatch.setattr(runtime, '_update_fenced', lambda homes: True)
    monkeypatch.setattr(runtime, '_observe_replacing_stale', lambda *args: pytest.fail('probed behind fence'))
    verdict = runtime.ensure_gateway_runtime(home, timeout=.05, wait_for_drain=True)
    assert (verdict.state, verdict.reason_code) == ('starting', 'deadline')


def test_tick_does_not_scan_advance_or_claim_during_marker_gap(home, monkeypatch):
    monkeypatch.setattr(drain_control, 'drain_requested', lambda **kwargs: True)
    monkeypatch.setattr(scheduler_tick, '_acquire_tick_lock_or_degrade', lambda *args: pytest.fail('scanned while draining'))
    assert scheduler_tick._tick_admitted() == 0


def test_cron_submits_once_with_original_identity_after_native_wait(home, monkeypatch):
    from gateway import session_cron
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)
    calls = []
    class Peer:
        async def rpc(self, method, **params):
            calls.append((method, params))
            if method == 'cron.submit':
                return {'session_id': 'owner-session', 'admission_id': 'same-admission'}
            assert method == 'cron.status'
            return {'status': 'terminal', 'result': [True, 'document', 'answer', None]}
    @asynccontextmanager
    async def connected(*, wait_for_drain=False):
        assert wait_for_drain is True
        yield Peer()
    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)
    job = {'id': 'abc012345678', 'execution_id': 'original-fire'}
    assert scheduler_authority.run_canonical_job(job) == (True, 'document', 'answer', None)
    submits = [p for method, p in calls if method == 'cron.submit']
    assert submits == [{'job_id': job['id'], 'request_id': 'original-fire', 'extra_prompt': None}]


def test_nonmaintenance_error_is_not_hidden_or_retried(home, monkeypatch):
    monkeypatch.setattr(runtime, '_observe_replacing_stale', lambda *args: GatewayDiscovery('inaccessible', reason_code='permission_denied'))
    result = runtime.ensure_gateway_runtime(home, timeout=2, wait_for_drain=True)
    assert (result.state, result.reason_code) == ('inaccessible', 'permission_denied')
