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
        if name in seen:
            continue
        seen.add(name)
        for job_id, job in workflow(name)['jobs'].items():
            # Only a literal disabled caller is unreachable; classified E2E lanes
            # and nested reusable workflows are part of the actual CI graph.
            if job.get('if') is False:
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


def test_enabled_e2e_call_graph_covers_all_nine_queued_jobs():
    """Replay enabled classifier outputs through callers, including the nested Windows job."""
    outputs = dict.fromkeys(('python', 'desktop_updater', 'e2e', 'e2e_upgrade',
                             'e2e_desktop_core', 'e2e_desktop_update'), 'true')
    leaves = [('tests', 'e2e', 'blacksmith-32vcpu-ubuntu-2404'),
              ('tests', 'e2e-upgrade-plan', 'blacksmith-4vcpu-ubuntu-2404'),
              ('tests-os', 'e2e-windows', 'blacksmith-32vcpu-windows-2025'),
              ('tests-os', 'install-update-e2e', 'blacksmith-32vcpu-windows-2025'),
              ('e2e-desktop-core', 'core', 'blacksmith-32vcpu-ubuntu-2404'),
              ('e2e-desktop-update', 'update', 'blacksmith-32vcpu-ubuntu-2404')]
    for repository in (UPSTREAM, FORK):
        count = 0
        for caller_id, child_id, upstream_runner in leaves:
            ctx = context(repository, needs={'detect': {'outputs': outputs}}, inputs={})
            caller = workflow('ci.yaml')['jobs'][caller_id]
            assert gha.evaluate(caller['if'], ctx), caller_id
            inputs = {key: gha.render(value, ctx) for key, value in caller['with'].items()}
            child = workflow(caller['uses'].rsplit('/', 1)[1])['jobs'][child_id]
            child_ctx = context(repository, inputs=inputs)
            if 'if' in child:
                assert gha.evaluate(child['if'], child_ctx), child_id
            if 'uses' in child:
                nested_inputs = {key: gha.render(value, child_ctx)
                                 for key, value in child.get('with', {}).items()}
                jobs = workflow(child['uses'].rsplit('/', 1)[1])['jobs']
                assert len(jobs) == 1
                child = next(iter(jobs.values()))
                child_ctx = context(repository, inputs=nested_inputs)
            expected = upstream_runner if repository == UPSTREAM else (
                'windows-2025' if 'windows' in upstream_runner else 'ubuntu-24.04')
            assert gha.render(child['runs-on'], child_ctx) == expected, (caller_id, child_id)
            count += len(child.get('strategy', {}).get('matrix', {}).get('include', [{}]))
        assert count == 9
        # The planner's currently blocked upgrade shards must also have an available
        # runner when its real output supplies the dynamic shard matrix.
        upgrade = workflow('tests.yml')['jobs']['e2e-upgrade']
        assert upgrade['needs'] == 'e2e-upgrade-plan'
        expected = 'blacksmith-32vcpu-ubuntu-2404' if repository == UPSTREAM else 'ubuntu-24.04'
        assert gha.render(upgrade['runs-on'], context(repository)) == expected


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
    for repository, workers in [(UPSTREAM, '16'), (FORK, '2')]:
        for row in rows:
            ctx = context(repository, matrix=row)
            expected_workers = workers if row['marker'] == 'windows' else ''
            assert gha.render(run['env']['HERMES_TEST_WORKERS'], ctx) == expected_workers
            # Existing ARM overrides win; only fork x64 receives a workload budget.
            limit = row.get('timeout') or (45 if repository == FORK and row['marker'] == 'windows' else 30)
            assert int(gha.render(workflow('tests-os.yml')['jobs']['os-tests']['timeout-minutes'], ctx)) == limit


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
