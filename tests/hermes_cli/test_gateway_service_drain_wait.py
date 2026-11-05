"""Native service teardown is the same pre-admission drain as endpoint discovery."""
import pytest

from hermes_cli import gateway_runtime as runtime
from hermes_cli import gateway_runtime_service as services
from hermes_cli.gateway_runtime import GatewayDiscovery, GatewayEndpoint


@pytest.mark.parametrize('phase', ['discover', 'start'])
@pytest.mark.parametrize('wait', [False, True])
def test_service_handoff_drain_obeys_native_wait(tmp_path, monkeypatch, phase, wait):
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(runtime, '_update_fenced', lambda homes: False)
    monkeypatch.setattr(runtime, '_starting_multiplexer_root', lambda home: None)
    draining = []
    starts = []
    endpoint = GatewayEndpoint(
        profile_id=str(home), instance_id='original-owner', authority_epoch=1,
        runtime_protocol=1, api_origin='http://127.0.0.1:1', supervisor='systemd',
        capabilities=frozenset({'session-authority-v1'}))

    def observe(*args, **kwargs):
        return GatewayDiscovery('ready', endpoint) if draining else GatewayDiscovery('absent')

    def drain():
        draining.append(True)
        raise services.RuntimeStartError('service_draining', 'draining')

    def discover(*args, **kwargs):
        if phase == 'discover':
            drain()
        return object()

    def start(*args, **kwargs):
        starts.append(True)
        drain()

    monkeypatch.setattr(runtime, '_observe_replacing_stale', observe)
    monkeypatch.setattr(runtime, 'discover_gateway_endpoint', observe)
    monkeypatch.setattr(services, 'discover_existing_gateway_service', discover)
    monkeypatch.setattr(services, 'start_existing_gateway_service', start)
    result = runtime.ensure_gateway_runtime(home, timeout=2, wait_for_drain=wait)
    assert result.state == ('ready' if wait else 'draining')
    assert draining == [True]
    assert starts == ([True] if phase == 'start' else [])


def test_failed_service_stays_a_real_failure_during_cron_wait(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(runtime, '_update_fenced', lambda homes: False)
    monkeypatch.setattr(runtime, '_starting_multiplexer_root', lambda home: None)
    monkeypatch.setattr(runtime, '_observe_replacing_stale', lambda *args: GatewayDiscovery('absent'))

    def failed(*args, **kwargs):
        raise services.RuntimeStartError('service_failed')

    monkeypatch.setattr(services, 'discover_existing_gateway_service', failed)
    result = runtime.ensure_gateway_runtime(home, timeout=2, wait_for_drain=True)
    assert (result.state, result.reason_code) == ('inaccessible', 'service_failed')
