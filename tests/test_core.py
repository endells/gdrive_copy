"""python -m unittest discover -s gdrive_copy/tests (플러그인 폴더의 상위에서 실행)"""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gdrive_copy.links import CopyError, base_path, message_links, source_link, target_path
from gdrive_copy.store import Store
from gdrive_copy.telegram import poll

FID = '1Sample_Folder-ID_0123456789abcd-'
URL = 'https://drive.google.com/drive/folders/' + FID
BASE = 'endells_gg:/FF'


class LinksTest(unittest.TestCase):
    def test_forms(self):
        for text in (FID, URL + '?hl=ko', URL + '/?usp=sharing', URL.replace('/drive/', '/drive/u/0/'),
                     'https://drive.google.com/open?id=' + FID):
            self.assertEqual(source_link(text), (FID, ''))
        self.assertEqual(source_link(URL+'?hl=ko&resourcekey=0-abc'), (FID, '0-abc'))

    def test_invalid(self):
        for value in ('', 'not a valid link', URL.replace('drive.google.com', 'drive.google.com.example.com'),
                      URL.replace('/folders/', '/file/d/'), URL.replace('drive.google.com','x@drive.google.com'),
                      URL + '?resourcekey=invalid/value'):
            with self.assertRaises(CopyError):
                source_link(value)

    def test_destination(self):
        self.assertEqual(base_path('endells_gg:/FF/'), BASE)
        self.assertEqual(base_path('endells_gg:FF'), BASE)
        self.assertEqual(target_path(BASE,'하이큐!! 쓰레기장의 결전 (2024)'), BASE+'/하이큐!! 쓰레기장의 결전 (2024)')
        self.assertEqual(target_path(BASE,'A/B'), BASE+'/A／B')
        for title in ('', '..', '.', 'one\ntwo'):
            with self.assertRaises(CopyError):
                target_path(BASE, title)
        for base in ('/tmp', '-r:/FF', 'r:/../FF', 'r,root_folder_id=x:'):
            with self.assertRaises(CopyError):
                base_path(base)

    def test_telegram_hidden_urls_and_caption(self):
        second = FID + 'Z'
        self.assertEqual(message_links({'caption': '본문 (' + URL + ')',
            'caption_entities':[{'type':'text_link','url':URL+'?resourcekey=0-AA'}],
            'reply_markup':{'inline_keyboard':[[{'url':URL+'Z'}]]}}), [(FID,'0-AA'),(second,'')])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'queue.db')
        self.conf = dict(self.store.settings(), base=BASE, chat='-10021', token='dummy', telegram=True)
        self.store.save(self.conf)

    def enqueue(self, fid=FID):
        with self.store.db(True) as db:
            return self.store.enqueue(db, fid, '', BASE, self.conf, '수동')

    def test_concurrent_dedup(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: self.enqueue(),range(15)))
        self.assertEqual(sum(v[1] for v in values), 1)
        self.assertEqual(len(set(v[0] for v in values)), 1)

    def test_restart_retry(self):
        idx,_ = self.enqueue()
        self.store.claim()
        with self.assertRaises(CopyError):
            self.store.delete(idx)
        self.store.recover()
        self.assertEqual(self.store.claim()['id'],idx)
        self.store.finish(idx, 'failed', '권한 없음')
        self.store.retry(idx, self.conf)
        self.assertEqual(self.store.claim()['id'],idx)
        self.store.finish(idx, 'done', '복사 완료')
        self.assertEqual(self.store.listing(1,'','done')['total'],1)
        self.assertGreaterEqual(len(self.store.logs()),6)

    def test_different_source_same_title(self):
        i,_ = self.enqueue()
        j,_ = self.enqueue(FID + 'x')
        self.store.reserve(i, '한글', BASE+'/한글', 2,20)
        self.store.copying(i)
        with self.assertRaises(CopyError):
            self.store.reserve(j, '한글', BASE+'/한글', 2,20)

    def test_telegram_cursor_dedup_and_allowlist(self):
        self.enqueue()
        updates = [
            {'update_id':1,'message':{'chat':{'id':999},'text':URL+'wrong'}},
            {'update_id':2,'channel_post':{'chat':{'id':-10021},'text':URL+'\n'+URL+'Z'}},
            {'update_id':3,'edited_channel_post':{'chat':{'id':-10021},'caption':URL+'Z'}}]
        def call(token,method,params):
            self.assertEqual(token,'dummy')
            return {} if method=='getWebhookInfo' else [u for u in updates if u['update_id']>=params['offset']]
        self.assertIn('신규 1건',poll(self.store, self.conf,call))
        self.assertIn('신규 0건',poll(self.store, self.conf,call))
        self.assertEqual(self.store.listing(1,'','')['total'],2)
        with self.store.db() as db:
            self.assertEqual(self.store.value(db,'offset',0),4)
        self.store.save(dict(self.conf,token='changed'))
        with self.store.db() as db:
            self.assertEqual(self.store.value(db,'offset',9),0)

    def test_webhook_unchanged(self):
        with self.assertRaisesRegex(CopyError,'웹훅'):
            poll(self.store, self.conf, lambda token, method, args: {'url':'https://example.org/hook'})

    def test_update_and_offset_rollback_together(self):
        original = self.store.enqueue
        def broken(db, *args):
            original(db, *args)
            raise RuntimeError()
        self.store.enqueue = broken
        update = {'update_id':20, 'message':{'chat':{'id':-10021},'text':URL}}
        with self.assertRaises(RuntimeError):
            poll(self.store,self.conf,lambda t,m,p:{} if m=='getWebhookInfo' else [update])
        self.assertEqual(self.store.listing(1,'','')['total'],0)
        with self.store.db() as db:
            self.assertEqual(self.store.value(db,'offset',0),0)


class GdsTest(unittest.TestCase):
    def test_terminal_status_reissue_has_previous_stamp(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from gdrive_copy.drive import Gds
        p = Mock()
        p.ModelRequestItem.get_by_source_id.return_value = SimpleNamespace(
            id=3, remote_path=BASE+'/제목', status='completed', completed_time='first')
        p.logic.get_module.return_value.add_copy_force.return_value = {'ret':'success'}
        gds = Gds(p)
        job = {'source_id':FID,'gds_id':3,'gds_previous':'','gds_retry':1}
        self.assertEqual(gds.submit(job, BASE+'/제목'), (3,'completed|first'))
        p.logic.get_module.return_value.add_copy_force.assert_called_once_with(3)
        job.update(gds_retry=0,gds_previous='completed|first')
        self.assertEqual(gds.submit(job, BASE+'/제목'), (3,'completed|first'))
        p.logic.get_module.return_value.add_copy_force.assert_called_once()

    def test_different_upstream_target(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from gdrive_copy.drive import Gds
        p = Mock()
        p.ModelRequestItem.get_by_source_id.return_value = SimpleNamespace(remote_path='elsewhere:/')
        with self.assertRaisesRegex(CopyError,'다른 경로'):
            Gds(p).submit({'gds_id':None,'source_id':FID}, BASE+'/제목')
        p.add_copy.assert_not_called()
