"""A delayed observational receipt may converge an uncertain send exactly once."""
import json

import pytest

from tests.helpers import MemoryRedis
from wemaa.errors import BizError
from wemaa.services import replies

EVENT = 'evt' + 'a' * 29
EVIDENCE = {'title_verified': True, 'outgoing_verified': True,
            'frame_sha256': 'c' * 64, 'snapshot_id': 7}
UNCERTAIN_RECEIPT = {'status': 'uncertain', 'reason': 'SEND_OUTCOME_UNKNOWN', 'evidence': {}}


@pytest.fixture()
def cache(monkeypatch):
    cache = MemoryRedis()
    monkeypatch.setattr(replies, 'client', lambda: cache)
    return cache


def seed(cache, status='uncertain', receipt=UNCERTAIN_RECEIPT, **extra):
    row = {'event_id': EVENT, 'conversation_id': 'b' * 64, 'status': status,
           'reply_id': 'r' * 32, 'created_at': 900.0, 'updated_at': 950.0,
           'expires_at': 1000.0, 'reason': None, 'receipt': receipt}
    row.update(extra)
    cache.set(replies.event_key(EVENT), json.dumps(row, ensure_ascii=False))
    return row


def sent_receipt(**evidence):
    return {'status': 'sent', 'reason': 'RECONCILED_OUTGOING_OBSERVED',
            'evidence': dict(EVIDENCE, **evidence)}


def test_uncertain_converges_to_sent_once(cache):
    seed(cache)
    record = replies.result(EVENT, sent_receipt())
    assert record['status'] == 'sent'
    raw = json.loads(cache.get(replies.event_key(EVENT)))
    assert raw['reconciled'] is True
    # The identical receipt stays idempotent.
    assert replies.result(EVENT, sent_receipt())['status'] == 'sent'
    # A second, different convergence is refused.
    with pytest.raises(BizError):
        replies.result(EVENT, sent_receipt(snapshot_id=8))


def test_uncertain_never_converges_to_failed_late(cache):
    seed(cache)
    with pytest.raises(BizError):
        replies.result(EVENT, {'status': 'failed', 'reason': 'RECONCILED_NOT_OBSERVED'})


def test_sent_requires_full_verification_evidence(cache):
    seed(cache)
    with pytest.raises(BizError):
        replies.result(EVENT, {'status': 'sent', 'reason': 'X', 'evidence': {}})


def test_terminal_sent_receipt_is_never_overwritten(cache):
    receipt = sent_receipt()
    seed(cache, status='sent', receipt=receipt)
    assert replies.result(EVENT, receipt)['status'] == 'sent'
    with pytest.raises(BizError):
        replies.result(EVENT, sent_receipt(snapshot_id=9))


def test_reconcile_rejects_missing_frame_and_snapshot_receipt(cache):
    seed(cache)
    # Missing snapshot_id and frame_sha256
    with pytest.raises(BizError, match="对账收敛"):
        replies.result(EVENT, {'status': 'sent', 'reason': 'RECONCILED_OUTGOING_OBSERVED',
                               'evidence': {'title_verified': True, 'outgoing_verified': True}})
    # Non-standard reason
    with pytest.raises(BizError, match="对账收敛"):
        replies.result(EVENT, {'status': 'sent', 'reason': 'ARBITRARY_REASON',
                               'evidence': EVIDENCE})
