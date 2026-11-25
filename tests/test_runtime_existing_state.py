"""Deployed runtimes must never recreate private state after the preflight check."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import anchor_roster as roster
import board_auth as auth


@pytest.mark.parametrize('content', [None, '{invalid', json.dumps({'anchors': [
    {'tag': 'synthetic', 'sec_uid': 'MS4wLjABAAAA' + 'x' * 40},
    {'tag': 'synthetic', 'sec_uid': 'MS4wLjABAAAA' + 'x' * 40},
]})])
def test_required_roster_is_never_reseeded(tmp_path, monkeypatch, content):
    monkeypatch.setenv('WB_REQUIRE_EXISTING_STATE', '1')
    path = tmp_path / 'anchors.json'
    if content is not None:
        path.write_text(content, encoding='utf-8')
    with pytest.raises((OSError, ValueError, TypeError)):
        roster.ensure(path)
    assert not list(tmp_path.glob('*.broken-*'))
    assert path.read_text(encoding='utf-8') == content if content is not None else not path.exists()


def test_roster_rechecks_after_successful_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv('WB_REQUIRE_EXISTING_STATE', '1')
    path = tmp_path / 'anchors.json'
    path.write_text(json.dumps({'anchors': [{'tag': 'synthetic',
        'sec_uid': 'MS4wLjABAAAA' + 'x' * 40, 'enabled': False}]}), encoding='utf-8')
    value, created = roster.ensure(path)
    assert not created and value['anchors'][0]['enabled'] is False
    path.write_text('{late corruption', encoding='utf-8')
    with pytest.raises(ValueError):
        roster.ensure(path)
    assert path.read_text(encoding='utf-8') == '{late corruption'


@pytest.mark.parametrize('entries', [["not an anchor"], [
    {'tag': 'synthetic', 'sec_uid': 'MS4wLjABAAAA' + 'x' * 40, 'enabled': 'false'}], [
    {'tag': 'synthetic', 'sec_uid': 'MS4wLjABAAAA' + 'x' * 40, 'watch': 0}]])
def test_required_roster_rejects_normalization_that_changes_intent(tmp_path, monkeypatch, entries):
    monkeypatch.setenv('WB_REQUIRE_EXISTING_STATE', '1')
    path = tmp_path / 'anchors.json'
    path.write_text(json.dumps({'anchors': entries}), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        roster.ensure(path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.broken-*'))


@pytest.mark.parametrize('content', [None, '{invalid', '{}'])
def test_required_user_database_is_never_regenerated(tmp_path, monkeypatch, content):
    monkeypatch.setenv('WB_REQUIRE_EXISTING_STATE', '1')
    path = tmp_path / 'board_users.json'
    if content is not None:
        path.write_text(content, encoding='utf-8')
    monkeypatch.setattr(auth, 'gen_password', lambda: pytest.fail('must not generate credentials'))
    with pytest.raises(RuntimeError, match='existing user database'):
        auth.ensure_db(str(path))
    assert not (tmp_path / 'board_initial_password.txt').exists()
    assert path.read_text(encoding='utf-8') == content if content is not None else not path.exists()


def test_existing_user_database_is_reused_then_late_loss_refused(tmp_path, monkeypatch):
    monkeypatch.setenv('WB_REQUIRE_EXISTING_STATE', '1')
    path = tmp_path / 'board_users.json'
    data = {'secret': 'synthetic-only', 'users': [{'name': 'test', 'salt': '11', 'hash': '22'}]}
    path.write_text(json.dumps(data), encoding='utf-8')
    before = path.read_bytes()
    assert auth.ensure_db(str(path)) == (data, None)
    assert path.read_bytes() == before
    path.unlink()
    monkeypatch.setattr(auth, 'gen_password', lambda: pytest.fail('must not generate credentials'))
    with pytest.raises(RuntimeError, match='existing user database'):
        auth.ensure_db(str(path))
    assert not path.exists()
