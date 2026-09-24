"""Cross-run budgets and source stops for the explicitly approved bulk campaign."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import scout
import body_backfill
from bulk_backfill import BudgetLedger
from public_http import PublicHTTP, BudgetExceeded, FetchError
import test_phase1 as phase1_fixtures
FakeHTTP=phase1_fixtures.FakeHTTP


class LedgerTests(unittest.TestCase):
    def test_restart_keeps_prior_usage_and_reservations(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'budget.json'
            scout.write_json(file,{'limits':{'http':35,'detail':31},'used':{'http':33,'detail':30},'new_attempts_by_kind':{}})
            ledger=BudgetLedger(file);ledger.reserve('https://jobs.example.test/1','detail')
            resumed=BudgetLedger(file);resumed.reserve('https://jobs.example.test/robots.txt','robots')
            self.assertEqual(resumed.data['used'],{'http':35,'detail':31})
            with self.assertRaises(BudgetExceeded):resumed.reserve('https://jobs.example.test/2','detail')
    def test_http_redirects_also_reserve_durable_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);file=root/'budget.json'
            scout.write_json(file,{'limits':{'http':3,'detail':1},'used':{'http':0,'detail':0},'new_attempts_by_kind':{}})
            ledger=BudgetLedger(file);client=PublicHTTP(root,'test',max_requests=3,max_detail_attempts=1,interval=0,on_attempt=ledger.reserve)
            client._before_attempt('https://jobs.example.test/robots.txt','robots');client._active_kind='robots'
            client._redirect_attempt('https://jobs.example.test/robots2.txt')
            client._before_attempt('https://jobs.example.test/1','detail')
            with self.assertRaises(BudgetExceeded):client._before_attempt('https://jobs.example.test/2','detail')
            self.assertEqual(BudgetLedger(file).data['used'],{'http':3,'detail':1})
    def test_corrupt_or_enlarged_budget_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'budget.json'
            scout.write_json(file,{'limits':{'http':4001,'detail':1},'used':{'http':0,'detail':0}})
            with self.assertRaises(ValueError):BudgetLedger(file)


class BulkBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.fixture=phase1_fixtures.Phase1Tests();self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()
    def test_access_block_stops_other_jobs_in_same_source(self):
        self.fixture.insert('one');self.fixture.insert('two')
        root=self.fixture.root;body_backfill.prepare(root)
        client=FakeHTTP(root,error=FetchError('HTTP 403'))
        blocked=set();events=[]
        result=body_backfill.run(root,client=client,max_http_attempts=4000,max_detail_attempts=3500,max_tasks=10,
            budget_profile='approved_bulk',blocked_sources=blocked,on_block=lambda source,error:events.append(source))
        self.assertEqual(client.requests,1);self.assertEqual(blocked,{'fake-list'});self.assertEqual(events,['fake-list'])
        self.assertEqual(result['queue']['counts']['pending'],1)
    def test_smoke_limit_remains_in_force_without_bulk_profile(self):
        with self.assertRaises(ValueError):body_backfill.run(self.fixture.root,max_http_attempts=4000,max_detail_attempts=3500)
