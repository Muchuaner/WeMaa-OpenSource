from copy import deepcopy
from datetime import datetime
import hashlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from fresh_tail import locate_fresh_tail
from inbound_messages import InboundMessages
from message_vision import _attach_time_anchors
from store import Store
from watch import planned_visible_reads,pending_visible_wakes,sidebar_changes
from watch_policy import CHINA_TIME


class FreshTailTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.frame=self.root/'fresh.png'
        self.stamp=datetime(2026,10,4,18,51,tzinfo=CHINA_TIME).timestamp()
        self.rgb=np.full((1040,1920,3),250,dtype=np.uint8)
        self.rgb[170:179,1180:1210]=150
        self.rgb[200:711,545:1294]=230;self.rgb[220:235,560:760]=10
        self.rgb[200:236,500:536]=(80,120,200)
        def sha(box):
            x,y,w,h=box;return hashlib.sha256(self.rgb[y:y+h,x:x+w].tobytes()).hexdigest()
        clock={'text':'18:51','direction':'time','message_kind':'time','ocr':{'stable':True},
               'visual':{'time_verified':True,'time_box':[1180,170,30,9],
                         'time_sha256':sha([1180,170,30,9])}}
        body={'text':'/生图 '+('long prompt '*50),'direction':'incoming','message_kind':'text',
              'reply_safe':True,'text_copy':{'verified':True,'method':'native_context_menu'},
              'visual':{'verified':True,'box':[545,200,749,511],
                        'sha256':sha([545,200,749,511]),'avatar_verified':True,
                        'avatar_box':[500,200,36,36],'avatar_sha256':sha([500,200,36,36])}}
        self.payloads=_attach_time_anchors([clock,body])
        Image.fromarray(self.rgb).save(self.frame)

    def tearDown(self):self.temp.cleanup()

    def locate(self,payloads=None,previous=None,now=None):
        return locate_fresh_tail(payloads or self.payloads,self.frame,
                                 self.stamp-20 if previous is None else previous,
                                 self.stamp+20 if now is None else now)

    def test_complete_long_message_after_observation_is_fresh(self):
        result=self.locate()
        self.assertEqual(result['status'],'append');self.assertEqual(result['overlap'],1)
        self.assertEqual(result['new'][0]['text'],self.payloads[1]['text'])

    def test_same_minute_older_or_future_times_do_not_grant_permission(self):
        for previous,now in [(self.stamp+1,self.stamp+20),
                             (self.stamp-20,self.stamp-1),
                             (float('nan'),self.stamp+20)]:
            with self.subTest(previous=previous,now=now):self.assertIsNone(self.locate(previous=previous,now=now))

    def test_quiet_chat_can_receive_new_input_after_longer_than_five_minutes(self):
        self.assertEqual(self.locate(previous=self.stamp-3600)['status'],'append')

    def test_long_unobserved_chat_and_delayed_read_do_not_expire_input(self):
        self.assertEqual(self.locate(previous=self.stamp-6*86400)['status'],'append')
        self.assertEqual(self.locate(previous=self.stamp-6*86400,
                                    now=self.stamp+3600)['status'],'append')

    def test_gap_keeps_receive_head_until_candidate_can_be_registered(self):
        store=Store(self.root/'gap.sqlite3');work=InboundMessages(store)
        try:
            old=deepcopy(self.payloads);old[1]['visual']['sha256']='f'*64
            earlier=self.stamp-6*86400
            with patch('store.time.time',return_value=earlier):
                baseline=store.snapshot('group','Group','a','old.png',old,
                    on_observation=lambda r,p:work.record('group',{},r,p,enabled=True,now=earlier))
            with patch('store.time.time',return_value=self.stamp+20), \
                    patch('fresh_tail.locate_fresh_tail',return_value=None):
                failed=store.snapshot('group','Group','a',str(self.frame),deepcopy(self.payloads),
                    observation_head=work.head('group'),
                    on_observation=lambda r,p:work.record('group',{},r,p,enabled=True,now=self.stamp+20))
            self.assertEqual(failed['status'],'gap')
            self.assertEqual(work.head('group'),baseline['snapshot_id'])
            with patch('store.time.time',return_value=self.stamp+3600):
                recovered=store.snapshot('group','Group','a',str(self.frame),deepcopy(self.payloads),
                    observation_head=work.head('group'),
                    on_observation=lambda r,p:work.record('group',{},r,p,enabled=True,now=self.stamp+3600))
            self.assertEqual(recovered['status'],'append')
            event=recovered['message_event_ids']['1']
            with patch('store.time.time',return_value=self.stamp+3610):
                repeated=store.snapshot('group','Group','a',str(self.frame),deepcopy(self.payloads),
                    observation_head=work.head('group'),
                    on_observation=lambda r,p:work.record('group',{},r,p,enabled=True,now=self.stamp+3610))
            self.assertEqual(repeated['message_event_ids']['1'],event)
            self.assertEqual(store.db.execute('SELECT count(*) FROM inbound_message_work').fetchone()[0],1)
        finally:store.db.close()

    def test_clipped_or_unverified_time_and_portrait_are_rejected(self):
        for field in ['verified','ocr_stable']:
            values=deepcopy(self.payloads);values[1]['time_anchor'][field]=False
            self.assertIsNone(self.locate(values))
        values=deepcopy(self.payloads);values[0]['ocr']['stable']=False
        self.assertIsNone(self.locate(values))
        values=deepcopy(self.payloads);values[1]['visual']['avatar_verified']=False
        self.assertIsNone(self.locate(values))

    def test_frame_changes_cannot_reuse_body_portrait_or_clock_proofs(self):
        for y,x in [(230,600),(210,510),(173,1185)]:
            picture=self.rgb.copy();picture[y,x]=0;Image.fromarray(picture).save(self.frame)
            self.assertIsNone(self.locate())

    def test_a_later_unlocated_or_outgoing_message_blocks_tail_recovery(self):
        for direction in ['incoming','outgoing']:
            values=deepcopy(self.payloads);extra=deepcopy(values[1]);extra.pop('time_anchor')
            extra['direction']=direction;values.append(extra)
            self.assertIsNone(self.locate(values))

    def test_uncopied_fresh_candidate_is_persisted_but_not_marked_readable(self):
        values=deepcopy(self.payloads);values[1]['text']='';values[1]['reply_safe']=False
        values[1]['text_copy']={'verified':False,'reason':'COPY_CLIPBOARD_BUSY'}
        store=Store(self.root/'work.sqlite3');work=InboundMessages(store)
        try:
            old=deepcopy(self.payloads);old[0]['text']='18:50'
            old[1]['visual']['sha256']='f'*64
            with patch('store.time.time',return_value=self.stamp-20):
                baseline=store.snapshot('group','Group','a','old.png',old,
                    on_observation=lambda r,p:work.record('group',{},r,p,enabled=True,now=self.stamp-20))
            with patch('store.time.time',return_value=self.stamp+20):
                result=store.snapshot('group','Group','a',str(self.frame),values,
                    observation_head=work.head('group'),
                    on_observation=lambda r,p:work.record('group',{},r,p,enabled=True,now=self.stamp+20))
            self.assertEqual(result['status'],'review')
            item=work.item(result['message_event_ids']['1'])
            self.assertEqual(item['first_seen'],self.stamp+20)
            self.assertEqual(item['status'],'pending')
            with patch('store.time.time',return_value=self.stamp+25):
                repeated=store.snapshot('group','Group','a',str(self.frame),deepcopy(self.payloads),
                    observation_head=work.head('group'),
                    on_observation=lambda r,p:work.record('group',{},r,p,enabled=True,now=self.stamp+25))
            self.assertEqual(repeated['message_event_ids']['1'],item['event_id'])
            self.assertEqual(work.item(item['event_id'])['first_seen'],self.stamp+20)
            self.assertEqual(store.db.execute('SELECT count(*) FROM inbound_message_work').fetchone()[0],1)
        finally:store.db.close()

    def test_initial_or_manual_observation_cannot_invent_a_fresh_event(self):
        store=Store(self.root/'history.sqlite3')
        try:
            with patch('store.time.time',return_value=self.stamp+20):
                first=store.snapshot('group','Group','a',str(self.frame),deepcopy(self.payloads))
                second=store.snapshot('group','Group','a',str(self.frame),deepcopy(self.payloads))
            self.assertEqual(first['status'],'baseline');self.assertEqual(second['status'],'unchanged')
            self.assertEqual(store.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
        finally:store.db.close()

    def test_unread_and_fresh_visible_change_preempt_unrelated_pending_retry(self):
        pending=[{'key':'old-retry'}];warm={'key':'warm'}
        changed=[{'key':'fresh','wake_reason':'chat_pixels'}];urgent=[{'key':'red'}]
        self.assertEqual(planned_visible_reads(urgent,changed,warm,pending,[],[]),urgent)
        self.assertEqual(planned_visible_reads([],changed,warm,pending,[],[])[0]['key'],'fresh')
        self.assertEqual(planned_visible_reads([],[],warm,pending,[],[]),[warm])
        self.assertEqual(planned_visible_reads([],[],None,pending,[],[]),pending)

    def test_deferred_read_hint_survives_badge_preemption_and_restart(self):
        row={'key':'group','name':'Group','avatar_sha':'a','unread':False}
        hint=dict(row,wake_reason='preview_pixels');pending={}
        first=pending_visible_wakes([row],[hint],pending,100)
        self.assertEqual(planned_visible_reads([{'key':'red'}],first,None,[],[],[])[0]['key'],'red')
        # The next frame has no new edge; persistence still schedules the group.
        later=pending_visible_wakes([row],[],deepcopy(pending),101)
        self.assertEqual(later[0]['key'],'group')
        self.assertEqual(pending['group']['seen'],100)
        self.assertEqual(pending_visible_wakes([dict(row,avatar_sha='different')],[],pending,102),[])
        self.assertEqual(pending_visible_wakes([row],[],pending,401),[])

    def test_same_preview_new_time_pixels_wake_muted_group(self):
        picture=np.full((1040,1920,3),250,dtype=np.uint8)
        row={'key':'group','name':'Group','avatar_sha':'a','y':373,'unread':False}
        cache={};self.assertEqual(sidebar_changes([row],picture,cache,{})[0],[])
        picture[360,445]=150
        hints,_=sidebar_changes([row],picture,cache,{})
        self.assertEqual(hints[0]['key'],'group')
        self.assertEqual(hints[0]['wake_reason'],'preview_pixels')


if __name__=='__main__':unittest.main()
