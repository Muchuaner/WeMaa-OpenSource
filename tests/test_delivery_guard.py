from pathlib import Path

import sqlite3

import pytest

from wemaa.delivery_guard import DeliveryGuard


def test_uncertain_blocks_new_output_ids_across_restart_until_new_inbound(tmp_path: Path):
    path = tmp_path / 'guards.sqlite3'
    guard = DeliveryGuard(path, 'account')
    source = {'event_id': 'a'*64, 'observed_at': 100.0}
    attempt = guard.reserve('group', 'output-1', source)
    guard.finish(attempt, 'uncertain', 'postcondition_failed', ['gui-1'])
    restarted = DeliveryGuard(path, 'account')
    for candidate in (None, source, dict(source, observed_at=200),
                      {'event_id': 'b'*64, 'observed_at': 99.0}):
        with pytest.raises(RuntimeError, match='WAIT_FOR_NEW_INBOUND'):
            restarted.reserve('group', 'new-output-id', candidate)
    fresh = restarted.reserve('group', 'output-2', {'event_id': 'b'*64, 'observed_at': 101.0})
    restarted.finish(fresh, 'verified', '', ['gui-2'])
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT state,command_ids FROM attempts ORDER BY id').fetchall() == [
            ('uncertain', '["gui-1"]'), ('verified', '["gui-2"]')]


def test_reservation_crash_and_separate_account_session(tmp_path: Path):
    path = tmp_path / 'guards.sqlite3'
    guard = DeliveryGuard(path, 'account')
    guard.reserve('group', 'output-1', None)
    with pytest.raises(RuntimeError, match='WAIT_FOR_NEW_INBOUND'):
        DeliveryGuard(path, 'account').reserve('group', 'output-2', None)
    guard.reserve('private', 'output-3', None)
    DeliveryGuard(path, 'another-account').reserve('group', 'output-4', None)


def test_verified_and_no_enqueue_failures_do_not_freeze_chat(tmp_path: Path):
    guard = DeliveryGuard(tmp_path / 'guards.sqlite3', 'account')
    first = guard.reserve('group', 'output-1', None)
    guard.finish(first, 'blocked', 'route_missing', [])
    second = guard.reserve('group', 'output-2', None)
    guard.finish(second, 'verified', '', ['gui-2'])
    guard.reserve('group', 'output-3', None)
    with pytest.raises(RuntimeError, match='ALREADY_TERMINAL'):
        guard.finish(first, 'verified', '', ['wrong-command'])
