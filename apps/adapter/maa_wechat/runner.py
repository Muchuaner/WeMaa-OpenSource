"""Independent MaaFramework runtime; no legacy gateway or UIA execution."""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
from dataclasses import asdict, is_dataclass
from importlib.metadata import version
import json
from pathlib import Path
import time
import traceback
import re

from PIL import Image
from maa.controller import Win32Controller, MaaWin32ScreencapMethodEnum as Capture, MaaWin32InputMethodEnum as Input
from maa.toolkit import Toolkit
from maa.resource import Resource
from maa.tasker import Tasker

import liveness

ROOT = Path(__file__).resolve().parent


def plain(value):
    if hasattr(value, 'node_id_list') and hasattr(value, 'nodes'):
        return {'task_id': value.task_id, 'entry': value.entry, 'nodes': plain(value.nodes), 'status': 'succeeded' if value.status.succeeded else 'failed'}
    if is_dataclass(value): return {k: plain(v) for k, v in asdict(value).items()}
    if isinstance(value, dict): return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)): return value
    return str(value)


class Runtime:
    def __init__(self, input_method='PostMessage', *, restore_window=False):
        (ROOT / 'data').mkdir(exist_ok=True)
        Toolkit.init_option(str(ROOT / 'data'))
        from window_visibility import ensure_visible
        self.restore_window = restore_window
        if restore_window:
            from storage_dialog import recover_storage_dialog
            self.record_window_recovery(recover_storage_dialog())
        visibility = ensure_visible(restore_hidden=restore_window, restore_geometry=restore_window)
        self.verified_window = visibility['window']
        if visibility['restored']:
            with (ROOT/'data'/'window-visibility.jsonl').open('a', encoding='utf-8') as output:
                output.write(json.dumps(dict(visibility, time=time.time()), ensure_ascii=False)+'\n')
        windows = Toolkit.find_desktop_windows()
        self.windows = [plain(w) for w in windows]
        user32 = ctypes.windll.user32
        matches = []
        for w in windows:
            rect = wintypes.RECT()
            user32.GetClientRect(w.hwnd, ctypes.byref(rect))
            if w.hwnd == visibility['window']['hwnd'] and rect.right == 1920 and rect.bottom == 1040:
                matches.append(w)
        if len(matches) != 1: raise RuntimeError(f'WECHAT_WINDOW_NOT_UNIQUE:{len(matches)}')
        self.hwnd = matches[0].hwnd
        self.controller = Win32Controller(self.hwnd, Capture.PrintWindow, getattr(Input, input_method), Input.PostMessage)
        self.controller.set_screenshot_use_raw_size(True)
        if not self.controller.post_connection().wait().succeeded: raise RuntimeError('MAA_CONNECTION_FAILED')
        self.resource = Resource()
        from list_rows import ListRows,VerifyRecentRow
        self.resource.register_custom_recognition('ListRows',ListRows())
        self.resource.register_custom_recognition('VerifyRecentRow',VerifyRecentRow())
        from categories import ContactCategory
        self.resource.register_custom_recognition('ContactCategory',ContactCategory())
        self.pipeline=json.loads((ROOT/'resource'/'pipeline'/'navigation.json').read_text(encoding='utf-8'))
        if not self.resource.post_bundle(str(ROOT / 'resource')).wait().succeeded: raise RuntimeError('RESOURCE_LOAD_FAILED')
        self.tasker = Tasker()
        self.tasker.bind(self.resource, self.controller)
        if not self.tasker.inited: raise RuntimeError('MAA_TASKER_INIT_FAILED')

    def geometry_ok(self):
        rect = wintypes.RECT()
        return bool(ctypes.windll.user32.GetClientRect(self.hwnd,ctypes.byref(rect))
                    and (rect.right,rect.bottom)==(1920,1040)
                    and ctypes.windll.user32.IsWindowVisible(self.hwnd)
                    and ctypes.windll.user32.IsWindowEnabled(self.hwnd)
                    and not ctypes.windll.user32.IsIconic(self.hwnd))

    def record_window_recovery(self, result):
        if result and result.get('restored'):
            with (ROOT/'data'/'window-visibility.jsonl').open('a', encoding='utf-8') as output:
                output.write(json.dumps(dict(result, time=time.time()), ensure_ascii=False)+'\n')

    def prepare_input(self):
        """Repair a verified modal/geometry only before a new round of GUI work."""
        if not self.restore_window or self.geometry_ok():
            return None
        from storage_dialog import recover_storage_dialog
        modal = recover_storage_dialog(expected_window=self.verified_window)
        self.record_window_recovery(modal)
        from window_visibility import ensure_visible
        result = ensure_visible(restore_hidden=True, restore_geometry=True,
                                expected_window=self.verified_window)
        if result['restored']:
            with (ROOT/'data'/'window-visibility.jsonl').open('a', encoding='utf-8') as output:
                output.write(json.dumps(dict(result, time=time.time()), ensure_ascii=False)+'\n')
        return modal if modal.get('restored') else result

    def capture_image(self):
        from capture_guard import capture_with_retry
        liveness.touch()
        return capture_with_retry(lambda:self.controller.post_screencap().wait().get(),self.geometry_ok)

    def capture(self, label):
        image = self.capture_image()
        path = ROOT / 'data' / (label + '.png')
        Image.fromarray(image[:, :, :3][:, :, ::-1]).save(path)
        return image, str(path)

    def node(self, name, overrides=None, allow_miss=False):
        liveness.touch()
        if self.pipeline.get(name,{}).get('action') not in (None,'DoNothing'):
            from desktop_input import desktop_probe
            if desktop_probe().get('available') is not True:
                raise RuntimeError('INPUT_DESKTOP_UNAVAILABLE')
        from layout import check_frame
        guard_image=self.capture_image()
        try: check_frame(guard_image)
        except Exception:
            from layout import navigation_entry_safe
            if name=='OpenRecent' and navigation_entry_safe(guard_image,self.recognize_image('Read',guard_image,roi=[65,35,400,45])):
                pass
            else:
                Image.fromarray(guard_image[:,:,:3][:,:,::-1]).save(ROOT/'data'/f'layout-failure-{time.time_ns()}.png')
                raise
        started = time.monotonic()
        tasker = self.tasker
        if name in ('OpenMessageProfile','OpenOwnProfile','DismissProfile','OpenMediaPicker','OpenSentVideo','OpenGroupInviteCard','CopyMessageMenu'):
            pointer = Win32Controller(self.hwnd, Capture.PrintWindow, Input.PostMessageWithCursorPos, Input.PostMessage)
            pointer.set_screenshot_use_raw_size(True)
            if not pointer.post_connection().wait().succeeded: raise RuntimeError('POPUP_CONTROLLER_UNAVAILABLE')
            from layout import check_frame
            check_frame(pointer.post_screencap().wait().get())
            tasker = Tasker(); tasker.bind(self.resource,pointer)
        job = tasker.post_task(name, overrides or {}).wait()
        detail = plain(job.get())
        result = {'run_id':getattr(self,'run_id',None), 'time_unix':time.time(), 'node': name, 'succeeded': job.succeeded, 'elapsed_ms': round((time.monotonic()-started)*1000), 'detail': detail}
        with (ROOT/'data'/'actions.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(result, ensure_ascii=False)+'\n')
        if not job.succeeded and not allow_miss: raise RuntimeError(f'MAA_NODE_FAILED:{name}:{detail}')
        return result

    def read(self, roi):
        result = self.node(roi) if isinstance(roi,str) else self.node('Read', {'Read': {'roi': roi}})
        output = []
        for node in (result['detail'] or {}).get('nodes', []):
            recognition = node.get('recognition') or {}
            output.extend(recognition.get('all_results') or [])
        return output

    def rows(self, kind):
        result=self.node('ListRows',{'ListRows':{'custom_recognition_param':{'kind':kind}}},allow_miss=True)
        for node in (result['detail'] or {}).get('nodes',[]):
            for hit in (node.get('recognition') or {}).get('all_results',[]):
                detail=hit.get('detail') or {}
                if isinstance(detail,str): detail=json.loads(detail)
                if 'centers' in detail: return detail['centers']
        return []

    def recognize_image(self,name,image,kind=None,roi=None):
        from maa.pipeline import JOCR,JCustomRecognition,JColorMatch,JTemplateMatch
        liveness.touch()
        data=self.pipeline[name]
        algorithm=data['recognition']
        if algorithm=='OCR': params=JOCR(expected=data.get('expected',[]),roi=roi or data['roi'])
        elif algorithm=='TemplateMatch': params=JTemplateMatch(template=[data['template']],roi=roi or data['roi'],threshold=[data.get('threshold',.95)])
        elif algorithm=='Custom': params=JCustomRecognition(custom_recognition=data['custom_recognition'],custom_recognition_param={'kind':kind})
        elif algorithm=='ColorMatch': params=JColorMatch(lower=[data['lower']],upper=[data['upper']],roi=roi or data['roi'],method=data['method'],count=data['count'],connected=data.get('connected',False))
        else: raise ValueError('UNSUPPORTED_SNAPSHOT_RECOGNITION')
        job=self.tasker.post_recognition(algorithm,params,image).wait()
        detail=plain(job.get())
        output=[]
        for node in (detail or {}).get('nodes',[]):
            output.extend((node.get('recognition') or {}).get('filtered_results') or [])
        return output

    def friend_count(self,label):
        owner=wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(self.hwnd,ctypes.byref(owner))
        def candidates():
            result=[]
            for window in Toolkit.find_desktop_windows():
                pid=wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(window.hwnd,ctypes.byref(pid))
                if window.window_name=='通讯录管理' and pid.value==owner.value: result.append(window)
            return result
        windows=candidates()
        if not windows:
            self.node('OpenContactManager')
            windows=candidates()
        if len(windows)!=1: raise RuntimeError('MANAGER_WINDOW_NOT_UNIQUE')
        window=windows[0]
        controller=Win32Controller(window.hwnd,Capture.PrintWindow,Input.PostMessage,Input.PostMessage)
        controller.set_screenshot_use_raw_size(True)
        if not controller.post_connection().wait().succeeded: raise RuntimeError('MANAGER_CONNECTION_FAILED')
        picture=controller.post_screencap().wait().get()
        if picture is None or picture.shape[:2]!=(560,720): raise RuntimeError('MANAGER_GEOMETRY_CHANGED')
        path=ROOT/'data'/(label+'-friend-manager.png')
        Image.fromarray(picture[:,:,:3][:,:,::-1]).save(path)
        tasker=Tasker()
        tasker.bind(self.resource,controller)
        try:
            job=tasker.post_task('ReadFriendCount').wait()
            detail=plain(job.get())
            texts=[]
            for node in detail['nodes']:
                texts.extend(i['text'] for i in (node.get('recognition') or {}).get('all_results',[]))
            match=re.search(r'全部\s*[（(]?\s*(\d+)', ' '.join(texts))
            if not job.succeeded or not match: raise RuntimeError('FRIEND_MANAGER_COUNT_NOT_READABLE')
            result={'count':int(match.group(1)),'source':'Maa OCR: contact manager All','image':str(path),'detail':detail}
        finally:
            closed=tasker.post_task('CloseContactManager').wait()
            if not closed.succeeded or ctypes.windll.user32.IsWindowVisible(window.hwnd): raise RuntimeError('MANAGER_CLOSE_NOT_VERIFIED')
        (ROOT/'data'/(label+'-friend-count.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['capture', 'node', 'read', 'contacts', 'watch','inspect','friend-count','send-self-test','cleanup-self-test'])
    parser.add_argument('--id', required=True)
    parser.add_argument('--node', default='OpenContacts')
    parser.add_argument('--roi', default='0,0,1920,1040')
    parser.add_argument('--input-method', default='PostMessage')
    parser.add_argument('--max-pages', type=int, default=100)
    parser.add_argument('--cycles', type=int, default=1)
    parser.add_argument('--auto-reply',action='store_true')
    parser.add_argument('--continuous',action='store_true')
    parser.add_argument('--reuse-scan',default=None)
    parser.add_argument('--resume-scan',default=None)
    parser.add_argument('--scope',choices=['personal','enterprise','public'],default='personal')
    args = parser.parse_args()
    if not args.id.replace('_','').replace('-','').isalnum(): raise ValueError('INVALID_ID')
    try:
        if args.mode == 'watch' and args.continuous:
            from watch_recovery import run_watch_with_recovery, ContinuousActivation
            from watch import watch
            from flows import progress
            liveness.start(ROOT, args.id)
            activation = ContinuousActivation()
            def factory():
                candidate = Runtime(args.input_method, restore_window=True)
                candidate.run_id = args.id
                return candidate
            def execute(runtime):
                _, before_path = runtime.capture(args.id+'-before')
                result = {'maafw': version('maafw'), 'windows': runtime.windows, 'before': before_path}
                result['watch'] = watch(runtime,ROOT,args.id,args.cycles,args.max_pages,args.auto_reply,True,
                                        activation=activation)
                _, result['after'] = runtime.capture(args.id+'-after')
                return result
            def notify(state):
                value = dict(state, mode='recent', run_id=args.id, continuous=True,
                             recovery_checked_at=time.time())
                progress(ROOT,value)
                with (ROOT/'data'/'window-recovery.jsonl').open('a',encoding='utf-8') as output:
                    output.write(json.dumps(value,ensure_ascii=False)+'\n')
            result = run_watch_with_recovery(factory,execute,notify)
        else:
            runtime = Runtime(args.input_method)
            before, before_path = runtime.capture(args.id+'-before')
            result = {'maafw': version('maafw'), 'windows': runtime.windows, 'before': before_path}
            runtime.run_id=args.id
            if args.mode=='inspect':
                owner=wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(runtime.hwnd,ctypes.byref(owner))
                result['owned_windows']=[]
                for window in Toolkit.find_desktop_windows():
                    pid=wintypes.DWORD()
                    ctypes.windll.user32.GetWindowThreadProcessId(window.hwnd,ctypes.byref(pid))
                    if window.hwnd==runtime.hwnd or pid.value!=owner.value: continue
                    controller=Win32Controller(window.hwnd,Capture.PrintWindow,Input.PostMessage,Input.PostMessage)
                    controller.set_screenshot_use_raw_size(True)
                    if not controller.post_connection().wait().succeeded: continue
                    picture=controller.post_screencap().wait().get()
                    if picture is None or picture.size==0: continue
                    path=ROOT/'data'/f'{args.id}-window-{window.hwnd}.png'
                    Image.fromarray(picture[:,:,:3][:,:,::-1]).save(path)
                    result['owned_windows'].append({'hwnd':window.hwnd,'title':window.window_name,'shape':list(picture.shape),'path':str(path)})
            if args.mode=='friend-count': result['friend_count']=runtime.friend_count(args.id)
            if args.mode=='cleanup-self-test':
                import numpy as np
                from send import _target,_draft,INPUT_ROI
                reference=ROOT/'data'/'maa-send-self-trial3-sep26-send-22a5df225fe34b2a-prepared.png'
                observed=np.asarray(Image.open(reference).convert('RGB'))[:,:,::-1].copy()
                current,_=runtime.capture(args.id+'-cleanup-before')
                request={'conversation_title':'文件传输助手','name':'文件传输助手','avatar_sha':'99e17f1842f45f9db9c0c8ebef84a88713004f42abbb88ad5941d116d5a72185'}
                if not _target(runtime,current,request)['verified']: raise ValueError('OWNED_DRAFT_TARGET_NOT_VERIFIED')
                x,y,w,h=INPUT_ROI
                difference=np.any(current[y:y+h,x:x+w,:3]!=observed[y:y+h,x:x+w,:3],axis=2)
                dy,dx=np.nonzero(difference)
                if len(dx) and not (len(dx)<=90 and dx.max()-dx.min()<=2 and dy.max()-dy.min()<=30):
                    raise ValueError('OWNED_DRAFT_PIXELS_CHANGED_PRESERVE')
                # Only this exact, locally created, unsent self-test draft is cleared.
                runtime.node('FocusChatInput');runtime.node('OwnedDraftEnd')
                runtime.node('OwnedDraftBackspace',{'OwnedDraftBackspace':{'repeat':100}})
                cleaned,_=runtime.capture(args.id+'-cleanup-after')
                result['cleanup']={'verified':_draft(runtime,cleaned)['verified'],'owned_test_only':True}
                if not result['cleanup']['verified']: raise ValueError('OWNED_DRAFT_CLEANUP_NOT_VERIFIED')
            if args.mode=='send-self-test':
                from watch import read_rows,select,read_title
                from flows import scroll_top
                from store import digest
                from send import send_verified
                runtime.node('OpenRecent');scroll_top(runtime,args.id)
                rows,_,_=read_rows(runtime,ROOT,args.id+'-recent')
                candidates=[r for r in rows if r['name']=='文件传输助手']
                if len(candidates)!=1: raise ValueError('SELF_TEST_TARGET_NOT_UNIQUE')
                row=candidates[0]
                if not select(runtime,row): raise ValueError('SELF_TEST_ROW_NOT_VERIFIED')
                image,_=runtime.capture(args.id+'-selected')
                title=read_title(runtime,image,row)
                if title!='文件传输助手': raise ValueError('SELF_TEST_HEADER_NOT_VERIFIED')
                result['send']=send_verified(runtime,ROOT,args.id,{'event_id':digest(['self-test',args.id]),
                     'name':row['name'],'conversation_title':title,'avatar_sha':row['avatar_sha'],'row_y':row['y'],
                     'reply_text':'中文发送核验第四次你好'})
            if args.mode == 'node': result['action'] = runtime.node(args.node)
            if args.mode == 'read': result['ocr'] = runtime.read([int(v) for v in args.roi.split(',')])
            if args.mode == 'contacts':
                from flows import contact_crawl
                result['contacts'] = contact_crawl(runtime,ROOT,args.id,args.max_pages,args.reuse_scan,args.scope,args.resume_scan)
            if args.mode == 'watch':
                from watch import watch
                result['watch'] = watch(runtime,ROOT,args.id,args.cycles,args.max_pages,args.auto_reply,args.continuous)
            _, result['after'] = runtime.capture(args.id+'-after')
    except Exception as exc:
        result = {'error': str(exc), 'traceback': traceback.format_exc()}
    (ROOT/'data').mkdir(exist_ok=True)
    (ROOT/'data'/(args.id+'.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if 'error' in result: return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
