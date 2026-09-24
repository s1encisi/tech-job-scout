"""Explicit official provider closure codes, never generic HTTP errors."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError
from email.message import Message
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import scout,body_fields as bf,discovery as d
import test_phase1 as fixtures
from public_http import PublicHTTP,FetchError
from job_sources import Tencent,PostingClosed


class LifecycleTests(unittest.TestCase):
    def setUp(self):self.fixture=fixtures.Phase1Tests();self.fixture.setUp();self.root=self.fixture.root
    def tearDown(self):self.fixture.tearDown()
    def job(self):
        key,job,ref=self.fixture.insert()
        job['url']='https://careers.tencent.com/jobdesc.html?postId=one'
        with scout.connect(self.root) as c:c.execute('UPDATE discovery_jobs SET payload=? WHERE key=?',(scout.dumps(job),key))
        return key,job
    def receipt(self,code='E1005',ident='one'):
        return fixtures.capture(self.root,{'Code':500,'Data':code},name='error',url='https://careers.tencent.com/tencentcareer/api/post/ByPostId?postId='+ident+'&language=zh-cn')
    def test_official_closure_is_hidden_but_history_kept(self):
        key,job=self.job();receipt=self.receipt()
        with scout.connect(self.root) as c:bf.mark_tencent_closed(c,self.root,key,receipt,'test')
        self.assertEqual(d.search(self.root),[])
        rows=d.search(self.root,include_excluded=True)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['source_status'],'closed')
    def test_generic_server_error_cannot_close_posting(self):
        key,job=self.job()
        with scout.connect(self.root) as c:
            with self.assertRaises(ValueError):bf.mark_tencent_closed(c,self.root,key,self.receipt('E1004'),'test')
        self.assertEqual(d.search(self.root)[0]['source_status'],'listed')
    def test_other_posting_error_cannot_close_this_job(self):
        key,job=self.job()
        with scout.connect(self.root) as c:
            with self.assertRaises(ValueError):bf.mark_tencent_closed(c,self.root,key,self.receipt(ident='different'),'test')
    def test_tencent_adapter_preserves_http_error_evidence(self):
        key,job=self.job();client=PublicHTTP(self.root,'http',interval=0,retries=0)
        headers=Message();headers['Content-Type']='application/json'
        error=HTTPError('https://careers.tencent.com/tencentcareer/api/post/ByPostId?postId=one&language=zh-cn',500,'error',headers,io.BytesIO(b'{"Code":500,"Data":"E1005"}'))
        with mock.patch.object(client,'_allowed'),mock.patch.object(client.opener,'open',side_effect=error):
            with self.assertRaises(PostingClosed) as raised:Tencent().detail(client,job)
        self.assertTrue((self.root/raised.exception.receipt['path']).exists())
