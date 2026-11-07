import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from media_send import send_media_verified


class FullMediaHistoryTests(unittest.TestCase):
    def test_verified_media_retains_times_text_and_concurrent_group_input(self):
        image=np.full((1040,1920,3),249,dtype=np.uint8)
        before=[{'text':'17:45','direction':'time','message_kind':'time'},
                {'text':'old','direction':'incoming','message_kind':'text'}]
        media={'text':'[图片/视频]','direction':'outgoing','message_kind':'media',
               'visual':{'box':[1684,533,160,213],'sha256':'b'*64,'verified':True}}
        incoming={'text':'/菜单','direction':'incoming','message_kind':'text',
                  'visual':{'avatar_box':[500,807,36,36],'avatar_verified':True}}
        after=before+[media,{'text':'18:03','direction':'time','message_kind':'time'},incoming]
        runtime=SimpleNamespace(capture=lambda label:(image,label+'.png'),node=lambda *args,**kw:{'succeeded':True})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'menu.png';Image.new('RGB',(2,2),'teal').save(path)
            item={'id':'a'*32,'type':'image','path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            with patch('send._target',return_value={'verified':True}),patch('send._draft',return_value={'verified':True}),\
                    patch('media_send.owned_windows',return_value=[]),patch('media_send.import_media',return_value={}),\
                    patch('media_send.exclusive_preview',return_value=True),patch('media_vision.preview_box',return_value=[504,779,149,200]),\
                    patch('send._payloads',side_effect=[before,after,after]) as payload_reader,\
                    patch('media_vision.media_payloads',side_effect=lambda im,values:[p for p in values if p.get('message_kind')=='media']),\
                    patch('media_vision.image_match',return_value=True),patch('sender_ocr.refine_group_senders',side_effect=lambda r,im,values:values),\
                    patch('media_send.time.sleep'):
                result=send_media_verified(runtime,Path(directory),'test',{'name':'开发组','conversation_title':'开发组(4)'},item)
            self.assertEqual(result['status'],'sent')
            self.assertEqual(result['diagnostic']['history_before'],before)
            self.assertEqual(len(result['diagnostic']['after_payloads']),2)
            self.assertEqual(result['diagnostic']['after_payloads'][-1]['payloads'],after)
            self.assertEqual(result['diagnostic']['outgoing'],media)
            self.assertTrue(all(call.kwargs.get('group_chat') is True for call in payload_reader.call_args_list))
            self.assertEqual(result['diagnostic']['after_payloads'][-1]['payloads'][-1]['visual']['avatar_box'],[500,807,36,36])

    def test_mp4_attachment_requires_new_stable_card(self):
        from contextlib import nullcontext
        image=np.full((1040,1920,3),249,dtype=np.uint8)
        card={'direction':'outgoing','message_kind':'video',
              'visual':{'box':[1600,500,226,60],'sha256':'c'*64}}
        runtime=SimpleNamespace(capture=lambda label:(image,label+'.png'),node=lambda *a,**k:{'succeeded':True})
        decoder=SimpleNamespace(streams=SimpleNamespace(video=[object()]),
            decode=lambda stream:iter([SimpleNamespace(to_image=lambda:Image.new('RGB',(640,360),'red'))]))
        for existing, expected in [(False,'sent'),(True,'uncertain')]:
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                calls=[0]
                def cards(*args,**kwargs):
                    if kwargs.get('preview'):return [card]
                    calls[0]+=1
                    return [card] if existing or calls[0]>1 else []
                with patch.dict('sys.modules', {'av':SimpleNamespace(open=lambda path:nullcontext(decoder))}),\
                        patch('send._target',return_value={'verified':True}),patch('send._draft',return_value={'verified':True}),\
                        patch('media_send.owned_windows',return_value=[]),patch('media_send.import_media',return_value={}),\
                        patch('media_send.exclusive_preview',return_value=True),patch('send._payloads',return_value=[]),\
                        patch('media_vision.media_payloads',return_value=[]),patch('media_send.video_card',side_effect=cards),\
                        patch('media_send.time.sleep'):
                    result=send_media_verified(runtime,Path(directory),'test',{'name':'Demo User','conversation_title':'Demo User'},
                        {'id':'a'*32,'type':'video','path':str(Path(directory)/'1.mp4'),'sha256':'d'*64})
                self.assertEqual(result['status'],expected)
                self.assertEqual(result['evidence']['outgoing_verified'],not existing)

    def test_card_uses_two_exact_filename_votes_and_fixed_preview_geometry(self):
        from media_send import video_card
        image=np.full((1040,1920,3),249,dtype=np.uint8);image[896:898,1000:1700]=235
        image[908:968,504:730]=238;image[920:952,690:714]=[50,90,130]
        def hit(name):return [{'text':name,'box':[511,917,114,20],'score':.98}]
        runtime=SimpleNamespace(recognize_image=lambda name,im,roi:hit('v9e918edO.mp4' if roi[2]==300 else '276.5K'))
        for correct_votes in (1,2):
            def scaled(runtime,im,name,roi,scale):
                if roi[2]!=300:return hit('276.5K')
                return hit('v9e918ed0.mp4' if scale==2 or correct_votes==2 else 'different.mp4')
            with patch('send._ocr_scaled',side_effect=scaled):
                cards=video_card(runtime,image,{'path':'v9e918ed0.mp4','size':283133},preview=True)
            self.assertEqual(len(cards),int(correct_votes==2))
            if cards:self.assertEqual(cards[0]['visual']['box'],[504,908,226,60])

    def test_sent_card_requires_complete_borders_and_adjacent_avatar(self):
        from media_send import sent_card_box
        a=np.full((1040,1920,3),249,np.uint8);a[724:784,1618:1844]=238;a[724:760,1858:1894]=[20,80,130]
        self.assertEqual(sent_card_box(a,732,896),[1618,724,226,60])
        self.assertEqual(sent_card_box(a,736,896),[1618,724,226,60])
        a[724:760,1858:1894]=249;self.assertIsNone(sent_card_box(a,732,896))
        a[724:760,1858:1894]=[20,80,130];a[784:786,1626:1836]=238
        self.assertIsNone(sent_card_box(a,732,896))


class CatalogCacheTests(unittest.TestCase):
    def test_catalog_import_accepts_only_its_fixed_cache_and_png(self):
        import media_send
        from media_send import import_media
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);cid='a'*32
            cache=root/'data/media-cache'/cid;cache.mkdir(parents=True)
            path=cache/'catalog.png';Image.new('RGB',(1440,1800),'white').save(path)
            raw=path.read_bytes()
            item={'id':cid,'media_key':'asset_catalog_v1','type':'image','mime':'image/png',
                  'path':str(path),'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
            with patch.object(media_send,'__file__',str(root/'media_send.py')),patch('media_send.file_dialog',side_effect=RuntimeError('CACHE_VALID')) as dialog:
                with self.assertRaisesRegex(RuntimeError,'CACHE_VALID'):import_media(None,item,'test')
                dialog.assert_called_once()
            for changed in ({**item,'media_key':'unknown'}, {**item,'asset_id':123}, {**item,'mime':'video/mp4'}):
                with patch.object(media_send,'__file__',str(root/'media_send.py')),patch('media_send.file_dialog') as dialog:
                    with self.assertRaises(ValueError):import_media(None,changed,'test')
                    dialog.assert_not_called()
