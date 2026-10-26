"""Offline stop intent must not launch a host, bypass a refusal, or disturb a service."""
from types import SimpleNamespace

import pytest


@pytest.fixture
def offline(tmp_path, monkeypatch):
    from hermes_cli import gateway as gw, gateway_profile_lifecycle as lifecycle
    from hermes_cli import gateway_runtime, gateway_runtime_service, gateway_lifecycle_guards

    root = tmp_path / 'root'
    home = root / 'profiles' / 'worker'
    home.mkdir(parents=True)
    marker = home / 'gateway.parked'
    monkeypatch.setattr(lifecycle, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(lifecycle, 'get_default_hermes_root', lambda: root)
    monkeypatch.setattr(lifecycle, 'profile_is_standalone', lambda _: False)
    monkeypatch.setattr(gw, '_current_profile_name', lambda: 'worker')
    monkeypatch.setattr(gw, 'find_gateway_pids', list)
    monkeypatch.setattr(gw, '_served_by_another_host_gateway', lambda: None)
    monkeypatch.setattr(gw, 'named_profile_served_by_running_multiplexer', lambda: False)
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda *a, **k: SimpleNamespace(state='absent'))
    monkeypatch.setattr(gateway_runtime_service, 'discover_existing_gateway_service', lambda *a, **k: None)
    monkeypatch.setattr(gateway_lifecycle_guards, 'live_work', lambda _: None)
    return SimpleNamespace(root=root, home=home, marker=marker, lifecycle=lifecycle,
                           runtime=gateway_runtime, service=gateway_runtime_service,
                           work=gateway_lifecycle_guards, gw=gw)


def test_offline_stop_persists_existing_marker_contract_without_start(offline, capsys):
    assert offline.lifecycle.profile_lifecycle('stop', SimpleNamespace()) is True
    assert offline.marker.is_file()
    assert offline.lifecycle.profile_lifecycle('stop', SimpleNamespace()) is True
    assert 'no gateway was started or stopped' in capsys.readouterr().out


@pytest.mark.parametrize('state', ['ready', 'starting', 'draining', 'conflict', 'inaccessible', 'unknown'])
@pytest.mark.parametrize('which', ['profile', 'host'])
def test_unproven_or_live_owner_cannot_be_parked(offline, monkeypatch, state, which):
    target = offline.home if which == 'profile' else offline.root
    monkeypatch.setattr(offline.runtime, 'discover_gateway_endpoint',
                        lambda home, **kw: SimpleNamespace(state=state if home == target else 'absent'))
    with pytest.raises(SystemExit) as exc:
        offline.lifecycle.profile_lifecycle('stop', SimpleNamespace())
    assert exc.value.code == 1
    assert not offline.marker.exists()


def test_owner_starting_during_checks_is_refused(offline, monkeypatch):
    calls = []

    def discover(home, **kwargs):
        calls.append(home)
        return SimpleNamespace(state='starting' if len(calls) == 4 else 'absent')

    monkeypatch.setattr(offline.runtime, 'discover_gateway_endpoint', discover)
    with pytest.raises(SystemExit):
        offline.lifecycle.profile_lifecycle('stop', SimpleNamespace())
    assert len(calls) == 4 and not offline.marker.exists()


@pytest.mark.parametrize('probe', ['discovery', 'service', 'work'])
def test_probe_exception_is_fail_closed_without_echoing_details(offline, monkeypatch, capsys, probe):
    def fail(*args, **kwargs):
        raise OSError('PRIVATE_CONTROL_DETAIL')

    obj, attr = {'discovery': (offline.runtime, 'discover_gateway_endpoint'),
                 'service': (offline.service, 'discover_existing_gateway_service'),
                 'work': (offline.work, 'live_work')}[probe]
    monkeypatch.setattr(obj, attr, fail)
    with pytest.raises(SystemExit):
        offline.lifecycle.profile_lifecycle('stop', SimpleNamespace())
    assert not offline.marker.exists()
    assert 'PRIVATE_CONTROL_DETAIL' not in capsys.readouterr().out


@pytest.mark.parametrize('work', [{'active_agents': 1}, {'active_work': [{'id': 'active'}]}])
def test_live_work_refuses_offline_marker(offline, monkeypatch, work):
    monkeypatch.setattr(offline.work, 'live_work', lambda _: work)
    with pytest.raises(SystemExit):
        offline.lifecycle.profile_lifecycle('stop', SimpleNamespace())
    assert not offline.marker.exists()


def test_installed_service_keeps_existing_stop_path(offline, monkeypatch):
    monkeypatch.setattr(offline.service, 'discover_existing_gateway_service', lambda *a, **k: object())
    assert offline.lifecycle.profile_lifecycle('stop', SimpleNamespace()) is False
    assert not offline.marker.exists()


def test_own_live_process_keeps_existing_stop_path(offline, monkeypatch):
    monkeypatch.setattr(offline.gw, 'find_gateway_pids', lambda: [4242])
    assert offline.lifecycle.profile_lifecycle('stop', SimpleNamespace()) is False
    assert not offline.marker.exists()


@pytest.mark.parametrize('verb', ['start', 'restart'])
def test_other_offline_verbs_keep_existing_path(offline, verb):
    assert offline.lifecycle.profile_lifecycle(verb, SimpleNamespace()) is False
    assert not offline.marker.exists()
