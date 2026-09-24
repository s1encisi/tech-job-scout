"""Phase 1 invariants: provenance, budgets, recovery, coverage and sampling."""
import copy
from datetime import timedelta
from email.message import Message
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import scout
import discovery as d
import body_fields as bf
import body_backfill as backfill
import phase1_reports as reports
from public_http import PublicHTTP, BudgetExceeded, FetchError
from job_sources import plain, normalize

CAT={'version':'test','companies':[{'id':'fiction','name':'虚构科技公司','sector':'人工智能','priority':1,
     'sources':[{'id':'fake-list','url':'https://jobs.example.test/','api_url':'https://jobs.example.test/api/list','adapter':'netease','channels':['fulltime']}]}]}


def capture(root,payload,name='capture',when=None,url='https://jobs.example.test/api/list'):
    file=root/'evidence'/(name+'.txt');scout.write_json(file,payload)
    receipt={'path':file.relative_to(root).as_posix(),'url':url,'captured_at':when or scout.timestamp(),
             'sha256':scout.digest(file.read_bytes()),'request_body':None}
    scout.write_json(file.with_suffix('.meta.json'),receipt)
    return receipt


class FakeHTTP:
    def __init__(self,root,payload=None,error=None):self.root=root;self.payload=payload;self.error=error;self.requests=0;self.detail_attempts=0
    def json(self,url,body=None):
        self.requests+=1;self.detail_attempts+=1
        if self.error:raise self.error
        payload=copy.deepcopy(self.payload)
        receipt=capture(self.root,payload,'detail-'+str(self.requests),url=url)
        return payload,receipt
    def metrics(self):return {'requests':self.requests,'detail_attempts':self.detail_attempts,'retries':0}


class Phase1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=d.initialize(Path(self.tmp.name)/'work')
        self.patch=mock.patch.object(d,'registry',return_value=copy.deepcopy(CAT));self.patch.start()
        with scout.connect(self.root) as c:bf.ensure_schema(c)
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def insert(self,ident='one',description='职责原文',requirements='',source='fake-list'):
        co=CAT['companies'][0];src={**co['sources'][0],'id':source}
        raw={'id':ident,'name':'软件工程师','description':description,'requirement':requirements,'workType':'0','workPlaceNameList':['上海']}
        receipt=capture(self.root,{'code':200,'data':{'list':[raw],'total':1,'pages':1}},'list-'+ident)
        job=normalize(co,src,ident,raw['name'],'https://hr.163.com/api/hr163/position/query?id='+ident,
                      description=description,requirements=requirements,locations=['上海'],employment_type='fulltime',raw=raw)
        with scout.connect(self.root) as c:
            d.save_jobs(c,self.root,'fixture',[job],receipt)
            row=c.execute('SELECT * FROM discovery_jobs WHERE source_id=? ORDER BY key',(source,)).fetchall()
            key=next(r['key'] for r in row if json.loads(r['payload'])['source_job_id']==ident)
        return key,job,receipt
    def field(self,key,field):
        with scout.connect(self.root) as c:return dict(c.execute('SELECT * FROM discovery_field_state WHERE job_key=? AND field=?',(key,field)).fetchone())
    def task(self,key):
        with scout.connect(self.root) as c:return dict(c.execute('SELECT * FROM discovery_body_tasks WHERE job_key=?',(key,)).fetchone())
    def test_01_absent_list_field_is_not_unpublished(self):
        self.assertEqual(bf.extract('netease',{'description':'x'},'list')['requirements']['status'],'not_requested')
    def test_02_absent_detail_field_is_parse_failure(self):
        self.assertEqual(bf.extract('netease',{'description':'x'},'detail')['requirements']['status'],'parse_failed')
    def test_03_explicit_empty_detail_is_distinct_from_missing(self):
        result=bf.extract('netease',{'description':'x','requirement':''},'detail')
        self.assertEqual(result['requirements']['status'],'not_disclosed')
    def test_04_html_paragraphs_are_preserved(self):
        self.assertEqual(plain('<p>第一段</p><ul><li>第二段</li><li>第三段</li></ul>'),'第一段\n第二段\n第三段')
    def test_05_cache_recovers_body_without_network(self):
        key,job,receipt=self.insert(requirements='完整要求')
        with scout.connect(self.root) as c:
            job['description']='';c.execute('UPDATE discovery_jobs SET payload=? WHERE key=?',(scout.dumps(job),key))
        with mock.patch('socket.socket',side_effect=AssertionError('Network forbidden')):result=backfill.prepare(self.root)
        self.assertEqual(result['restored_jobs'],1)
        self.assertEqual(result['network_requests'],0)
        self.assertEqual(d.search(self.root)[0]['description'],'职责原文')
    def test_06_corrupt_cache_is_reported_and_not_applied(self):
        key,job,receipt=self.insert()
        (self.root/receipt['path']).write_text('tampered',encoding='utf-8')
        result=backfill.prepare(self.root)
        self.assertTrue(result['cache']['errors']);self.assertEqual(d.search(self.root)[0]['requirements'],'')
    def test_07_prepare_is_idempotent(self):
        key,_,_=self.insert();backfill.prepare(self.root);backfill.prepare(self.root)
        with scout.connect(self.root) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM discovery_body_tasks').fetchone()[0],1)
            self.assertEqual(self.task(key)['attempts'],0)
    def test_08_detail_backfill_preserves_identity_notes_and_list_time(self):
        key,job,receipt=self.insert()
        with scout.connect(self.root) as c:
            old=dict(c.execute('SELECT * FROM discovery_jobs WHERE key=?',(key,)).fetchone())
            c.execute('INSERT INTO discovery_notes VALUES(?,?,?,?)',(key,'已投递','高','保留'))
        backfill.prepare(self.root)
        client=FakeHTTP(self.root,{'code':200,'data':{'id':'one','description':'完整职责','requirement':'完整要求'}})
        result=backfill.run(self.root,client=client,max_tasks=1)
        with scout.connect(self.root) as c:
            new=dict(c.execute('SELECT * FROM discovery_jobs WHERE key=?',(key,)).fetchone())
            self.assertEqual(c.execute('SELECT note FROM discovery_notes WHERE key=?',(key,)).fetchone()[0],'保留')
        self.assertEqual(old['first_seen'],new['first_seen']);self.assertEqual(old['last_seen'],new['last_seen'])
        self.assertEqual(result['detail_attempts'],1);self.assertEqual(self.task(key)['status'],'succeeded')
    def test_09_failed_detail_keeps_last_good_text(self):
        key,job,receipt=self.insert(description='已有职责')
        backfill.prepare(self.root)
        client=FakeHTTP(self.root,error=FetchError('HTTP 500'))
        backfill.run(self.root,client=client,max_tasks=1)
        self.assertEqual(d.search(self.root)[0]['description'],'已有职责')
        self.assertEqual(self.field(key,'requirements')['status'],'fetch_failed')
        self.assertEqual(self.field(key,'description')['last_attempt_status'],'fetch_failed')
        self.assertEqual(self.task(key)['attempts'],1)
    def test_10_completed_task_not_requested_again(self):
        key,_,_=self.insert();backfill.prepare(self.root)
        client=FakeHTTP(self.root,{'code':200,'data':{'id':'one','description':'职责','requirement':'要求'}})
        backfill.run(self.root,client=client,max_tasks=1)
        idle=FakeHTTP(self.root,error=AssertionError('Should not request'))
        self.assertEqual(backfill.run(self.root,client=idle)['requests'],0)
    def test_11_budget_counts_failed_retries(self):
        client=PublicHTTP(self.root,'budget',max_requests=100,max_detail_attempts=2,interval=0,retries=2)
        client.request_kind='detail';headers=Message()
        error=HTTPError('https://jobs.example.test/',500,'error',headers,None)
        with mock.patch.object(client.opener,'open',side_effect=error) as opened,mock.patch('public_http.time.sleep'):
            with self.assertRaises(BudgetExceeded):client._read('https://jobs.example.test/')
            self.assertEqual(opened.call_count,2)
        self.assertEqual(client.detail_attempts,2)
    def test_12_redirects_share_global_budget(self):
        client=PublicHTTP(self.root,'redirects',max_requests=2,interval=0)
        client._before_attempt('https://jobs.example.test/a','list')
        client._redirect_attempt('https://jobs.example.test/b')
        with self.assertRaises(BudgetExceeded):client._redirect_attempt('https://jobs.example.test/c')
        self.assertEqual(client.requests,2);self.assertEqual(client.redirects,1)
    def test_13_robots_requests_do_not_spend_detail_budget(self):
        client=PublicHTTP(self.root,'robots',max_requests=2,max_detail_attempts=1,interval=0)
        client._before_attempt('https://jobs.example.test/robots.txt','robots')
        client._before_attempt('https://jobs.example.test/job','detail')
        self.assertEqual(client.detail_attempts,1);self.assertEqual(client.requests,2)
    def test_14_logical_task_limit_also_bounds_fake_client(self):
        self.insert('one');self.insert('two');backfill.prepare(self.root)
        client=FakeHTTP(self.root,error=FetchError('HTTP 500'))
        result=backfill.run(self.root,client=client,max_tasks=1,max_detail_attempts=30)
        self.assertEqual(result['requests'],1)
    def test_15_expired_lease_is_recoverable(self):
        key,_,_=self.insert();backfill.prepare(self.root)
        before=scout.timestamp(scout.now_utc()-timedelta(minutes=10))
        with scout.connect(self.root) as c:
            c.execute("UPDATE discovery_body_tasks SET status='running',lease_owner='old',lease_until=?",(before,))
            ready=backfill.candidates(c,scout.timestamp())
        self.assertEqual(ready[0]['job_key'],key);self.assertEqual(self.task(key)['status'],'retry')
    def test_16_interrupt_records_attempt_and_releases_lease(self):
        key,_,_=self.insert();backfill.prepare(self.root)
        client=FakeHTTP(self.root,error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):backfill.run(self.root,client=client)
        task=self.task(key);self.assertEqual(task['status'],'retry');self.assertEqual(task['attempts'],1)
        self.assertEqual(task['http_attempts'],1);self.assertIsNone(task['lease_owner'])
        self.assertFalse((self.root/'state/run.lock').exists())
    def test_17_empty_new_detail_preserves_prior_evidence(self):
        key,_,_=self.insert(requirements='原要求')
        first=capture(self.root,{'id':'one'},'proof1')
        with scout.connect(self.root) as c:bf.apply(c,self.root,key,bf.extract('netease',{'description':'原职责','requirement':'原要求'},'detail'),first,'detail','first')
        later=capture(self.root,{'id':'one'},'proof2',scout.timestamp(scout.now_utc()+timedelta(seconds=1)))
        for _ in range(2):
            with scout.connect(self.root) as c:bf.apply(c,self.root,key,bf.extract('netease',{'description':'原职责','requirement':''},'detail'),later,'detail','empty')
        field=self.field(key,'requirements')
        self.assertEqual(field['status'],'conflict');self.assertEqual(json.loads(field['evidence'])['path'],first['path'])
        self.assertEqual(d.search(self.root)[0]['requirements'],'原要求')
    def test_18_list_summary_does_not_overwrite_detail(self):
        key,job,receipt=self.insert()
        full=capture(self.root,{'id':'one'},'full')
        with scout.connect(self.root) as c:bf.apply(c,self.root,key,bf.extract('netease',{'description':'完整职责','requirement':'完整要求'},'detail'),full,'detail','full')
        short=capture(self.root,{'id':'one'},'short',scout.timestamp(scout.now_utc()+timedelta(seconds=1)))
        job.update(description='摘要',requirements='',raw={'id':'one','description':'摘要','requirement':''})
        with scout.connect(self.root) as c:d.save_jobs(c,self.root,'short',[job],short)
        self.assertEqual(d.search(self.root)[0]['description'],'完整职责')
    def test_19_new_list_body_can_update_prior_list_body(self):
        key,job,receipt=self.insert(requirements='要求')
        newer=capture(self.root,{'id':'one'},'new',scout.timestamp(scout.now_utc()+timedelta(seconds=1)))
        job.update(description='新职责',raw={'id':'one','description':'新职责','requirement':'要求'})
        with scout.connect(self.root) as c:d.save_jobs(c,self.root,'new',[job],newer)
        self.assertEqual(d.search(self.root)[0]['description'],'新职责')
    def test_20_report_joint_presence_is_computed_directly(self):
        self.insert('one',requirements='要求');self.insert('two',description='',requirements='要求')
        report=reports.audit(self.root,self.root/'audit')
        self.assertEqual(report['quality']['both_nonempty'],1)
        self.assertIsNone(report['quality']['capture_completeness_rate']);self.assertIsNone(report['quality']['field_accuracy_rate'])
    def test_21_catalog_size_is_not_employer_denominator(self):
        self.insert();report=reports.audit(self.root,self.root/'audit')
        self.assertEqual(report['coverage']['catalog_entries'],1);self.assertEqual(report['coverage']['unresolved_catalog_entries'],1)
        self.assertIsNone(report['coverage']['formal_denominator']);self.assertIsNone(report['coverage']['partial_enterprise_coverage'])
    def test_22_audit_read_does_not_create_tables(self):
        with scout.connect(self.root) as c:before=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        reports.audit(self.root,self.root/'audit')
        with scout.connect(self.root) as c:after=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertEqual(before,after)
    def test_23_sampling_is_reproducible_and_unlabelled(self):
        for i in range(6):self.insert(str(i),requirements='要求' if i%2 else '')
        a=reports.sample(self.root,self.root/'sample-a',['fake-list'],per_source=4,seed='fixed')
        b=reports.sample(self.root,self.root/'sample-b',['fake-list'],per_source=4,seed='fixed')
        self.assertEqual(a['snapshot_digest'],b['snapshot_digest']);self.assertEqual(a['gold_labels_completed'],0)
        payload=scout.read_json(self.root/'sample-a/samples.json')
        self.assertTrue(all(r['gold']['title'] is None for r in payload['records']))
    def test_24_sample_shortfall_is_not_padded(self):
        self.insert();summary=reports.sample(self.root,self.root/'sample',['fake-list'],per_source=30)
        self.assertEqual(summary['samples'],1);self.assertEqual(summary['shortfalls']['fake-list'],29)
    def test_25_phase1_limits_cannot_be_raised_above_authorization(self):
        with self.assertRaises(ValueError):backfill.run(self.root,max_http_attempts=101)
        with self.assertRaises(ValueError):backfill.run(self.root,max_detail_attempts=31)
    def test_26_incomplete_mapping_does_not_mark_task_success(self):
        key,_,_=self.insert();backfill.prepare(self.root)
        client=FakeHTTP(self.root,{'code':200,'data':{'id':'one','description':'职责'}})
        backfill.run(self.root,client=client,max_tasks=1)
        self.assertEqual(self.task(key)['status'],'retry');self.assertEqual(self.field(key,'requirements')['status'],'parse_failed')
    def test_27_blocked_request_is_not_zero_jobs_success(self):
        key,_,_=self.insert();backfill.prepare(self.root)
        backfill.run(self.root,client=FakeHTTP(self.root,error=FetchError('HTTP 405')),max_tasks=1)
        self.assertEqual(self.task(key)['status'],'blocked')
    def test_28_source_round_robin_does_not_starve_small_source(self):
        with scout.connect(self.root) as c:
            for i,source in enumerate(['a','a','a','b','b']):backfill.enqueue(c,str(i),source,'requirements',True,scout.timestamp())
            ready=backfill.candidates(c,scout.timestamp())
        self.assertEqual([x['source_id'] for x in ready[:4]],['a','b','a','b'])

    def test_29_frozen_cohort_keeps_exact_jobs_after_values_change(self):
        for i in range(6):self.insert(str(i),requirements='要求' if i%2 else '')
        reports.sample(self.root,self.root/'before',['fake-list'],per_source=4)
        original=scout.read_json(self.root/'before/samples.json')
        before_csv=(self.root/'before/review.csv').read_bytes()
        with scout.connect(self.root) as c:
            for row in c.execute('SELECT key,payload FROM discovery_jobs').fetchall():
                job=json.loads(row['payload']);job['requirements']='补齐要求'
                c.execute('UPDATE discovery_jobs SET payload=? WHERE key=?',(scout.dumps(job),row['key']))
        reports.sample(self.root,self.root/'after',['fake-list'],cohort=self.root/'before/samples.json')
        revised=scout.read_json(self.root/'after/samples.json')
        self.assertEqual([r['job_key'] for r in original['records']],[r['job_key'] for r in revised['records']])
        self.assertEqual(before_csv,(self.root/'before/review.csv').read_bytes())
        self.assertEqual(revised['requested_per_source'],4)
    def test_30_already_filled_pending_task_needs_no_http(self):
        key,_,_=self.insert();backfill.prepare(self.root)
        receipt=capture(self.root,{'id':'one'},'filled')
        with scout.connect(self.root) as c:bf.apply(c,self.root,key,bf.extract('netease',{'description':'完整职责','requirement':'完整要求'},'detail'),receipt,'detail','daily')
        result=backfill.run(self.root,client=FakeHTTP(self.root,error=AssertionError('No request expected')))
        self.assertEqual(result['requests'],0);self.assertEqual(self.task(key)['status'],'succeeded')
    def test_31_priority_samples_preserve_source_fairness(self):
        with scout.connect(self.root) as c:
            for key,source in [('a1','a'),('a2','a'),('b1','b'),('b2','b')]:backfill.enqueue(c,key,source,'requirements',True,scout.timestamp())
            result=backfill.candidates(c,scout.timestamp(),prioritize_keys={'a2','b2'})
        self.assertEqual([r['job_key'] for r in result[:2]],['a2','b2'])
    def test_32_total_http_and_detail_attempts_are_separate(self):
        key,_,_=self.insert();backfill.prepare(self.root)
        client=FakeHTTP(self.root,{'code':200,'data':{'id':'one','description':'职责','requirement':'要求'}})
        original=client.json
        def with_robot(url,body=None):
            client.requests+=1
            return original(url,body)
        client.json=with_robot
        backfill.run(self.root,client=client,max_tasks=1)
        task=self.task(key);self.assertEqual(task['http_attempts'],2);self.assertEqual(task['detail_attempts'],1)
    def test_33_formal_coverage_requires_channel_inventory(self):
        self.insert()
        proof=self.root/'evidence/review.txt';proof.write_text('OFFLINE REVIEW FIXTURE',encoding='utf-8')
        ref={'path':'evidence/review.txt','sha256':scout.digest(proof.read_bytes())}
        review={'entries':{'fiction':{'review_status':'confirmed','employer_id':'reviewed-employer','evidence_refs':[ref],
          'channel_inventory_status':'confirmed','applicable_channels':['social','internship']}},
          'source_reviews':{'fake-list':{'review_status':'confirmed','evidence_refs':[ref],'collection_mode':'automated','verified_channels':['social']}}}
        scout.write_json(self.root/'sources/entity_review.json',review)
        co=CAT['companies'][0]
        with scout.connect(self.root) as c:d.save_source(c,co,co['sources'][0],'complete',None,1,1,1,'',None)
        report=reports.audit(self.root,self.root/'audit')
        self.assertEqual(report['coverage']['formal_denominator'],1)
        self.assertEqual(report['coverage']['partial_enterprise_coverage'],1)
        self.assertEqual(report['coverage']['sufficient_enterprise_coverage'],0)
        self.assertIsNone(report['coverage']['national_coverage'])

    def review_fixture(self,channels):
        proof=self.root/'evidence/entity.txt';proof.write_text('FICTIONAL REVIEW',encoding='utf-8')
        ref={'path':'evidence/entity.txt','sha256':scout.digest(proof.read_bytes())}
        scout.write_json(self.root/'sources/entity_review.json',{'entries':{'fiction':{'review_status':'confirmed','employer_id':'employer',
          'evidence_refs':[ref],'channel_inventory_status':'confirmed','applicable_channels':channels}},
          'source_reviews':{'fake-list':{'review_status':'confirmed','collection_mode':'automated','evidence_refs':[ref],'verified_channels':channels}}})
        co=CAT['companies'][0]
        with scout.connect(self.root) as c:d.save_source(c,co,co['sources'][0],'complete',None,0,1,0,'',None)
    def test_34_employment_type_is_not_a_verified_channel(self):
        self.review_fixture(['fulltime'])
        result=reports.audit(self.root,self.root/'audit')
        self.assertEqual(result['coverage']['partial_enterprise_coverage'],0)
        self.assertEqual(result['coverage']['channel_applicability_unverified_sources'],1)
    def test_35_future_source_state_is_not_fresh_at_past_asof(self):
        self.review_fixture(['social'])
        past=scout.timestamp(scout.now_utc()-timedelta(days=1))
        result=reports.audit(self.root,self.root/'audit',asof=past)
        self.assertEqual(result['coverage']['partial_enterprise_coverage'],0)
        self.assertFalse(result['sources'][0]['fresh_72h'])
    def test_36_changed_frozen_sample_is_rejected(self):
        self.insert();reports.sample(self.root,self.root/'before',['fake-list'],per_source=1)
        file=self.root/'before/samples.json';value=scout.read_json(file)
        value['records'][0]['candidate']['title']='changed';scout.write_json(file,value)
        with self.assertRaises(ValueError):reports.sample(self.root,self.root/'after',['fake-list'],cohort=file)
    def test_37_future_cached_capture_is_rejected(self):
        receipt=capture(self.root,{'id':'one'},when=scout.timestamp(scout.now_utc()+timedelta(minutes=10)))
        with self.assertRaises(ValueError):bf.checked_evidence(self.root,receipt)

class RecordedContractTests(unittest.TestCase):
    def test_real_response_shapes_replay_without_network(self):
        folder=Path(__file__).parent/'recorded_responses'
        manifest=scout.read_json(folder/'manifest.json')
        self.assertEqual(len(manifest['fixtures']),6)
        with mock.patch('socket.socket',side_effect=AssertionError('Network forbidden')):
            for item in manifest['fixtures']:
                with self.subTest(adapter=item['adapter'],kind=item['kind']):
                    file=folder/item['file']
                    self.assertEqual(scout.digest(file.read_bytes()),item['fixture_sha256'])
                    records,kind=bf.decode_response(item['adapter'],scout.read_json(file))
                    self.assertEqual(kind,item['kind']);self.assertEqual(set(records),{'10001'})
                    fields=bf.extract(item['adapter'],records['10001'],kind)
                    if kind=='detail':
                        self.assertEqual(fields['description']['status'],'obtained')
                        self.assertEqual(fields['requirements']['status'],'obtained')
