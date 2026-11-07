import unittest
from watch import incoming_requests,sidebar_changes,seek_cold_page,send_ready_on_rows,ready_page_hint,advance_cold_bookmark,is_group_title,startup_warmup,filter_changed_wakes,selected_after_wake_filter,finish_read_hint,pending_visible_wakes
from unittest.mock import patch
import json
import numpy as np
from activity_scheduler import ActivityScheduler,DAY
from copy import deepcopy


class GuardedNavigationTests(unittest.TestCase):
    def runtime(self, selected):
        from unittest.mock import Mock
        r=Mock();r.run_id='guarded-nav-test';r.navigation_evidence_dir=None
        r.capture.return_value=(np.zeros((1040,1920,3),dtype=np.uint8),'frame.png')
        r.recognize_image.side_effect=selected;r.node.return_value={'succeeded':True}
        return r

    def test_delayed_selected_row_refresh_succeeds_without_a_second_click(self):
        from watch import select
        other=[{'box':[61,211,9,65]}];target=[{'box':[61,341,9,65]}]
        r=self.runtime([other,other,target])
        with patch('watch.read_title',return_value='Demo User'):
            self.assertTrue(select(r,{'y':373,'name':'Demo User','avatar_sha':'a'*64}))
        self.assertEqual([c.args[0] for c in r.node.call_args_list],['SelectRecentRow'])
        self.assertIsNone(r.selection_diagnostics)

    def test_persistently_wrong_row_stops_after_two_guarded_navigation_attempts(self):
        from watch import select
        other=[{'box':[61,211,9,65]}];r=self.runtime([other]*4)
        self.assertFalse(select(r,{'y':373,'name':'Demo User','avatar_sha':'a'*64}))
        self.assertEqual([c.args[0] for c in r.node.call_args_list],['SelectRecentRow']*2)
        self.assertEqual(r.selection_diagnostics['reason'],'selected_row_not_confirmed')
        self.assertEqual(r.selection_diagnostics['selected_boxes'],[[61,211,9,65]])

    def test_selected_row_with_wrong_title_stops_immediately(self):
        from watch import select
        other=[{'box':[61,211,9,65]}];target=[{'box':[61,341,9,65]}]
        r=self.runtime([other,target])
        with patch('watch.read_title',side_effect=ValueError('CHAT_TITLE_MISMATCH')):
            self.assertFalse(select(r,{'y':373,'name':'Demo User','avatar_sha':'a'*64}))
        self.assertEqual(r.node.call_count,1)
        self.assertEqual(r.selection_failure['reason'],'CHAT_TITLE_MISMATCH')
        self.assertEqual(r.selection_diagnostics['reason'],'selected_title_mismatch')

    def test_already_open_conversation_moved_in_sidebar_needs_no_click(self):
        import hashlib
        from watch import select
        target=[{'box':[61,341,9,65]}];r=self.runtime([target]);image=r.capture.return_value[0]
        row={'y':308,'name':'Demo User','avatar_sha':hashlib.sha256(image[360:386,78:102,:3].tobytes()).hexdigest()}
        with patch('recent_name.row_name',return_value=('Demo User',[])),patch('watch.read_title',return_value='Demo User'):
            self.assertTrue(select(r,row))
        self.assertEqual(row['y'],373);r.node.assert_not_called()
        self.assertEqual(r.selection_position_refresh['previous_y'],308)

    def test_selected_position_refresh_requires_same_name_avatar_and_title(self):
        import hashlib
        from watch import refresh_selected_position
        r=self.runtime([]);image=r.capture.return_value[0];selected=[{'box':[61,341,9,65]}]
        sha=hashlib.sha256(image[360:386,78:102,:3].tobytes()).hexdigest()
        for bad in ('name','avatar','title','ambiguous'):
            row={'y':308,'name':'Demo User','avatar_sha':sha if bad!='avatar' else 'a'*64}
            with patch('recent_name.row_name',return_value=('Other' if bad=='name' else 'Demo User',[])), \
                 patch('watch.read_title',side_effect=ValueError('mismatch') if bad=='title' else None):
                self.assertFalse(refresh_selected_position(r,image,row,selected*2 if bad=='ambiguous' else selected))
            self.assertEqual(row['y'],308);r.node.assert_not_called()
    def test_guard_miss_freezes_the_actual_failure_frame(self):
        import tempfile,hashlib
        from pathlib import Path
        from watch import select
        other=[{'box':[61,211,9,65]}];r=self.runtime([other]);r.node.return_value={'succeeded':False}
        with tempfile.TemporaryDirectory() as folder:
            r.navigation_evidence_dir=Path(folder)
            self.assertFalse(select(r,{'y':373,'name':'Demo User','avatar_sha':'a'*64}))
            self.assertTrue(Path(r.selection_diagnostics['frame_path']).is_file())
            self.assertEqual(r.selection_diagnostics['frame_sha256'],hashlib.sha256(r.capture.return_value[0].tobytes()).hexdigest())
        self.assertEqual(r.node.call_count,1)


class IncomingTests(unittest.TestCase):
    def test_arrival_during_native_copy_retains_an_urgent_read_hint(self):
        source=np.full((1040,1920,3),250,dtype=np.uint8)
        current=source.copy();current[850,800]=20
        row={'key':'k','name':'Demo User','avatar_sha':'a'*64,'y':113,'unread':False}
        pending={}
        self.assertTrue(finish_read_hint(row,source,current,pending,100))
        due=pending_visible_wakes([row],[],pending,101)
        self.assertEqual(due[0]['wake_reason'],'post_read_pixels')
        self.assertEqual(filter_changed_wakes(due,{'k':200},101),due)
        # 这只是下一次读取的优先级，不是已核验新来信或发送许可。
        self.assertEqual(self.requests([self.payload()],status='unchanged'),[])
        self.assertFalse(finish_read_hint(row,current,current,pending,102))
        self.assertNotIn('k',pending)

    def test_input_caret_or_toolbar_change_does_not_create_a_receive_hint(self):
        source=np.full((1040,1920,3),250,dtype=np.uint8)
        current=source.copy();current[930,800]=20
        pending={};row={'key':'k','name':'Demo User','avatar_sha':'a'*64}
        self.assertFalse(finish_read_hint(row,source,current,pending,100))
        self.assertEqual(pending,{})

    def test_new_preview_survives_selection_background_round_trip(self):
        row={'key':'k','name':'Demo User','y':113};rows=[row];previews={}
        plain=np.full((1040,1920,3),250,dtype=np.uint8)
        sidebar_changes(rows,plain,previews,{})
        selected=plain.copy();selected[80:145,60:480]=[100,180,20]
        self.assertEqual(sidebar_changes(rows,selected,previews,{})[0],[])
        # 收到消息后切到别的会话，未读可能已被当前选中聊天清除。
        arrived=plain.copy();arrived[120,180]=20
        changed,_=sidebar_changes(rows,arrived,previews,{})
        self.assertEqual([x['wake_reason'] for x in changed],['preview_pixels'])

    def payload(self,**extra): return dict({'text':'你好','direction':'incoming','reply_safe':True,'message_kind':'text'},**extra)
    def requests(self,payloads,status='append',title='Demo User',row=None):
        return incoming_requests(payloads,{'status':status,'overlap':0,'snapshot_id':11},row or {'name':'Demo User'},title,'a'*64,100,90)
    def test_history_and_gap_never_reply(self):
        for state in ('baseline','gap','ambiguous','review','unchanged'):
            self.assertEqual(self.requests([self.payload()],state),[])
    def test_only_stable_new_incoming_text(self):
        self.assertEqual(len(self.requests([self.payload()])),1)
        for extra in ({'direction':'outgoing'},{'reply_safe':False},{'message_kind':'media'},{'text':''}):
            self.assertEqual(self.requests([self.payload(**extra)]),[])
    def test_group_needs_verified_visible_sender(self):
        row={'name':'开发组'}
        self.assertEqual(self.requests([self.payload()],title='开发组（10）',row=row),[])
        result=self.requests([self.payload(group_sender_name='Demo User',group_sender_verified=True)],title='开发组（10）',row=row)
        self.assertEqual(result[0][1]['sender_name'],'Demo User');self.assertEqual(result[0][1]['conversation_type'],'group')
    def test_repeated_equal_messages_have_distinct_occurrences(self):
        events=self.requests([self.payload(),self.payload()])
        self.assertNotEqual(events[0][1]['event_id'],events[1][1]['event_id'])
    def test_personal_count_suffix_does_not_make_arbitrary_group(self):
        self.assertFalse(is_group_title({'name':'Tom(10)'},'Tom(10)'))
        self.assertTrue(is_group_title({'name':'开发组'},'开发组（10）'))
        result=self.requests([self.payload()],title='Tom(10)',row={'name':'Tom(10)'})
        self.assertEqual(result[0][1]['conversation_type'],'private')
    def test_open_chat_new_pixels_wake_even_without_unread_badge(self):
        image=np.full((1040,1920,3),250,dtype=np.uint8);image[113,65]=[100,180,20]
        rows=[{'key':'k','name':'Demo User','preview':'你好','y':113,'unread':False}];previews={}
        first,state=sidebar_changes(rows,image,previews,{})
        self.assertEqual(first,[])
        image[100,1000]=[200,200,200]
        changed,new=sidebar_changes(rows,image,previews,state)
        self.assertEqual(changed[0]['key'],'k');self.assertNotEqual(state['sha'],new['sha'])
    def test_changed_preview_pixels_are_read_hint_not_first_discovery(self):
        image=np.full((1040,1920,3),250,dtype=np.uint8);rows=[{'key':'k','preview':'旧','y':113}];cache={}
        self.assertEqual(sidebar_changes(rows,image,cache,{})[0],[])
        rows[0]['preview']='新';image[120,180]=[200,200,200]
        hint=sidebar_changes(rows,image,cache,{})[0][0]
        self.assertEqual(hint['key'],'k');self.assertEqual(hint['wake_reason'],'preview_pixels')
    def test_ocr_preview_string_wobble_without_pixel_change_does_not_wake(self):
        image=np.full((1040,1920,3),250,dtype=np.uint8);rows=[{'key':'k','preview':'你好','y':113}];cache={}
        sidebar_changes(rows,image,cache,{})
        rows[0]['preview']='你子'
        self.assertEqual(sidebar_changes(rows,image,cache,{})[0],[])
    def test_legacy_preview_hash_upgrades_once_without_fake_message(self):
        image=np.full((1040,1920,3),250,dtype=np.uint8);rows=[{'key':'k','preview':'新','y':113}];cache={'k':'old_ocr_hash'}
        self.assertEqual(sidebar_changes(rows,image,cache,{})[0],[])
        self.assertEqual(set(cache['k']),{'sha','time_sha','selected'})
    def test_selection_background_transition_is_navigation_not_preview_message(self):
        image=np.full((1040,1920,3),250,dtype=np.uint8);rows=[{'key':'k','y':113}];cache={}
        sidebar_changes(rows,image,cache,{})
        image[113,65]=[100,180,20];image[114:142,115:465]=[100,180,20]
        self.assertEqual(sidebar_changes(rows,image,cache,{})[0],[])
        image[120,180]=[0,0,0]
        self.assertEqual(sidebar_changes(rows,image,cache,{})[0][0]['wake_reason'],'preview_pixels')
    def test_same_row_preview_pixels_take_precedence_over_chat_pixels(self):
        image=np.full((1040,1920,3),250,dtype=np.uint8);image[113,65]=[100,180,20]
        rows=[{'key':'k','y':113}];cache={};_,selected=sidebar_changes(rows,image,cache,{})
        image[120,180]=[200,200,200];image[100,1000]=[200,200,200]
        changed,_=sidebar_changes(rows,image,cache,selected)
        self.assertEqual(len(changed),1);self.assertEqual(changed[0]['wake_reason'],'preview_pixels')


class PixelWakeFilterTests(unittest.TestCase):
    def test_only_chat_pixels_deferred_for_at_most_fifteen_seconds(self):
        chat={'key':'k','wake_reason':'chat_pixels'};cooldowns={'k':115}
        self.assertEqual(filter_changed_wakes([chat],cooldowns,100),[])
        self.assertEqual(filter_changed_wakes([chat],cooldowns,114.99),[])
        self.assertEqual(filter_changed_wakes([chat],cooldowns,115),[chat])
    def test_preview_and_unread_bypass_cooldown(self):
        rows=[{'key':'k','wake_reason':'preview_pixels'},{'key':'k','wake_reason':'chat_pixels','unread':True}]
        self.assertEqual(filter_changed_wakes(rows,{'k':115},100),rows)
    def test_filter_is_pure_and_does_not_extend_cooldown(self):
        rows=[{'key':'k','wake_reason':'chat_pixels'}];cooldowns={'k':115};before=deepcopy((rows,cooldowns))
        filter_changed_wakes(rows,cooldowns,100)
        self.assertEqual((rows,cooldowns),before)
    def test_deferred_chat_pixel_edge_survives_to_expiry_not_lost_in_cache(self):
        image=np.full((1040,1920,3),250,dtype=np.uint8);image[113,65]=[100,180,20]
        rows=[{'key':'k','y':113}];previews={};_,previous=sidebar_changes(rows,image,previews,{})
        image[100,1000]=[200,200,200]
        changed,current=sidebar_changes(rows,image,previews,previous)
        filtered=filter_changed_wakes(changed,{'k':115},100)
        self.assertEqual(filtered,[])
        retained=selected_after_wake_filter(previous,current,changed,filtered)
        self.assertEqual(retained,previous)
        later,later_state=sidebar_changes(rows,image,previews,retained)
        self.assertEqual(filter_changed_wakes(later,{'k':115},115)[0]['wake_reason'],'chat_pixels')
        self.assertEqual(selected_after_wake_filter(retained,later_state,later,later),later_state)
    def test_navigation_to_different_selected_row_is_never_held_by_old_cooldown(self):
        previous={'key':'old','sha':'a'};current={'key':'new','sha':'b'}
        self.assertEqual(selected_after_wake_filter(previous,current,[{'key':'old','wake_reason':'chat_pixels'}],[]),current)


class ColdRoutingTests(unittest.TestCase):
    class Runtime:
        def __init__(self,*,badge=False,unchanged=False):
            self.actions=[];self.badge=badge;self.unchanged=unchanged
        def node(self,name,override): self.actions.append((name,override))
        def capture(self,label):
            value=0 if self.unchanged else len(self.actions)
            return np.full((1040,1920,3),value,dtype=np.uint8),label+'.png'
        def recognize_image(self,name,image): return [{'box':[100,90,10,10]}] if self.badge else []
    class Scheduler:
        def __init__(self,deadline_after=None): self.checks=0;self.deadline_after=deadline_after
        def badge_scan_due(self):
            self.checks+=1
            return self.deadline_after is not None and self.checks>self.deadline_after
    class Client:
        def __init__(self,items): self.items=items;self.sent=[]
        def ready(self): return self.items
        def send(self,*args): self.sent.append(args)
    def image(self): return np.zeros((1040,1920,3),dtype=np.uint8)
    def item(self,page=5,event='e'):
        return {'event_id':event,'request_json':json.dumps({'_target':{'name':'Demo User','avatar_sha':'a','list_page':page}})}
    def test_deep_bookmark_uses_bounded_two_actions_not_100_slow_batches(self):
        runtime=self.Runtime()
        reached,_,_,reason=seek_cold_page(runtime,'trial',99,self.Scheduler(),self.image(),'top.png')
        self.assertEqual((reached,reason),(99,'reached'));self.assertEqual(len(runtime.actions),2)
        values=[p['ContactsScrollDown'] for _,p in runtime.actions]
        self.assertEqual(sum(v['dy'] for v in values),-600*99)
        self.assertTrue(all(-30000<=v['dy']<0 and v['repeat']==1 for v in values))
    def test_red_badge_between_seek_segments_interrupts_without_false_completion(self):
        runtime=self.Runtime(badge=True)
        reached,_,_,reason=seek_cold_page(runtime,'trial',99,self.Scheduler(),self.image(),'top.png')
        self.assertEqual((reached,reason),(50,'unread'));self.assertEqual(len(runtime.actions),1)
    def test_deadline_between_seek_segments_is_not_completed_page(self):
        runtime=self.Runtime()
        reached,_,_,reason=seek_cold_page(runtime,'trial',99,self.Scheduler(deadline_after=1),self.image(),'top.png')
        self.assertEqual((reached,reason),(50,'badge_scan_due'));self.assertEqual(len(runtime.actions),1)
    def test_already_due_does_not_start_wheel_action(self):
        runtime=self.Runtime()
        reached,_,_,reason=seek_cold_page(runtime,'trial',10,self.Scheduler(deadline_after=0),self.image(),'top.png')
        self.assertEqual((reached,reason),(0,'badge_scan_due'));self.assertEqual(runtime.actions,[])
    def test_unchanged_fingerprint_reports_boundary_not_coverage(self):
        runtime=self.Runtime(unchanged=True)
        reached,_,_,reason=seek_cold_page(runtime,'trial',99,self.Scheduler(),self.image(),'top.png')
        self.assertEqual((reached,reason),(50,'scroll_unchanged'));self.assertEqual(len(runtime.actions),1)
    def test_wrap_zero_needs_no_scroll(self):
        runtime=self.Runtime()
        self.assertEqual(seek_cold_page(runtime,'trial',0,self.Scheduler(),self.image(),'top.png')[-1],'reached')
        self.assertEqual(runtime.actions,[])
    def test_bad_bookmark_rejected(self):
        for value in (-1,101,True,1.5):
            with self.assertRaises(ValueError): seek_cold_page(self.Runtime(),'trial',value,self.Scheduler(),self.image(),'top.png')
    def test_ready_reply_on_cold_page_uses_same_identity_and_selection_guards(self):
        client=self.Client([self.item()]);row={'name':'Demo User','avatar_sha':'a','y':800}
        with patch('watch.select',return_value=True) as select:
            self.assertTrue(send_ready_on_rows(client,None,'trial',[row]));select.assert_called_once_with(None,row)
        self.assertEqual(client.sent[0][-1]['row_y'],800)
    def test_duplicate_or_wrong_avatar_cold_rows_never_send(self):
        row={'name':'Demo User','avatar_sha':'a','y':800}
        for rows in ([dict(row,avatar_sha='other')],[row,dict(row,y=500)]):
            client=self.Client([self.item()])
            with patch('watch.select') as select:
                self.assertFalse(send_ready_on_rows(client,None,'trial',rows));select.assert_not_called()
            self.assertEqual(client.sent,[])
    def test_failed_selection_never_send(self):
        client=self.Client([self.item()]);row={'name':'Demo User','avatar_sha':'a','y':800}
        with patch('watch.select',return_value=False): self.assertFalse(send_ready_on_rows(client,None,'trial',[row]))
        self.assertEqual(client.sent,[])
    def test_pending_deep_reply_gets_origin_bookmark_before_full_cold_sweep(self):
        client=self.Client([self.item()]);tried={}
        self.assertEqual(ready_page_hint(client,10,tried,100),5)
        self.assertIsNone(ready_page_hint(client,10,tried,110))
        self.assertEqual(ready_page_hint(client,10,tried,115),5)
    def test_legacy_or_invalid_page_hints_are_not_identity_search(self):
        for page in (None,True,0,10,-1,'5'):
            self.assertIsNone(ready_page_hint(self.Client([self.item(page)]),10,{},100))
    def test_interrupted_repeated_shallow_page_does_not_reset_saved_goal(self):
        for reason in ('unread','badge_scan_due'):
            self.assertEqual(advance_cold_bookmark(60,'same',61,'same',reason),(60,'same'))
    def test_completed_or_unmoving_seek_updates_only_hinted_bookmark(self):
        self.assertEqual(advance_cold_bookmark(60,'old',61,'new','reached'),(61,'new'))
        self.assertEqual(advance_cold_bookmark(60,'same',61,'same','reached'),(0,None))
        self.assertEqual(advance_cold_bookmark(60,'old',61,'new','scroll_unchanged'),(0,None))


class WarmupTests(unittest.TestCase):
    def rows(self): return [{'name':'Demo User','key':'tom','unread':False},{'name':'文件传输助手','key':'self','unread':False}]
    def states(self): return {'tom':{'weight':.6,'retired':False},'self':{'weight':.9,'retired':False}}
    def test_one_weighted_candidate_ignores_persisted_next_due(self):
        states=self.states();states['self']['next_due_at']=999999
        row=startup_warmup(self.rows(),states,set(),enabled=True)
        self.assertEqual(row['key'],'self');self.assertEqual(row['schedule']['reason'],'startup_warmup')
    def test_once_attempted_failed_row_cannot_monopolize_warmup(self):
        attempted={'self'}
        row=startup_warmup(self.rows(),self.states(),attempted,enabled=True)
        self.assertEqual(row['key'],'tom')
        attempted.add('tom');self.assertIsNone(startup_warmup(self.rows(),self.states(),attempted,enabled=True))
    def test_retired_rows_remain_unopened(self):
        states=self.states();states['self']['retired']=True
        self.assertEqual(startup_warmup(self.rows(),states,set(),enabled=True)['key'],'tom')
        states['tom']['retired']=True
        self.assertIsNone(startup_warmup(self.rows(),states,set(),enabled=True))
    def test_readonly_and_urgent_work_do_not_warmup(self):
        self.assertIsNone(startup_warmup(self.rows(),self.states(),set(),enabled=False))
        self.assertIsNone(startup_warmup(self.rows(),self.states(),set(),enabled=True,urgent=True))
    def test_selector_does_not_mutate_or_claim_activity_baseline_or_attempt(self):
        rows=self.rows();states=self.states();attempted=set();before=deepcopy((rows,states,attempted))
        startup_warmup(rows,states,attempted,enabled=True)
        self.assertEqual((rows,states,attempted),before)
    def test_unknown_or_nameless_rows_are_not_pure_selector_candidates(self):
        self.assertIsNone(startup_warmup([{'name':'','key':'tom'},{'name':'New','key':'unknown'}],self.states(),set(),enabled=True))
    def test_real_scheduler_retirement_and_hour_backoff_remain_unchanged(self):
        now=100*DAY;scheduler=ActivityScheduler(clock=lambda:now)
        scheduler.register('tom',last_message_time=now-2*DAY)
        for _ in range(6): scheduler.observe('tom',False)
        scheduler.register('self',last_message_time=now-31*DAY)
        before=scheduler.dump_state();states={key:scheduler.status(key) for key in ('tom','self')}
        self.assertEqual(scheduler.plan(self.rows()),[])
        self.assertEqual(startup_warmup(self.rows(),states,set(),enabled=True)['key'],'tom')
        self.assertEqual(scheduler.dump_state(),before)


if __name__=='__main__': unittest.main()

class RecentSurfaceTests(unittest.TestCase):
    def runtime(self,headers,arrow=True):
        from unittest.mock import Mock
        runtime=Mock();runtime.capture.return_value=(np.zeros((1040,1920,3),dtype=np.uint8),'frame')
        values=iter(headers)
        def recognize(name,*args,**kwargs):
            if name=='FoldedChatsHeader': return [{'text':'折叠的聊天'}] if next(values) else []
            if name=='FoldedChatsBack': return [{'box':[72,90,15,20]}] if arrow else []
            return []
        runtime.recognize_image.side_effect=recognize
        return runtime
    def test_folded_sublist_returns_before_reading_rows(self):
        from watch import read_rows
        runtime=self.runtime([True,False]);self.assertEqual(read_rows(runtime,'.','test')[0],[])
        runtime.node.assert_called_once_with('FoldedChatsBack')
        self.assertEqual(runtime.capture.call_count,2)
    def test_normal_surface_does_not_click(self):
        from watch import read_rows
        runtime=self.runtime([False]);read_rows(runtime,'.','test');runtime.node.assert_not_called()
    def test_header_without_back_arrow_is_rejected(self):
        from watch import read_rows
        runtime=self.runtime([True],False)
        with self.assertRaisesRegex(ValueError,'BACK_NOT_VERIFIED'):read_rows(runtime,'.','test')
        runtime.node.assert_not_called()
    def test_failed_exit_does_not_scan_folded_rows(self):
        from watch import read_rows
        runtime=self.runtime([True,True])
        with self.assertRaisesRegex(ValueError,'EXIT_NOT_VERIFIED'):read_rows(runtime,'.','test')
        self.assertFalse(any(c.args[0]=='ListRows' for c in runtime.recognize_image.call_args_list))
    def test_folded_navigation_entry_is_not_a_conversation(self):
        from watch import read_rows
        runtime=self.runtime([False])
        with patch('watch.named_avatar_rows',return_value=[{'name':'折叠的聊天','y':990}]):
            self.assertEqual(read_rows(runtime,'.','test')[0],[])
        runtime.node.assert_not_called()

class ActivationUpdateTests(unittest.TestCase):
    def test_planned_update_preserves_verified_baseline_once(self):
        import tempfile
        from pathlib import Path
        from store import Store
        from watch import resume_activation
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);s=Store(p/'db');s.db.execute("INSERT INTO snapshots VALUES(1,'Demo User','f','[]','baseline',95)");s.db.commit()
            (p/'reply-runtime.json').write_text(json.dumps({'run_id':'old','enabled_at':90,'auto_reply_enabled':True}))
            (p/'activation-update.json').write_text(json.dumps({'old_run_id':'old','new_run_id':'new','enabled_at':90,'created_at':100,'baselines':{'Demo User':1}}))
            self.assertEqual(resume_activation(p,s,'new',110)[:2],(90,{'Demo User'}))
            self.assertIsNone(resume_activation(p,s,'new',111))
            s.db.close()
    def test_old_checkpoint_wrong_run_or_unverified_baseline_cannot_resume(self):
        import tempfile
        from pathlib import Path
        from store import Store
        from watch import resume_activation
        for changes in ({'created_at':1},{'new_run_id':'wrong'},{'enabled_at':89},{'baselines':{'Demo User':2}}):
            with tempfile.TemporaryDirectory() as d:
                p=Path(d);s=Store(p/'db');s.db.execute("INSERT INTO snapshots VALUES(2,'Demo User','f','[]','gap',95)");s.db.commit()
                (p/'reply-runtime.json').write_text(json.dumps({'run_id':'old','enabled_at':90,'auto_reply_enabled':True}))
                value={'old_run_id':'old','new_run_id':'new','enabled_at':90,'created_at':100,'baselines':{'Demo User':1}};value.update(changes)
                (p/'activation-update.json').write_text(json.dumps(value));self.assertIsNone(resume_activation(p,s,'new',110));self.assertFalse((p/'activation-update.json').exists())
                s.db.close()
