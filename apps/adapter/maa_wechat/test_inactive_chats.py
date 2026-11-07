import unittest
from inactive_chats import eligible,menu_target,notice_target
class HideGuards(unittest.TestCase):
 def setUp(self):
  self.row={'key':'r','name':'Demo User','avatar_sha':'a','unread':False};self.protected=(set(),set(),set());self.now=1790755200
 def check(self,labels=['昨天22:36','昨天 22:36'],**kwargs):return eligible(self.row,labels,now=self.now,protected=self.protected,**kwargs)
 def test_two_verified_old_times(self):self.assertTrue(self.check()[0])
 def test_unread_never_hidden(self):self.row['unread']=True;self.assertFalse(self.check()[0])
 def test_review_protected_across_model_key_change(self):self.protected=(set(),{'a'},set());self.assertFalse(self.check()[0])
 def test_name_protected_across_avatar_change(self):self.protected=(set(),set(),{'Demo User'});self.assertFalse(self.check()[0])
 def test_selected_or_clipped(self):
  self.assertFalse(self.check(selected=True)[0]);self.row['name_clipped']=True;self.assertFalse(self.check()[0])
 def test_recent_or_conflicting_or_unknown(self):
  for votes in [['15:25','15:25'],['昨天','前天'],['',''],['明天','明天'],['星期一']]:self.assertFalse(self.check(votes)[0],votes)
 def menu(self):return [{'text':s,'score':.99,'box':[35,y,60,15]} for s,y in [('置顶',40),('标为未读',75),('消息免打扰',106),('独立窗口显示',145),('不显示',175),('删除',224)]]
 def test_exact_hide_menu(self):self.assertEqual(menu_target([self.menu(),self.menu()]),[35,175,60,15])
 def test_no_delete_fallback(self):
  hits=[h for h in self.menu() if h['text']!='不显示'];self.assertIsNone(menu_target([hits,hits]))
 def test_pinned_and_wrong_location_and_disagreement(self):
  hits=self.menu();hits[0]['text']='取消置顶';self.assertIsNone(menu_target([hits,hits]))
  hits=self.menu();hits[-2]['box'][1]=225;self.assertIsNone(menu_target([hits,hits]))
  self.assertIsNone(menu_target([self.menu()]))

 def notice(self):return [{'text':s,'score':.99,'box':b} for s,b in [
  ('不显示聊天',[916,451,84,18]),('不会删除聊天记录，当有新消息时会',[846,482,225,14]),
  ('重新显示，也可以通过搜索找回这个聊天。',[847,501,225,14]),('我知道了',[928,567,60,18])]]
 def test_first_use_notice_verified(self):self.assertEqual(notice_target([self.notice(),self.notice()]),[928,567,60,18])
 def test_unrelated_or_changed_notice_never_clicked(self):
  hits=self.notice();hits[1]['text']='删除聊天记录';self.assertIsNone(notice_target([hits,hits]))
  hits=self.notice();hits[-1]['box'][1]=800;self.assertIsNone(notice_target([hits,hits]))
 def test_pinned_long_menu_never_hidden(self):
  hits=self.menu();hits[0]['text']='取消置顶';hits[-2]['box'][1]=207;hits[-1]['box'][1]=256
  self.assertIsNone(menu_target([hits,hits]))
