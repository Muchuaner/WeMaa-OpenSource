import unittest
import numpy as np
from PIL import Image
from media_vision import composer_top,media_payloads,image_match,preview_box,video_match
class MediaVisionTests(unittest.TestCase):
 def frame(self,top=897):
  a=np.full((1040,1920,3),249,np.uint8);a[top-1:top+1,1000:1700]=235;return a
 def source(self):
  rng=np.random.default_rng(8);return Image.fromarray(rng.integers(0,255,(150,250,3),dtype=np.uint8))
 def test_outgoing_media_has_avatar_and_ordered_anchor(self):
  a=self.frame();im=self.source();a[400:550,1594:1844]=np.asarray(im)[:,:,::-1];a[400:436,1858:1894]=[150,70,20]
  p=media_payloads(a,[]);self.assertEqual(len(p),1);self.assertEqual(p[0]['direction'],'outgoing');self.assertFalse(p[0]['reply_safe']);self.assertTrue(image_match(a,p[0]['visual']['box'],im))
  a[400:436,1858:1894]=249;self.assertEqual(media_payloads(a,[]),[])
 def test_preview_wrong_image_and_added_text(self):
  a=self.frame(830);im=self.source();a[842:962,504:704]=np.asarray(im.resize((200,120),Image.Resampling.LANCZOS))[:,:,::-1]
  box=preview_box(a,im);self.assertIsNotNone(box)
  from media_send import exclusive_preview
  self.assertTrue(exclusive_preview(a,box));a[960:971,850:855]=0;self.assertFalse(exclusive_preview(a,box))
  self.assertFalse(image_match(a,box,Image.new('RGB',im.size,'red')))
 def test_clipped_media_not_an_anchor(self):
  a=self.frame();a[82:300,1594:1844]=[20,40,60];a[82:118,1858:1894]=[150,70,20]
  self.assertEqual(media_payloads(a,[]),[])

 def test_tall_preview_insertion_caret_is_not_user_draft(self):
  from media_send import exclusive_preview
  a=self.frame(767);box=[504,779,149,200]
  a[779:979,504:653]=[30,60,60]
  a[777:981,655:658]=[117,195,0]
  self.assertTrue(exclusive_preview(a,box))
  a[820:834,800:808]=0;self.assertFalse(exclusive_preview(a,box))
  a[820:834,800:808]=249;a[777:981,657]=[0,0,200]
  self.assertFalse(exclusive_preview(a,box))

 def test_video_requires_correct_content_and_play_control(self):
  a=self.frame();source=self.source();box=[1594,400,250,150]
  a[400:550,1594:1844]=np.asarray(source)[:,:,::-1]
  self.assertFalse(video_match(a,box,source))
  a[465:475,1714:1726]=255
  self.assertTrue(video_match(a,box,source))
  self.assertFalse(video_match(a,box,Image.new('RGB',source.size,'red')))
 def test_image_cache_rejects_arbitrary_paths_before_gui(self):
  from media_send import import_media
  with self.assertRaisesRegex(ValueError,'MEDIA_CACHE_PATH_INVALID'):
   import_media(None,{'id':'a'*32,'asset_id':115,'path':'/tmp/115.jpg','mime':'image/jpeg'},'invalid')

 def test_native_png_thumbnail_uses_wechat_sampling(self):
  from pathlib import Path
  fixtures=Path(__file__).parent/'test_fixtures'
  with Image.open(fixtures/'media123-source-thumb.png') as source,Image.open(fixtures/'media123-received-thumb.png') as received:
   a=self.frame();a[400:540,1597:1844]=np.asarray(received)[:,:,::-1]
   self.assertTrue(image_match(a,[1597,400,247,140],source))
   self.assertFalse(image_match(a,[1597,400,247,140],source.transpose(Image.Transpose.FLIP_LEFT_RIGHT)))

 def test_sent_thumbnail_opaque_inset_keeps_pixel_thresholds(self):
  from media_vision import sent_image_match
  from PIL import ImageDraw
  source=Image.new('RGB',(800,450),(25,54,77));draw=ImageDraw.Draw(source)
  draw.rectangle((38,38,762,412),outline=(245,160,45),width=7)
  draw.text((70,100),'UNIQUE MEDIA',fill='white',stroke_width=1)
  a=self.frame();a[400:540,1596:1845]=np.asarray(source.resize((249,140),Image.Resampling.LANCZOS))[:,:,::-1]
  box=[1597,400,247,140]
  self.assertTrue(sent_image_match(a,box,source))
  self.assertFalse(sent_image_match(a,box,source.transpose(Image.Transpose.FLIP_TOP_BOTTOM)))
  self.assertFalse(sent_image_match(a,box,Image.new('RGB',source.size,'red')))
  self.assertFalse(sent_image_match(a,[1597,410,247,140],source))
