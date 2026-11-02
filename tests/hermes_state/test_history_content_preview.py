"""Display previews bound transport materialization; stored/exported history stays intact."""
import gc
import tracemalloc

import pytest
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    # Message identity lazily imports conversation_loop/bootstrap. This worktree's
    # Git metadata belongs to the installed checkout; updater recovery is outside
    # a read-only history unit test and must not inspect that real home.
    import hermes_cli._early_recovery
    monkeypatch.setattr(hermes_cli._early_recovery, 'restore_interrupted_pull', lambda root: False)
    store = SessionDB(tmp_path / 'state.db')
    yield store
    store.close()


def test_54mb_plain_text_is_bounded_before_python_materialization_and_full_read_survives(db):
    db.create_session('large', 'cli')
    body = 'X' * 54_000_000
    row_id = db.append_message('large', 'user', body, timestamp=1)
    del body
    gc.collect()
    tracemalloc.start()
    messages = db.get_message_previews('large', preview_chars=1024, include_compacted=True)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 4_000_000, 'Preview fetched the full 54MB body into Python'
    assert len(messages) == 1 and messages[0]['id'] == row_id
    assert messages[0]['content_preview'] == {'truncated': True, 'original_chars': 54_000_000,
                                             'shown_chars': 1024, 'full_content_available': True}
    assert messages[0]['content'].startswith('X' * 1024)
    assert len(messages[0]['content']) < 1500
    assert 'History preview' in messages[0]['content']
    assert len(db.get_messages('large')[0]['content']) == 54_000_000
    assert db._read_one('SELECT length(content) FROM messages WHERE id=?', (row_id,))[0] == 54_000_000


def test_normal_history_is_identical_and_preview_does_not_mutate(db):
    db.create_session('normal', 'cli')
    db.append_message('normal', 'user', 'normal ask', timestamp=1)
    db.append_message('normal', 'assistant', 'normal answer', timestamp=2)
    before = db.get_messages('normal', include_compacted=True)
    assert db.get_message_previews('normal', preview_chars=256, include_compacted=True) == before
    assert db.get_messages('normal', include_compacted=True) == before


def test_lineage_paging_and_identical_prefixes_preserve_distinct_rows(db):
    db.create_session('parent', 'cli')
    first = db.append_message('parent', 'user', 'P' * 4096 + 'A', timestamp=1)
    db.end_session('parent', 'compression')
    db.create_session('child', 'cli', parent_session_id='parent')
    second = db.append_message('child', 'user', 'P' * 4096 + 'B', timestamp=2)
    kwargs = dict(preview_chars=256, include_compacted=True, include_ancestors=True)
    assert [m['id'] for m in db.get_message_previews('child', **kwargs)] == [first, second]
    assert [m['id'] for m in db.get_message_previews('child', limit=1, latest=True, **kwargs)] == [second]
    assert [m['id'] for m in db.get_message_previews('child', limit=1, offset=1, latest=True, **kwargs)] == [first]


def test_compaction_copy_dedup_and_rewind_removal_match_full_history(db):
    db.create_session('compacted', 'cli')
    body = 'B' * 4096
    db.append_message('compacted', 'user', body, timestamp=1)
    db.append_message('compacted', 'assistant', 'answer', timestamp=2)
    db.archive_and_compact('compacted', [{'role': 'user', 'content': body, 'timestamp': 1},
                                       {'role': 'assistant', 'content': 'answer', 'timestamp': 2}])
    full = db.get_messages('compacted', include_compacted=True)
    preview = db.get_message_previews('compacted', preview_chars=256, include_compacted=True)
    assert [m['id'] for m in preview] == [m['id'] for m in full]
    db.rewind_to_message('compacted', next(m['id'] for m in db.get_messages('compacted') if m['role'] == 'user'))
    assert [m['id'] for m in db.get_message_previews('compacted', preview_chars=256, include_compacted=True)] == [m['id'] for m in db.get_messages('compacted', include_compacted=True)]


@pytest.mark.parametrize('limit', [0, 255, 65537, True, '1024'])
def test_invalid_preview_budget_fails_closed(db, limit):
    with pytest.raises(ValueError):
        db.get_message_previews('missing', preview_chars=limit)


def test_legacy_huge_rows_with_same_prefix_and_timestamp_never_false_merge(db):
    db.create_session('legacy', 'cli')
    ids = [db.append_message('legacy', 'user', 'S' * 4096 + tail, timestamp=1) for tail in ('a', 'b')]
    db._execute_write(lambda conn: conn.execute('UPDATE messages SET display_identity=NULL, display_order=NULL WHERE session_id=?', ('legacy',)))
    assert [m['id'] for m in db.get_message_previews('legacy', preview_chars=256, include_compacted=True)] == ids


def test_removed_child_identity_hides_huge_ancestor_copy(db):
    body = 'G' * 4096
    db.create_session('old', 'cli')
    db.append_message('old', 'user', body, timestamp=1)
    db.end_session('old', 'compression')
    db.create_session('new', 'cli', parent_session_id='old')
    copied = db.append_message('new', 'user', body, timestamp=1)
    kept = db.append_message('new', 'assistant', 'keep', timestamp=2)
    db._execute_write(lambda conn: conn.execute('UPDATE messages SET active=0,compacted=0 WHERE id=?', (copied,)))
    kwargs = dict(include_compacted=True, include_ancestors=True)
    assert [m['id'] for m in db.get_message_previews('new', preview_chars=256, **kwargs)] == [m['id'] for m in db.get_messages('new', **kwargs)] == [kept]


def test_structured_and_reserved_json_content_keeps_existing_projection(db):
    db.create_session('json', 'cli')
    values = [[{'type': 'text', 'text': 'J' * 512}], {'nested': 'K' * 512}, '\x00json:malformed literal']
    for i, value in enumerate(values):
        db.append_message('json', 'user', value, timestamp=i)
    assert db.get_message_previews('json', preview_chars=256) == db.get_messages('json')
