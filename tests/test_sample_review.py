"""Review origin and immutable evidence must stay separate from gold labels."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import scout
import sample_review as review


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        file=self.root/'evidence/response.json';scout.write_json(file,{'code':200,'data':{'id':7,'name':'软件实习生','description':'职责第一段\n第二段','requirement':'Python','workPlaceNameList':['上海']}})
        ref={'path':'evidence/response.json','sample_path':'evidence/response.json','url':'https://jobs.example.test/7','captured_at':scout.timestamp(),'sha256':scout.digest(file.read_bytes())}
        row={'sample_id':'sample-one','job_key':'job-one','source_id':'netease-public','source_job_id':'7',
          'candidate':{'company':'虚构公司','title':'软件实习生','url':'https://jobs.example.test/7','locations':['上海'],'employment_type':'internship','description':'职责第一段\n第二段','requirements':'Python'},
          'primary_evidence_ref':ref,'field_evidence_refs':{'description':ref,'requirements':ref},'evidence_refs':[ref]}
        self.row=row;scout.write_json(self.root/'samples.json',{'records':[row],'snapshot_digest':scout.digest([row]),'purpose':'development_pilot_not_a_holdout_accuracy_estimate'})
    def tearDown(self):self.temp.cleanup()
    def labels(self,status='reviewed'):
        row={'sample_id':'sample-one','job_key':'job-one','review_status':status,'gold_company':'虚构公司','gold_title':'软件实习生','gold_locations':'["上海"]',
             'gold_employment_type':'internship','gold_description_capture':'complete','gold_requirements_capture':'complete','reviewer':'TEST REVIEWER','reviewed_at':scout.timestamp()}
        path=self.root/'labels.csv'
        with path.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=list(row));w.writeheader();w.writerow(row)
        return path
    def test_evidence_check_is_not_human_validation(self):
        result=review.evidence_check(self.root/'samples.json',self.root/'check')
        self.assertEqual(result['counts_by_field']['description']['agrees_with_saved_value'],1)
        self.assertIsNone(result['human_accuracy_rate']);self.assertIsNone(result['capture_completeness_rate'])
    def test_corrupt_evidence_does_not_pass(self):
        (self.root/'evidence/response.json').write_text('{}',encoding='utf-8')
        result=review.evidence_check(self.root/'samples.json',self.root/'check')
        self.assertEqual(result['counts_by_field']['title']['no_source_field'],1)
    def test_assistant_labels_are_never_human_gold(self):
        result=review.evaluate(self.root/'samples.json',self.labels(),self.root/'eval','assistant')
        self.assertFalse(result['labelled_by_human']);self.assertFalse(result['human_review_complete'])
        self.assertFalse(result['eligible_for_release_acceptance']);self.assertIsNone(result['generalization_accuracy'])
    def test_human_pilot_still_is_not_independent_holdout(self):
        result=review.evaluate(self.root/'samples.json',self.labels(),self.root/'eval','human')
        self.assertTrue(result['human_review_complete']);self.assertFalse(result['independent_validation_established'])
        self.assertEqual(result['sample_metrics']['title']['sample_agreement'],1)
    def test_pending_rows_do_not_create_accuracy(self):
        result=review.evaluate(self.root/'samples.json',self.labels('pending'),self.root/'eval','human')
        self.assertEqual(result['valid_reviewed_rows'],0);self.assertIsNone(result['sample_metrics']['title']['sample_agreement'])
    def test_changed_job_key_is_rejected(self):
        path=self.labels();text=path.read_text(encoding='utf-8-sig').replace('job-one','different');path.write_text(text,encoding='utf-8')
        result=review.evaluate(self.root/'samples.json',path,self.root/'eval','human')
        self.assertTrue(result['errors']);self.assertFalse(result['human_review_complete'])

    def test_official_closure_is_not_an_unexplained_missing_field(self):
        row=self.row;row['source_id']='tencent-public';row['source_status']='closed';row['candidate']['url']='https://careers.tencent.com/jobdesc.html?postId=7';row['candidate']['requirements']=''
        primary=self.root/'evidence/tencent-list.json';scout.write_json(primary,{'Code':200,'Data':{'Posts':[{'PostId':'7','RecruitPostName':'软件实习生','LocationName':'上海','Responsibility':'职责第一段\n第二段'}]}})
        ref={'path':'evidence/tencent-list.json','sample_path':'evidence/tencent-list.json','url':'https://careers.tencent.com/list','captured_at':scout.timestamp(),'sha256':scout.digest(primary.read_bytes())}
        error=self.root/'evidence/closed.json';scout.write_json(error,{'Code':500,'Data':'E1005'})
        closure={'path':'evidence/closed.json','sample_path':'evidence/closed.json','url':'https://careers.tencent.com/tencentcareer/api/post/ByPostId?postId=7','captured_at':scout.timestamp(),'sha256':scout.digest(error.read_bytes())}
        row['evidence_refs']=[ref,closure];row['primary_evidence_ref']=ref;row['field_evidence_refs']={'description':ref,'requirements':ref};row['closure_evidence']={'definition':{'meaning':'closed'}}
        scout.write_json(self.root/'samples.json',{'records':[row],'snapshot_digest':scout.digest([row])})
        result=review.evidence_check(self.root/'samples.json',self.root/'check-closed')
        self.assertEqual(result['counts_by_field']['requirements']['officially_closed_no_current_body'],1)
