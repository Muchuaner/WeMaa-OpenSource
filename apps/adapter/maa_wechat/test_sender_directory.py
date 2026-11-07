import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
from store import Store
from sender_directory import SenderDirectory
from message_vision import reconcile_visual, verify_time_anchors


class SenderDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'sender.sqlite')
        self.directory=SenderDirectory(self.store)
        self.payload={'sender_observation_key':'c'*64,'direction':'incoming','group_sender_name':'Demo User','group_sender_verified':True,
                      'visual':{'avatar_verified':True,'avatar_sha256':'a'*64}}

    def tearDown(self):
        self.store.db.close();self.temp.cleanup()

    def test_visual_identity_does_not_pretend_to_be_wechat_id(self):
        p=self.directory.enrich('group',[self.payload])[0]
        self.assertEqual(p['sender_name'],'Demo User')
        self.assertEqual(len(p['sender_identity_key']),64)
        self.assertFalse(p['sender_identity_verified'])
        self.assertNotIn('sender_wechat_id',p)

    def test_profile_binding_requires_exact_message_avatar(self):
        proof={'stable':True,'wechat_id':'demo_wechat','message_avatar_sha256':'a'*64,'display_name':'Demo User'}
        self.directory.remember('group',self.payload,proof)
        p=self.directory.enrich('group',[self.payload])[0]
        self.assertEqual(p['sender_wechat_id'],'demo_wechat')
        for changes in [{'avatar_sha256':'b'*64},{'avatar_verified':False}]:
            other=dict(self.payload,visual=dict(self.payload['visual'],**changes))
            self.assertNotIn('sender_wechat_id',self.directory.enrich('group',[other])[0])
        self.assertNotIn('sender_wechat_id',self.directory.enrich('other group',[self.payload])[0])

    def test_same_nickname_or_photo_never_borrows_another_member_id(self):
        proof={'stable':True,'wechat_id':'demo_wechat','message_avatar_sha256':'a'*64}
        self.directory.remember('group',self.payload,proof)
        other=dict(self.payload,group_sender_name='Another Tom')
        self.assertNotIn('sender_wechat_id',self.directory.enrich('group',[other])[0])
        with self.assertRaisesRegex(ValueError,'PROOF_NOT_BOUND'):
            self.directory.remember('group',self.payload,dict(proof,message_avatar_sha256='b'*64))
        with self.assertRaisesRegex(ValueError,'PROFILE_CONFLICT'):
            self.directory.remember('group',self.payload,dict(proof,wechat_id='different_id'))

    def test_previous_member_id_is_not_carried_into_a_new_frame(self):
        proof={'stable':True,'wechat_id':'demo_wechat','message_avatar_sha256':'a'*64}
        first=self.directory.enrich('group',[self.payload],frame_sha256='f'*64)[0]
        self.directory.remember('group',first,proof)
        bound=self.directory.enrich('group',[first])[0]
        self.assertTrue(bound['sender_identity_verified'])
        next_frame=self.directory.enrich('group',[bound],frame_sha256='d'*64)[0]
        self.assertNotIn('sender_wechat_id',next_frame)
        self.assertFalse(next_frame['sender_identity_verified'])

    def occurrence_payload(self, pixel, y, direction='incoming'):
        return {'direction':direction,'message_kind':'text','text':pixel,
                'reply_safe':direction=='incoming','ocr':{'stable':True},
                'group_sender_name':'Demo User' if direction=='incoming' else None,
                'group_sender_verified':direction=='incoming',
                'visual':{'sha256':pixel*64,'verified':True,'box':[546,y,90,40],
                          'avatar_verified':True,'avatar_sha256':'a'*64,
                          'avatar_box':[500,y,36,36], 'avatar_color_order':'RGB',
                          'avatar_source':'same_frame_message_avatar'}}

    def occurrence_source(self):
        original=[self.occurrence_payload('b',100),self.occurrence_payload('c',200)]
        original=self.directory.enrich('group',original,frame_sha256='f'*64)
        proof={'stable':True,'wechat_id':'demo_wechat','display_name':'Demo User',
               'message_avatar_sha256':'a'*64,'message_frame_sha256':'1'*64,
               'message_identity':'2'*64,'profile_sha256':'3'*64}
        self.directory.remember('group',original[0],proof)
        original=self.directory.enrich('group',original)
        source=self.store.snapshot('group','group','0'*64,'old.png',original)
        current=copy.deepcopy(original)
        for item in current:
            item['visual']['box'][1]-=50
            item['visual']['avatar_box'][1]-=50
        # A fresh identical bubble from the same visual member must not borrow
        # the first occurrence's profile merely because its glyphs match.
        current.append(self.occurrence_payload('b',300))
        current=self.directory.enrich('group',current,frame_sha256='d'*64)
        reconciliation=dict(reconcile_visual(original,current),previous_snapshot_id=source['snapshot_id'])
        return original,current,reconciliation,proof

    def test_shifted_exact_occurrence_retains_profile_and_current_pixel_binding(self):
        original,current,result,proof=self.occurrence_source()
        shifted=self.directory.transfer_occurrences('group',current,result)
        self.assertEqual(shifted[0]['sender_wechat_id'],'demo_wechat')
        self.assertTrue(shifted[0]['sender_identity_verified'])
        self.assertNotEqual(shifted[0]['sender_observation_key'],original[0]['sender_observation_key'])
        self.assertEqual(shifted[0]['sender_identity_verified_at'],original[0]['sender_identity_verified_at'])
        stored=self.store.db.execute('SELECT proof_json FROM message_sender_observations WHERE observation_key=?',
                                    (shifted[0]['sender_observation_key'],)).fetchone()
        stored=json.loads(stored[0])
        self.assertEqual(stored['profile_sha256'],proof['profile_sha256'])
        self.assertEqual(stored['message_frame_sha256'],proof['message_frame_sha256'])
        transfer=stored['occurrence_transfer']
        self.assertEqual(transfer['source_observation_key'],original[0]['sender_observation_key'])
        self.assertEqual(transfer['source_snapshot_id'],result['previous_snapshot_id'])
        self.assertEqual(transfer['current_frame_sha256'],'d'*64)
        self.assertEqual(transfer['bubble_box'],current[0]['visual']['box'])
        self.assertEqual(transfer['avatar_box'],current[0]['visual']['avatar_box'])
        self.assertNotIn('sender_wechat_id',shifted[1])
        self.assertNotIn('sender_wechat_id',shifted[2])

    def test_occurrence_transfer_rejects_gap_ambiguity_and_wrong_mapping(self):
        _,current,result,_=self.occurrence_source()
        for status in ('gap','ambiguous','baseline','review','unread_recovery'):
            with self.subTest(status=status):
                rows=self.directory.transfer_occurrences('group',current,dict(result,status=status))
                self.assertNotIn('sender_wechat_id',rows[0])
        wrong=copy.deepcopy(result)
        wrong['metadata']['matched_occurrences']={'0':1,'1':0}
        self.assertNotIn('sender_wechat_id',self.directory.transfer_occurrences('group',current,wrong)[0])

    def test_occurrence_transfer_requires_current_frame_observation(self):
        _,current,result,_=self.occurrence_source()
        for key,value in [('sender_observation_key','0'*64),('sender_observation_frame_sha256',None)]:
            changed=copy.deepcopy(current);changed[0][key]=value
            with self.subTest(key=key):
                self.assertNotIn('sender_wechat_id',self.directory.transfer_occurrences('group',changed,result)[0])

    def test_fresh_tail_keeps_its_own_profile_proof(self):
        _,current,result,proof=self.occurrence_source()
        own_proof=dict(proof,message_frame_sha256='4'*64,profile_sha256='5'*64)
        self.directory.remember('group',current[2],own_proof)
        transferred=self.directory.transfer_occurrences('group',current,result)
        self.assertEqual(transferred[2]['sender_wechat_id'],'demo_wechat')
        stored=json.loads(self.store.db.execute('SELECT proof_json FROM message_sender_observations WHERE observation_key=?',
                                               (transferred[2]['sender_observation_key'],)).fetchone()[0])
        self.assertEqual(stored['profile_sha256'],'5'*64)
        self.assertNotIn('occurrence_transfer',stored)

    def test_occurrence_transfer_rejects_avatar_name_and_pixel_changes(self):
        _,current,result,_=self.occurrence_source()
        changes=[('avatar_sha256','e'*64),('avatar_verified',False),
                 ('avatar_source','conversation_avatar'),('avatar_color_order','BGR'),
                 ('avatar_box',[500,50,40,40]),('sha256','9'*64),('box',[546,50,91,40])]
        for key,value in changes:
            changed=copy.deepcopy(current);changed[0]['visual'][key]=value
            changed=self.directory.enrich('group',changed,frame_sha256='d'*64)
            with self.subTest(key=key):
                self.assertNotIn('sender_wechat_id',self.directory.transfer_occurrences('group',changed,result)[0])
        for changes in [{'group_sender_name':'Another Tom'},{'group_sender_verified':False},{'direction':'outgoing'}]:
            changed=copy.deepcopy(current);changed[0].update(changes)
            changed=self.directory.enrich('group',changed,frame_sha256='d'*64)
            with self.subTest(changes=changes):
                self.assertNotIn('sender_wechat_id',self.directory.transfer_occurrences('group',changed,result)[0])

    def test_occurrence_transfer_requires_same_conversation_and_source_profile_row(self):
        original,current,result,_=self.occurrence_source()
        other=self.directory.enrich('other group',current,frame_sha256='d'*64)
        self.assertNotIn('sender_wechat_id',self.directory.transfer_occurrences('other group',other,result)[0])
        self.store.db.execute('DELETE FROM message_sender_observations WHERE observation_key=?',
                              (original[0]['sender_observation_key'],));self.store.db.commit()
        # The global visual member row still exists and must not be used.
        self.assertIsNotNone(self.store.db.execute('SELECT wechat_id FROM message_senders').fetchone())
        self.assertNotIn('sender_wechat_id',self.directory.transfer_occurrences('group',current,result)[0])

    def test_repeated_transfers_preserve_original_profile_anchor_without_nested_proofs(self):
        original,current,result,proof=self.occurrence_source()
        transferred=self.directory.transfer_occurrences('group',current,result)
        source=self.store.snapshot('group','group','0'*64,'middle.png',transferred)
        last=copy.deepcopy(transferred)
        for item in last:
            item['visual']['box'][1]+=10;item['visual']['avatar_box'][1]+=10
        last=self.directory.enrich('group',last,frame_sha256='e'*64)
        result=dict(reconcile_visual(transferred,last),previous_snapshot_id=source['snapshot_id'])
        last=self.directory.transfer_occurrences('group',last,result)
        self.assertEqual(last[0]['sender_wechat_id'],'demo_wechat')
        stored=json.loads(self.store.db.execute('SELECT proof_json FROM message_sender_observations WHERE observation_key=?',
                                               (last[0]['sender_observation_key'],)).fetchone()[0])
        self.assertEqual(stored['occurrence_transfer']['profile_observation_key'],original[0]['sender_observation_key'])
        self.assertEqual(stored['profile_sha256'],proof['profile_sha256'])
        self.assertEqual(stored['occurrence_transfer']['source_observation_key'],transferred[0]['sender_observation_key'])
        self.assertNotIn('source_proof',stored['occurrence_transfer'])

    def test_legacy_timestamp_upgrade_uses_original_frame_for_identity_transfer(self):
        def scene(shift=0, fresh=False):
            image=Image.new('RGB',(1920,1040),(250,250,250));draw=ImageDraw.Draw(image)
            values=[]
            clocks=['14:46','15:25','16:40']+(['18:03'] if fresh else [])
            for index,clock in enumerate(clocks):
                y=100+index*150+shift
                draw.text((1175,y),clock,fill=(128,128,128))
                time_box=[1170,y-2,48,20]
                values.append({'direction':'time','message_kind':'time','text':clock,
                               'ocr':{'stable':True},'visual':{'box':time_box,'verified':False}})
                draw.rectangle((545,y+50,634,y+89),fill=(255,255,255))
                draw.text((555,y+60),'/menu',fill=(0,0,0))
                draw.rectangle((500,y+29,535,y+64),fill=(210,100,30))
                item=self.occurrence_payload('b',y+50)
                item['visual']['avatar_box']=[500,y+29,36,36]
                values.append(item)
            rgb=np.asarray(image)
            for item in values:
                v=item['visual'];x,y,w,h=v['box']
                v['sha256']=hashlib.sha256(rgb[y:y+h,x:x+w].tobytes()).hexdigest()
                if item['direction']=='incoming':
                    x,y,w,h=v['avatar_box']
                    v['avatar_sha256']=hashlib.sha256(rgb[y:y+h,x:x+w].tobytes()).hexdigest()
            return image,values
        picture,legacy=scene()
        frame=Path(self.temp.name)/'original-frame.png';picture.save(frame)
        legacy=self.directory.enrich('group',legacy,frame_sha256='f'*64)
        proof={'stable':True,'wechat_id':'demo_wechat','message_avatar_sha256':legacy[1]['visual']['avatar_sha256']}
        self.directory.remember('group',legacy[1],proof)
        source=self.store.snapshot('group','group','0'*64,str(frame),legacy)
        # Restore the pre-upgrade record, just as a deployed old snapshot lacks
        # time_verified/anchor fields while preserving its real original PNG.
        self.store.db.execute('UPDATE snapshot_payloads SET payloads_json=? WHERE snapshot_id=?',
                              (json.dumps(legacy),source['snapshot_id']));self.store.db.commit()
        current_image,current=scene(shift=10,fresh=True)
        current=verify_time_anchors(np.asarray(current_image),current,color_order='RGB')
        current=self.directory.enrich('group',current,frame_sha256='d'*64)
        upgraded=self.store.verified_snapshot_payloads(source['snapshot_id'])
        result=dict(reconcile_visual(upgraded,current),previous_snapshot_id=source['snapshot_id'])
        self.assertEqual(result['metadata']['strategy'],'exact_time_bubble_avatar_sequence')
        # Keep DB legacy once more to prove SenderDirectory uses Store's source
        # frame loader itself rather than trusting the callback's match map.
        self.store.db.execute('UPDATE snapshot_payloads SET payloads_json=? WHERE snapshot_id=?',
                              (json.dumps(legacy),source['snapshot_id']));self.store.db.commit()
        rows=self.directory.transfer_occurrences('group',current,result)
        self.assertEqual(rows[1]['sender_wechat_id'],'demo_wechat')
        self.assertNotIn('sender_wechat_id',rows[-1])
        self.store.db.execute('UPDATE snapshot_payloads SET payloads_json=? WHERE snapshot_id=?',
                              (json.dumps(legacy),source['snapshot_id']));self.store.db.commit()
        picture.putpixel((546,150),(0,0,0));picture.save(frame)
        another_frame=self.directory.enrich('group',current,frame_sha256='e'*64)
        self.assertNotIn('sender_wechat_id',self.directory.transfer_occurrences('group',another_frame,result)[1])
