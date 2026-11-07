"""Maa API tests use no production Redis, MinIO, Windows or legacy bridge."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from wemaa.errors import BizError
from wemaa.services import maa as service


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **_kw):
        self.values[key] = value
        return True

    def mget(self, keys):
        return [self.get(key) for key in keys]

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def lpop(self, key):
        values = self.lists.get(key, [])
        return values.pop(0) if values else None

    def ltrim(self, key, start, end):
        self.lists[key] = self.lrange(key, start, end)

    def expire(self, *_args):
        return True

    def pipeline(self):
        return MemoryPipeline(self)


class MemoryPipeline:
    def __init__(self, cache):
        self.cache = cache
        self.transaction = False
        self.pending = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def watch(self, *_args):
        self.transaction = False
        self.pending = []

    def multi(self):
        self.transaction = True

    def execute(self):
        results = [getattr(self.cache, name)(*args, **kwargs) for name, args, kwargs in self.pending]
        self.pending = []
        self.transaction = False
        return results

    def __getattr__(self, name):
        def command(*args, **kwargs):
            if self.transaction:
                self.pending.append((name, args, kwargs))
                return self
            return getattr(self.cache, name)(*args, **kwargs)
        return command


@pytest.fixture()
def cache(monkeypatch):
    value = MemoryRedis()
    monkeypatch.setattr(service, "client", lambda: value)
    monkeypatch.setattr(service.time, "time", lambda: 1000.0)
    return value


def observation(**updates):
    row = {"schema_version": 1, "gateway_id": "win-wechat-maa", "collected_at": 1000,
           "runtime": {"worker_state": "Ready", "trial_state": "Ready", "old_gateway_state": "Disabled",
                       "progress": {}, "versions": {"maafw": "5.14.0"},
                       "layout": {"screen": [1920, 1080], "client": [1920, 1040], "divider_x": 480, "dpi": 96}},
           "summary": {"conversation_count": 1, "message_row_count": 1, "snapshot_count": 2},
           "scans": [], "contacts": [], "conversations": [], "events": [], "actions": [],
           "backups": [], "lessons": {}, "acceptance": {}}
    row.update(updates)
    return row

def test_manual_send_requires_observed_target_and_idempotency(cache, monkeypatch):
    monkeypatch.setattr(service.settings,'WECHAT_MAA_REPLY_ENABLED',True)
    value=observation()
    value['runtime'].update(worker_state='Running',progress={'manual_commands_enabled':True})
    value['conversations']=[{'id':'a'*64,'title':'Demo User','manual_target_observed':True}]
    service.store_telemetry(value)
    body={'action':'send_message','params':{'conversation_id':'a'*64,'text':'你好'},'request_id':str(uuid.uuid4())}
    command,created=service.enqueue(body,1)
    assert created and command['expires_at']==1300
    assert service.enqueue(body,1)==(command,False)
    changed=deepcopy(body);changed['params']['text']='不同消息'
    with pytest.raises(BizError): service.enqueue(changed,1)

def test_manual_target_cannot_be_guessed(cache, monkeypatch):
    monkeypatch.setattr(service.settings,'WECHAT_MAA_REPLY_ENABLED',True)
    value=observation();value['runtime'].update(worker_state='Running',progress={'manual_commands_enabled':True})
    service.store_telemetry(value)
    for action,params in [('send_message',{'conversation_id':'a'*64,'text':'你好'}),('open_contact',{'contact_id':'wx:unknown'})]:
        with pytest.raises(BizError): service.enqueue({'action':action,'params':params,'request_id':str(uuid.uuid4())},1)


def new_command(action="capture", **params):
    return {"action": action, "params": params, "request_id": str(uuid.uuid4())}


def test_group_contacts_are_observed_not_scan_coverage(cache):
    row = {'id':'a'*64,'title':'开发组 (4)','group_profile':{'conversation_id':'a'*64,'kind':'group','name':'开发组','row_name':'开发组','header':'开发组 (4)','member_count':4,'observed_at':990}}
    service.store_telemetry(observation(conversations=[row]))
    result = service.contacts('group',None,1,40)
    assert result['coverage']=='observed_only'
    assert result['items'][0]['member_count']==4
    assert service.contacts('personal',None,1,40)['total']==0
    row['group_profile']['header']='开发组'
    with pytest.raises(BizError): service.validate_telemetry(observation(conversations=[row]),1000)


def test_activity_dedupes_delivery_filters_and_walks_cursor(cache):
    events = [{'id':i,'kind':'message_received','seen':990+i,'detail':{'name':'Demo User','token':'secret','text':'not exported'}} for i in range(4)]
    body=observation(events=events)
    service.store_telemetry(body); service.store_telemetry(body)
    rows=service.activity(limit=2)['items']
    assert [r['seq'] for r in rows]==[4,3]
    assert rows[0]['detail']=={'name':'Demo User'}
    assert [r['seq'] for r in service.activity(before=3,limit=2)['items']]==[2,1]
    assert [r['seq'] for r in service.activity(after=1,limit=2)['items']]==[3,2]
    assert service.activity(category='contacts')['items']==[]
    assert len(service.activity(category='messages')['items'])==4
    with pytest.raises(BizError): service.activity(after=1,before=2)


def test_machine_token_is_required_and_constant_time_compared(monkeypatch):
    monkeypatch.setattr(service, "settings", SimpleNamespace(WECHAT_MAA_TOKEN="machine-only"))
    assert service.authorised("Bearer machine-only")
    for header in ("", "machine-only", "Bearer wrong", "Bearer machine-only ", "Bearer 错误"):
        assert not service.authorised(header)
    monkeypatch.setattr(service, "settings", SimpleNamespace(WECHAT_MAA_TOKEN=""))
    assert not service.authorised("Bearer ")


@pytest.mark.parametrize("patch", [
    {"gateway_id": "legacy"}, {"schema_version": True}, {"collected_at": 1100},
    {"collected_at": 600}, {"collected_at": float("nan")}, {"summary": {"conversation_count": -1}},
    {"scans": [{"id": "../unsafe", "scope": "personal", "status": "complete"}]},
    {"contacts": [{"id": "c", "scope": "group", "scan_id": "a" * 32}]},
    {"conversations": [{"id": "../path", "title": "x"}]},
    {"scans": [{"id": "a" * 32, "scope": [], "status": "complete"}]},
    {"collected_at": 10**1000},
])
def test_telemetry_rejects_invalid_schema_clock_and_identity(patch):
    with pytest.raises(BizError):
        service.validate_telemetry(observation(**patch), now=1000)


def test_telemetry_extra_actual_metadata_is_supported(cache):
    body = observation(source_updated_at=995)
    body["runtime"].update({"execution_time_limit_seconds": 7200, "capture_asset": "f" * 64,
                            "sending_enabled": False, "layout": {"kind": "expected_guard_baseline"}})
    service.store_telemetry(body)
    result = service.overview()
    assert not result["offline"] and not result["stale"]
    assert result["runtime"]["execution_time_limit_seconds"] == 7200
    assert result["sending_enabled"] is False
    assert "contacts" not in result and "conversations" not in result


def test_speaker_avatar_asset_round_trip_and_invalid_id_rejected(cache):
    payload = {'text': '/菜单', 'direction': 'incoming', 'message_kind': 'text',
               'sender_name': 'Demo User', 'sender_identity_key': 'b' * 64,
               'sender_wechat_id': 'tom_verified', 'sender_avatar_asset': 'c' * 64}
    row = {'id': 'a' * 64, 'title': '开发组(4)', 'latest_snapshot': {'payloads': [payload]},
           'messages': [{'payload': dict(payload)}]}
    service.store_telemetry(observation(conversations=[row]))
    detail = service.conversation_detail('a' * 64)
    assert detail['latest_snapshot']['payloads'][0] == payload
    assert detail['messages'][0]['payload']['sender_avatar_asset'] == 'c' * 64
    for section in ('latest_snapshot', 'messages'):
        unsafe = deepcopy(row)
        target = unsafe['latest_snapshot']['payloads'][0] if section == 'latest_snapshot' else unsafe['messages'][0]['payload']
        target['sender_avatar_asset'] = '../keys'
        with pytest.raises(BizError):
            service.validate_telemetry(observation(conversations=[unsafe]), 1000)


def test_maa_reply_runtime_requires_server_enable_and_does_not_fake_online(cache,monkeypatch):
    body=observation()
    body['runtime'].update(worker_state='Running',sending_enabled=True,auto_reply_enabled=True,continuous=True)
    monkeypatch.setattr(service,'settings',SimpleNamespace(WECHAT_MAA_REPLY_ENABLED=False))
    with pytest.raises(BizError): service.validate_telemetry(body,now=1000)
    monkeypatch.setattr(service,'settings',SimpleNamespace(WECHAT_MAA_REPLY_ENABLED=True))
    service.store_telemetry(body)
    assert service.overview()['sending_enabled'] is True
    monkeypatch.setattr(service.time,'time',lambda:1200.0)
    assert service.overview()['sending_enabled'] is False


@pytest.mark.parametrize('flag', ['sending_enabled','auto_reply_enabled','continuous'])
def test_maa_runtime_flags_must_be_boolean(flag):
    body=observation();body['runtime'][flag]='true'
    with pytest.raises(BizError): service.validate_telemetry(body,now=1000)


def test_auto_reply_command_requires_explicit_boolean_and_server_feature(monkeypatch):
    monkeypatch.setattr(service,'settings',SimpleNamespace(WECHAT_MAA_REPLY_ENABLED=False))
    with pytest.raises(BizError): service.validate_command(new_command('start_watch',auto_reply=True,continuous=True))
    monkeypatch.setattr(service,'settings',SimpleNamespace(WECHAT_MAA_REPLY_ENABLED=True))
    good=service.validate_command(new_command('start_watch',auto_reply=True,continuous=True))
    assert good['params']=={'auto_reply':True,'continuous':True}
    for params in ({'auto_reply':'true'},{'continuous':1}):
        with pytest.raises(BizError): service.validate_command(new_command('start_watch',**params))


def test_offline_uses_both_source_and_received_times(cache, monkeypatch):
    assert service.overview()["offline"]
    service.store_telemetry(observation())
    monkeypatch.setattr(service.time, "time", lambda: 1091)
    assert service.overview()["offline"]
    record = {"received_at": 1091, "telemetry": observation(collected_at=1000)}
    assert service.is_stale(record)
    assert not service.is_stale({"received_at": 1091, "telemetry": observation(collected_at=1090)})


def test_older_telemetry_does_not_replace_newer_snapshot(cache):
    service.store_telemetry(observation())
    with pytest.raises(BizError):
        service.store_telemetry(observation(collected_at=999))
    assert service.latest()["telemetry"]["collected_at"] == 1000


def test_contacts_choose_latest_verified_complete_scope_not_cumulative_rows(cache):
    scans = [{"id": "a" * 32, "scope": "personal", "status": "complete", "coverage_valid": True, "bottom_verified": True, "finished": 995},
             {"id": "b" * 32, "scope": "personal", "status": "error", "coverage_valid": False, "finished": 999},
             {"id": "c" * 32, "scope": "public", "status": "complete", "coverage_valid": True, "bottom_verified": True, "finished": 990}]
    contacts = [{"id": "old", "scope": "personal", "scan_id": "b" * 32, "ordinal": 1, "display_name": "Demo User", "list_name": "Demo User"},
                {"id": "p1", "scope": "personal", "scan_id": "a" * 32, "ordinal": 1, "display_name": "Demo User", "list_name": "Demo User"},
                {"id": "p2", "scope": "personal", "scan_id": "a" * 32, "ordinal": 2, "display_name": "李四", "list_name": "李四"},
                {"id": "pub", "scope": "public", "scan_id": "c" * 32, "ordinal": 1, "display_name": "Demo User", "list_name": "Demo User"}]
    service.store_telemetry(observation(scans=scans, contacts=contacts))
    assert service.contacts("personal", "demo", 1, 100)["total"] == 1
    assert service.contacts("personal", None, 2, 1)["items"][0]["id"] == "p2"
    assert service.contacts("public", None, 1, 100)["items"][0]["id"] == "pub"
    assert service.contacts(None, None, 1, 100)["total"] == 3


def test_conversation_metadata_omits_payload_but_detail_preserves_ocr_evidence(cache):
    row = {"id": "a" * 64, "title": "Demo User", "identity_status": "visual_provisional",
           "latest_snapshot": {"id": 160, "status": "append", "seen": 990,
                               "frame_asset": "b" * 64, "payloads": [{"text": "MAA0926A", "sender": "unresolved"}]},
           "messages": [{"id": "m1", "payload": {"text": "MAA0926A"}}]}
    service.store_telemetry(observation(conversations=[row]))
    meta = service.conversations()["items"][0]
    assert "messages" not in meta and "payloads" not in meta["latest_snapshot"]
    detail = service.conversation_detail("a" * 64)
    assert detail["latest_snapshot"]["payloads"][0]["sender"] == "unresolved"
    with pytest.raises(BizError):
        service.conversation_detail("../private")


@pytest.mark.parametrize("action,params", [
    ("send_message", {}), ("capture", {"node": "SendText"}), ("capture", {"shell": "calc.exe"}),
    ("scan_contacts", {"scope": "group"}), ("scan_contacts", {"scan_id": "../db"}),
    ("start_watch", {"cycles": 0}), ("start_watch", {"cycles": 1001}), ("start_watch", {"cycles": True}),
    ([], {}), ("scan_contacts", {"scope": []}),
    ("scan_contacts", {"reuse_scan": "../private"}),
    ("scan_contacts", {"scan_id": "a" * 32, "reuse_scan": "b" * 32}),
    ("start_watch", {"reuse_scan": "a" * 32}), ("capture", {"scan_id": "a" * 32}),
])
def test_command_whitelist_rejects_unknown_or_unsafe_params(action, params):
    with pytest.raises(BizError):
        service.validate_command(new_command(action, **params))


def test_commands_idempotent_and_atomic_one_shot_issued(cache):
    service.store_telemetry(observation())
    body = new_command("capture")
    row, created = service.enqueue(body, 1)
    assert created and row["status"] == "queued" and row["run_id"].startswith("maa-web-")
    same, created = service.enqueue(body, 1)
    assert same["id"] == row["id"] and not created
    issued = service.next_command()
    assert issued["id"] == row["id"] and issued["status"] == "issued"
    assert service.next_command() is None
    assert service.record_result(row["id"], {"status": "succeeded", "detail": {"started": True}})["status"] == "succeeded"
    assert service.record_result(row["id"], {"status": "succeeded", "detail": {"started": True}})["status"] == "succeeded"
    assert service.enqueue(body, 1)[0]["status"] == "succeeded"
    with pytest.raises(BizError):
        service.record_result(row["id"], {"status": "failed"})
    altered = {**body, "action": "checkpoint"}
    with pytest.raises(BizError):
        service.enqueue(altered, 1)


def test_queued_result_invalid_expired_command_never_issued(cache, monkeypatch):
    service.store_telemetry(observation())
    row, _ = service.enqueue(new_command(), 1)
    with pytest.raises(BizError):
        service.record_result(row["id"], {"status": "succeeded"})
    monkeypatch.setattr(service.time, "time", lambda: 1091)
    assert service.next_command() is None
    assert service.command_history()[0]["status"] == "expired"
    with pytest.raises(BizError):
        service.record_result(row["id"], {"status": "succeeded"})


def test_pending_queued_and_issued_commands_block_parallel_actions(cache):
    service.store_telemetry(observation())
    row, _ = service.enqueue(new_command(), 1)
    with pytest.raises(BizError):
        service.enqueue(new_command("checkpoint"), 1)
    service.next_command()
    with pytest.raises(BizError):
        service.enqueue(new_command("checkpoint"), 1)
    service.record_result(row["id"], {"status": "failed"})
    assert service.enqueue(new_command("checkpoint"), 1)[1]


def test_popped_but_not_marked_issued_still_blocks_parallel_enqueue(cache):
    service.store_telemetry(observation())
    row, _ = service.enqueue(new_command(), 1)
    # LPOP and marking issued are separate transactions. No second command may
    # sneak into this interval, even if another request observes an empty queue.
    assert cache.lpop(service.PREFIX + "queue") == row["id"]
    with pytest.raises(BizError):
        service.enqueue(new_command("open_recent"), 1)


def test_busy_worker_only_allows_stop_or_checkpoint(cache):
    body = observation()
    body["runtime"]["worker_state"] = "Running"
    service.store_telemetry(body)
    with pytest.raises(BizError):
        service.enqueue(new_command("open_recent"), 1)
    stop, _ = service.enqueue(new_command("stop_worker"), 1)
    assert stop["action"] == "stop_worker"


def test_reuse_verified_same_scope_scan_is_valid_and_idempotent(cache):
    row = {"id": "a" * 32, "scope": "enterprise", "status": "complete",
           "coverage_valid": True, "bottom_verified": True, "position_count": 16, "finished": 990}
    service.store_telemetry(observation(scans=[row]))
    body = new_command("scan_contacts", scope="enterprise", reuse_scan=row["id"])
    command, created = service.enqueue(body, 1)
    assert created and command["params"] == {"scope": "enterprise", "reuse_scan": row["id"]}
    assert service.enqueue(body, 1) == (command, False)


@pytest.mark.parametrize("updates", [
    {"scope": "public"}, {"status": "incomplete"}, {"coverage_valid": False},
    {"bottom_verified": False}, {"id": "b" * 32},
])
def test_reuse_rejects_wrong_scope_incomplete_unverified_or_unknown_scan(cache, updates):
    row = {"id": "a" * 32, "scope": "personal", "status": "complete",
           "coverage_valid": True, "bottom_verified": True, "position_count": 396, "finished": 990}
    row.update(updates)
    service.store_telemetry(observation(scans=[row]))
    with pytest.raises(BizError):
        service.enqueue(new_command("scan_contacts", scope="personal", reuse_scan="a" * 32), 1)
    assert cache.lists.get(service.PREFIX + "queue", []) == []


def test_resume_latest_incomplete_scan_with_positions_is_valid(cache):
    older = {"id": "b" * 32, "scope": "personal", "status": "complete",
             "coverage_valid": True, "bottom_verified": True, "position_count": 396, "finished": 980}
    current = {"id": "a" * 32, "scope": "personal", "status": "incomplete",
               "coverage_valid": False, "bottom_verified": False, "position_count": 245, "started": 990}
    service.store_telemetry(observation(scans=[current, older]))
    command, created = service.enqueue(new_command("scan_contacts", scope="personal", scan_id=current["id"]), 1)
    assert created and command["params"]["scan_id"] == current["id"]


@pytest.mark.parametrize("updates", [
    {"scope": "public"}, {"status": "complete"}, {"position_count": 0}, {"id": "b" * 32},
])
def test_resume_requires_same_scope_current_incomplete_nonempty_scan(cache, updates):
    row = {"id": "a" * 32, "scope": "personal", "status": "incomplete", "position_count": 245, "started": 990}
    row.update(updates)
    service.store_telemetry(observation(scans=[row]))
    with pytest.raises(BizError):
        service.enqueue(new_command("scan_contacts", scope="personal", scan_id="a" * 32), 1)


def test_resume_cannot_skip_newer_complete_scan_to_old_incomplete(cache):
    old = {"id": "a" * 32, "scope": "personal", "status": "incomplete", "position_count": 245, "started": 980}
    latest = {"id": "b" * 32, "scope": "personal", "status": "complete", "position_count": 396,
              "coverage_valid": True, "bottom_verified": True, "started": 990, "finished": 995}
    service.store_telemetry(observation(scans=[old, latest]))
    with pytest.raises(BizError):
        service.enqueue(new_command("scan_contacts", scope="personal", scan_id=old["id"]), 1)


def test_resume_and_reuse_rejected_when_no_matching_evidence(cache):
    service.store_telemetry(observation())
    for key in ("scan_id", "reuse_scan"):
        with pytest.raises(BizError):
            service.enqueue(new_command("scan_contacts", **{key: "a" * 32}), 1)
    # A fresh scan still needs no existing evidence.
    assert service.enqueue(new_command("scan_contacts", scope="personal"), 1)[1]


def png_payload(size=(24, 24)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (20, 80, 160)).save(buffer, "PNG")
    data = buffer.getvalue()
    return data, {"sha256": hashlib.sha256(data).hexdigest(), "png_base64": base64.b64encode(data).decode()}


class MemoryStorage:
    def __init__(self):
        self.values = {}

    def put_object(self, **kw):
        assert kw["Bucket"] == service.ASSET_BUCKET
        self.values[kw["Key"]] = kw["Body"]

    def head_bucket(self, **kw):
        assert kw["Bucket"] == service.ASSET_BUCKET

    def get_object(self, **kw):
        assert kw["Bucket"] == service.ASSET_BUCKET
        return {"Body": io.BytesIO(self.values[kw["Key"]])}


def test_png_byte_hash_and_size_validated_stored_only_private_prefix(monkeypatch):
    storage = MemoryStorage()
    monkeypatch.setattr(service, "get_client", lambda: storage)
    data, item = png_payload()
    assert service.upload_assets({"items": [item]})["stored"] == [item["sha256"]]
    assert set(storage.values) == {f"wechat-maa/{item['sha256']}.png"}
    assert service.asset_bytes(item["sha256"]) == data
    for unsafe in ({**item, "sha256": "a" * 64}, {**item, "sha256": "../secret"},
                   {**item, "png_base64": "not-base64"}, png_payload((1921, 24))[1]):
        with pytest.raises(BizError):
            service.upload_assets({"items": [unsafe]})
    with pytest.raises(BizError):
        service.asset_bytes("../keys")
    assert len(storage.values) == 1





@pytest.mark.parametrize('value',[None,0,1,'true'])
def test_polling_switch_requires_actual_boolean(value):
    with pytest.raises(BizError):service.validate_command(new_command('set_polling',enabled=value))

def test_polling_switch_is_allowed_while_worker_owns_desktop(cache):
    body=observation();body['runtime']['worker_state']='Running'
    service.store_telemetry(body)
    command,_=service.enqueue(new_command('set_polling',enabled=False),1)
    assert command['params']=={'enabled':False}





