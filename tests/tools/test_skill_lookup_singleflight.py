"""Concurrent skill lookup must share discovery without losing collision checks."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import pytest
from types import SimpleNamespace

from agent import skill_utils
from tools import skills_tool


def skill(root, relative, declared=None):
    path = root / relative / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text(f'---\nname: {declared or path.parent.name}\n---\nBody\n')
    return path


@pytest.mark.parametrize('warm', [False, True])
def test_four_concurrent_names_share_signature_and_candidate_traversals(tmp_path, monkeypatch, warm):
    names = ['desk', 'mesh', 'brain', 'ladder']
    paths = {name: skill(tmp_path, name) for name in names}
    skills_tool.clear_skills_cache()
    if warm:
        skills_tool._collect_skill_candidates(names[0], None, [tmp_path])
    counts = {'signature': 0, 'active': 0, 'legacy': 0}
    signature = skills_tool._skills_scan_signature
    scan = skill_utils.iter_skill_index_files
    barrier = threading.Barrier(4)
    lock = threading.RLock()

    class ConcurrentFirstEntry:
        def __enter__(self):
            barrier.wait(timeout=5)
            lock.acquire()

        def __exit__(self, *_):
            lock.release()

    def counted_signature(*args):
        counts['signature'] += 1
        return signature(*args)

    def counted_scan(*args, **kwargs):
        counts['legacy' if kwargs.get('legacy_markdown') else 'active'] += 1
        return scan(*args, **kwargs)

    monkeypatch.setattr(skills_tool, '_LOOKUP_SCAN_LOCK', ConcurrentFirstEntry())
    monkeypatch.setattr(skills_tool, '_skills_scan_signature', counted_signature)
    monkeypatch.setattr(skill_utils, 'iter_skill_index_files', counted_scan)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda name: skills_tool._collect_skill_candidates(name, None, [tmp_path]), names))
    assert results == [[(paths[name].parent, paths[name])] for name in names]
    assert counts == {'signature': 1, 'active': int(not warm), 'legacy': int(not warm)}


def test_snapshot_invalidates_nested_install_explicit_reload_and_ttl(tmp_path, monkeypatch):
    skill(tmp_path, 'category/existing')
    skills_tool.clear_skills_cache()
    assert not skills_tool._collect_skill_candidates('new', None, [tmp_path])
    added = skill(tmp_path, 'category/new', 'new')
    assert skills_tool._collect_skill_candidates('new', None, [tmp_path]) == [(added.parent, added)]
    clock = skills_tool.time.monotonic()
    added.write_text('---\nname: alias\n---\nBody\n')
    skills_tool.clear_skills_cache()
    assert skills_tool._collect_skill_candidates('alias', None, [tmp_path]) == [(added.parent, added)]
    added.write_text('---\nname: renamed\n---\nBody\n')
    monkeypatch.setattr(skills_tool.time, 'monotonic', lambda: clock + 60)
    assert skills_tool._collect_skill_candidates('renamed', None, [tmp_path]) == [(added.parent, added)]


def test_legacy_flat_archive_collision_is_retained(tmp_path):
    active = skill(tmp_path, 'demo')
    archive = tmp_path / '.archive/demo.md'
    archive.parent.mkdir()
    skills_tool.clear_skills_cache()
    assert skills_tool._collect_skill_candidates('demo', None, [tmp_path]) == [(active.parent, active)]
    archive.write_text('Historical different content')
    assert set(skills_tool._collect_skill_candidates('demo', None, [tmp_path])) == {
        (active.parent, active), (None, archive)}


def test_cycle_terminates_and_category_aliases_remain_visible(tmp_path):
    path = skill(tmp_path, 'actual/demo', 'alias-name')
    (tmp_path / 'actual/back').symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / 'linked').symlink_to(tmp_path / 'actual', target_is_directory=True)
    found = list(skill_utils.iter_skill_index_files(tmp_path, 'SKILL.md'))
    assert found == [path, tmp_path / 'linked/demo/SKILL.md']
    # Signature and lookup share the cycle-safe behavior, not just listing.
    skills_tool.clear_skills_cache()
    matches = skills_tool._collect_skill_candidates('alias-name', None, [tmp_path])
    assert len(matches) == 1
    assert matches[0][1].resolve() == path


def test_zero_inode_filesystem_keeps_distinct_categories_and_stops_cycles(tmp_path, monkeypatch):
    path = skill(tmp_path, 'category/demo')
    (tmp_path / 'category/back').symlink_to(tmp_path, target_is_directory=True)
    original_stat = skill_utils.os.stat

    def zero_inode(*args, **kwargs):
        info = original_stat(*args, **kwargs)
        values = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
        values['st_ino'] = 0
        return SimpleNamespace(**values)

    monkeypatch.setattr(skill_utils.os, 'stat', zero_inode)
    assert list(skill_utils.iter_skill_index_files(tmp_path, 'SKILL.md')) == [path]
    signature = skills_tool._skills_scan_signature([tmp_path], set())
    assert any(str(path.parent) == entry[0] for entry in signature[0])
