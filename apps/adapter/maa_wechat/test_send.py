import hashlib
import json
from pathlib import Path
import unittest

import numpy as np
from PIL import Image

try:
    from maa_wechat.send import INPUT_ROI, send_verified, _new_outgoing, _empty_pixels, _payloads, _draft
except ModuleNotFoundError:
    from send import INPUT_ROI, send_verified, _new_outgoing, _empty_pixels, _payloads, _draft


class FakeRuntime:
    """Maa-shaped fake: screenshots, same-frame OCR and pipeline-only actions."""
    def __init__(self, *, draft='', target_change=None, draft_change=None,
                 send_clears=True, outgoing=True, send_raises=False,
                 send_succeeded=True, after_capture_raises=False,
                 hidden_draft=False, low_draft_score=False, old_outgoing=False,
                 after_row_y=None, draft_pass_texts=None, outgoing_pass_texts=None, stage_rows=None):
        self.draft = draft
        self.target_change = target_change
        self.draft_change = draft_change
        self.send_clears = send_clears
        self.outgoing = outgoing
        self.send_raises = send_raises
        self.send_succeeded = send_succeeded
        self.after_capture_raises = after_capture_raises
        self.hidden_draft = hidden_draft
        self.low_draft_score = low_draft_score
        self.old_outgoing = old_outgoing
        self.after_row_y = after_row_y
        self.stage_rows = stage_rows or {}
        self.draft_pass_texts = draft_pass_texts
        self.outgoing_pass_texts = outgoing_pass_texts
        self.recognition_calls = []
        self.nodes = []
        self.sent = False
        self.stage = 'before'
        self.reply = ''
        self.avatar = np.random.default_rng(17).integers(0, 256, (26, 24, 3), dtype=np.uint8)
        self.avatar_sha = hashlib.sha256(self.avatar.tobytes()).hexdigest()

    def _hit(self, text, box, score=0.99):
        return {'text': text, 'box': list(box), 'score': score}

    def _draw_bubble(self, image, text, y, outgoing):
        if outgoing:
            left, width, rgb = 1660, 190, (150, 234, 140)
        else:
            left, width, rgb = 550, 150, (235, 235, 235)
        image[y:y+40, left:left+width] = rgb[::-1]
        # Stable per-string glyph-like pixels, deliberately excluding screen y.
        bits = np.unpackbits(np.frombuffer(hashlib.sha256(text.encode('utf-8')).digest(), dtype=np.uint8))[:128].reshape(8, 16)
        patch = image[y+13:y+21, left+13:left+29]
        patch[bits.astype(bool)] = (20, 20, 20)
        return self._hit(text, [left+12, y+10, min(150, 10*len(text)), 18])

    def capture(self, label):
        self.stage = label.rsplit('-', 1)[-1]
        if '-after-' in label:
            self.stage = 'after'
        if self.stage == 'after' and self.after_capture_raises:
            raise RuntimeError('CAPTURE_FAILED_AFTER_SEND')
        image = np.full((1040, 1920, 3), 250, dtype=np.uint8)
        center = self.stage_rows.get(self.stage,self.after_row_y if self.stage == 'after' and self.after_row_y else 113)
        image[center-13:center+13, 78:102] = self.avatar
        self._draw_bubble(image, '测试入站', 180, False)
        if self.old_outgoing:
            self._draw_bubble(image, self.reply or '你好呀', 350, True)
        if self.sent and self.outgoing:
            self._draw_bubble(image, self.draft_change or self.reply, 650, True)
        if self.draft or self.hidden_draft:
            image[930:944, 610:660] = 30
        else:
            # The genuine input cursor must not make an empty draft look full.
            image[916:934, 505:506] = (70, 190, 10)
        return image, '/fake/'+label+'.png'

    def node(self, name, overrides=None, allow_miss=False):
        self.nodes.append((name, overrides))
        if name == 'InputDraft':
            self.reply = overrides[name]['input_text']
            self.draft = self.reply
        elif name == 'SendDraft':
            self.sent = True
            if self.send_clears:
                self.draft = ''
            if self.send_raises:
                raise RuntimeError('SEND_JOB_FAILED_AFTER_ATTEMPT')
            return {'succeeded': self.send_succeeded}
        return {'succeeded': True}

    def recognize_image(self, name, image, kind=None, roi=None):
        self.recognition_calls.append((name, image.shape))
        scale = 1.0 if image.shape[:2] == (1040, 1920) else (2.0 if image.shape[1] in (2800, 2790) else 1.5)
        pass_index = {1.0: 0, 2.0: 1, 1.5: 2}[scale]
        if name == 'ChatTitleBlueBadge':
            return []
        if name == 'ReadChatTitle':
            title = 'Jerry' if self.stage == self.target_change else 'Demo User'
            return [self._hit(title, [500, 40, 40, 20])]
        if name == 'ContactSelectedRow':
            center = self.stage_rows.get(self.stage,self.after_row_y if self.stage == 'after' and self.after_row_y else 113)
            return [{'box': [61, center-32, 18, 65]}]
        if name == 'ListRows':
            return [{'box': [70, 80, 400, 950], 'detail': {'centers': [113, 178, 243]}}]
        if name == 'ReadRecentList':
            center = self.stage_rows.get(self.stage,self.after_row_y if self.stage == 'after' and self.after_row_y else 113)
            return [self._hit('Demo User', [120, center-18, 50, 20])]
        if name == 'ReadChatInput':
            text = self.draft
            if text and self.draft_pass_texts:
                text = self.draft_pass_texts[pass_index]
            if self.stage in ('prepared', 'preclick') and self.draft_change:
                text = self.draft_change
            if not text:
                return []
            hits = [self._hit(text, [610, 930, 130, 18], 0.5 if self.low_draft_score else 0.99)]
        elif name == 'ReadChatMessages':
            hits = [self._hit('测试入站', [562, 190, 40, 18])]
            if self.old_outgoing:
                hits.append(self._hit(self.reply or '你好呀', [1672, 360, 30, 18]))
            if self.sent and self.outgoing:
                text = self.outgoing_pass_texts[pass_index] if self.outgoing_pass_texts else self.draft_change or self.reply
                hits.append(self._hit(text, [1672, 660, min(150, len(self.reply)*10), 18]))
        else:
            raise AssertionError('unexpected Maa recognition '+name)
        if image.shape[:2] != (1040, 1920):
            x, y, _, _ = INPUT_ROI if name == 'ReadChatInput' else (490, 82, 1395, 805)
            return [dict(hit, box=[round((hit['box'][0]-x)*scale), round((hit['box'][1]-y)*scale), round(hit['box'][2]*scale), round(hit['box'][3]*scale)]) for hit in hits]
        return hits


class SendTests(unittest.TestCase):
    def setUp(self):
        # Receipt/state-machine tests inject acquisition results. Native menu,
        # clipboard freshness and masking are tested in test_text_copy and the
        # Windows no-send probe; this historical fake has no Windows clipboard.
        from unittest.mock import patch
        def acquired(runtime,image,*,group_chat=False,previous_payloads=None):
            from send import _ocr_passes,_ocr_scaled,CHAT_ROI
            from message_vision import extract_message_payloads
            native,large=_ocr_passes(runtime,image,'ReadChatMessages',CHAT_ROI)
            passes=[large]
            payloads=extract_message_payloads(image,native,consensus_passes=passes,group_chat=group_chat)
            if any(p.get('direction')=='outgoing' and not p.get('ocr',{}).get('stable') for p in payloads):
                passes.append(_ocr_scaled(runtime,image,'ReadChatMessages',CHAT_ROI,1.5))
                payloads=extract_message_payloads(image,native,consensus_passes=passes,group_chat=group_chat)
            for p in payloads:
                if p.get('message_kind')=='text':
                    p['source']='maa_native_clipboard'
                    p['text_copy']={'verified':bool(p.get('ocr',{}).get('stable')),'method':'test_fixture'}
            return payloads,native
        # Both import forms are used by the isolated and full-suite harnesses.
        import text_copy
        self.native_patch=patch.object(text_copy,'read_messages',side_effect=acquired)
        self.native_patch.start();self.addCleanup(self.native_patch.stop)
        try:
            import maa_wechat.text_copy as package_copy
            if package_copy is not text_copy:
                extra=patch.object(package_copy,'read_messages',side_effect=acquired)
                extra.start();self.addCleanup(extra.stop)
        except ImportError:pass
    def request(self, runtime, **extra):
        return {'event_id': 'event-test-1', 'snapshot_id': 17, 'name': 'Demo User',
                'conversation_title': 'Demo User', 'avatar_sha': runtime.avatar_sha,
                'row_y': 113, 'reply_text': '你好呀', **extra}

    def send(self, runtime, **extra):
        return send_verified(runtime, Path('/fake'), 'maa-send-test', self.request(runtime, **extra))

    def sends(self, runtime):
        return [node for node in runtime.nodes if node[0] == 'SendDraft']

    def test_complete_maa_send_has_one_click_and_new_visual_occurrence(self):
        runtime = FakeRuntime()
        result = self.send(runtime)
        self.assertEqual(result['status'], 'sent', result)
        self.assertEqual(len(self.sends(runtime)), 1)
        self.assertTrue(result['evidence']['outgoing_verified'])
        self.assertFalse(result['diagnostic']['draft_retained'])
        self.assertEqual(set(result['evidence']), {'frame_sha256', 'snapshot_id', 'title_verified', 'outgoing_verified'})
        self.assertEqual([name for name, _ in runtime.nodes], ['FocusChatInput', 'InputDraft', 'SendDraft'])
        self.assertTrue(result['diagnostic']['history_before'])
        self.assertTrue(result['diagnostic']['after_payloads'])

    def test_existing_user_draft_is_never_overwritten_or_cleared(self):
        runtime = FakeRuntime(draft='用户未发的草稿')
        result = self.send(runtime)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'existing_or_unreadable_draft')
        self.assertEqual(runtime.nodes, [])
        self.assertEqual(runtime.draft, '用户未发的草稿')

    def test_pixel_visible_draft_cannot_be_hidden_by_empty_ocr(self):
        runtime = FakeRuntime(hidden_draft=True)
        self.assertEqual(self.send(runtime)['status'], 'blocked')
        self.assertEqual(runtime.nodes, [])

    def test_wrong_avatar_or_selected_row_blocks_all_actions(self):
        for extra in ({'avatar_sha': '0'*64}, {'row_y': 178}, {'name': 'Jerry'}, {'conversation_title': 'Jerry'}):
            runtime = FakeRuntime()
            with self.subTest(extra=extra):
                self.assertEqual(self.send(runtime, **extra)['status'], 'blocked')
                self.assertEqual(runtime.nodes, [])

    def test_owned_worker_refreshes_row_position_after_identity_verification(self):
        runtime=FakeRuntime()
        result=send_verified(runtime,Path('/fake'),'maa-send-test',self.request(runtime,row_y=178),refresh_row_position=True)
        self.assertEqual(result['status'],'sent')
        self.assertEqual(len(self.sends(runtime)),1)
        self.assertEqual(result['diagnostic']['row_position_refresh'],{'previous':178,'verified':113})
        for wrong in ({'avatar_sha':'0'*64},{'conversation_title':'Jerry'}):
            runtime=FakeRuntime()
            result=send_verified(runtime,Path('/fake'),'maa-send-test',self.request(runtime,row_y=178,**wrong),refresh_row_position=True)
            self.assertEqual(result['status'],'blocked');self.assertEqual(self.sends(runtime),[])
        runtime=FakeRuntime(target_change='preclick')
        result=send_verified(runtime,Path('/fake'),'maa-send-test',self.request(runtime,row_y=178),refresh_row_position=True)
        self.assertEqual(result['status'],'blocked');self.assertEqual(self.sends(runtime),[])
        self.assertEqual(runtime.draft,'你好呀')

    def test_owned_row_movement_before_input_and_before_click_keeps_exact_recipient(self):
        moves={'focused':178,'prepared':243,'preclick':113}
        runtime=FakeRuntime(stage_rows=moves)
        result=send_verified(runtime,Path('/fake'),'maa-send-test',self.request(runtime),refresh_row_position=True)
        self.assertEqual(result['status'],'sent',result)
        self.assertEqual(len(self.sends(runtime)),1)
        self.assertEqual([(x['stage'],x['verified']) for x in result['diagnostic']['verified_row_moves']],[('focused',178),('prepared',243),('preclick',113)])
        runtime=FakeRuntime(stage_rows=moves)
        self.assertEqual(self.send(runtime)['status'],'blocked');self.assertEqual(self.sends(runtime),[])
        for stage in ('focused','prepared','preclick'):
            runtime=FakeRuntime(stage_rows=moves,target_change=stage)
            result=send_verified(runtime,Path('/fake'),'maa-send-test',self.request(runtime),refresh_row_position=True)
            self.assertEqual(result['status'],'blocked');self.assertEqual(self.sends(runtime),[])

    def test_bad_small_title_needs_two_scaled_observations_before_send(self):
        runtime=FakeRuntime();original=runtime.recognize_image
        def recognize(node,image,*args,**kwargs):
            if node=='ReadChatTitle' and image.shape[0]>120:
                return [runtime._hit('TO 1',[490,35,40,20])]
            return original(node,image,*args,**kwargs)
        runtime.recognize_image=recognize
        result=self.send(runtime)
        self.assertEqual(result['status'],'sent');self.assertEqual(len(self.sends(runtime)),1)
        self.assertEqual(result['diagnostic']['stages'][0]['target']['title_ocr_primary'],'TO 1')
        runtime=FakeRuntime();original=runtime.recognize_image
        def disagree(node,image,*args,**kwargs):
            if node=='ReadChatTitle':
                return [runtime._hit('Demo User' if image.shape[0]==80 else 'Jerry',[490,35,40,20])]
            return original(node,image,*args,**kwargs)
        runtime.recognize_image=disagree
        self.assertEqual(self.send(runtime)['status'],'blocked');self.assertEqual(self.sends(runtime),[])

    def test_target_changes_after_focus_blocks_input(self):
        runtime = FakeRuntime(target_change='focused')
        result = self.send(runtime)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual([name for name, _ in runtime.nodes], ['FocusChatInput'])

    def test_target_changes_immediately_before_click_keeps_prepared_draft(self):
        runtime = FakeRuntime(target_change='preclick')
        result = self.send(runtime)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(len(self.sends(runtime)), 0)
        self.assertEqual(runtime.draft, '你好呀')

    def test_wrong_or_low_confidence_prepared_text_is_not_sent(self):
        for runtime in (FakeRuntime(draft_change='你子呀'), FakeRuntime(low_draft_score=True)):
            with self.subTest(runtime=runtime):
                result = self.send(runtime)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(len(self.sends(runtime)), 0)
                self.assertTrue(result['diagnostic']['draft_retained'])

    def test_third_draft_pass_requires_two_exact_high_confidence_observations(self):
        runtime = FakeRuntime(draft_pass_texts=['你好呀', '你子呀', '你好呀'])
        result = self.send(runtime)
        self.assertEqual(result['status'], 'sent', result)
        prepared = next(s for s in result['diagnostic']['stages'] if s['stage'] == 'prepared')
        self.assertEqual(prepared['draft']['matching_passes'], [0, 2])
        self.assertEqual(len(prepared['draft']['texts']), 3)
        for votes in (['你好呀', '你子呀', '你子呀'], ['你子呀', '你子呀', '你好呀']):
            runtime = FakeRuntime(draft_pass_texts=votes)
            self.assertEqual(self.send(runtime)['status'], 'blocked')
            self.assertEqual(len(self.sends(runtime)), 0)

    def test_real_prepared_colon_conflict_is_resolved_by_observation_not_mapping(self):
        # Actual Windows Maa outputs from saved self-trial3 prepared frame.
        expected = 'Maa发送自检maa-send-self-trial3-sep26：你好。验证中文输入、发送与回读。'
        native = [{'box': [499, 908, 502, 21], 'score': .983126, 'text': expected}]
        zoom = [{'box': [8, 13, 1004, 42], 'score': .987684,
                 'text': 'Maa发送自检 maa-send-self-trial3-sep26: 你好。验证中文输入、发送与回读。'}]
        third = [{'box': [6, 10, 753, 31], 'score': .984123, 'text': expected}]

        class FixedRecognition:
            def recognize_image(self, name, image, **kwargs):
                return {1920: native, 2800: zoom, 2100: third}[image.shape[1]]

        image = np.full((1040, 1920, 3), 250, dtype=np.uint8)
        result = _draft(FixedRecognition(), image, expected)
        self.assertTrue(result['verified'])
        self.assertEqual(result['matching_passes'], [0, 2])
        self.assertNotEqual(result['texts'][1].replace(' ', ''), expected)

    def test_third_outgoing_pass_can_resolve_one_conflicting_ocr_without_correction(self):
        runtime = FakeRuntime(outgoing_pass_texts=['你好呀', '你子呀', '你好呀'])
        result = self.send(runtime)
        self.assertEqual(result['status'], 'sent', result)
        self.assertEqual(result['diagnostic']['outgoing']['payload']['ocr']['agreement'], 2)
        self.assertEqual(len(result['diagnostic']['outgoing']['payload']['ocr']['raw_votes']), 3)
        runtime = FakeRuntime(outgoing_pass_texts=['你子呀', '你子呀', '你好呀'])
        self.assertEqual(self.send(runtime)['status'], 'uncertain')
        self.assertEqual(len(self.sends(runtime)), 1)

    def test_accurate_two_draft_passes_do_not_spend_an_extra_ocr_pass(self):
        runtime = FakeRuntime(draft='你好呀')
        image, _ = runtime.capture('unit-prepared')
        result = _draft(runtime, image, '你好呀')
        self.assertTrue(result['verified'])
        self.assertEqual(result['matching_passes'], [0, 1])
        self.assertEqual(sum(name == 'ReadChatInput' for name, _ in runtime.recognition_calls), 2)

    def test_send_node_exception_returns_uncertain_without_retry(self):
        runtime = FakeRuntime(send_raises=True)
        result = self.send(runtime)
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(len(self.sends(runtime)), 1)
        self.assertTrue(result['diagnostic']['send_click_attempted'])

    def test_click_success_without_empty_draft_or_outgoing_is_not_sent(self):
        for runtime in (FakeRuntime(send_clears=False), FakeRuntime(outgoing=False), FakeRuntime(send_succeeded=False)):
            with self.subTest(runtime=runtime):
                result = self.send(runtime)
                self.assertEqual(result['status'], 'uncertain')
                self.assertEqual(len(self.sends(runtime)), 1)
                self.assertFalse(result['evidence']['outgoing_verified'])

    def test_capture_failure_or_target_change_after_click_is_uncertain(self):
        for runtime in (FakeRuntime(after_capture_raises=True), FakeRuntime(target_change='after')):
            result = self.send(runtime)
            self.assertEqual(result['status'], 'uncertain')
            self.assertEqual(len(self.sends(runtime)), 1)

    def test_repeated_identical_reply_needs_new_occurrence_not_global_text_dedup(self):
        runtime = FakeRuntime(old_outgoing=True)
        result = self.send(runtime)
        self.assertEqual(result['status'], 'sent', result)
        self.assertEqual(result['diagnostic']['outgoing']['prior_occurrences'], 1)
        self.assertEqual(result['diagnostic']['outgoing']['current_occurrences'], 2)

    def test_same_target_moving_in_recent_list_after_send_is_allowed(self):
        runtime = FakeRuntime(after_row_y=178)
        self.assertEqual(self.send(runtime)['status'], 'sent')

    def test_reply_controls_newlines_and_overlong_text_block_before_actions(self):
        for text in ('你好\n世界', '你好\r世界', '你好\t世界', '你'*401, ''):
            runtime = FakeRuntime()
            with self.subTest(text=text):
                self.assertEqual(self.send(runtime, reply_text=text)['status'], 'blocked')
                self.assertEqual(runtime.nodes, [])

    def test_partial_or_old_outgoing_evidence_never_confirms_a_send(self):
        def bubble(text, sha, direction='outgoing', stable=True):
            return {'text': text, 'direction': direction, 'message_kind': 'text',
                    'visual': {'verified': True, 'sha256': sha*64, 'box': [1700, 600, 80, 40]},
                    'ocr': {'stable': stable}}
        old = [bubble('入站', 'a', 'incoming')]
        self.assertIsNone(_new_outgoing(old, old, '你好呀'))
        self.assertIsNone(_new_outgoing(old, old+[bubble('你好', 'b')], '你好呀'))
        self.assertIsNone(_new_outgoing(old, old+[bubble('你好呀', 'b', stable=False)], '你好呀'))
        self.assertIsNone(_new_outgoing(old, old+[bubble('你好呀', 'b', 'incoming')], '你好呀'))
        self.assertIsNotNone(_new_outgoing(old, old+[bubble('你好\n呀', 'b')], '你好呀'))

    def test_empty_history_with_bare_time_label_still_accepts_a_new_bubble(self):
        time_label = {'text': '20:57', 'direction': 'time', 'message_kind': 'time',
                      'visual': {'verified': False, 'sha256': 'a'*64, 'box': [1180, 95, 33, 18]},
                      'ocr': {'stable': False}}
        outgoing = {'text': '中文自检', 'direction': 'outgoing', 'message_kind': 'text',
                    'visual': {'verified': True, 'sha256': 'b'*64, 'box': [1300, 130, 548, 36]},
                    'ocr': {'stable': True}}
        result = _new_outgoing([], [time_label, outgoing], '中文自检')
        self.assertIsNotNone(result)
        self.assertEqual(result['prior_occurrences'], 0)

    def test_new_incoming_after_reply_does_not_invalidate_the_sent_occurrence(self):
        from test_message_vision import payload
        before = [payload('原来信', 'a')]
        reply = payload('OK', 'b', 'outgoing')
        reply['ocr'] = {'stable': True}
        after = before + [reply, payload('后续来信', 'c')]
        self.assertIsNotNone(_new_outgoing(before, after, 'OK'))
        self.assertIsNone(_new_outgoing(before, after, '别的内容'))
        later = payload('另一次发送', 'd', 'outgoing')
        later['ocr'] = {'stable': True}
        self.assertIsNone(_new_outgoing(before, after + [later], 'OK'))
        self.assertIsNone(_new_outgoing(before + [reply], after, 'OK'))

    def test_outgoing_width_variant_does_not_relax_words_drafts_or_other_punctuation(self):
        from send import _normal
        def bubble(text,sha,direction='outgoing'):
            return {'text':text,'direction':direction,'message_kind':'text',
                'visual':{'verified':True,'sha256':sha*64,'box':[1400,800,300,36]},'ocr':{'stable':True}}
        old=[bubble('入站','a','incoming')]
        for expected,seen in [('你好！','你好!'),('你好？','你好?')]:
            self.assertIsNotNone(_new_outgoing(old,old+[bubble(seen,'b')],expected))
            self.assertNotEqual(_normal(expected),_normal(seen))
        for expected,seen in [('你好！','你子!'),('甲、乙','甲。乙'),('甲。乙','甲乙')]:
            self.assertIsNone(_new_outgoing(old,old+[bubble(seen,'b')],expected))

    def test_scrolled_card_shadow_does_not_hide_verified_new_outgoing(self):
        from test_message_vision import payload
        card=payload('卡片','a');card['visual']['box']=[545,200,252,93]
        shadow=payload('片段','b');shadow['visual']['box']=[545,200,206,93]
        history=[card,shadow,payload('入站1','c'),payload('入站2','d')]
        after=[card,payload('入站1','c'),payload('入站2','d'),payload('回复!','e','outgoing')]
        after[-1]['ocr']={'stable':True}
        self.assertIsNotNone(_new_outgoing(history,after,'回复！'))

    def test_real_self_send_frame_native_and_lanczos_ocr_prove_exact_outgoing(self):
        directory = Path(__file__).resolve().parent.parent/'artifacts'/'maa-recovery-sep26'
        prefix = 'maa-send-self-trial-sep26-send-714a6429df949681-'
        if not (directory/(prefix+'after-2.png')).is_file():
            self.skipTest('Optional saved real self-send frame not installed')
        text = 'Maa发送自检：你好。此消息仅发送到文件传输助手，验证中文输入、发送与回读。'
        # Exact Windows Maa v5.14.0 offline output on the same saved screenshot.
        native = [{'box': [1180, 95, 33, 18], 'score': .992597, 'text': '20:57'},
                  {'box': [1309, 139, 513, 19], 'score': .998939, 'text': text}]
        lanczos = [{'box': [1377, 20, 73, 45], 'score': .992464, 'text': '20:57'},
                   {'box': [1636, 112, 1029, 44], 'score': .999004, 'text': text}]

        class FixedRecognition:
            def recognize_image(self, name, image, **kwargs):
                return native if image.shape[:2] == (1040, 1920) else lanczos

        image = np.asarray(Image.open(directory/(prefix+'after-2.png')).convert('RGB'))[:, :, ::-1].copy()
        current = _payloads(FixedRecognition(), image)
        outgoing = [p for p in current if p['direction'] == 'outgoing']
        self.assertEqual(len(outgoing), 1)
        self.assertEqual(outgoing[0]['text'], text)
        self.assertTrue(outgoing[0]['ocr']['stable'])
        self.assertIsNotNone(_new_outgoing([], current, text))

    def test_nonlight_input_theme_is_not_treated_as_empty(self):
        self.assertFalse(_empty_pixels(np.full((1040, 1920, 3), 30, dtype=np.uint8)))

    def test_pipeline_send_nodes_have_no_enter_or_auto_send_chain(self):
        pipeline = json.loads((Path(__file__).parent/'resource'/'pipeline'/'navigation.json').read_text(encoding='utf-8'))
        self.assertEqual(pipeline['InputDraft']['action'], 'InputText')
        self.assertEqual(pipeline['InputDraft']['input_text'], '')
        self.assertEqual(pipeline['SendDraft']['recognition'], 'OCR')
        self.assertEqual(pipeline['SendDraft']['action'], 'Click')
        self.assertEqual(pipeline['SendDraft']['repeat'], 1)
        self.assertEqual(pipeline['SendDraft']['next'], [])
        for name in ('InputDraft', 'FocusChatInput', 'ReadChatInput', 'SendDraft'):
            self.assertNotEqual(pipeline[name]['action'], 'ClickKey')


if __name__ == '__main__':
    unittest.main()
