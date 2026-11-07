import json,tempfile,unittest
from pathlib import Path
from store import Store
from inbound_work import InboundWork,tail_for_badge
from recent_name import name_roi

def payload(n,direction='incoming',kind='text'):
    return {'direction':direction,'message_kind':kind,'reply_safe':kind=='text','text':str(n),
      'visual':{'verified':True,'sha256':f'{n:064x}','box':[550,100+n*50,120,40]}}

class InboundTests(unittest.TestCase):
    def test_badge_free_gap_stays_unhandled_but_strict_future_input_is_not_blocked(self):
        from watch import incoming_requests
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'state.sqlite');work=InboundWork(store)
            row={'key':'person','name':'person','unread':False}
            store.snapshot('cid','person','avatar','old',[payload(1),payload(2)])
            unknown=[payload(7),payload(8)]
            gap=store.snapshot('cid','person','avatar','gap',unknown)
            self.assertEqual(gap['status'],'gap')
            self.assertTrue(work.record_context_gap(row,gap))
            review=work.get(row['key']);self.assertEqual(review['status'],'review')
            unsafe=store.snapshot('cid','person','avatar','unsafe',unknown+[dict(payload(9),reply_safe=False)],
                reconciliation_guard=lambda result,values:work.guard_review_append(row,result,values))
            self.assertEqual(unsafe['status'],'review')
            self.assertEqual(incoming_requests(unknown+[dict(payload(9),reply_safe=False)],unsafe,row,'person','cid',99,90),[])
            fresh=unknown+[payload(9)]
            append=store.snapshot('cid','person','avatar','fresh',fresh,
                reconciliation_guard=lambda result,values:work.guard_review_append(row,result,values))
            self.assertEqual(append['status'],'append');self.assertEqual(append['overlap'],2)
            self.assertTrue(append['metadata']['independent_review_append'])
            requests=incoming_requests(fresh,append,row,'person','cid',100,90)
            self.assertEqual([body['text'] for _,body in requests],['9'])
            self.assertTrue(work.capture(row,append,fresh,[2]))
            self.assertNotEqual(work.get(row['key'])['seed'],review['seed'])
            self.assertEqual(store.db.execute('SELECT status FROM snapshots WHERE id=?',(gap['snapshot_id'],)).fetchone()[0],'gap')
            same=store.snapshot('cid','person','avatar','same',fresh)
            self.assertEqual(incoming_requests(fresh,same,row,'person','cid',101,90),[])
            store.db.close()

    def test_unstable_new_append_retries_without_red_and_keeps_original_seed(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'data.sqlite');work=InboundWork(store)
            row={'key':'k','name':'开发组','unread':True,'unread_count':None}
            result={'status':'review','snapshot_id':7,'metadata':{'new_tail_verbatim':True}}
            with patch('inbound_work.time.time',return_value=100):
                work.observe([row]);seed=work.get('k')['seed'];self.assertTrue(work.retry_unstable_append(row,result))
            row['unread']=False
            with patch('inbound_work.time.time',return_value=116):self.assertEqual(len(work.pending([row])),1)
            self.assertEqual(work.get('k')['seed'],seed)
            with patch('inbound_work.time.time',return_value=341):self.assertEqual(work.pending([row]),[])
            self.assertEqual(work.get('k')['reason'],'verification_retry_expired');store.db.close()
    def test_unknown_history_gap_is_not_automatically_promoted(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'data.sqlite');work=InboundWork(store)
            self.assertFalse(work.retry_unstable_append({'key':'k'}, {'status':'gap','snapshot_id':1}))
            self.assertIsNone(work.get('k'));store.db.close()
    def test_bounded_unread_tail_and_unknown_count(self):
        p=[payload(1,'outgoing'),payload(2),payload(3,kind='media')]
        self.assertEqual([i for i,_ in tail_for_badge(p,2)],[1,2])
        self.assertIsNone(tail_for_badge(p,None));self.assertIsNone(tail_for_badge(p,3))
    def test_already_answered_and_repeated_visuals_are_reviewed(self):
        self.assertIsNone(tail_for_badge([payload(1),payload(2,'outgoing')],1))
        self.assertIsNone(tail_for_badge([payload(1),payload(1)],2))
    def test_work_survives_badge_clearing_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'data.sqlite');work=InboundWork(store)
            row={'key':'key','name':'name','unread':True,'unread_count':2}
            work.observe([row]);seed=work.get('key')['seed'];row['unread']=False
            work.retire_badges([row]);self.assertEqual(len(work.pending([row])),1)
            store.db.close();store=Store(Path(directory)/'data.sqlite');work=InboundWork(store)
            self.assertEqual(work.get('key')['seed'],seed);self.assertEqual(len(work.pending([row])),1)
            work.update('key','review','count_not_verified');self.assertEqual(work.count(),1)
            store.db.close()
    def test_captured_exact_pixels_preserve_original_text_and_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'data.sqlite');work=InboundWork(store);row={'key':'key','name':'name','unread':True,'unread_count':1}
            p=[payload(1)];result=store.snapshot('cid','name','avatar','frame',p)
            work.capture(row,result,p,[0]);seed=work.get('key')['seed']
            work.navigation_failed(row['key'])
            self.assertEqual(work.get('key')['status'],'captured')
            self.assertFalse(work.eligible(row));self.assertEqual(work.pending([row]),[])
            self.assertTrue(work.visible_pending([row]))
            self.assertTrue(work.eligible({'key':'another'}))
            current=[dict(p[0],text='different OCR')];self.assertEqual(work.recover(row,current)[0][1]['text'],'1')
            current[0]['visual']=dict(current[0]['visual'],sha256='f'*64);self.assertIsNone(work.recover(row,current))
            self.assertEqual(work.get('key')['seed'],seed)
            store.db.close()
    def test_canonical_capture_survives_scroll_and_recapture_without_losing_original_text(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state.sqlite');work=InboundWork(store)
            row={'key':'row','name':'Demo User','unread':True,'unread_count':1}
            originals=[payload(1,'outgoing'),payload(2)]
            first=store.snapshot('cid','Demo User','avatar','one',originals)
            work.capture(row,first,originals,[1]);seed=work.get('row')['seed']
            current=[dict(originals[1],text='noisy',visual=dict(originals[1]['visual'],box=[550,100,120,40]))]
            recovered=work.recover(row,current);self.assertEqual(recovered[0][1]['text'],'2')
            mapping=work.source_ordinals(row,recovered);self.assertEqual(mapping,{0:1})
            second=store.snapshot('cid','Demo User','avatar','two',current)
            work.capture(row,dict(second,source_ordinals=mapping),current,[0])
            self.assertEqual(work.get('row')['seed'],seed)
            self.assertEqual(work.recover(row,current)[0][1]['text'],'2')
            store.db.close()

    def test_badge_roi_is_pixel_bound_not_letter_deletion(self):
        self.assertEqual(name_roi(503,[{'box':[241,488,15,15]}]),[115,478,123,29])
        self.assertIsNone(name_roi(503,[]))
        self.assertIsNone(name_roi(503,[{'box':[600,40,15,15]}]))

class OccurrenceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.store=Store(Path(self.folder.name)/'state.sqlite')
        self.work=InboundWork(self.store)
        self.row={'key':'row','name':'group','unread':True,'unread_count':1}

    def tearDown(self):
        self.store.db.close();self.folder.cleanup()

    def snapshot(self,values):
        return self.store.snapshot('cid','group','avatar','frame',values)

    def test_successive_new_occurrences_same_ordinal_keep_old_capture_and_distinct_seed(self):
        old=[payload(1),payload(2),payload(3)]
        first=self.snapshot(old);self.assertTrue(self.work.capture(self.row,first,old,[2]))
        seed=self.work.get('row')['seed'];self.work.update('row','queued','reply_queued')
        new=[payload(2),payload(3),payload(4)]
        second=self.snapshot(new);self.assertEqual(second['status'],'append')
        self.assertTrue(self.work.capture(self.row,second,new,[2]))
        self.assertNotEqual(self.work.get('row')['seed'],seed)
        captures=self.store.db.execute('SELECT seed,source_ordinal,payload_json FROM inbound_captures ORDER BY rowid').fetchall()
        self.assertEqual([(r[1],json.loads(r[2])['text']) for r in captures],[(2,'3'),(2,'4')])
        self.work.update('row','queued','reply_queued');new_seed=self.work.get('row')['seed']
        self.assertFalse(self.work.capture(self.row,dict(second,status='unchanged'),new,[2]))
        self.assertEqual(self.work.get('row')['seed'],new_seed)

    def test_ordinary_review_new_append_rotates_seed_and_archives_original_review(self):
        old=[payload(1),payload(2),payload(3)];first=self.snapshot(old)
        self.work.capture(self.row,first,old,[2]);seed=self.work.get('row')['seed']
        self.work.update('row','review','sender_or_content_not_verified')
        new=[payload(2),payload(3),payload(4)];second=self.snapshot(new)
        self.assertTrue(self.work.capture(self.row,second,new,[2]))
        self.assertNotEqual(self.work.get('row')['seed'],seed)
        audit=json.loads(self.store.db.execute("SELECT detail_json FROM events WHERE kind='inbound_new_occurrence_seed'").fetchone()[0])
        self.assertEqual(audit['previous_work']['reason'],'sender_or_content_not_verified')
        self.assertEqual(audit['previous_work']['seed'],seed)
        self.assertEqual(json.loads(self.store.db.execute('SELECT payload_json FROM inbound_captures WHERE seed=?',(seed,)).fetchone()[0])['text'],'3')

    def test_pending_capture_collision_preserves_canonical_and_blocks_handled_baseline(self):
        old=[payload(1),payload(2),payload(3)];first=self.snapshot(old)
        self.work.capture(self.row,first,old,[2]);seed=self.work.get('row')['seed']
        new=[payload(2),payload(3),payload(4)];second=self.snapshot(new)
        self.assertFalse(self.work.capture(self.row,second,new,[2]))
        self.assertEqual(self.work.get('row')['seed'],seed)
        self.assertEqual(self.work.get('row')['reason'],'capture_source_collision')
        self.assertEqual(self.store.db.execute('SELECT status FROM snapshots WHERE id=?',(second['snapshot_id'],)).fetchone()[0],'review')
        self.assertEqual(json.loads(self.store.db.execute('SELECT payload_json FROM inbound_captures').fetchone()[0])['text'],'3')

    def test_operator_recovery_of_review_retains_original_seed(self):
        old=[payload(1),payload(2),payload(3)];first=self.snapshot(old)
        self.work.capture(self.row,first,old,[2]);seed=self.work.get('row')['seed']
        self.work.update('row','review','sender_or_content_not_verified')
        recovery=dict(first,status='unread_recovery',source_ordinals={2:2})
        self.assertTrue(self.work.capture(self.row,recovery,old,[2]))
        self.assertEqual(self.work.get('row')['seed'],seed)

    def test_same_bubble_different_verified_avatar_is_capture_collision(self):
        old=payload(3);old['visual'].update(avatar_verified=True,avatar_sha256='a'*64)
        first=self.snapshot([old]);self.work.capture(self.row,first,[old],[0])
        new=dict(old,visual=dict(old['visual'],avatar_sha256='b'*64))
        self.assertFalse(self.work.capture(self.row,first,[new],[0]))
        self.assertEqual(self.work.get('row')['reason'],'capture_source_collision')
        self.assertEqual(json.loads(self.store.db.execute('SELECT payload_json FROM inbound_captures').fetchone()[0])['visual']['avatar_sha256'],'a'*64)

    def test_stale_gap_only_partitions_strict_new_tail_and_preserves_original_gap_audit(self):
        old=[payload(1),payload(2)];self.snapshot(old)
        gap=[payload(2),payload(3),payload(4)];source=self.snapshot(gap)
        self.work.observe([self.row]);self.work.update('row','review','context_gap_not_verified',source['snapshot_id'])
        old_seed=self.work.get('row')['seed']
        self.store.db.execute('UPDATE inbound_work SET seen=0');self.store.db.commit()
        # An independently verified post-send context may now overlap the
        # former gap, while the original incoming review remains unhandled.
        self.store.snapshot('cid','group','avatar','after-send',gap,activation_baseline=True)
        current=[payload(3),payload(4),payload(5)]
        result=self.store.snapshot('cid','group','avatar','current',current,
            reconciliation_guard=lambda r,p:self.work.guard_review_append(self.row,r,p))
        self.assertEqual(result['status'],'append');self.assertEqual(result['overlap'],2)
        self.assertTrue(result['metadata']['independent_review_append'])
        self.assertTrue(self.work.capture(self.row,result,current,[2]))
        self.assertNotEqual(self.work.get('row')['seed'],old_seed)
        self.assertEqual(json.loads(self.store.db.execute('SELECT payload_json FROM inbound_captures').fetchone()[0])['text'],'5')

    def test_unpartitioned_old_gap_append_is_review_before_snapshot_commit(self):
        old=[payload(1),payload(2)];self.snapshot(old)
        source=self.snapshot([payload(8),payload(9)])
        self.work.observe([self.row]);self.work.update('row','review','context_gap_not_verified',source['snapshot_id'])
        self.store.db.execute('UPDATE inbound_work SET seen=0');self.store.db.commit()
        result=self.store.snapshot('cid','group','avatar','current',old+[payload(3)],
            reconciliation_guard=lambda r,p:self.work.guard_review_append(self.row,r,p))
        self.assertEqual(result['status'],'review')
        self.assertEqual(self.store.db.execute('SELECT status FROM snapshots WHERE id=?',(result['snapshot_id'],)).fetchone()[0],'review')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)

    def test_repeated_body_recovers_only_exact_verified_time_avatar_occurrence(self):
        def timed(clock,sha,y):
            p=payload(7);p['visual']=dict(p['visual'],box=[550,y,120,40],
                avatar_box=[500,y-20,36,36],avatar_verified=True,avatar_sha256='b'*64)
            p['time_anchor']={'text':clock,'sha256':sha,'box':[1100,y-50,40,18],
                'verified':True,'ocr_stable':True,'relative_message':[-550,50],'relative_avatar':[-600,30]}
            return p
        new=timed('18:03','d'*64,700);first=self.snapshot([new])
        self.work.capture(self.row,first,[new],[0]);seed=self.work.get('row')['seed']
        old=timed('16:40','c'*64,350);current=timed('18:03','d'*64,750)
        recovered=self.work.recover(self.row,[old,current])
        self.assertEqual([i for i,_ in recovered],[1]);self.assertEqual(self.work.get('row')['seed'],seed)
        current['time_anchor']['verified']=False
        self.assertIsNone(self.work.recover(self.row,[old,current]))
