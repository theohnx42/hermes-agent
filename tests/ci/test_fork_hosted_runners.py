"""Public-fork CI must run full native coverage without upstream runner grants."""
from __future__ import annotations

import importlib.util
import re
from collections import Counter
from pathlib import Path

import pytest
import hermes_yaml as yaml
from tests.ci import _gha_expr as gha

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = 'NousResearch/hermes-agent'
FORK = 'theohnx42/hermes-agent'
MANUAL_E2E = {'e2e-desktop.yml', 'e2e-desktop-core.yml', 'e2e-desktop-update.yml'}
NATIVE = {'ubuntu-24.04', 'windows-2025', 'windows-11-arm', 'macos-15'}


def workflow(name):
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text())


def context(repository, **extra):
    return {'github': {'repository': repository}, '__status__': {'cancelled': False}, **extra}


def test_pr_graph_has_available_native_runners():
    """Follow the actual reusable graph; fixing only three detectors is insufficient."""
    pending = ['ci.yaml', 'nix.yml', 'docker.yml']
    seen = set()
    checked = 0
    while pending:
        name = pending.pop()
        if name in seen or name in MANUAL_E2E:
            continue
        seen.add(name)
        for job_id, job in workflow(name)['jobs'].items():
            if job_id.startswith('e2e') or job_id == 'install-update-e2e':
                continue
            # Existing E2E jobs remain release/manual-only, not part of PR admission.
            if job.get('if') is False or re.fullmatch(r'inputs\.e2e(?:_upgrade)?', str(job.get('if', ''))):
                continue
            uses = job.get('uses', '')
            if uses.startswith('./.github/workflows/'):
                pending.append(uses.rsplit('/', 1)[1])
            if 'runs-on' not in job:
                continue
            # Docker's upstream-only build/publish guards are independently checked below.
            if name == 'docker.yml' and 'blacksmith-32vcpu' in str(job):
                continue
            runner = job['runs-on']
            if 'matrix.runner' in runner:
                runners = [gha.render(row['runner'], context(FORK)) for row in job['strategy']['matrix']['include']]
            elif 'blacksmith' in runner:
                runners = [gha.render(runner, context(FORK))]
            else:
                runners = [runner]
            assert all(r in NATIVE or r == 'ubuntu-latest' for r in runners), (name, runners)
            checked += len(runners)
    assert checked >= 40


@pytest.mark.parametrize('repository,count,workers', [(UPSTREAM, 2, 32), (FORK, 16, 4)])
def test_full_python_partition_once_per_file(repository, count, workers):
    job = workflow('tests.yml')['jobs']['test']
    ctx = context(repository)
    slices = gha.render(job['strategy']['matrix']['slice'], ctx)
    assert slices == list(range(1, count + 1))
    run = next(s for s in job['steps'] if s.get('name') == 'Run tests')
    assert run['run'].strip() == 'scripts/run_tests.sh'  # unchanged full discovery
    spec = importlib.util.spec_from_file_location('fork_runner_partition', ROOT / 'scripts/run_tests_parallel.py')
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    files = [ROOT / 'tests' / f'test_fixture_{i:04d}.py' for i in range(1001)]
    partition = []
    for index in slices:
        ctx['matrix'] = {'slice': index}
        selected = re.sub(r'\$\{\{(.*?)\}\}', lambda match: gha.to_string(gha.evaluate(match[1], ctx)), run['env']['HERMES_TEST_SLICE'])
        assert selected == f'{index}/{count}'
        assert int(gha.render(run['env']['HERMES_TEST_WORKERS'], ctx)) == workers
        # Different per-job historical caches must not drop/duplicate files.
        partition.extend(module._slice_files(files, index, count, {str(files[0]): index * 100}, ROOT))
    assert Counter(partition) == Counter(files)


def test_native_architecture_and_upstream_labels_preserved():
    rows = workflow('tests-os.yml')['jobs']['os-tests']['strategy']['matrix']['include']
    for row in rows:
        upstream = gha.render(row['runner'], context(UPSTREAM))
        fork = gha.render(row['runner'], context(FORK))
        if row['marker'] == 'macos':
            assert (upstream, fork) == ('blacksmith-12vcpu-macos-latest', 'macos-15')
        elif 'arm64' in row['name']:
            assert (upstream, fork) == ('windows-latest-32-arm-core', 'windows-11-arm')
        else:
            assert (upstream, fork) == ('blacksmith-32vcpu-windows-2025', 'windows-2025')
    run = next(s for s in workflow('tests-os.yml')['jobs']['os-tests']['steps'] if 'HERMES_TEST_WORKERS' in s.get('env', {}))
    for repository, workers in [(UPSTREAM, '16'), (FORK, '4')]:
        assert gha.render(run['env']['HERMES_TEST_WORKERS'], context(repository, matrix={'marker': 'windows'})) == workers


def test_fork_runner_selection_does_not_grant_docker_publication():
    docker = workflow('docker.yml')
    job = docker['jobs']['build']
    needs = {'mode': {'result': 'success', 'outputs': {'release': 'false', 'phase': ''}}, 'detect': {'outputs': {'build': 'true'}}}
    # Docker has its own literal upstream-repository fence; forks stay excluded.
    assert not gha.evaluate(job['if'], context(FORK, needs=needs))
    assert gha.evaluate(job['if'], context(UPSTREAM, needs=needs))
    assert workflow('ci.yaml')['jobs']['e2e-desktop']['if'] is False
    for name in ('ci.yaml', 'nix.yml', 'tests.yml', 'tests-os.yml'):
        parsed = workflow(name)
        assert parsed['permissions'].get('contents') == 'read'
        assert 'secrets' not in parsed


@pytest.mark.parametrize('repository,limit,args', [(UPSTREAM, 30, []), (FORK, 60, ['--concurrency', '1'])])
def test_fork_workspace_admission_retains_all_checks(repository, limit, args):
    import shlex

    job = workflow('js-tests.yml')['jobs']['check']
    ctx = context(repository)
    assert int(gha.render(job['timeout-minutes'], ctx)) == limit
    step = next(s for s in job['steps'] if s.get('name') == 'Run all workspace checks')
    command = re.sub(r'\$\{\{(.*?)\}\}', lambda match: gha.to_string(gha.evaluate(match[1], ctx)), step['run'])
    assert shlex.split(command) == ['node', '.github/scripts/run-workspace-checks.mjs', *args]
    assert 'if' not in step
    assert not step.get('continue-on-error', False)
