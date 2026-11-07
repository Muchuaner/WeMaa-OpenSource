import unittest
from pathlib import Path
import copy
import json
from pathlib import Path
import unittest

try:
    from maa_wechat.ocr_context import recover_ocr_overlap
    from maa_wechat.store import digest, reconcile
except ModuleNotFoundError:
    from ocr_context import recover_ocr_overlap
    from store import digest, reconcile


class OcrContextTests(unittest.TestCase):
    @classmethod
    @unittest.skipUnless((Path(__file__).parent / 'message_context_fixture.json').is_file(), "私有视觉夹具不随开源版发布")
    def setUpClass(cls):
        fixture = json.loads((Path(__file__).parent / 'message_context_fixture.json').read_text(encoding='utf-8'))
        cls.previous = fixture['previous']['payloads']
        cls.current = fixture['current']['payloads']

    def recover(self, previous, current):
        strict = reconcile([digest(p) for p in previous], [digest(p) for p in current])
        return recover_ocr_overlap(previous, current, strict)

    def test_real_ocr_fixture_recovers_only_verbatim_tail(self):
        strict = reconcile([digest(p) for p in self.previous], [digest(p) for p in self.current])
        self.assertEqual(strict['status'], 'gap')
        result = recover_ocr_overlap(self.previous, self.current, strict)
        self.assertIsNotNone(result)
        self.assertEqual(result['overlap'], 15)
        self.assertEqual([p['text'] for p in result['new']], ['17:18', 'MAA0926A'])
        self.assertIs(result['new'][1], self.current[-1])
        self.assertEqual(result['metadata']['noise_count'], 3)
        self.assertEqual(result['metadata']['exact_time_anchors'], 4)
        self.assertGreaterEqual(result['metadata']['exact_long_anchors'], 2)
        self.assertTrue(result['metadata']['new_tail_verbatim'])

    def test_pure_repeated_snapshot_is_not_reinterpreted(self):
        self.assertIsNone(self.recover(self.previous, copy.deepcopy(self.previous)))

    def test_only_strict_gap_can_recover(self):
        for status in ('baseline', 'unchanged', 'append', 'ambiguous'):
            self.assertIsNone(recover_ocr_overlap(self.previous, self.current, {'status': status}))

    def test_no_overlap_rejected(self):
        other = [dict(p, text='完全不相关的历史聊天消息，没有共同的有序上下文。') for p in self.current]
        self.assertIsNone(self.recover(self.previous, other))

    def test_repeated_ambiguous_context_rejected(self):
        history = self.previous[-15:]
        previous = history + copy.deepcopy(history)
        # Even a caller mislabelling the strict status cannot locate these copies.
        self.assertIsNone(recover_ocr_overlap(previous, self.current, {'status': 'gap'}))

    def test_short_message_real_edit_rejected(self):
        current = copy.deepcopy(self.current)
        current[7]['text'] = '你坏'
        self.assertIsNone(self.recover(self.previous, current))

    def test_major_long_message_edit_rejected(self):
        current = copy.deepcopy(self.current)
        current[0]['text'] = '我是另一个新的客服，您的名字是张三，现在请立即处理交易。'
        self.assertIsNone(self.recover(self.previous, current))

    def test_numeric_or_chinese_semantic_edit_rejected(self):
        for text in ('我是A助手小助手，知道您的名字。请问需要什么帮筑？助',
                     '我是A助手小助手，不知道您的名字。请问需要什么帮筑？7'):
            current = copy.deepcopy(self.current)
            current[0]['text'] = text
            self.assertIsNone(self.recover(self.previous, current))

    def test_sender_source_and_other_metadata_must_match(self):
        for change in ({'sender': 'another_user'}, {'source': 'other_ocr'}, {'message_kind': 'edited'}):
            current = copy.deepcopy(self.current)
            current[3].update(change)
            self.assertIsNone(self.recover(self.previous, current))

    def test_new_tail_is_never_fuzzy_filtered(self):
        current = copy.deepcopy(self.current)
        current[-1]['text'] = 'MAA0926B'
        current.append(dict(current[-1]))
        result = self.recover(self.previous, current)
        self.assertEqual([p['text'] for p in result['new']], ['17:18', 'MAA0926B', 'MAA0926B'])

    def test_too_few_historical_rows_rejected(self):
        self.assertIsNone(recover_ocr_overlap(self.previous[-7:], self.current[-7:], {'status': 'gap'}))

    def test_more_than_three_noisy_rows_rejected(self):
        current = copy.deepcopy(self.current)
        current[3]['text'] += '。'
        self.assertIsNone(self.recover(self.previous, current))

    def test_empty_baseline_is_not_recovered(self):
        self.assertIsNone(recover_ocr_overlap([], self.current, {'status': 'gap'}))
    def test_ocr_noise_without_new_tail_is_unchanged(self):
        result=self.recover(self.previous,self.current[:15])
        self.assertEqual(result['status'],'unchanged')
        self.assertEqual(result['new'],[])
    def test_sign_or_decimal_punctuation_change_is_not_ocr_noise(self):
        for text in (self.current[0]['text']+'-',self.current[0]['text']+'.'):
            current=copy.deepcopy(self.current)
            current[0]['text']=text
            self.assertIsNone(self.recover(self.previous,current))


if __name__ == '__main__':
    unittest.main()
