import unittest
from unittest.mock import patch
import numpy as np
from sender_ocr import refine_group_senders

class SenderOcrTests(unittest.TestCase):
    def payload(self):
        return {'direction':'incoming','message_kind':'text','text':'/菜单','ocr':{'stable':True},
                'visual':{'box':[545,828,63,36],'verified':True},'reply_safe':False,
                'group_sender_name':'Ton','group_sender_verified':False}
    def hit(self,text,x=550,y=809,score=.99):return {'text':text,'box':[x,y,30,15],'score':score}
    def test_two_local_observations_resolve_without_correcting_old_guess(self):
        original=self.payload()
        with patch('sender_ocr._ocr_scaled',side_effect=[[self.hit('Demo User')],[self.hit('Demo User')]]) as reads:
            result=refine_group_senders(None,np.zeros((1040,1920,3),dtype=np.uint8),[original])[0]
        self.assertEqual(result['group_sender_name'],'Demo User');self.assertTrue(result['reply_safe'])
        self.assertEqual(result['group_sender_ocr']['box'],[550,809,30,15])
        self.assertEqual(len(result['group_sender_ocr']['raw_hits']),2)
        self.assertFalse(original['reply_safe']);self.assertEqual(reads.call_count,2)
    def test_conflict_low_confidence_and_unrelated_geometry_remain_blocked(self):
        for votes in [([self.hit('Demo User')],[self.hit('Ton')]),([self.hit('Demo User',score=.6)],[self.hit('Demo User')]),
                      ([self.hit('Demo User',x=800)],[self.hit('Demo User',x=800)]),
                      ([self.hit('Demo User',y=830)],[self.hit('Demo User',y=830)]),
                      ([self.hit('16:40')],[self.hit('16:40')])]:
            with patch('sender_ocr._ocr_scaled',side_effect=votes):
                self.assertFalse(refine_group_senders(None,np.zeros((1040,1920,3),dtype=np.uint8),[self.payload()])[0]['reply_safe'])
    def test_local_crop_can_recover_a_clipped_whole_frame_nickname(self):
        original=self.payload();original.update(group_sender_name='om',group_sender_verified=True,reply_safe=True)
        with patch('sender_ocr._ocr_scaled',side_effect=[[self.hit('Demo User')],[self.hit('Demo User')]]):
            result=refine_group_senders(None,np.zeros((1040,1920,3),dtype=np.uint8),[original])[0]
        self.assertEqual(result['group_sender_name'],'Demo User')
        self.assertTrue(result['reply_safe'])
    def test_unverified_bubble_does_not_gain_reply_permission_from_nickname(self):
        original=self.payload();original['visual']['verified']=False
        with patch('sender_ocr._ocr_scaled') as reads:
            result=refine_group_senders(None,np.zeros((1040,1920,3),dtype=np.uint8),[original])[0]
        self.assertFalse(result['reply_safe']);reads.assert_not_called()
