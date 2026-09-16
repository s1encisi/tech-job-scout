"""Offline adversarial tests. Every employer/job here is FICTIONAL and temporary.
These tests never call the internet, Codex, submit applications or register a schedule.
"""
import copy
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import scout
import xlsx_writer as xw
import run_daily

from fixtures import FictionalJobFixture


class LedgerTests(FictionalJobFixture, unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/"workspace"
        scout.init_workspace(self.root)
        profile=scout.read_json(self.root/"profile.json")
        for key,value in (("degree_program","master"),("student_status","enrolled"),("major","环境工程"),("graduation_year",2027)):
            profile["facts"][key]={"value":value,"status":"confirmed","source":"TEST FIXTURE ONLY, NOT THE REAL USER","confirmed_at":"2026-09-15"}
        scout.write_json(self.root/"profile.json",profile)
        self.rid=scout.begin(self.root)["run_id"]
        self.company="虚构测试公司（不是实际招聘企业）"
        self.prefix="https://jobs.example.test/acme"
        self.title="环境算法工程师（虚构测试）"

    def tearDown(self):self.temp.cleanup()

    def ingest(self,job):return scout.ingest(self.root,self.rid,[job])
    def row(self):return scout.materialize(self.root)[0]
    def count(self,table):
        with scout.connect(self.root) as c:return c.execute("SELECT COUNT(*) FROM "+table).fetchone()[0]

    def test_01_default_profile_does_not_invent_major_or_year(self):
        p=scout.read_json(scout.BASE/"templates/profile.json")
        self.assertIsNone(p["facts"]["major"]["value"]);self.assertIsNone(p["facts"]["graduation_year"]["value"])

    def test_02_valid_fictional_fixture_qualifies(self):
        self.assertEqual(self.ingest(self.make_job())["accepted"],1)
        self.assertEqual(self.row()["assessment"]["bucket"],"eligible")

    def test_03_unknown_graduation_year_is_pending(self):
        job=self.make_job();p=scout.read_json(self.root/"profile.json");p["facts"]["graduation_year"]["value"]=None
        # Simulate a separately authorized profile version before a NEW run.
        scout.write_json(self.root/"profile.json",p);self.rid=scout.begin(self.root)["run_id"]
        self.assertEqual(self.ingest(job)["accepted"],1)
        self.assertEqual(self.row()["assessment"]["bucket"],"qualification_pending")

    def test_04_explicit_wrong_major_fails_even_with_high_score(self):
        job=self.make_job(major="计算机科学与技术");self.assertEqual(self.ingest(job)["accepted"],1)
        self.assertEqual(self.row()["assessment"]["eligibility"],"fail")
        self.assertEqual(self.row()["assessment"]["bucket"],"history")
        self.assertGreater(self.row()["assessment"]["match_score"],90)

    def test_05_omitted_core_gate_quarantined(self):
        job=self.make_job();job["checks"].pop()
        self.assertEqual(self.ingest(job)["quarantined"],1);self.assertEqual(self.count("jobs"),0)

    def test_06_fake_quote_quarantined(self):
        job=self.make_job();job["checks"][1]["evidence"]["quote"]="THIS WAS NOT ON THE PAGE"
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_07_unknown_evidence_id_quarantined(self):
        job=self.make_job();job["identity_evidence"]["evidence_id"]="ev_invented"
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_08_future_observation_rejected(self):
        job=self.make_job();job["observed_at"]=scout.timestamp(scout.now_utc()+timedelta(days=1))
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_09_naive_timestamp_rejected(self):
        with self.assertRaises(scout.ValidationError):scout.dt("2026-09-15T09:00:00")

    def test_10_stale_body_is_not_currently_verified(self):
        job=self.make_job(when=scout.now_utc()-timedelta(days=3));self.ingest(job)
        self.assertEqual(self.row()["assessment"]["bucket"],"verification_pending")

    def test_11_snippet_cannot_verify(self):
        self.assertEqual(self.ingest(self.make_job(completeness="snippet"))["quarantined"],1)

    def test_12_partial_page_cannot_verify(self):
        self.assertEqual(self.ingest(self.make_job(completeness="partial"))["quarantined"],1)

    def test_13_old_capture_cannot_be_relabelled_today(self):
        job=self.make_job(when=scout.now_utc()-timedelta(days=1));job["observed_at"]=scout.timestamp()
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_14_deadline_cannot_be_invented(self):
        job=self.make_job();job["deadline_at"]="2099-01-01T23:59:59+08:00"
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_15_deadline_requires_raw_evidence(self):
        job=self.make_job();job["facts"]["deadline_raw"]=None
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_16_expired_deadline_moves_to_history(self):
        job=self.make_job(deadline_days=-1);self.assertEqual(self.ingest(job)["accepted"],1)
        self.assertEqual(self.row()["assessment"]["status"],"expired")

    def test_17_explicit_closed_overrides_apply_text(self):
        job=self.make_job(status_text="岗位已关闭；立即申请");self.ingest(job)
        self.assertEqual(self.row()["assessment"]["status"],"closed")

    def test_18_http_404_is_not_a_closure(self):
        job=self.make_job();self.ingest(job);key=scout.job_key(job);before=self.row()["last_verified"]
        scout.probe(self.root,self.rid,key,"http_404","fixture network failure")
        row=self.row();self.assertEqual(row["assessment"]["status"],"unverified");self.assertEqual(row["last_verified"],before)

    def test_19_http_200_does_not_refresh_or_restore_body(self):
        job=self.make_job();self.ingest(job);key=scout.job_key(job)
        scout.probe(self.root,self.rid,key,"http_403","blocked")
        scout.probe(self.root,self.rid,key,"ok","HTTP 200 only")
        self.assertEqual(self.row()["probe_status"],"http_403")

    def test_20_repeated_batch_is_idempotent(self):
        job=self.make_job();self.ingest(job);r=self.ingest(job)
        self.assertEqual(r["new"],0);self.assertEqual(self.count("jobs"),1);self.assertEqual(self.count("observations"),1)

    def test_21_reverify_is_not_business_change_or_new_job(self):
        job=self.make_job();self.ingest(job)
        self.rid=scout.begin(self.root)["run_id"]
        result=self.ingest(self.make_job())
        self.assertEqual(result["new"],0);self.assertEqual(result["changed"],0);self.assertEqual(self.count("jobs"),1)

    def test_22_multiple_locations_are_one_posting(self):
        job=self.make_job();job["locations"]=["上海","北京"];self.ingest(job)
        self.assertEqual(self.count("jobs"),1)

    def test_23_different_ids_do_not_merge(self):
        job=self.make_job();other=copy.deepcopy(job);other["job_id"]="TEST-002"
        self.assertNotEqual(scout.job_key(job),scout.job_key(other))

    def test_24_tracking_parameters_do_not_change_fallback_key(self):
        job=self.make_job();job["job_id"]=None;other=copy.deepcopy(job);other["url"]+="?utm_source=email"
        self.assertEqual(scout.job_key(job),scout.job_key(other))

    def test_25_meaningful_ids_and_hash_routes_are_preserved(self):
        self.assertNotEqual(scout.canonical_url("https://jobs.example.test/a?id=1"),scout.canonical_url("https://jobs.example.test/a?id=2"))
        self.assertNotEqual(scout.canonical_url("https://jobs.example.test/#/jobs/1"),scout.canonical_url("https://jobs.example.test/#/jobs/2"))

    def test_26_shared_ats_root_not_trusted(self):
        self.make_job();source=copy.deepcopy(self.last_source);source["prefix"]="https://jobs.example.test/"
        with self.assertRaises(scout.ValidationError):scout.register_source(self.root,self.rid,source)

    def test_27_tenant_prefix_boundary(self):
        self.assertFalse(scout.under_prefix("https://jobs.example.test/acme-evil/jobs/1",self.prefix))
        self.assertFalse(scout.under_prefix("https://jobs.example.test.evil.org/acme/jobs/1",self.prefix))
        self.assertTrue(scout.under_prefix(self.prefix+"/jobs/1",self.prefix))

    def test_28_nonofficial_repost_never_counted_as_verified(self):
        self.ingest(self.make_job(source_kind="third_party"))
        self.assertEqual(self.row()["assessment"]["bucket"],"verification_pending")

    def test_29_source_identity_tampering_demotes_job(self):
        job=self.make_job();self.ingest(job)
        with scout.connect(self.root) as c:
            row=c.execute("SELECT path FROM evidence WHERE id=?",(self.last_source_evidence,)).fetchone()
        (self.root/row[0]).write_text("tampered",encoding="utf-8")
        self.assertEqual(self.row()["assessment"]["bucket"],"verification_pending")

    def test_30_job_evidence_tampering_demotes_job(self):
        job=self.make_job();self.ingest(job)
        with scout.connect(self.root) as c:row=c.execute("SELECT path FROM evidence WHERE id=?",(self.last_job_evidence,)).fetchone()
        (self.root/row[0]).write_text("tampered",encoding="utf-8")
        self.assertEqual(self.row()["assessment"]["bucket"],"verification_pending")

    def test_31_config_drift_blocks_ingest(self):
        job=self.make_job();p=scout.read_json(self.root/"profile.json");p["facts"]["graduation_year"]["value"]=2028;scout.write_json(self.root/"profile.json",p)
        with self.assertRaises(scout.ValidationError):self.ingest(job)

    def test_32_policy_drift_blocks_finalization(self):
        p=scout.read_json(self.root/"policy.json");p["fresh_hours"]=999;scout.write_json(self.root/"policy.json",p)
        with self.assertRaises(scout.ValidationError):scout.finalize(self.root,self.rid,export=False)

    def test_33_invented_matching_skill_is_rejected(self):
        job=self.make_job();job["matched_skills"].append("InventedExpertSkill")
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_34_invented_accepted_major_is_rejected(self):
        job=self.make_job(major="计算机科学与技术");job["checks"][1]["expected"]=["计算机科学与技术","环境工程"]
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_35_not_stated_does_not_mean_unrestricted(self):
        job=self.make_job();job["checks"][1]["rule"]="unrestricted";job["checks"][1]["expected"]=None
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_36_explicit_unrestricted_major_can_pass(self):
        job=self.make_job(major="不限专业");job["checks"][1]["rule"]="unrestricted";job["checks"][1]["expected"]=None
        self.assertEqual(self.ingest(job)["accepted"],1);self.assertEqual(self.row()["assessment"]["bucket"],"eligible")

    def test_37_unpublished_major_stays_unknown(self):
        job=self.make_job();job["checks"][1]={"axis":"major","rule":"unpublished","expected":None,"evidence":None};job["facts"]["major_raw"]=None
        self.ingest(job);self.assertEqual(self.row()["assessment"]["eligibility"],"unknown")

    def test_38_missing_separate_review_never_passes(self):
        job=self.make_job();job["requirements_review"]["all_public_hard_requirements_captured"]=False
        self.ingest(job);self.assertEqual(self.row()["assessment"]["bucket"],"qualification_pending")

    def test_39_date_conflict_cannot_be_verified_open(self):
        job=self.make_job();job["date_conflict"]=True;self.ingest(job)
        self.assertEqual(self.row()["assessment"]["bucket"],"verification_pending")

    def test_40_foreign_application_route_is_rejected(self):
        job=self.make_job();job["application_url"]="https://unrelated.example.test/apply"
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_41_source_review_expires(self):
        job=self.make_job(source_when=scout.now_utc()-timedelta(days=31));self.ingest(job)
        self.assertEqual(self.row()["assessment"]["bucket"],"verification_pending")

    def test_42_zero_queries_is_partial_not_success(self):
        result=scout.finalize(self.root,self.rid,export=False)
        self.assertEqual(result["run_status"],"partial");self.assertEqual(result["completed_tasks"],0)

    def test_43_adhoc_task_ids_cannot_inflate_coverage(self):
        for i in range(30):
            entry={"task_id":"invented-"+str(i),"industry":"环保水务","region":"全国","channel":"environment","query":"TEST ONLY","result":"done","tool_ref":"fixture-query","pages_checked":1,"next_cursor":None,"notes":"OFFLINE FIXTURE"}
            scout.add_coverage(self.root,self.rid,entry)
        result=scout.finalize(self.root,self.rid,export=False)
        self.assertEqual(result["completed_tasks"],0);self.assertEqual(result["run_status"],"partial")

    def test_44_actual_plan_ids_are_the_coverage_denominator(self):
        for entry in scout.plan(self.root)["required_tasks"]:
            entry.update(result="zero_found",tool_ref="fixture-query",pages_checked=1)
            scout.add_coverage(self.root,self.rid,entry)
        result=scout.finalize(self.root,self.rid,export=False)
        self.assertEqual(result["completed_tasks"],24);self.assertEqual(result["run_status"],"completed")

    def test_45_backup_is_readable_and_keeps_jobs(self):
        self.ingest(self.make_job());scout.finalize(self.root,self.rid,export=False)
        with sqlite3.connect(self.root/"backups"/(self.rid+".sqlite3"), factory=scout.ClosingConnection) as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],1)

    def test_46_reinit_never_overwrites_profile(self):
        before=(self.root/"profile.json").read_bytes();self.assertEqual(scout.init_workspace(self.root)["status"],"already_initialized")
        self.assertEqual((self.root/"profile.json").read_bytes(),before)

    def test_47_checkpoint_has_hard_size_limit(self):
        cp={"phase":"discovery","completed_task_ids":[],"pending_task_ids":[],"next_actions":["x"*25000],"blockers":[],"evidence_ids":[]}
        with self.assertRaises(scout.ValidationError):scout.checkpoint(self.root,self.rid,cp)

    def test_48_exclusive_lock_prevents_double_run(self):
        path=self.root/"state/run.lock"
        with run_daily.RunLock(path):
            with self.assertRaises(RuntimeError):
                with run_daily.RunLock(path):pass
        self.assertFalse(path.exists())

    def test_49_stale_lock_is_not_automatically_stolen(self):
        path=self.root/"state/run.lock";path.write_text('{"pid":0}',encoding="utf-8")
        with self.assertRaises(RuntimeError):
            with run_daily.RunLock(path):pass
        self.assertTrue(path.exists())

    def test_50_internal_url_and_path_traversal_rejected(self):
        for url in ("http://127.0.0.1/a","http://localhost/a","file:///etc/passwd","http://10.1.2.3/x","https://user:pass@example.test/"):
            with self.subTest(url=url):
                with self.assertRaises(scout.ValidationError):scout.safe_url(url)
        with self.assertRaises(scout.ValidationError):scout.within(self.root,"../../escape")

    def test_51_portable_export_is_valid_zip_and_preserves_headers(self):
        self.ingest(self.make_job());result=scout.finalize(self.root,self.rid)
        path=Path(result["exports"]["latest"])
        with ZipFile(path) as z:
            self.assertIsNone(z.testzip());w=ET.fromstring(z.read("xl/workbook.xml"))
            self.assertEqual(len(w.find("{"+xw.S+"}sheets")),13)
        self.assertEqual(len(xw.read_followup(path)),1)

    def test_52_manual_annotations_survive_reexport(self):
        job=self.make_job();self.ingest(job);result=scout.finalize(self.root,self.rid)
        latest=Path(result["exports"]["latest"]);key=scout.job_key(job)
        # Emulate only the user's editable sheet; exporter must read those cells before regeneration.
        xw.write_xlsx(latest,[("人工跟进",[["岗位唯一键","人工投递状态","人工优先级","人工备注"],[key,"面试","高","用户写入：不覆盖这条备注"]])])
        self.rid=scout.begin(self.root)["run_id"];result=scout.finalize(self.root,self.rid)
        self.assertEqual(xw.read_followup(result["exports"]["latest"])[0][1:],["面试","高","用户写入：不覆盖这条备注"])

    def test_53_website_excel_formula_is_literal_text(self):
        path=self.root/"inbox/formula-test.xlsx"
        xw.write_xlsx(path,[("测试",[["来源文本"],["=HYPERLINK(\"https://evil.example.test\",\"click\")"],["@SUM(1,2)"]])])
        with ZipFile(path) as z:
            root=ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
            self.assertFalse(root.findall(".//{"+xw.S+"}f"))
            self.assertIn("'=HYPERLINK",z.read("xl/worksheets/sheet1.xml").decode())

    def test_54_missing_followup_sheet_preserves_previous_file(self):
        self.ingest(self.make_job());result=scout.finalize(self.root,self.rid);latest=Path(result["exports"]["latest"])
        xw.write_xlsx(latest,[("用户改了结构",[["请勿覆盖"],["原数据"]])]);before=latest.read_bytes()
        self.rid=scout.begin(self.root)["run_id"];result=scout.finalize(self.root,self.rid)
        self.assertEqual(result["run_status"],"export_failed");self.assertEqual(latest.read_bytes(),before)

    def test_55_formula_in_followup_is_rejected(self):
        path=self.root/"inbox/followup-formula.xlsx"
        xw.write_xlsx(path,[("人工跟进",[["岗位唯一键","人工投递状态","人工优先级","人工备注"],["test","","",xw.Formula("1+1",2)]])])
        with self.assertRaises(ValueError):xw.read_followup(path)

    def test_56_unknown_user_note_key_rejected_not_guessed(self):
        with self.assertRaises(scout.ValidationError):scout.annotations_from_rows(self.root,[["invented","面试","高","keep"]])

    def test_57_actual_cli_default_is_dry_run(self):
        result1=mock.Mock(stdout="codex-cli MOCK VERSION",returncode=0)
        result2=mock.Mock(stdout="--sandbox --skip-git-repo-check --json --cd",returncode=0)
        with mock.patch.object(run_daily.shutil,"which",return_value="MOCK_CODEX"),mock.patch.object(run_daily.subprocess,"run",side_effect=[result1,result2]) as proc:
            result=run_daily.run(self.root,execute=False)
        self.assertEqual(result["mode"],"dry_run");self.assertFalse(result["scheduled"]);self.assertEqual(proc.call_count,2)

    def test_58_out_of_order_observation_cannot_overwrite(self):
        job=self.make_job();self.ingest(job);new_time=self.row()["last_seen"]
        old=self.make_job(when=scout.now_utc()-timedelta(hours=2));result=self.ingest(old)
        self.assertEqual(result["quarantined"],1);self.assertEqual(self.row()["last_seen"],new_time)

    def test_59_exclusion_cannot_be_positive_major_match(self):
        job=self.make_job(major="环境工程除外");job["checks"][1]["expected"]=["环境工程"]
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_60_unknown_normalized_recruitment_type_cannot_claim_campus(self):
        job=self.make_job();job["facts"]["recruitment_type_raw"]=None
        self.assertEqual(self.ingest(job)["quarantined"],1)


    def test_61_invented_apply_link_under_valid_tenant_is_rejected(self):
        job=self.make_job();job["application_url"]=self.prefix+"/invented-submit"
        self.assertEqual(self.ingest(job)["quarantined"],1)

    def test_62_unverified_lead_is_preserved_but_never_eligible(self):
        lead={"url":"https://jobs.example.test/unknown", "title_hint":"测试搜索标题（未核实）", "company_hint":"待确认", "industry":"环保水务", "found_at":scout.timestamp(), "tool_ref":"fixture-search", "reason":"正文无法读取"}
        result=scout.add_lead(self.root,self.rid,lead)
        self.assertFalse(result["counted_as_eligible"])
        scout.add_lead(self.root,self.rid,lead)
        self.assertEqual(self.count("leads"),1);self.assertEqual(self.count("jobs"),0)
        result=scout.finalize(self.root,self.rid,export=False)
        self.assertEqual(result["eligible"],0);self.assertEqual(result["unverified_leads"],1)

    def test_63_lead_resolution_keeps_history(self):
        job=self.make_job()
        lead={"url":job["url"], "title_hint":job["title"], "company_hint":"待确认", "industry":"环保水务", "found_at":scout.timestamp(), "tool_ref":"fixture-search", "reason":"等待职位核验"}
        scout.add_lead(self.root,self.rid,lead);self.ingest(job)
        with scout.connect(self.root) as c:resolved=c.execute("SELECT resolved_key FROM leads").fetchone()[0]
        self.assertEqual(resolved,scout.job_key(job));self.assertEqual(self.count("leads"),1)

    def test_64_connection_closes_after_transaction(self):
        with scout.connect(self.root) as c:c.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):c.execute("SELECT 1")

    def test_65_captured_local_process_completes(self):
        outpath=self.root/"inbox/local-process.txt"
        with outpath.open("w",encoding="utf-8") as out, (self.root/"inbox/local-error.txt").open("w",encoding="utf-8") as err:
            result=run_daily.execute_captured([sys.executable,"-c","import sys; print(sys.stdin.read())"],"offline-test",self.root,out,err,5)
        self.assertEqual(result,0);self.assertIn("offline-test",outpath.read_text())

    def test_66_timeout_terminates_owned_local_process(self):
        with (self.root/"inbox/timeout-out.txt").open("w",encoding="utf-8") as out, (self.root/"inbox/timeout-err.txt").open("w",encoding="utf-8") as err:
            with self.assertRaises(subprocess.TimeoutExpired):
                run_daily.execute_captured([sys.executable,"-c","import time; time.sleep(30)"],"",self.root,out,err,0.1)

    def test_67_opc_parts_use_compatible_default_namespaces(self):
        path=self.root/"inbox/opc-check.xlsx"
        xw.write_xlsx(path,[("检查",[["字段"],["数据"]])])
        with ZipFile(path) as z:
            self.assertIn(b'<Types xmlns=',z.read('[Content_Types].xml'))
            self.assertIn(b'<Relationships xmlns=',z.read('_rels/.rels'))
            self.assertIn(b'<worksheet xmlns=',z.read('xl/worksheets/sheet1.xml'))


if __name__=="__main__":unittest.main(verbosity=2)
