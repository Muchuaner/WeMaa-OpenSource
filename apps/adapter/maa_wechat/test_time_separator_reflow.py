import copy
import unittest

from message_vision import reconcile_visual, _attach_time_anchors


def bubble(letter, y):
    return {'direction': 'incoming', 'message_kind': 'text', 'text': letter,
            'visual': {'box': [545, y, 100, 36], 'sha256': letter * 64, 'verified': True,
                       'avatar_verified': True, 'avatar_box': [500, y, 36, 36],
                       'avatar_sha256': 'f' * 64},
            'text_copy': {'verified': True, 'method': 'native_context_menu'}}


def clock(label, letter, y):
    return {'direction': 'time', 'message_kind': 'time', 'text': label,
            'ocr': {'stable': True}, 'visual': {'time_box': [1182, y, 31, 9],
            'time_sha256': letter * 64, 'time_verified': True}}


class SeparatorReflowTests(unittest.TestCase):
    def frames(self):
        old = [bubble('a', 100), clock('07:27', '1', 165), bubble('b', 197), bubble('c', 253),
               clock('07:35', '2', 318), bubble('d', 350), bubble('e', 406)]
        new = [bubble('a', 100), bubble('b', 156), clock('07:28', '3', 221), bubble('c', 253),
               clock('07:35', '2', 318), bubble('d', 350), bubble('e', 406), bubble('9', 462)]
        return _attach_time_anchors(old), _attach_time_anchors(new)

    def test_inserted_and_removed_verified_time_rows_preserve_exact_native_tail(self):
        old, new = self.frames();original = copy.deepcopy(new)
        result = reconcile_visual(old, new)
        self.assertEqual(result['status'], 'append')
        self.assertEqual(result['metadata']['strategy'], 'verified_native_time_separator_reflow')
        self.assertEqual(result['new'], [new[-1]])
        self.assertEqual(new, original)
        self.assertEqual(reconcile_visual(old, new[:-1])['status'], 'unchanged')

    def test_reflow_never_uses_ocr_only_or_changed_native_proof(self):
        for field in ('missing_native', 'native_changed', 'avatar_changed', 'unstable_time',
                      'unexplained_pixel', 'changed_stable_clock', 'hidden_card'):
            old, new = self.frames()
            if field == 'missing_native':new[1].pop('text_copy')
            elif field == 'native_changed':new[1]['text'] = 'different'
            elif field == 'avatar_changed':new[1]['visual']['avatar_sha256'] = '0' * 64
            elif field == 'unstable_time':new[2]['ocr']['stable'] = False
            elif field == 'unexplained_pixel':
                new[1]['visual']['box'][1] += 1;new[1]['visual']['avatar_box'][1] += 1
            elif field == 'changed_stable_clock':new[4]['visual']['time_sha256'] = '0' * 64
            else:new.insert(2, {'message_kind': 'unsupported_card', 'direction': 'unknown'})
            with self.subTest(field=field):self.assertIsNone(reconcile_visual(old, new))

    def test_at_least_one_unchanged_time_occurrence_remains_mandatory(self):
        old, new = self.frames()
        old.pop(4);new.pop(4)
        self.assertIsNone(reconcile_visual(old, new))
