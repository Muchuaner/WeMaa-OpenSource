import unittest,tempfile
from pathlib import Path
from unittest.mock import patch
from name_match import names_match,names_confusable,ordered_score,group_header
from store import Store

class NameToleranceTests(unittest.TestCase):
    def test_ordered_seventy_percent_boundary(self):
        self.assertTrue(names_match('ABCDEFGHIJ','ABCXEFYHIZ'))
        self.assertFalse(names_match('ABCDEFGHIJ','ABXXEFYHIZ'))
        self.assertFalse(names_match('ABCDEFGHIJ','JIHGFEDCBA'))
        self.assertFalse(names_match('ABC','ABCDEFGHIJ'))
        self.assertFalse(names_match('',''))
        self.assertEqual(ordered_score('创作平台AI-内测001','普界AI-内测001'),.9)
        self.assertTrue(names_match(' 创作平台 AI-内测001 ','普界AI-内测001'))

    def test_short_names_match_only_exactly(self):
        self.assertTrue(names_match('张三','张三'))
        self.assertTrue(names_match(' 张三 ','张三'))
        self.assertFalse(names_match('欧阳小明','欧阳小红'))
        self.assertFalse(names_match('开发组','开发组二'))
        self.assertTrue(names_match('开发组','开发组'))

    def test_confusable_guard_stays_loose_for_ambiguity_checks(self):
        self.assertTrue(names_confusable('欧阳小明','欧阳小红'))
        self.assertTrue(names_confusable('产品部-张三北京…','产品部-张三北京上海广州深圳'))
        self.assertFalse(names_confusable('产品部-张三北京…','产品部-李四上海广州深圳成都'))
        self.assertFalse(names_confusable('张三','李四'))
        self.assertFalse(names_confusable('','张三'))

    def test_title_group_and_private_count_suffix(self):
        from flows import title_matches
        from watch import is_group_title
        self.assertTrue(title_matches('普界AI-内测001','创作平台AI-内测001（54）'))
        self.assertTrue(is_group_title({'name':'普界AI-内测001'},'创作平台AI-内测001（54）'))
        self.assertTrue(title_matches('普界AI-内测001','创作平台AI-内测001 (54'))
        self.assertTrue(is_group_title({'name':'普界AI-内测001'},'创作平台AI-内测001 (54'))
        from group_policy import is_group
        self.assertTrue(is_group({'name':'普界AI-内测001','conversation_title':'创作平台AI-内测001 (54'}))
        self.assertIsNone(group_header('Tom(10)','Tom(10)'))
        self.assertFalse(title_matches('ABCDEFGHIJ','ABXXEFYHIZ'))

    def test_group_alias_and_ambiguous_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            s=Store(Path(directory)/'db')
            name='创作平台AI-内测001';cid=s.group_identity(name,name+'(54)','avatar1')
            s.manual_view(cid,name+'(54)','avatar1','frame',[]);s.observe_group(cid,name,name+'(54)')
            self.assertEqual(cid,s.group_identity('普界AI-内测001',name+'(55)','avatar2'))
            # Two separately established groups cannot silently collapse via fuzzy matching.
            s.db.execute('INSERT INTO conversation_profiles VALUES(?,?,?,?,?,?,?)',('other','group','创作平台AI-内测002',54,'创作平台AI-内测002','创作平台AI-内测002(54)',1));s.db.commit()
            self.assertEqual(cid,s.group_identity('普界AI-内测001',name+'(54)','avatar3'))
            with self.assertRaisesRegex(ValueError,'GROUP_NAME_AMBIGUOUS'):
                s.group_identity('普界AI-内测001','普界AI-内测001(54)','avatar4')
            s.db.close()

    def test_recent_route_recovers_typo_but_rejects_multiple_matches(self):
        from manual_gui import find_recent
        rows=[{'name':'普界AI-内测001','avatar_sha':'changed','y':100}]
        target={'name':'创作平台AI-内测001','conversation_title':'创作平台AI-内测001(54)','avatar_sha':'old'}
        class Runtime:
            def node(self,*a):pass
        with patch('watch.read_rows',return_value=(rows,None,'frame')),patch('watch.select',return_value=True),patch('manual_gui.scroll_top'):
            self.assertEqual(find_recent(Runtime(),None,target,'test',10**12)[0],rows[0])
            rows.append({'name':'创作平台AI-内测002','avatar_sha':'other','y':200})
            with self.assertRaisesRegex(ValueError,'TARGET_AMBIGUOUS'):
                find_recent(Runtime(),None,target,'test',10**12)

    def test_send_target_tolerates_name_but_keeps_avatar_guard(self):
        from test_send import FakeRuntime
        from send import _target
        runtime=FakeRuntime();image,_=runtime.capture('test-before')
        request={'name':'ABCDEFGHIJ','conversation_title':'ABCDEFGHIJ','avatar_sha':runtime.avatar_sha,'row_y':113}
        original=runtime.recognize_image
        def recognize(node,*args,**kwargs):
            hits=original(node,*args,**kwargs)
            if node in ('ReadChatTitle','ReadRecentList'):
                for hit in hits:hit['text']='ABCXEFYHIZ'
            return hits
        runtime.recognize_image=recognize
        self.assertTrue(_target(runtime,image,request)['verified'])
        request['avatar_sha']='wrong'
        self.assertFalse(_target(runtime,image,request)['verified'])

    def test_title_scaled_fallback_requires_both_passes(self):
        import numpy as np
        from unittest.mock import Mock
        from watch import read_title
        image=np.zeros((1040,1920,3),dtype=np.uint8)
        runtime=Mock();runtime.recognize_image.side_effect=[[],[{'text':'三标 蓝宝宝','box':[490,35,100,20]}]]
        good=[{'text':'白样@蓝宝宝','box':[490,35,100,20]}]
        with patch('send._ocr_scaled',side_effect=[good,good]):
            self.assertEqual(read_title(runtime,image,{'name':'白樣@蓝宝宝'}),'白样@蓝宝宝')
        runtime.recognize_image.side_effect=[[],[{'text':'三标 蓝宝宝','box':[490,35,100,20]}]]
        with patch('send._ocr_scaled',side_effect=[good,[{'text':'其他人','box':[490,35,100,20]}]]):
            with self.assertRaisesRegex(ValueError,'CHAT_TITLE_MISMATCH'):read_title(runtime,image,{'name':'白樣@蓝宝宝'})

    def test_bottom_row_is_scrolled_fully_into_view_before_selection(self):
        from manual_gui import find_recent
        from unittest.mock import Mock
        bottom={'name':'Demo User','avatar_sha':'photo','y':1009}
        visible=dict(bottom,y=930)
        target={'name':'Demo User','avatar_sha':'photo','conversation_title':'Demo User'}
        with patch('watch.read_rows',side_effect=[([bottom],None,'first'),([visible],None,'second')]),patch('watch.select',return_value=True) as select,patch('manual_gui.scroll_top'):
            self.assertEqual(find_recent(Mock(),None,target,'test',10**12)[0],visible)
            select.assert_called_once_with(unittest.mock.ANY,visible)
