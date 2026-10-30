"""Run the actual lint-base workflow step against shallow, diverged Git history."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import hermes_yaml as yaml
from tests.ci import _gha_expr as gha

ROOT = Path(__file__).resolve().parents[2]


def git(root, *args, check=True):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                          text=True, check=check, timeout=10).stdout.strip()


def commit(root, value):
    (root / 'source').write_text(value, encoding='utf-8')
    git(root, 'add', 'source')
    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', value)
    return git(root, 'rev-parse', 'HEAD')


@pytest.fixture(params=['diverged-input', 'merged-input'])
def graph(tmp_path, request):
    remote = tmp_path / 'remote'
    remote.mkdir()
    git(remote, 'init', '-q')
    commit(remote, 'old')
    git(remote, 'tag', 'unrelated-old-tag')
    common = commit(remote, 'common')
    git(remote, 'checkout', '-qb', 'target')
    target = commit(remote, 'target advanced')
    git(remote, 'checkout', '-qb', 'judged', common)
    commit(remote, 'intermediate judged input')
    judged = commit(remote, 'explicit judged input')
    # The PR's present head has advanced beyond the explicitly judged commit.
    current_head = commit(remote, 'later PR head')
    if request.param == 'merged-input':
        git(remote, 'checkout', '-qb', 'merged', judged)
        # A real two-parent checkout; its tree strategy does not alter ancestry.
        git(remote, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'merge', '-q', '--no-ff', '-s', 'ours', '-m', 'synthetic merge', target)
        judged = git(remote, 'rev-parse', 'HEAD')
    git(remote, 'branch', 'input-commit', judged)
    checkout = tmp_path / 'checkout'
    subprocess.run(['git', 'clone', '-q', '--depth=2', '--single-branch',
                    '--branch', 'input-commit', remote.as_uri(), str(checkout)], check=True)
    merge_base = git(remote, 'merge-base', target, judged)
    return checkout, common, target, judged, current_head, merge_base


def case_metadata(graph, invalid):
    _, common, target, judged, current_head, merge_base = graph
    pr = {'base': {'repo': {'full_name': 'fixture/repo'}, 'sha': common, 'ref': 'target'},
          'head': {'sha': current_head}}
    comparison = {'base_commit': {'sha': target}, 'merge_base_commit': {'sha': merge_base},
                  'status': 'ahead' if merge_base == target else 'diverged'}
    event_base = {'repo': 'fixture/repo', 'sha': target}
    removed = object()
    mutations = {
        'foreign_base': (event_base, 'repo', 'other/repo'),
        'missing_event_base': (event_base, 'sha', ''),
        'invalid_event_sha': (event_base, 'sha', '--all'),
        'missing_merge_base': (comparison, 'merge_base_commit', removed),
        'invalid_sha': (comparison['merge_base_commit'], 'sha', '--all'),
        'wrong_compare_base': (comparison['base_commit'], 'sha', judged),
    }
    if invalid is not None:
        mapping, key, value = mutations[invalid]
        if value is removed:
            mapping.pop(key)
        else:
            mapping[key] = value
    responses = {'repos/fixture/repo/pulls/7': pr,
                 f'repos/fixture/repo/compare/{target}...{judged}': comparison}
    return responses, event_base


def install_fake_api(tmp_path, responses):
    response_path = tmp_path / 'responses.json'
    response_path.write_text(json.dumps(responses), encoding='utf-8')
    calls = tmp_path / 'api-calls.jsonl'
    fake_bin = tmp_path / 'fake-bin'
    fake_bin.mkdir()
    gh = fake_bin / 'gh'
    gh.write_text(f'#!{sys.executable}\nimport json,os,sys\n'
                  "with open(os.environ['CALLS'],'a',encoding='utf-8') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                  "with open(os.environ['RESPONSES'],encoding='utf-8') as f: responses=json.load(f)\n"
                  "assert sys.argv[1]=='api'\nprint(json.dumps(responses[sys.argv[2]]))\n",
                  encoding='utf-8')
    gh.chmod(0o755)
    return fake_bin, response_path, calls


def workflow_base_step():
    workflow = yaml.safe_load((ROOT / '.github/workflows/lint.yml').read_text(encoding='utf-8'))
    job = workflow['jobs']['lint-diff']
    checkout_step = next(step for step in job['steps'] if step.get('name') == 'Checkout code')
    assert checkout_step['with']['ref'] == '${{ inputs.commit }}'
    assert checkout_step['with']['fetch-depth'] == 2
    assert job['timeout-minutes'] == 10
    step = next(step for step in job['steps'] if step.get('id') == 'base')
    return step


def run_step(tmp_path, graph, invalid=None, event='pull_request'):
    checkout, _, _, _, current_head, _ = graph
    responses, event_base = case_metadata(graph, invalid)
    fake_bin, response_path, calls = install_fake_api(tmp_path, responses)
    step = workflow_base_step()
    ctx = {'github': {'repository': 'fixture/repo', 'token': 'fixture-token',
                      'event': {'pull_request': {'base': {'repo': {'full_name': event_base['repo']},
                                                        'sha': event_base['sha'], 'ref': 'target'},
                                                 'head': {'sha': current_head}}}},
           'inputs': {'event_name': event}}
    event_values = {key: gha.render(value, ctx) for key, value in step['env'].items()}
    output = tmp_path / 'output'
    result = subprocess.run(['bash', '-c', step['run']], cwd=checkout, capture_output=True,
                            text=True, check=False, timeout=15, env=dict(os.environ,
                            PATH=str(fake_bin) + os.pathsep + os.environ['PATH'],
                            RESPONSES=str(response_path), CALLS=str(calls),
                            GITHUB_OUTPUT=str(output), **event_values))
    return result, output, calls


def test_shallow_lint_fetches_true_merge_base_of_current_target_and_judged_head(tmp_path, graph):
    result, output, calls = run_step(tmp_path, graph)
    assert result.returncode == 0, result.stderr
    checkout, common, target, judged, current_head, merge_base = graph
    assert judged != current_head and target != common
    assert output.read_text(encoding='utf-8').splitlines() == [
        f'sha={merge_base}', 'ref=target (merge base)']
    assert json.loads(calls.read_text(encoding='utf-8').splitlines()[0]) == [
        'api', f'repos/fixture/repo/compare/{target}...{judged}']
    assert len(calls.read_text(encoding='utf-8').splitlines()) == 1
    # The stale PR REST response exists in the fake API, but is never requested.
    assert git(checkout, 'rev-parse', 'FETCH_HEAD') == merge_base
    assert git(checkout, 'rev-parse', 'HEAD') == judged
    assert git(checkout, 'tag') == ''
    assert git(checkout, 'branch', '-r') == 'origin/input-commit'
    assert git(checkout, 'rev-parse', '--is-shallow-repository') == 'true'
    base_tree = tmp_path / 'lint-base'
    git(checkout, 'worktree', 'add', '--detach', str(base_tree), merge_base)
    assert (base_tree / 'source').is_file()
    if merge_base == target:
        assert (base_tree / 'source').read_text(encoding='utf-8') == 'target advanced'
    else:
        assert merge_base == common
        assert (base_tree / 'source').read_text(encoding='utf-8') == 'common'


@pytest.mark.parametrize('invalid', ['foreign_base', 'missing_event_base', 'invalid_event_sha', 'missing_merge_base',
                                     'invalid_sha', 'wrong_compare_base'])
def test_bad_compare_metadata_never_publishes_a_lint_base(tmp_path, graph, invalid):
    result, output, _ = run_step(tmp_path, graph, invalid)
    assert result.returncode != 0
    assert not output.exists()
    checkout, _, _, judged, _, _ = graph
    assert git(checkout, 'rev-parse', 'HEAD') == judged
    assert not (checkout / '.git/FETCH_HEAD').exists()


def test_non_pr_uses_previous_commit_without_any_api_request(tmp_path, graph):
    result, output, calls = run_step(tmp_path, graph, event='push')
    assert result.returncode == 0, result.stderr
    checkout = graph[0]
    assert output.read_text(encoding='utf-8').splitlines() == [
        f"sha={git(checkout, 'rev-parse', 'HEAD~1')}", 'ref=HEAD~1']
    assert not calls.exists()
