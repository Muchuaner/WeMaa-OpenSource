import unittest
from pathlib import Path
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import numpy as np
from PIL import Image

try:
    from maa_wechat.message_vision import (consensus_text, extract_message_payloads,
                                         message_identity, reconcile_visual,verify_time_anchors,
                                         time_identity,message_occurrence_identity)
    from maa_wechat.store import Store
except ModuleNotFoundError:
    from message_vision import (consensus_text, extract_message_payloads,
                                message_identity, reconcile_visual,verify_time_anchors,
                                time_identity,message_occurrence_identity)
    from store import Store


def hit(text, x, y, w=28, h=14, score=.96):
    return {'text': text, 'box': [x, y, w, h], 'score': score}


def canvas():
    return np.full((1040, 1920, 3), 250, dtype=np.uint8)


def bubble(image, x, y, w=84, h=38, outgoing=False):
    image[y:y+h, x:x+w] = (149, 235, 105) if outgoing else (237, 237, 237)
    # Artificial visible glyph pixels, independent of the OCR hypothesis.
    image[y+12:y+25, x+12:x+20] = 25
    image[y+12:y+25, x+24:x+31] = 25


def avatar(image, x, y, shade=40):
    image[y:y+36, x:x+36] = (shade, 120, 185)
    image[y+6:y+28, x+6:x+28] = (180, 65, shade)


def payload(text, fingerprint, direction='incoming', y=100):
    return {'text': text, 'direction': direction, 'message_kind': 'text',
            'visual': {'sha256': fingerprint*64, 'box': [550, y, 80, 38], 'verified': True},
            'reply_safe': direction == 'incoming'}


class ConsensusTests(unittest.TestCase):
    def test_matching_independent_passes_confirm(self):
        result = consensus_text([{'text': '你好', 'score': .96, 'pass_id': 'base'},
                                 {'text': '你好', 'score': .98, 'pass_id': 'upscale'}])
        self.assertTrue(result['stable'])
        self.assertEqual(result['text'], '你好')
        self.assertEqual(len(result['raw_votes']), 2)

    def test_short_chinese_disagreement_is_not_dictionary_corrected(self):
        result = consensus_text([{'text': '你好', 'score': .96, 'pass_id': 'base'},
                                 {'text': '你子', 'score': .98, 'pass_id': 'upscale'}])
        self.assertFalse(result['stable'])
        self.assertIn(result['text'], ('你好', '你子'))

    def test_duplicate_pass_does_not_manufacture_consensus(self):
        result = consensus_text([{'text': '你好', 'score': .96, 'pass_id': 'base'},
                                 {'text': '你好', 'score': .98, 'pass_id': 'base'}])
        self.assertFalse(result['stable'])
        self.assertEqual(result['agreement'], 1)

    def test_confident_majority_can_win_a_third_pass(self):
        result = consensus_text([{'text': '你好', 'score': .96, 'pass_id': 0},
                                 {'text': '你子', 'score': .61, 'pass_id': 1},
                                 {'text': '你好', 'score': .98, 'pass_id': 2}])
        self.assertTrue(result['stable'])
        self.assertEqual(result['text'], '你好')
        self.assertEqual(len(result['raw_votes']), 3)

    def test_low_confidence_matching_text_is_pending(self):
        self.assertFalse(consensus_text([{'text': '你好', 'score': .7},
                                         {'text': '你好', 'score': .98}])['stable'])

    def test_whitespace_only_pass_variation_keeps_observed_text(self):
        result = consensus_text([{'text': '长句\n第二行', 'score': .98},
                                 {'text': '长句 第二行', 'score': .95}])
        self.assertTrue(result['stable'])
        self.assertEqual(result['text'], '长句\n第二行')

    def test_bad_scores_do_not_raise_or_establish_agreement(self):
        result = consensus_text([{'text': '你好', 'score': 'nan'},
                                 {'text': '你好', 'score': 'bad'}])
        self.assertFalse(result['stable'])


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.image = canvas()
        bubble(self.image, 550, 150)
        bubble(self.image, 1760, 250, outgoing=True)
        self.hits = [hit('你好', 562, 162), hit('收到', 1772, 262),
                     hit('20:23', 1183, 215, 34)]

    def extract(self, **kwargs):
        return extract_message_payloads(self.image, self.hits, color_order='RGB', **kwargs)

    def test_incoming_outgoing_and_time_are_geometry_separated(self):
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)])
        self.assertEqual([p['direction'] for p in result], ['incoming', 'time', 'outgoing'])
        self.assertEqual([p['reply_safe'] for p in result], [True, False, False])

    def test_one_pass_never_enables_reply(self):
        self.assertFalse(any(p['reply_safe'] for p in self.extract()))

    def test_bgr_and_rgb_produce_identical_visual_fingerprints(self):
        rgb = self.extract()
        bgr = extract_message_payloads(self.image[:, :, ::-1], self.hits)
        self.assertEqual([p['visual']['sha256'] for p in rgb], [p['visual']['sha256'] for p in bgr])

    def test_ocr_box_padding_jitter_does_not_change_complete_pixel_component(self):
        old = self.extract()[0]
        self.hits[0]['box'] = [563, 164, 27, 12]
        new = self.extract()[0]
        self.assertEqual(message_identity(old), message_identity(new))

    def test_changed_short_ocr_does_not_change_visual_id(self):
        old = self.extract()[0]
        self.hits[0]['text'] = '你子'
        new = self.extract()[0]
        self.assertEqual(message_identity(old), message_identity(new))
        self.assertNotEqual(old['text'], new['text'])

    def test_changed_glyph_pixels_change_visual_id(self):
        old = self.extract()[0]
        self.image[165, 578] = 15
        new = self.extract()[0]
        self.assertNotEqual(message_identity(old), message_identity(new))

    def test_real_short_new_word_is_not_merged_with_other_word(self):
        variants = copy.deepcopy(self.hits)
        variants[0]['text'] = '你子'
        result = self.extract(consensus_passes=[variants])
        self.assertFalse(result[0]['reply_safe'])
        self.assertEqual({v['text'] for v in result[0]['ocr']['raw_votes']}, {'你好', '你子'})

    def test_multiline_bubble_is_one_message(self):
        self.image = canvas()
        bubble(self.image, 550, 150, w=180, h=60)
        self.hits = [hit('第一行', 562, 162, 80), hit('第二行', 562, 182, 80)]
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['text'], '第一行\n第二行')
        self.assertTrue(result[0]['reply_safe'])

    def test_bare_left_label_does_not_gain_incoming_direction(self):
        self.hits.append(hit('20:23', 562, 350, 36))
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)])
        self.assertEqual(result[-1]['direction'], 'unknown')
        self.assertFalse(result[-1]['reply_safe'])

    def test_time_string_inside_verified_bubble_is_user_text(self):
        self.hits[0]['text'] = '20:23'
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)])
        self.assertEqual(result[0]['message_kind'], 'text')
        self.assertTrue(result[0]['reply_safe'])

    def test_partial_bubble_is_not_reply_safe(self):
        self.image = canvas()
        bubble(self.image, 550, 870)
        self.hits = [hit('你好', 562, 882, h=5)]
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)])
        self.assertFalse(result[0]['reply_safe'])

    def test_unsupported_media_label_never_reply_safe(self):
        self.hits[0]['text'] = '[图片]'
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)])
        self.assertEqual(result[0]['message_kind'], 'unsupported')
        self.assertFalse(result[0]['reply_safe'])

    def test_unfamiliar_theme_fails_closed(self):
        self.image = 255-self.image
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)])
        self.assertFalse(any(p['reply_safe'] for p in result))

    def test_bad_roi_raises(self):
        with self.assertRaises(ValueError):
            self.extract(chat_roi=(490, 82, 2000, 805))

    def test_group_nickname_requires_geometry_and_independent_pass_agreement(self):
        self.hits.append(hit('张三', 552, 128, 50))
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)], group_chat=True)
        incoming = next(p for p in result if p['direction'] == 'incoming')
        self.assertEqual(incoming['group_sender_name'], '张三')
        self.assertTrue(incoming['group_sender_verified'])
        self.assertTrue(incoming['reply_safe'])

    def test_group_nickname_disagreement_is_pending_not_wrong_sender(self):
        self.hits.append(hit('张三', 552, 128, 50))
        variants = copy.deepcopy(self.hits)
        variants[-1]['text'] = '张四'
        result = self.extract(consensus_passes=[variants], group_chat=True)
        incoming = next(p for p in result if p['direction'] == 'incoming')
        self.assertFalse(incoming['group_sender_verified'])
        self.assertFalse(incoming['reply_safe'])

    def test_group_missing_nickname_is_not_private_sender_assumption(self):
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)], group_chat=True)
        incoming = next(p for p in result if p['direction'] == 'incoming')
        self.assertIsNone(incoming['group_sender_name'])
        self.assertFalse(incoming['reply_safe'])

    def test_group_centered_timestamp_is_not_borrowed_as_nickname(self):
        self.hits.append(hit('20:23', 552, 128, 50))
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)], group_chat=True)
        incoming = next(p for p in result if p['direction'] == 'incoming')
        self.assertIsNone(incoming['group_sender_name'])

    def test_same_timestamp_and_nickname_are_one_physical_label_across_passes(self):
        self.hits.append(hit('张三', 552, 128, 50))
        second = copy.deepcopy(self.hits)
        for item in second:
            item['box'][0] += 1
            item['box'][1] += 1
        result = self.extract(consensus_passes=[second], group_chat=True)
        self.assertEqual(sum(p['message_kind'] == 'time' for p in result), 1)
        labels = [p for p in result if p['message_kind'] == 'sender_label']
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]['text'], '张三')
        self.assertFalse(labels[0]['reply_safe'])
        incoming = next(p for p in result if p['direction'] == 'incoming')
        self.assertEqual(incoming['group_sender_ocr']['box'], [552, 128, 51, 15])

    def test_conflicting_bare_ocr_variants_are_not_two_apparent_messages(self):
        self.hits.append(hit('张三', 552, 128, 50))
        second = copy.deepcopy(self.hits)
        second[-1].update(text='张四', box=[553, 129, 49, 13])
        result = self.extract(consensus_passes=[second], group_chat=True)
        labels = [p for p in result if p['message_kind'] == 'sender_label']
        self.assertEqual(len(labels), 1)
        self.assertFalse(labels[0]['ocr']['stable'])
        self.assertEqual({v['text'] for v in labels[0]['ocr']['raw_votes']}, {'张三', '张四'})
        self.assertFalse(next(p for p in result if p['direction'] == 'incoming')['reply_safe'])

    def test_weekday_dash_ocr_only_classifies_centered_bare_time(self):
        self.hits[2]['text'] = '星期—15:20'
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)])
        self.assertEqual(result[1]['message_kind'], 'time')
        self.hits[0]['text'] = '星期—15:20'
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)])
        self.assertEqual(result[0]['message_kind'], 'text')

    def test_message_avatars_are_distinct_from_conversation_avatar_and_nickname(self):
        avatar(self.image, 500, 129)
        avatar(self.image, 1858, 250, shade=80)
        self.hits.append(hit('张三', 552, 128, 50))
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)], group_chat=True)
        incoming = next(p for p in result if p['direction'] == 'incoming')
        outgoing = next(p for p in result if p['direction'] == 'outgoing')
        self.assertEqual(incoming['visual']['avatar_box'], [500, 129, 36, 36])
        self.assertEqual(outgoing['visual']['avatar_box'], [1858, 250, 36, 36])
        self.assertTrue(incoming['visual']['avatar_verified'])
        self.assertEqual(incoming['visual']['avatar_color_order'], 'RGB')
        self.assertNotEqual(incoming['visual']['avatar_sha256'], outgoing['visual']['avatar_sha256'])
        bgr = extract_message_payloads(self.image[:, :, ::-1], self.hits,
                                      consensus_passes=[copy.deepcopy(self.hits)], group_chat=True)
        self.assertEqual(incoming['visual']['avatar_sha256'],
                         next(p for p in bgr if p['direction'] == 'incoming')['visual']['avatar_sha256'])

    def test_missing_or_partial_avatar_does_not_manufacture_portrait(self):
        avatar(self.image, 500, 129)
        self.image[129:139, 500:536] = 250
        result = self.extract(group_chat=True)
        incoming = next(p for p in result if p['direction'] == 'incoming')
        self.assertFalse(incoming['visual']['avatar_verified'])
        self.assertNotIn('avatar_box', incoming['visual'])
        self.assertFalse(next(p for p in result if p['direction'] == 'outgoing')['visual']['avatar_verified'])

    def test_ocr_of_padded_portrait_is_evidence_not_a_new_user_message(self):
        avatar(self.image, 500, 129)
        self.hits.extend([hit('张三',552,128,50),hit('8',495,125,42,43,score=.65)])
        result = self.extract(consensus_passes=[copy.deepcopy(self.hits)],group_chat=True)
        portrait = next(p for p in result if p['text']=='8')
        incoming = next(p for p in result if p['direction']=='incoming')
        self.assertEqual(portrait['message_kind'],'avatar_ocr')
        self.assertEqual(portrait['label_for_visual_sha256'],incoming['visual']['sha256'])
        self.assertFalse(portrait['reply_safe'])


class RealFrameTests(unittest.TestCase):
    def test_real_repeated_menu_append_uses_three_exact_timestamp_bubble_portrait_anchors(self):
        directory=Path(__file__).resolve().parent.parent/'artifacts/wechat-group-identity-sep30'
        proof=directory/'gap-context-full.json'
        if not proof.is_file() or not (directory/'snapshot-3514.png').is_file() or not (directory/'snapshot-3515.png').is_file():
            self.skipTest('Real repeated-menu frames are an optional offline fixture')
        rows=json.loads(proof.read_text())['snapshots']
        values=[]
        for row in rows:
            rgb=np.asarray(Image.open(directory/('snapshot-'+str(row['id'])+'.png')).convert('RGB'))
            values.append(verify_time_anchors(rgb,json.loads(row['payloads_json']),color_order='RGB'))
        old,current=values
        result=reconcile_visual(old,current)
        self.assertEqual(result['status'],'append')
        self.assertEqual(result['overlap'],14)
        self.assertEqual(result['metadata']['strategy'],'exact_time_bubble_avatar_sequence')
        self.assertEqual(result['metadata']['translation'],[0,-392])
        self.assertEqual(result['metadata']['matched_occurrences'],{'5':15,'9':19,'13':23})
        self.assertEqual([(p['text'],p['direction']) for p in result['new'] if message_identity(p)],
                         [('[图片/视频]','outgoing'),('/菜单','incoming')])
        self.assertIs(result['new'][-1],current[-1])
        self.assertEqual(current[-1]['time_anchor']['text'],'18:03')
        self.assertEqual(message_occurrence_identity(old[23]),message_occurrence_identity(current[13]))
        self.assertNotEqual(message_occurrence_identity(old[23]),message_occurrence_identity(current[19]))

    def test_real_windows_group_frame_separates_people_time_and_sent_avatar(self):
        directory = Path(__file__).resolve().parent.parent/'artifacts/wechat-stall-sep30'
        frame = directory/'maa-recovered-sep30-send-f222f1ae2a8b5fc8-after-0.png'
        if not frame.is_file():
            self.skipTest('Real Windows group frame is an optional offline fixture')
        rgb = np.asarray(Image.open(frame).convert('RGB'))
        # OCR boxes correspond to the visible rows in this frozen Windows frame.
        # The duplicated fixture pass exercises grouping, not real OCR accuracy.
        hits = [hit('Demo User',553,127,20,12),hit('这些图和视频都是自动发的',559,154,173,19),
                hit('星期—15:20',1162,204,73,17),hit('13:47',1184,481,28,12),
                hit('AZIK-',551,517,33,16),hit('@创作平台-AI你好',561,545,101,20),
                hit('AZIK-',550,593,35,17),hit('/菜单',561,623,36,18),
                hit('13:56',1183,677,29,12),hit('Demo User',552,715,20,12),
                hit('我去看看启动了没有',559,740,131,21),
                hit('14:13',1183,794,29,12),
                hit('请问具体是指哪个应用或服务的启动状态？',1566,840,266,18)]
        result = extract_message_payloads(rgb,hits,color_order='RGB',
                                          consensus_passes=[copy.deepcopy(hits)],group_chat=True)
        incoming = [p for p in result if p['direction']=='incoming']
        self.assertEqual([p['text'] for p in incoming],
                         ['这些图和视频都是自动发的','@创作平台-AI你好','/菜单','我去看看启动了没有'])
        self.assertEqual([p['group_sender_name'] for p in incoming],['Demo User','AZIK-','AZIK-','Demo User'])
        self.assertTrue(all(p['visual']['avatar_verified'] for p in incoming))
        self.assertEqual(incoming[0]['visual']['avatar_sha256'],incoming[3]['visual']['avatar_sha256'])
        self.assertEqual(incoming[1]['visual']['avatar_sha256'],incoming[2]['visual']['avatar_sha256'])
        self.assertNotEqual(incoming[0]['visual']['avatar_sha256'],incoming[1]['visual']['avatar_sha256'])
        self.assertEqual(sum(p['message_kind']=='time' for p in result),4)
        self.assertEqual(sum(p['message_kind']=='sender_label' for p in result),4)
        outgoing = next(p for p in result if p['direction']=='outgoing' and p['message_kind']=='text')
        self.assertEqual(outgoing['visual']['avatar_box'],[1858,828,36,36])
        self.assertNotIn(outgoing['visual']['avatar_sha256'],
                         [p['visual']['avatar_sha256'] for p in incoming])

    @unittest.skipUnless((Path(__file__).parent / 'message_vision_fixture.json').is_file(), "私有视觉夹具不随开源版发布")
    def test_real_tom_recovery_frame_geometry_and_short_word_evidence(self):
        directory = Path(__file__).resolve().parent
        fixture = json.loads((directory/'message_vision_fixture.json').read_text(encoding='utf-8'))
        archive = directory.parent/fixture['archive']
        if not archive.is_file():
            self.skipTest('Real recovery archive is an optional offline acceptance fixture')
        with zipfile.ZipFile(archive) as zipped:
            rgb = np.asarray(Image.open(io.BytesIO(zipped.read(fixture['frame']))).convert('RGB'))
        hits = fixture['ocr_hits']
        payloads = extract_message_payloads(rgb, hits, color_order='RGB')
        self.assertEqual([p['text'] for p in payloads if p['direction']=='incoming'],
                         fixture['expected_incoming'])
        self.assertEqual(sum(p['direction']=='time' for p in payloads), fixture['expected_time_count'])
        self.assertEqual(sum(p['direction']=='outgoing' for p in payloads), fixture['expected_outgoing_count'])
        # A stored single OCR pass validates geometry, not independent consensus.
        self.assertFalse(any(p['reply_safe'] for p in payloads))
        altered = copy.deepcopy(hits)
        next(h for h in altered if h['text']=='你好')['text'] = '你子'
        noisy = extract_message_payloads(rgb, altered, color_order='RGB')
        result = reconcile_visual(payloads, noisy)
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(result['new'], [])

    @unittest.skipUnless((Path(__file__).parent / 'message_vision_fixture.json').is_file(), "私有视觉夹具不随开源版发布")
    def test_real_tom_scrolled_append_survives_short_historical_ocr_noise(self):
        directory = Path(__file__).resolve().parent
        fixture = json.loads((directory/'message_vision_fixture.json').read_text(encoding='utf-8'))
        archive = directory.parent/fixture['archive']
        if not archive.is_file():
            self.skipTest('Real recovery archive is an optional offline acceptance fixture')
        with zipfile.ZipFile(archive) as zipped:
            old_rgb = np.asarray(Image.open(io.BytesIO(zipped.read(fixture['previous_frame']))).convert('RGB'))
            new_rgb = np.asarray(Image.open(io.BytesIO(zipped.read(fixture['frame']))).convert('RGB'))
        previous = extract_message_payloads(old_rgb, fixture['previous_ocr_hits'], color_order='RGB')
        altered = copy.deepcopy(fixture['ocr_hits'])
        next(h for h in altered if h['text']=='你好')['text'] = '你子'
        current = extract_message_payloads(new_rgb, altered, color_order='RGB')
        result = reconcile_visual(previous, current)
        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'append')
        self.assertEqual([p['text'] for p in result['new']], ['17:18', 'MAA0926A'])
        noise = result['metadata']['historical_ocr_variants']
        self.assertTrue(any(n['previous_text']=='你好' and n['current_text']=='你子' for n in noise))


class TimestampOccurrenceTests(unittest.TestCase):
    def frame(self,times,start=140):
        image=canvas();hits=[]
        seeds={'14:46':1,'15:25':2,'16:40':3,'18:03':4}
        for index,text in enumerate(times):
            ty=start+index*118;by=ty+53
            yy,xx=np.indices((9,30))
            mask=((xx+yy*seeds[text])%5)<2
            image[ty:ty+9,1183:1213][mask]=130
            bubble(image,545,by,w=63,h=36)
            avatar(image,500,by-21)
            hits.extend([hit(text,1178,ty-6,37,19),hit('Demo User',552,by-22,25,14),
                         hit('/菜单',557,by+12,28,14)])
        second=copy.deepcopy(hits)
        for value in second:value['box'][0]+=1;value['box'][1]+=1
        payloads=extract_message_payloads(image,hits,color_order='RGB',consensus_passes=[second],group_chat=True)
        return image,payloads

    def incoming(self,items):return [p for p in items if p['direction']=='incoming']

    def test_repeated_same_count_scrolled_body_sequence_is_append_not_unchanged(self):
        _,old=self.frame(['14:46','15:25','16:40'],400)
        _,current=self.frame(['15:25','16:40','18:03'],282)
        self.assertEqual([message_identity(p) for p in self.incoming(old)],
                         [message_identity(p) for p in self.incoming(current)])
        result=reconcile_visual(old,current)
        self.assertEqual(result['status'],'append')
        self.assertEqual(result['metadata']['translation'],[0,-236])
        self.assertEqual([p['time_anchor']['text'] for p in result['new'] if p['direction']=='incoming'],['18:03'])
        self.assertIs(self.incoming(result['new'])[0],self.incoming(current)[-1])

    def test_same_complete_timed_occurrences_are_unchanged_with_mapping(self):
        _,items=self.frame(['14:46','15:25','16:40'])
        result=reconcile_visual(items,copy.deepcopy(items))
        self.assertEqual(result['status'],'unchanged')
        self.assertEqual(result['new'],[])
        self.assertEqual(len(result['metadata']['matched_occurrences']),3)

    def distinct_bodies(self,items):
        # Independent exact pixel tokens exercise the distinct-body fallback;
        # all time/portrait/layout evidence still comes from the frame fixture.
        for index,message in enumerate(self.incoming(items)):
            message['visual']['sha256']=('a','b')[index]*64
        return items

    def test_distinct_body_reposts_at_new_times_cannot_borrow_old_occurrences(self):
        _,old=self.frame(['14:46','15:25'])
        _,current=self.frame(['16:40','18:03'])
        self.distinct_bodies(old);self.distinct_bodies(current)
        self.assertEqual([message_identity(p) for p in self.incoming(old)],
                         [message_identity(p) for p in self.incoming(current)])
        self.assertNotEqual(message_occurrence_identity(self.incoming(old)[0]),
                            message_occurrence_identity(self.incoming(current)[0]))
        for append in (False,True):
            candidate=current+[payload('新消息','c',y=800)] if append else current
            with self.subTest(append=append):
                self.assertIsNone(reconcile_visual(old,candidate))

    def test_distinct_timed_bodies_with_incompatible_scroll_translation_fail_closed(self):
        _,old=self.frame(['14:46','15:25'],400)
        _,current=self.frame(['14:46','15:25'],282)
        self.distinct_bodies(old);self.distinct_bodies(current)
        second=self.incoming(current)[1]
        for box in (second['visual']['box'],second['visual']['avatar_box'],second['time_anchor']['box']):
            box[1]+=1
        self.assertEqual([message_occurrence_identity(p) for p in self.incoming(old)],
                         [message_occurrence_identity(p) for p in self.incoming(current)])
        for append in (False,True):
            candidate=current+[payload('新消息','c',y=800)] if append else current
            with self.subTest(append=append):
                self.assertIsNone(reconcile_visual(old,candidate))

    def test_time_upgrade_is_read_only_and_ignores_ocr_padding_jitter(self):
        image,items=self.frame(['14:46','15:25'])
        source=copy.deepcopy(items)
        for p in source:
            p.pop('time_anchor',None)
            if p['direction']=='time':
                for key in ('time_box','time_sha256','time_verified','time_source','time_color_order'):
                    p['visual'].pop(key,None)
        before=copy.deepcopy(source)
        upgraded=verify_time_anchors(image,source,color_order='RGB')
        self.assertEqual(source,before)
        self.assertEqual([p['reply_safe'] for p in upgraded],[p['reply_safe'] for p in source])
        for original,recovered in zip([p for p in items if p['direction']=='time'],
                                      [p for p in upgraded if p['direction']=='time']):
            self.assertEqual(time_identity(original),time_identity(recovered))

    def test_missing_unstable_or_changed_time_pixels_cannot_locate_repeated_body(self):
        _,old=self.frame(['14:46','15:25','16:40'],400)
        image,current=self.frame(['15:25','16:40','18:03'],282)
        for mode in ('unstable','missing','changed_pixels'):
            with self.subTest(mode=mode):
                changed=copy.deepcopy(current);frame=image.copy()
                if mode=='unstable':
                    for p in changed:
                        if p['direction']=='time':p['ocr']['stable']=False
                elif mode=='missing':
                    frame[282:291,1183:1213]=250
                else:frame[283,1184]=110
                changed=verify_time_anchors(frame,changed,color_order='RGB')
                self.assertIsNone(reconcile_visual(old,changed))

    def test_one_clock_or_same_minute_repetitions_are_not_enough(self):
        for left,right in ((['14:46'],['14:46','18:03']),
                           (['14:46']*3,['14:46']*4),
                           (['14:46','15:25','14:46','15:25'],['14:46','15:25','18:03'])):
            with self.subTest(left=left,right=right):
                _,old=self.frame(left);_,current=self.frame(right)
                self.assertIsNone(reconcile_visual(old,current))

    def test_avatar_or_relative_layout_conflict_rejects_exact_clock_and_body(self):
        _,old=self.frame(['14:46','15:25','16:40'],400)
        _,current=self.frame(['15:25','16:40','18:03'],282)
        for mode in ('portrait','translation'):
            changed=copy.deepcopy(current)
            message=self.incoming(changed)[0]
            if mode=='portrait':message['visual']['avatar_sha256']='f'*64
            else:
                message['visual']['avatar_box'][1]+=1
                message['time_anchor']['relative_avatar'][1]+=1
            with self.subTest(mode=mode):self.assertIsNone(reconcile_visual(old,changed))

    def test_timestamp_cannot_be_borrowed_through_another_bubble(self):
        image,items=self.frame(['14:46'])
        first=self.incoming(items)[0]
        second=copy.deepcopy(first);second['visual']['box'][1]+=50;second['visual']['avatar_box'][1]+=50
        items.append(second)
        upgraded=verify_time_anchors(image,items,color_order='RGB')
        self.assertIsNotNone(message_occurrence_identity(self.incoming(upgraded)[0]))
        self.assertIsNone(message_occurrence_identity(self.incoming(upgraded)[1]))

    def test_unverified_intervening_clock_blocks_borrowing_an_older_time(self):
        image,items=self.frame(['14:46'])
        clock={'text':'15:25','direction':'time','message_kind':'time','ocr':{'stable':False},
               'visual':{'box':[1178,166,37,19],'verified':False},'reply_safe':False}
        items.insert(1,clock)
        upgraded=verify_time_anchors(image,items,color_order='RGB')
        self.assertIsNone(message_occurrence_identity(self.incoming(upgraded)[0]))


class VisualOverlapTests(unittest.TestCase):
    def test_verified_portrait_conflicts_block_both_unchanged_and_append_body_fallbacks(self):
        old=[payload('历史','a',y=100),payload('你好','b',y=200)]
        for p in old:
            p['visual'].update(avatar_verified=True,avatar_sha256='d'*64,
                               avatar_box=[500,p['visual']['box'][1]-21,36,36])
        for mode in ('pixels','size','relative_position'):
            for append in (False,True):
                current=copy.deepcopy(old)
                avatar=current[0]['visual']
                if mode=='pixels':avatar['avatar_sha256']='f'*64
                elif mode=='size':avatar['avatar_box'][2]+=1
                else:avatar['avatar_box'][1]+=1
                if append:current.append(payload('新消息','c',y=300))
                with self.subTest(mode=mode,append=append):
                    self.assertIsNone(reconcile_visual(old,current))

    def test_unverified_portraits_do_not_invent_conflicting_identity_evidence(self):
        old=[payload('历史','a'),payload('你好','b')]
        current=copy.deepcopy(old)+[payload('新消息','c',y=300)]
        for p in current[:2]:
            p['visual'].update(avatar_verified=False,avatar_sha256='f'*64,
                               avatar_box=[500,79,36,36])
        result=reconcile_visual(old,current)
        self.assertEqual(result['status'],'append')
        self.assertEqual(result['metadata']['matched_occurrences'],{'0':0,'1':1})

    def test_nested_card_rectangle_is_one_physical_history_anchor(self):
        card=payload('邀请卡片','a');card['visual']['box']=[545,307,252,93]
        fragment=payload('看详情','d');fragment['visual']['box']=[545,307,206,93]
        menu=payload('/菜单','b');media=payload('图片','c','outgoing')
        previous=[media,card,menu,media]
        current=[card,fragment,menu,media,payload('新消息','e')]
        result=reconcile_visual(previous,current)
        self.assertEqual(result['status'],'append')
        self.assertEqual(result['overlap'],4)
        self.assertEqual([p['text'] for p in result['new']],['新消息'])

    def test_vertically_separate_or_opposite_bubble_is_not_a_card_fragment(self):
        card=payload('卡片','a');card['visual']['box']=[545,307,252,93]
        menu=payload('菜单','b')
        for direction,y in [('outgoing',307),('incoming',340)]:
            other=payload('实际消息','d',direction);other['visual']['box']=[545,y,206,93]
            self.assertIsNone(reconcile_visual([card,menu],[card,other,menu,payload('新','e')]))

    def test_short_historical_ocr_noise_keeps_new_tail_verbatim(self):
        previous = [payload('历史长消息', 'a'), payload('你好', 'b')]
        current = [payload('历史长消息', 'a', y=90), payload('你子', 'b', y=150),
                   {'text': '20:23', 'direction': 'time'}, payload('你好', 'c', y=260)]
        result = reconcile_visual(previous, current, {'status': 'gap'})
        self.assertEqual(result['status'], 'append')
        self.assertEqual([p['text'] for p in result['new']], ['20:23', '你好'])
        self.assertIs(result['new'][-1], current[-1])
        self.assertEqual(result['metadata']['historical_ocr_variants'][0]['proof'], 'identical_bubble_pixels')

    def test_different_pixels_with_same_or_similar_word_are_not_aligned(self):
        old = [payload('历史', 'a'), payload('你好', 'b')]
        for text in ('你好', '你子'):
            current = [payload('历史', 'a'), payload(text, 'c'), payload('新消息', 'd')]
            self.assertIsNone(reconcile_visual(old, current))

    def test_visual_ocr_changes_alone_are_unchanged(self):
        old = [payload('历史', 'a'), payload('你好', 'b')]
        current = [payload('历史', 'a'), payload('你子', 'b')]
        result = reconcile_visual(old, current)
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(result['new'], [])

    def test_identical_repeated_messages_remain_repeated_in_new_tail(self):
        old = [payload('历史', 'a'), payload('你好', 'b')]
        current = old + [payload('相同消息', 'c'), payload('相同消息', 'c')]
        result = reconcile_visual(old, current)
        self.assertEqual([p['text'] for p in result['new']], ['相同消息', '相同消息'])

    def test_repeated_sequence_cannot_locate_new_occurrence(self):
        old = [payload('甲', 'a'), payload('乙', 'b')]*2
        current = [payload('甲', 'a'), payload('乙', 'b'), payload('新', 'c')]
        self.assertIsNone(reconcile_visual(old, current))

    def test_only_repeated_identical_greetings_need_distinguishable_context(self):
        old = [payload('你好', 'a')]*3
        current = [payload('你好', 'a')]*4
        self.assertIsNone(reconcile_visual(old, current))

    def test_one_visual_anchor_not_enough(self):
        self.assertIsNone(reconcile_visual([payload('你好', 'a')],
                                          [payload('你好', 'a'), payload('新', 'b')]))

    def test_legacy_line_schema_not_treated_as_new_user_messages(self):
        self.assertIsNone(reconcile_visual([{'text': '你好', 'sender': 'unresolved'}],
                                          [payload('你好', 'a'), payload('新', 'b')]))

    def test_baseline_requires_caller_to_suppress_historical_replies(self):
        self.assertIsNone(reconcile_visual(None, [payload('旧消息', 'a')]))

    def test_opposite_direction_never_same_message(self):
        old = [payload('历史', 'a'), payload('你好', 'b')]
        current = [payload('历史', 'a'), payload('你好', 'b', 'outgoing'), payload('新', 'c')]
        self.assertIsNone(reconcile_visual(old, current))

    def test_visual_token_rejects_invalid_or_unverified_evidence(self):
        for change in ({'verified': False}, {'sha256': 'bad'}, {'box': [550]}):
            p = payload('你好', 'a')
            p['visual'].update(change)
            self.assertIsNone(message_identity(p))


class StoreVisionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='maa-store-vision-')
        self.store = Store(Path(self.temporary.name)/'wechat.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.temporary.cleanup()

    def snapshot(self, payloads):
        return self.store.snapshot('tom-visual-test', 'Demo User', 'avatar-evidence',
                                   'read-only-fixture-frame.png', payloads)

    def messages(self):
        return [json.loads(row[0]) for row in self.store.db.execute(
            'SELECT payload_json FROM messages ORDER BY snapshot_id,ordinal')]

    def test_legacy_line_to_visual_is_history_baseline_not_reply(self):
        legacy = [{'text': '你好', 'sender': 'unresolved', 'source': 'maa_ocr_line'}]
        self.assertEqual(self.snapshot(legacy)['status'], 'baseline')
        result = self.snapshot([payload('历史甲', 'a'), payload('你好', 'b')])
        self.assertEqual(result['status'], 'baseline')
        self.assertEqual(result['new'], [])
        self.assertEqual(self.messages(), [])

    def test_identical_historical_pixels_with_different_short_ocr_not_new(self):
        self.snapshot([payload('历史甲', 'a'), payload('你好', 'b')])
        result = self.snapshot([payload('历史甲', 'a'), payload('你子', 'b')])
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(self.messages(), [])

    def test_pending_incoming_does_not_commit_anchor_then_stable_appends_once(self):
        baseline = [payload('历史甲', 'a'), payload('历史乙', 'b')]
        self.snapshot(baseline)
        unstable = payload('你子', 'c')
        unstable['reply_safe'] = False
        unstable['ocr'] = {'stable': False}
        review = self.snapshot(baseline+[unstable])
        self.assertEqual(review['status'], 'review')
        self.assertEqual(review['new'], [])
        self.assertEqual(self.messages(), [])
        stable = payload('你好', 'c')
        stable['ocr'] = {'stable': True}
        append = self.snapshot(baseline+[stable])
        self.assertEqual(append['status'], 'append')
        self.assertEqual([p['text'] for p in self.messages()], ['你好'])
        self.assertEqual(self.snapshot(baseline+[stable])['status'], 'unchanged')
        self.assertEqual([p['text'] for p in self.messages()], ['你好'])

    def test_genuine_identical_new_messages_are_not_global_text_deduplicated(self):
        baseline = [payload('历史甲', 'a'), payload('历史乙', 'b')]
        self.snapshot(baseline)
        current = baseline+[payload('你好', 'c'), payload('你好', 'c')]
        result = self.snapshot(current)
        self.assertEqual(result['status'], 'append')
        self.assertEqual([p['text'] for p in self.messages()], ['你好', '你好'])
        self.assertEqual(self.snapshot(current)['status'], 'unchanged')
        self.assertEqual(len(self.messages()), 2)

    def test_metadata_and_absolute_positions_do_not_create_new_messages(self):
        baseline = [payload('历史甲', 'a'), payload('你好', 'b')]
        self.snapshot(baseline)
        current = copy.deepcopy(baseline)
        for p in current:
            p['visual']['box'][0] += 1
            p['visual']['box'][1] -= 100
            p['visual']['fill_fraction'] = .987
            p['ocr'] = {'stable': True, 'confidence': .985, 'raw_votes': ['new metadata']}
            p['group_sender_name'] = None
            p['source'] = 'maa_bubble_ocr'
        self.assertEqual(self.snapshot(current)['status'], 'unchanged')
        self.assertEqual(self.messages(), [])

    def test_one_visual_anchor_cannot_be_rescued_by_strict_hash_append(self):
        self.snapshot([payload('你好', 'a')])
        result = self.snapshot([payload('你好', 'a'), payload('新消息', 'b')])
        self.assertIn(result['status'], ('gap', 'ambiguous', 'review'))
        self.assertEqual(self.messages(), [])

    def test_only_repeated_greetings_cannot_be_rescued_by_strict_hash_append(self):
        self.snapshot([payload('你好', 'a')]*3)
        result = self.snapshot([payload('你好', 'a')]*4)
        self.assertIn(result['status'], ('gap', 'ambiguous', 'review'))
        self.assertEqual(self.messages(), [])

    def test_repeated_context_sequence_remains_ambiguous_not_new(self):
        old = [payload('甲', 'a'), payload('乙', 'b')]*2
        self.snapshot(old)
        result = self.snapshot([payload('甲', 'a'), payload('乙', 'b'), payload('新消息', 'c')])
        self.assertIn(result['status'], ('gap', 'ambiguous', 'review'))
        self.assertEqual(self.messages(), [])

    @unittest.skipUnless((Path(__file__).parent / 'message_vision_fixture.json').is_file(), "私有视觉夹具不随开源版发布")
    def test_real_tom_scrolled_append_stores_only_original_new_tail(self):
        directory = Path(__file__).resolve().parent
        fixture = json.loads((directory/'message_vision_fixture.json').read_text(encoding='utf-8'))
        archive = directory.parent/fixture['archive']
        if not archive.is_file():
            self.skipTest('Real recovery archive is an optional offline acceptance fixture')
        with zipfile.ZipFile(archive) as zipped:
            old_rgb = np.asarray(Image.open(io.BytesIO(zipped.read(fixture['previous_frame']))).convert('RGB'))
            new_rgb = np.asarray(Image.open(io.BytesIO(zipped.read(fixture['frame']))).convert('RGB'))
        previous = extract_message_payloads(old_rgb, fixture['previous_ocr_hits'], color_order='RGB')
        altered = copy.deepcopy(fixture['ocr_hits'])
        next(h for h in altered if h['text']=='你好')['text'] = '你子'
        current = extract_message_payloads(new_rgb, altered, color_order='RGB')
        # This test isolates Store's stable-input contract. The recovery only
        # contains one genuine OCR pass; Windows acceptance must independently
        # establish consensus before setting reply_safe in production.
        for p in current:
            if p['direction'] == 'incoming':
                p['reply_safe'] = True
                p['ocr']['stable'] = True
        self.snapshot(previous)
        result = self.snapshot(current)
        self.assertEqual(result['status'], 'append')
        self.assertEqual([p['text'] for p in self.messages()], ['17:18', 'MAA0926A'])
        self.assertEqual(self.snapshot(current)['status'], 'unchanged')
        self.assertEqual(len(self.messages()), 2)


if __name__ == '__main__':
    unittest.main()


class SenderWhitespaceTests(unittest.TestCase):
    def test_sender_consensus_preserves_punctuation_and_observed_name(self):
        votes = [{'text': 'AZIK', 'score': .99, 'pass_id': 0},
                 {'text': 'AZIK -', 'score': .95, 'pass_id': 1},
                 {'text': 'AZIK-', 'score': .91, 'pass_id': 2}]
        result = consensus_text(votes, min_score=.85, ignore_whitespace=True)
        self.assertTrue(result['stable'])
        self.assertEqual(result['text'], 'AZIK -')
        self.assertEqual([v['text'] for v in result['raw_votes']], ['AZIK', 'AZIK -', 'AZIK-'])
        self.assertFalse(consensus_text(votes, min_score=.85)['stable'])
        self.assertFalse(consensus_text(votes[:2], min_score=.85, ignore_whitespace=True)['stable'])
