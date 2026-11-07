"""Outgoing HTTPS telemetry + a bounded, at-most-once Maa command consumer.

Runs as a separate background scheduled task, not in the RDP GUI worker. No
arbitrary code, coordinates, filenames or shell strings come from the server.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from admin_export import Assets, export, read_json

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'private' / 'admin-agent.json'
STATE = ROOT / 'private' / 'admin-agent-state.json'
MAX_RESULT_BYTES = 64 * 1024
CONTROL_INTERVAL_SECONDS = 1
TELEMETRY_INTERVAL_SECONDS = 15
MAX_ASSET_BATCHES_PER_SYNC = 1


def result_receipt(status, detail):
    """Keep full diagnostics locally; the API receives a bounded receipt.

    Sending state and the evidence used by the public API are never inferred
    from a size limit. The same command retains its original outcome.
    """
    import hashlib
    def encoded(value):
        return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    body={'status':status,'detail':detail}
    raw=encoded(body)
    if len(raw)<=MAX_RESULT_BYTES:return body
    summary={k:detail[k] for k in ('status','reason','error','conversation_id','event_id',
        'evidence','profile_proof','context_refresh_failed') if k in detail}
    diagnostic=detail.get('diagnostic') or {}
    summary['diagnostic']={k:diagnostic[k] for k in ('send_click_attempted','draft_retained',
        'draft_prepared','phase') if k in diagnostic}
    summary['diagnostic_archive']={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),
        'source':'local_command_result','full_detail_retained':True}
    body={'status':status,'detail':summary}
    if len(encoded(body))>MAX_RESULT_BYTES:
        raise ValueError('COMMAND_RESULT_METADATA_EXCEEDS_LIMIT')
    return body


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=True), encoding='utf-8')
    temporary.replace(path)


def powershell(script=None, arguments=(), command=None, timeout=12):
    argv = ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass']
    if command is not None:
        argv += ['-Command', command]
    else:
        argv += ['-File', str(ROOT / 'tools' / script), *arguments]
    result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise RuntimeError('LOCAL_LAUNCHER_FAILED:' + (result.stderr or result.stdout)[-1200:])
    return result.stdout.strip()


def runtime_state():
    raw = powershell(command="""[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();
    $worker=Get-ScheduledTask WeChatMaaWorker -ErrorAction SilentlyContinue;
    $trial=Get-ScheduledTask CodexMaaTrial -ErrorAction SilentlyContinue;
    $old=Get-ScheduledTask JinjieWeChatGuiGateway -ErrorAction SilentlyContinue;
    $info=if($worker){Get-ScheduledTaskInfo WeChatMaaWorker};
    @{worker_state=if($worker){$worker.State.ToString()}else{'Missing'};
      trial_state=if($trial){$trial.State.ToString()}else{'Missing'};
      old_gateway_state=if($old){$old.State.ToString()}else{'Missing'};
      worker_arguments=if($worker){$worker.Actions.Arguments}else{''};
      last_result=if($info){$info.LastTaskResult}else{$null}}|ConvertTo-Json -Compress""")
    value = json.loads(raw)
    arguments=value.pop('worker_arguments','')
    match = re.search(r'--id\s+([a-z0-9_-]+)',arguments)
    value['run_id'] = match.group(1) if match else None
    value['auto_reply_enabled']=value['worker_state']=='Running' and '--auto-reply' in arguments
    value['sending_enabled']=value['auto_reply_enabled']
    value['continuous']='--continuous' in arguments
    value['layout'] = {'screen': [1920, 1080], 'client': [1920, 1040], 'divider_x': 480,
                       'dpi': 96, 'kind': 'expected_guard_baseline', 'capture': 'PrintWindow', 'input': 'PostMessage'}
    from importlib.metadata import version
    value['versions'] = {'maafw': version('maafw'), 'python': sys.version.split()[0]}
    return value


def execute(command):
    """Commands are IDs/enums only; GUI mutual exclusion is rechecked locally."""
    if command.get('expires_at', 0) < time.time():
        raise ValueError('COMMAND_EXPIRED')
    if not re.fullmatch(r'[a-f0-9]{32}|[a-f0-9-]{36}', command.get('id', '')):
        raise ValueError('INVALID_COMMAND_ID')
    run_id = command.get('run_id', '')
    if not re.fullmatch(r'maa-web-[a-z0-9_-]{1,50}', run_id):
        raise ValueError('INVALID_RUN_ID')
    action = command.get('action')
    params = command.get('params') or {}
    allowed = {'set_polling': {'enabled'}, 'set_group_whitelist': {'conversation_ids','expected_revision'}, 'start_watch': {'cycles','auto_reply','continuous'}, 'scan_contacts': {'scope', 'scan_id', 'reuse_scan'},
               'send_message': {'conversation_id','text'}, 'send_contact_message': {'contact_id','wechat_id','text'}, 'open_conversation': {'conversation_id'}, 'open_contact': {'contact_id'}}
    if set(params) - allowed.get(action, set()):
        raise ValueError('UNSUPPORTED_COMMAND_PARAMETERS')
    scope = params.get('scope', 'personal')
    if scope not in ('personal', 'enterprise', 'public'):
        raise ValueError('INVALID_SCOPE')
    if params.get('scan_id') and params.get('reuse_scan'):
        raise ValueError('RESUME_AND_REUSE_ARE_EXCLUSIVE')
    for key in ('scan_id', 'reuse_scan'):
        if key in params and (not isinstance(params[key], str) or not re.fullmatch(r'[a-f0-9]{32}', params[key])):
            raise ValueError('INVALID_' + key.upper())
    state = runtime_state()
    if state['old_gateway_state'] not in ('Disabled', 'Missing'):
        raise ValueError('OLD_GATEWAY_NOT_DISABLED')
    if action=='set_polling':
        from watch_policy import set_polling
        set_polling(ROOT,params.get('enabled'))
        return {'polling_enabled':params['enabled']}
    if action=='set_group_whitelist':
        from group_policy import apply_configuration
        return apply_configuration(ROOT,params)
    if action in ('send_message','send_contact_message','open_conversation','open_contact'):
        if state['worker_state']!='Running' or state['trial_state']=='Running': raise ValueError('MANUAL_WORKER_NOT_AVAILABLE')
        from manual_gui import validate_intent
        from store import Store
        validate_intent(command)
        store=Store(ROOT/'data'/'wechat.sqlite3')
        try: store.queue_gui(command)
        finally: store.db.close()
        return {'deferred_gui':True}
    if action not in ('stop_worker', 'checkpoint') and (state['worker_state'] == 'Running' or state['trial_state'] == 'Running'):
        raise ValueError('DESKTOP_WORKER_BUSY')
    if action == 'stop_worker':
        powershell('stop_maa_worker.ps1')
        if runtime_state()['worker_state'] == 'Running':
            raise ValueError('STOP_NOT_VERIFIED')
        return {'worker_state': 'stopped', 'sending_enabled': False}
    if action in ('start_watch', 'scan_contacts'):
        cycles = params.get('cycles', 1000)
        if type(cycles) is not int or not 1 <= cycles <= 1000:
            raise ValueError('INVALID_CYCLES')
        argv = ['-Mode', 'watch' if action == 'start_watch' else 'contacts', '-RunId', run_id,
                '-MaxPages', '100', '-Cycles', str(cycles), '-Scope', scope]
        for field,flag in (('auto_reply','-AutoReply'),('continuous','-Continuous')):
            if field in params and type(params[field]) is not bool: raise ValueError('INVALID_'+field.upper())
            if params.get(field): argv.append(flag)
        scan = params.get('scan_id')
        if scan:
            if action != 'scan_contacts' or not re.fullmatch(r'[a-f0-9]{32}', scan):
                raise ValueError('INVALID_RESUME_SCAN')
            argv += ['-ResumeScan', scan]
        if params.get('reuse_scan'):
            argv += ['-ReuseScan', params['reuse_scan']]
        if action=='start_watch':
            from watch_policy import set_polling
            set_polling(ROOT,True)  # explicit human start in the admin page
        powershell('start_maa_worker.ps1', argv)
        return {'accepted': True, 'run_id': run_id, 'worker_state': runtime_state()['worker_state'],
                'execution_time_limit_seconds': 0 if params.get('continuous') else 7200,
                'auto_reply_enabled':params.get('auto_reply',False),'note': '启动成功不代表采集或发送验收完成'}
    if action in ('capture', 'open_contacts', 'open_recent', 'friend_count'):
        mode = {'capture': 'capture', 'open_contacts': 'node', 'open_recent': 'node', 'friend_count': 'friend-count'}[action]
        argv = ['-Mode', mode, '-ProbeId', run_id]
        if action in ('open_contacts', 'open_recent'):
            argv += ['-Node', 'OpenContacts' if action == 'open_contacts' else 'OpenRecent']
        powershell('run_maa_trial.ps1', argv, timeout=55)
        result = read_json(ROOT / 'data' / (run_id + '.json'))
        if result.get('error'):
            raise ValueError(result['error'])
        return {'run_id': run_id, 'friend_count': result.get('friend_count', {}).get('count'), 'verified': True}
    if action == 'checkpoint':
        result = subprocess.run([sys.executable, str(ROOT / 'checkpoint.py'), '--id', run_id],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError('CHECKPOINT_FAILED')
        evidence = json.loads(result.stdout)
        name = Path(evidence['path']).name
        records = read_json(ROOT / 'data' / 'admin-backups.json', [])
        item = {'name': name, 'sha256': evidence['sha256'], 'bytes': (ROOT / 'data' / name).stat().st_size,
                'kind': 'sqlite_consistent_checkpoint', 'integrity': evidence['integrity'], 'checked_at': time.time()}
        atomic_json(ROOT / 'data' / 'admin-backups.json', (records + [item])[-20:])
        return item
    raise ValueError('UNSUPPORTED_COMMAND')


class Agent:
    def __init__(self):
        self.config = read_json(CONFIG)
        endpoint = self.config.get('endpoint', '')
        url = urlsplit(endpoint)
        from endpoint_config import valid_endpoint
        if not valid_endpoint(endpoint, self.config.get('trusted_host')):
            raise ValueError('INVALID_TRUSTED_ENDPOINT')
        token = self.config.get('token', '')
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError('MISSING_MACHINE_TOKEN')
        self.endpoint = endpoint
        self.token = token
        self.state = read_json(STATE, {'uploaded': [], 'commands': {}})
        self.state_lock = threading.RLock()
        self.assets = Assets(ROOT)
        self.sync_status = {}

    def api(self, path, body=None, method='GET'):
        raw = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode() if body is not None else None
        request = urllib.request.Request(self.endpoint + path, data=raw, method=method,
                                         headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read()).get('data')

    def save(self):
        with self.state_lock:
            atomic_json(STATE, self.state)

    def sync(self):
        began = time.time()
        def phase(name):
            self.sync_status = {'phase': name, 'started_at': began, 'updated_at': time.time()}
        phase('export')
        runtime = runtime_state()
        with self.state_lock:
            commands = [(cid, dict(item)) for cid, item in self.state['commands'].items()]
            uploaded = set(self.state['uploaded'])
        runtime['result_delivery_warnings'] = [
            {'command_id': cid, 'error': item['delivery_error']}
            for cid, item in commands if item.get('delivery_error')][-8:]
        report, images = export(ROOT, runtime, self.assets)
        # Add the most recent successful web capture as evidence without touching GUI.
        done = [item for _cid, item in commands
                if item.get('action') == 'capture' and item.get('status') == 'succeeded']
        if done:
            last = max(done, key=lambda item: item.get('completed_at', 0))
            report['runtime']['capture_asset'] = self.assets.image(ROOT / 'data' / (last['run_id'] + '-after.png'))
        # A busy picture store must not suppress live event/status telemetry.
        # Hide not-yet-uploaded asset references so the page does not cache 404s.
        def available_assets(value):
            if isinstance(value,dict):
                return {key:(child if child in uploaded else None) if key.endswith('_asset') and isinstance(child,str)
                        else available_assets(child) for key,child in value.items()}
            if isinstance(value,list): return [available_assets(child) for child in value]
            return value
        metadata = available_assets(report)
        metadata['runtime']['asset_upload_pending'] = any(sha not in uploaded for sha in images)
        phase('metadata')
        self.api('/telemetry', metadata, 'PUT')
        batch = []
        byte_count = 0
        batches = 0
        for sha, raw in images.items():
            if sha in uploaded:
                continue
            if batch and (byte_count + len(raw) > 4 * 1024 * 1024 or len(batch) >= 40):
                phase('assets')
                self.api('/assets', {'items': batch}, 'POST')
                uploaded.update(item['sha256'] for item in batch)
                batch, byte_count = [], 0
                batches += 1
                if batches >= MAX_ASSET_BATCHES_PER_SYNC:
                    break
            batch.append({'sha256': sha, 'png_base64': base64.b64encode(raw).decode()})
            byte_count += len(raw)
        if batch:
            phase('assets')
            self.api('/assets', {'items': batch}, 'POST')
            uploaded.update(item['sha256'] for item in batch)
        with self.state_lock:
            self.state['uploaded'] = sorted(set(self.state['uploaded']) | uploaded)
            self.save()
        # collected_at refers to data read time, not the upload completion time.
        final = available_assets(report)
        final['runtime']['asset_upload_pending'] = any(sha not in uploaded for sha in images)
        phase('metadata_final')
        self.api('/telemetry', final, 'PUT')
        phase('complete')

    def deliver_results(self):
        for cid, result in self.state['commands'].items():
            if result.get('status')=='pending_gui':
                import sqlite3
                db=sqlite3.connect((ROOT/'data'/'wechat.sqlite3').as_uri()+'?mode=ro',uri=True)
                try: row=db.execute('SELECT status,detail_json FROM gui_commands WHERE id=?',(cid,)).fetchone()
                finally: db.close()
                if not row or row[0] in ('pending','executing'): continue
                with self.state_lock:
                    result.update(status=row[0],detail=json.loads(row[1]))
            if result.get('reported') or result.get('status') == 'executing':
                continue
            try:
                self.api('/commands/' + cid + '/result', result_receipt(result['status'], result['detail']), 'PUT')
            except urllib.error.HTTPError as exc:
                if exc.code not in (404, 409):
                    with self.state_lock:
                        result['delivery_error']='RESULT_HTTP_'+str(exc.code)
                        self.save()
                    raise
                # An expired or already-final command is never re-executed. Keep
                # the local result and surface the mismatch without wedging sync.
                with self.state_lock:
                    result['delivery_error'] = 'RESULT_EXPIRED_OR_CONFLICT_HTTP_' + str(exc.code)
            else:
                with self.state_lock:
                    result.pop('delivery_error',None)
            with self.state_lock:
                result['reported'] = True
                self.save()

    def command(self):
        command = self.api('/commands/next')
        if not command:
            return
        if isinstance(command, dict) and 'command' in command:
            command = command['command']
        if not command:
            return
        cid = command['id']
        with self.state_lock:
            if cid in self.state['commands']:
                return  # uncertain responses must never repeat GUI execution
            self.state['commands'][cid] = {'status': 'executing', 'action': command['action'], 'run_id': command['run_id'], 'reported': False}
            self.save()  # at-most-once execution, journal before any action
        try:
            detail = execute(command)
            status = 'pending_gui' if detail.get('deferred_gui') else 'succeeded'
        except Exception as exc:
            status, detail = 'failed', {'error': str(exc)[:1500]}
        with self.state_lock:
            self.state['commands'][cid].update(status=status, detail=detail, completed_at=time.time())
            self.save()
        self.deliver_results()

    def run(self, once=False, *, stop_event=None):
        # A crash after journaling can be uncertain; never auto-repeat the action.
        for item in self.state['commands'].values():
            if item.get('status') == 'executing':
                item.update(status='failed', detail={'error': 'INTERRUPTED_EXECUTION_VERIFY_DESKTOP_BEFORE_RETRY'})
        self.save()
        if not once:
            return self.run_continuous(stop_event or threading.Event())
        while True:
            started = time.monotonic()
            delivery_error = None
            try:
                self.deliver_results()
            except Exception as exc:
                error='HTTP_'+str(exc.code) if isinstance(exc,urllib.error.HTTPError) else type(exc).__name__
                delivery_error = 'RESULT_DELIVERY_' + error
            try:
                if delivery_error is None:
                    self.command()
                # The local GUI owner rechecks task/identity/expiry. Picture
                # storage failures must not starve already-authorized intents.
                self.sync()
                atomic_json(ROOT / 'data' / 'admin-agent-health.json',
                            {'status': 'warning' if delivery_error else 'ok',
                             'checked_at': time.time(), 'result_delivery_error': delivery_error})
            except Exception as exc:
                # Credentials, message payloads and headers never enter logs.
                error = 'HTTP_' + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
                atomic_json(ROOT / 'data' / 'admin-agent-health.json', {'status': 'error', 'checked_at': time.time(), 'error': error})
                if once:
                    raise RuntimeError(error) from None
            if once:
                return
            time.sleep(max(1, 15 - (time.monotonic() - started)))

    def run_continuous(self, stop_event):
        """控制面不等待遥测导出或图片上传；后台线程没有 GUI 执行权限。"""
        future = None
        next_sync = 0
        sync_error = None
        last_sync_completed = None
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='maa-telemetry') as executor:
            while not stop_event.is_set():
                started = time.monotonic()
                delivery_error = None
                control_error = None
                if future is not None and future.done():
                    try:
                        future.result()
                        sync_error = None
                        last_sync_completed = time.time()
                    except Exception as exc:
                        sync_error = 'HTTP_' + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
                    future = None
                if future is None and started >= next_sync:
                    future = executor.submit(self.sync)
                    next_sync = started + TELEMETRY_INTERVAL_SECONDS
                try:
                    self.deliver_results()
                except Exception as exc:
                    error = 'HTTP_' + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
                    delivery_error = 'RESULT_DELIVERY_' + error
                try:
                    if delivery_error is None:
                        self.command()
                except Exception as exc:
                    control_error = 'HTTP_' + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
                atomic_json(ROOT / 'data' / 'admin-agent-health.json', {
                    'status': 'warning' if delivery_error or control_error or sync_error else 'ok',
                    'checked_at': time.time(), 'result_delivery_error': delivery_error,
                    'control_error': control_error, 'sync_error': sync_error,
                    'sync_in_flight': future is not None,
                    'last_sync_completed': last_sync_completed,
                    'sync_stage': self.sync_status,
                    'control_interval_seconds': CONTROL_INTERVAL_SECONDS,
                    'telemetry_interval_seconds': TELEMETRY_INTERVAL_SECONDS})
                stop_event.wait(max(0, CONTROL_INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--sync-only', action='store_true', help='one read-only data upload; no commands or persistent task')
    args = parser.parse_args()
    if args.sync_only:
        Agent().sync()
    else:
        Agent().run(args.once)
