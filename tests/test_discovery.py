"""Deterministic collector tests; fixtures contain no real employers or jobs."""
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import discovery as d
import scout
import job_sources as adapters
from public_http import FetchError, PublicHTTP, BudgetExceeded
import xlsx_writer as xw
import run_daily


CATALOG={'companies':[{'id':'fiction','name':'虚构科技公司','sector':'人工智能','priority':1,
    'sources':[{'id':'fixture-list','url':'https://jobs.example.test/','api_url':'https://jobs.example.test/api/list','adapter':'netease'}]}]}


def record(ident, title='软件研发实习生', work='1'):
    return {'id':ident,'name':title,'workType':work,'description':'开发软件与测试','requirement':'掌握 Python',
            'workPlaceNameList':['上海'],'reqEducationName':'本科','reqWorkYearsName':'不限'}


class Client:
    def __init__(self,root,pages):self.root=root;self.pages=pages;self.calls=[]
    def json(self,url,body=None):
        page=body['currentPage'];self.calls.append(page);value=self.pages[page]
        if isinstance(value,Exception):raise value
        raw=json.dumps(value,ensure_ascii=False).encode();path=self.root/'evidence'/('fixture-'+str(page)+'.txt');path.write_bytes(raw)
        return value,{'url':url,'captured_at':scout.timestamp(),'sha256':hashlib.sha256(raw).hexdigest(),'path':path.relative_to(self.root).as_posix()}
    def metrics(self):return {'requests':len(self.calls),'retries':0}


def page(rows,total=2,pages=2):return {'code':200,'data':{'list':rows,'total':total,'pages':pages}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=d.initialize(Path(self.tmp.name)/'work')
        self.real_registry=d.registry
        self.patch=mock.patch.object(d,'registry',return_value=copy.deepcopy(CATALOG));self.patch.start()
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def collect(self,pages,**kwargs):
        return d.collect(self.root,client=Client(self.root,pages),max_details=0,interval=0,**kwargs)
    def test_01_resume_continues_next_page(self):
        self.collect({1:page([record(1)])},max_pages=1,page_size=1)
        report=self.collect({2:page([record(2)])},max_pages=1,page_size=1,resume=True)
        self.assertEqual(report['sources'][0]['pages'],2);self.assertEqual(report['status'],'complete')
        self.assertEqual(len(d.search(self.root)),2)
    def test_02_repeated_collection_is_idempotent(self):
        pages={1:page([record(1)],1,1)}
        self.collect(pages);self.collect(pages)
        with scout.connect(self.root) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM discovery_jobs').fetchone()[0],1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM discovery_events WHERE kind='new'").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM discovery_events WHERE kind='changed'").fetchone()[0],0)
    def test_03_repeated_page_cannot_report_complete(self):
        report=self.collect({1:page([record(1)]),2:page([record(1)])},page_size=1)
        self.assertEqual(report['sources'][0]['status'],'partial')
        self.assertIn('Repeated page',report['sources'][0]['error'])
    def test_04_failure_preserves_cursor_and_records(self):
        report=self.collect({1:page([record(1)]),2:FetchError('HTTP 429')},page_size=1)
        self.assertEqual(report['sources'][0]['next_cursor'],2);self.assertEqual(len(d.search(self.root)),1)
    def test_05_empty_page_with_positive_total_is_partial(self):
        report=self.collect({1:page([],2,1)})
        self.assertEqual(report['status'],'partial')
    def test_06_unknown_response_is_not_zero_jobs_success(self):
        report=self.collect({1:{'code':200,'data':{}}})
        self.assertEqual(report['sources'][0]['status'],'blocked')
    def test_07_filters_combine_company_type_city_and_keyword(self):
        self.collect({1:page([record(1),record(2,'Java后端工程师','0')],2,1)})
        found=d.search(self.root,keyword='Python',company='虚构',kind='internship',city='上海',role='后端与分布式')
        self.assertEqual(found,[])
        self.assertEqual(len(d.search(self.root,kind='fulltime',keyword='Java',city='上海')),1)
    def test_08_stale_listings_can_be_explicitly_included(self):
        self.collect({1:page([record(1)],1,1)})
        with scout.connect(self.root) as c:c.execute('UPDATE discovery_jobs SET last_seen=?',(scout.timestamp(scout.now_utc()-timedelta(days=4)),))
        self.assertEqual(d.search(self.root),[]);self.assertEqual(d.search(self.root,include_stale=True)[0]['freshness'],'stale')
    def test_09_environment_engineering_excluded_game_art_kept(self):
        self.collect({1:page([record(1,'环境工程师','0'),record(2,'游戏环境美术','0')],2,1)})
        self.assertEqual([j['title'] for j in d.search(self.root)],['游戏环境美术'])
    def test_10_export_preserves_manual_notes(self):
        self.collect({1:page([record(1)],1,1)});key=d.search(self.root)[0]['key']
        target=self.root/'exports/科技岗位发现_latest.xlsx'
        xw.write_xlsx(target,[('人工跟进',[['岗位唯一键','人工投递状态','人工优先级','人工备注'],[key,'已投递','高','保留备注']])])
        d.export(self.root);self.assertEqual(xw.read_followup(target)[0][3],'保留备注')
        with ZipFile(target) as z:self.assertEqual(len([x for x in z.namelist() if x.startswith('xl/worksheets/sheet')]),7)
    def test_11_tampered_evidence_is_rejected(self):
        evidence={'path':'evidence/invalid.txt','captured_at':scout.timestamp(),'sha256':'wrong'}
        (self.root/evidence['path']).write_text('fixture',encoding='utf-8')
        with scout.connect(self.root) as c:
            with self.assertRaises(ValueError):d.save_jobs(c,self.root,'x',[],evidence)
    def test_12_plan_has_both_types_and_specific_queries(self):
        plan=d.make_plan(self.root);self.assertEqual(plan['tasks'],2)
        self.assertEqual({t['employment_type'] for t in plan['queue']},{'internship','fulltime'})
        self.assertTrue(all(t['queries'] for t in plan['queue']))
    def test_13_runtime_reports_do_not_trigger_code_drift(self):
        before=run_daily.watched_hashes(self.root)
        (self.root/'reports/new.json').write_text('{}',encoding='utf-8')
        self.assertEqual(before,run_daily.watched_hashes(self.root))
    def test_14_experienced_jobs_are_not_automatically_excluded(self):
        from fixtures import FictionalJobFixture
        p=scout.read_json(self.root/'profile.json')
        for axis,value in [('degree_program','master'),('major','计算机科学与技术'),('graduation_year',2027)]:p['facts'][axis]={'value':value,'status':'confirmed'}
        scout.write_json(self.root/'profile.json',p)
        fixture=FictionalJobFixture();fixture.root=self.root;fixture.rid=scout.begin(self.root)['run_id'];fixture.company='虚构科技公司';fixture.prefix='https://jobs.example.test/acme';fixture.title='软件工程师'
        job=fixture.make_job();job['job_type']='experienced'
        result=scout.evaluate(job,p,scout.read_json(self.root/'policy.json'),trusted=True,source_fresh=True,evidence_fresh=True)
        self.assertEqual(result['bucket'],'eligible')
    def test_15_request_budget_stops_before_network(self):
        client=PublicHTTP(self.root,'budget-test',max_requests=0)
        with mock.patch.object(client.opener,'open',side_effect=AssertionError('network')):
            with self.assertRaises(BudgetExceeded):client._read('https://example.test/jobs')
    def test_16_unknown_company_id_is_an_error(self):
        with self.assertRaises(ValueError):d.collect(self.root,companies=['missing'],client=Client(self.root,{}))
    def test_17_unpublished_type_is_not_assumed_fulltime(self):
        self.assertEqual(adapters.employment('游戏策划'), 'unknown')
    def test_18_structured_html_reads_jobposting_and_next(self):
        html='<script type="application/ld+json">'+json.dumps({'@type':'JobPosting','title':'Backend Intern','url':'/jobs/1','description':'Build services','employmentType':'INTERN','identifier':{'value':'one'}})+'</script><a rel="next" href="?page=2">Next</a>'
        client=mock.Mock();client.text.return_value=(html,{'path':'fixture'})
        company=CATALOG['companies'][0];source=company['sources'][0]
        result=adapters.StructuredHTML().page(client,company,source,None,10)
        self.assertEqual(result.jobs[0]['employment_type'],'internship');self.assertIn('page=2',result.next_cursor)
    def test_19_html_without_job_schema_queues_browser(self):
        client=mock.Mock();client.text.return_value=('<div id="app"></div>',{'path':'fixture'})
        company=CATALOG['companies'][0]
        self.assertEqual(adapters.StructuredHTML().page(client,company,company['sources'][0],None,10).outcome,'browser_required')
    def test_20_mihoyo_uses_published_job_nature(self):
        client=mock.Mock();client.json.return_value=({'code':0,'data':{'list':[{'id':'one','title':'CG动画实习生','jobNature':'实习','addressDetailList':[{'addressDetail':'上海'}]}],'pageNo':1,'total':1}}, {})
        company=CATALOG['companies'][0];source={**company['sources'][0],'hire_type':1}
        result=adapters.Mihoyo().page(client,company,source,None,10)
        self.assertEqual(result.jobs[0]['employment_type'],'internship');self.assertIn('/campus/position/',result.jobs[0]['url'])
    def test_21_mihoyo_wrong_page_is_rejected(self):
        client=mock.Mock();client.json.return_value=({'code':0,'data':{'list':[],'pageNo':1,'total':20}}, {})
        company=CATALOG['companies'][0];source={**company['sources'][0],'hire_type':1}
        with self.assertRaises(ValueError):adapters.Mihoyo().page(client,company,source,2,10)

    def test_22_runtime_source_overlay_keeps_bundled_catalog(self):
        path=self.root/'evidence/official.txt';path.write_text('新科技 https://jobs.new.example.test/',encoding='utf-8')
        d.add_source(self.root,'new-tech','新科技','人工智能','https://jobs.new.example.test/','https://new.example.test/careers','evidence/official.txt')
        catalog=self.real_registry(self.root)
        self.assertTrue(any(c['id']=='new-tech' for c in catalog['companies']))
        self.assertTrue(any(c['id']=='tencent' for c in catalog['companies']))
    def test_23_source_registration_checks_actual_link(self):
        (self.root/'evidence/official.txt').write_text('新科技',encoding='utf-8')
        with self.assertRaises(ValueError):d.add_source(self.root,'new-tech','新科技','人工智能','https://jobs.new.example.test/','https://new.example.test/','evidence/official.txt')
    def capture(self):
        body='软件实习 上海 开发软件 掌握Python https://jobs.example.test/123'
        path=self.root/'evidence/browser.txt';path.write_text(body,encoding='utf-8')
        payload={'company_id':'fiction','source_id':'fixture-list','pagination_complete':False,
          'evidence':{'path':'evidence/browser.txt','url':'https://jobs.example.test/','captured_at':scout.timestamp(),'sha256':scout.digest(path.read_bytes())},
          'jobs':[{'source_job_id':'123','title':'软件实习','url':'https://jobs.example.test/123','locations':['上海'],
                   'employment_type':'internship','description':'开发软件','requirements':'掌握Python'}]}
        file=self.root/'inbox/capture.json';scout.write_json(file,payload);return file,payload
    def test_24_browser_capture_imports_into_search(self):
        file,payload=self.capture();result=d.import_capture(self.root,file)
        self.assertEqual(result['new'],1);self.assertEqual(d.search(self.root,kind='internship')[0]['title'],'软件实习')
    def test_25_browser_capture_rejects_invented_link(self):
        file,payload=self.capture();payload['jobs'][0]['url']='https://jobs.example.test/invented-123';scout.write_json(file,payload)
        with self.assertRaises(ValueError):d.import_capture(self.root,file)
    def test_26_daily_browser_drain_is_idempotent(self):
        file,payload=self.capture();directory=self.root/'inbox/browser';directory.mkdir();scout.write_json(directory/'batch.json',payload)
        first=run_daily.drain_browser_captures(self.root);second=run_daily.drain_browser_captures(self.root)
        self.assertEqual(first[0]['status'],'imported');self.assertEqual(second,[])

    def test_27_resume_preserves_original_page_size(self):
        self.collect({1:page([record(1)])},max_pages=1,page_size=1)
        client=Client(self.root,{2:page([record(2)])})
        with mock.patch.object(client,'json',wraps=client.json) as call:
            d.collect(self.root,client=client,max_pages=1,page_size=50,max_details=0,resume=True)
            self.assertEqual(call.call_args.args[1]['pageSize'],1)
    def test_28_explicit_expired_posting_is_hidden_by_default(self):
        self.collect({1:page([record(1)],1,1)})
        job=d.search(self.root)[0]
        job['valid_through']='2000-01-01T00:00:00+00:00'
        self.assertEqual(d.classify(job)['source_status'],'expired')

    def test_29_same_source_id_keeps_key_when_url_changes(self):
        self.collect({1:page([record(1)],1,1)})
        original=d.search(self.root)[0];key=original.pop('key');evidence=original.pop('evidence')
        original['url']='https://hr.163.com/job-list.html/1'
        with scout.connect(self.root) as c:d.save_jobs(c,self.root,'new-url',[original],evidence)
        found=d.search(self.root);self.assertEqual(len(found),1);self.assertEqual(found[0]['key'],key)

    def test_30_daily_drain_registers_source_before_capture_phase(self):
        (self.root/'evidence/official.txt').write_text('新科技 https://jobs.new.example.test/',encoding='utf-8')
        directory=self.root/'inbox/sources';directory.mkdir()
        scout.write_json(directory/'new.json',{'company':'new-tech','name':'新科技','sector':'人工智能','url':'https://jobs.new.example.test/',
            'official_page':'https://new.example.test/','capture':'evidence/official.txt'})
        result=run_daily.drain_browser_captures(self.root)
        self.assertEqual(result[0]['status'],'source_registered')
        self.assertTrue((self.root/'sources/registry.json').exists())
        self.assertEqual(run_daily.drain_browser_captures(self.root),[])

    def test_31_foreign_only_locations_hidden_mixed_city_retained(self):
        abroad=record(1);abroad['workPlaceNameList']=['新加坡']
        mixed=record(2);mixed['workPlaceNameList']=['上海','东京']
        self.collect({1:page([abroad,mixed],2,1)})
        self.assertEqual(len(d.search(self.root)),1)
        self.assertEqual(len(d.search(self.root,include_excluded=True)),2)

    def test_32_missing_location_does_not_discard_a_job(self):
        job=record(1);job['workPlaceNameList']=[]
        self.collect({1:page([job],1,1)})
        found=d.search(self.root);self.assertEqual(len(found),1);self.assertEqual(found[0]['region'],'unknown')

    def test_33_short_latin_role_terms_do_not_match_inside_words(self):
        job={'title':'Data Analyst','locations':['上海'],'sector':'人工智能'}
        result=d.classify(job)
        self.assertIn('数据与分析',result['roles']);self.assertNotIn('游戏程序与引擎',result['roles'])
    def test_34_experience_filter_uses_published_text(self):
        a=record(1);a['reqWorkYearsName']='3-5年'
        b=record(2);b['reqWorkYearsName']='经验不限'
        self.collect({1:page([a,b],2,1)})
        self.assertEqual(len(d.search(self.root,experience='3-5年')),1)
