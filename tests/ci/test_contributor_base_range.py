"""Attribution checks a stacked PR's own commits, while retaining its gate."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import hermes_yaml as yaml
from tests.ci import _gha_expr as gha

ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args, **kwargs):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def commit(repo, email, label):
    (repo / 'change').write_text(label)
    git(repo, 'add', 'change')
    git(repo, '-c', 'user.name=Fixture Author', '-c', f'user.email={email}', 'commit', '-qm', label)
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    main = commit(repo, '1+fixture@users.noreply.github.com', 'main')
    git(repo, 'update-ref', 'refs/remotes/origin/main', main)
    base = commit(repo, 'historical-unmapped@example.invalid', 'already accepted stack')
    return repo, main, base


def run_gate(repo, base_sha, tmp_path):
    data = yaml.safe_load((ROOT / '.github/workflows/contributor-check.yml').read_text())
    step = next(s for s in data['jobs']['check-attribution']['steps'] if s.get('id') == 'check-emails')
    ctx = {'github': {'event': {'pull_request': {'base': {'sha': base_sha}}}}}
    env = dict(os.environ, PR_BASE_SHA=gha.render(step['env']['PR_BASE_SHA'], ctx), GITHUB_OUTPUT=str(tmp_path / 'output'))
    return subprocess.run(['bash', '-c', step['run']], cwd=repo, env=env, text=True, capture_output=True, timeout=10, check=False)


def test_stacked_pr_does_not_reaudit_old_unmapped_authors(history, tmp_path):
    repo, _, base = history
    commit(repo, '2+current@users.noreply.github.com', 'PR change')
    result = run_gate(repo, base, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'All contributor emails are mapped' in result.stdout
    assert 'historical-unmapped' not in result.stdout


def test_new_unmapped_pr_author_still_fails(history, tmp_path):
    repo, _, base = history
    commit(repo, 'new-unmapped@example.invalid', 'PR change')
    result = run_gate(repo, base, tmp_path)
    assert result.returncode == 1
    assert 'new-unmapped@example.invalid' in result.stdout
    assert 'historical-unmapped' not in result.stdout


def test_non_pr_retains_main_range(history, tmp_path):
    repo, _, _ = history
    commit(repo, '2+current@users.noreply.github.com', 'PR change')
    result = run_gate(repo, '', tmp_path)
    assert result.returncode == 1
    assert 'historical-unmapped@example.invalid' in result.stdout


def test_missing_base_fails_closed_without_empty_success(history, tmp_path):
    repo, _, _ = history
    result = run_gate(repo, 'f' * 40, tmp_path)
    assert result.returncode != 0
    assert not (repo / 'review-status.json').exists()
    assert 'No new commits' not in result.stdout
