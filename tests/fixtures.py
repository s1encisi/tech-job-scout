"""Fictional, offline-only evidence fixtures shared by tests and the demo."""
from datetime import timedelta
import scout


class FictionalJobFixture:
    def make_job(self,major="环境工程",when=None,completeness="full_detail",source_kind="official_ats",deadline_days=15,status_text="立即申请",source_when=None):
        when=when or scout.now_utc();captured=scout.timestamp(when)
        source_when=source_when or when
        url=self.prefix+"/jobs/TEST-001"
        last=(when+timedelta(days=deadline_days)).date().isoformat()
        source_text=self.company+"\n官网发布的招聘链接："+self.prefix+"\n"
        p=self.root/"inbox/source-capture.txt";p.write_text(source_text,encoding="utf-8")
        source_ev=scout.register_evidence(self.root,self.rid,p,"https://corp.example.test/",scout.timestamp(source_when),"tool_export","fixture-source-tool", "full_detail")["evidence_id"]
        source={"source_id":"test_source","employer_key":"test_employer","company":self.company,"kind":source_kind,"prefix":self.prefix,
            "identity_evidence":{"evidence_id":source_ev,"quote":self.company},
            "authorization_evidence":{"evidence_id":source_ev,"quote":"官网发布的招聘链接："+self.prefix},
            "reviewed_at":scout.timestamp(source_when),"reviewer":"OFFLINE TEST FIXTURE","ownership":"unknown","ownership_evidence":None}
        scout.register_source(self.root,self.rid,source)
        lines=[self.company,"2027校园招聘",self.title,"编号：TEST-001","招聘类型：校园招聘","地点：上海；北京","学历：硕士","专业："+major,"毕业届别：2027届",
               "职责：环境过程建模与数据分析","技能：Python、机器学习","工具：Python","经验：应届生","截止："+last,"状态："+status_text,"投递链接："+url+"/apply"]
        text="\n".join(lines)
        p=self.root/"inbox/job-capture.txt";p.write_text(text,encoding="utf-8")
        eid=scout.register_evidence(self.root,self.rid,p,url,captured,"tool_export","fixture-job-tool",completeness)["evidence_id"]
        def ref(q):return {"evidence_id":eid,"quote":q}
        def fact(q):return {"value":q,"evidence":ref(q)}
        facts={"location_raw":fact("地点：上海；北京"),"recruitment_type_raw":fact("招聘类型：校园招聘"),"duties_raw":fact("职责：环境过程建模与数据分析"),
            "degree_raw":fact("学历：硕士"),"major_raw":fact("专业："+major),"cohort_raw":fact("毕业届别：2027届"),"skills_raw":fact("技能：Python、机器学习"),"tools_raw":fact("工具：Python"),
            "deadline_raw":fact("截止："+last),"salary_raw":None,"experience_raw":fact("经验：应届生"),"campaign_raw":fact("2027校园招聘")}
        job={"employer_key":"test_employer","company":self.company,"source_id":"test_source","job_id":"TEST-001","campaign":"2027校园招聘","title":self.title,
            "locations":["上海"],"job_type":"campus","industry":"环保水务","match_track":"environment_ai","url":url,"application_url":url+"/apply","application_evidence":ref("投递链接："+url+"/apply"),"observed_at":captured,
            "status":"open","status_evidence":ref("状态："+status_text),"identity_evidence":ref(self.title),"facts":facts,
            "checks":[{"axis":"degree_program","rule":"one_of","expected":["master"],"evidence":ref("学历：硕士")},
                {"axis":"major","rule":"one_of","expected":[major],"evidence":ref("专业："+major)},
                {"axis":"graduation_year","rule":"one_of","expected":[2027],"evidence":ref("毕业届别：2027届")}],
            "deadline_at":last+"T23:59:59+08:00","opens_at":None,"date_conflict":False,
            "requirements_review":{"all_public_hard_requirements_captured":True,"reviewer":"OFFLINE TEST FIXTURE","tool_ref":"fixture-reread-tool"},
            "match_reason":"测试推断：环境与AI交叉；日期仅到日按+08:00日末约定。","matched_skills":["Python","机器学习"]}
        self.last_source=source;self.last_job_evidence=eid;self.last_source_evidence=source_ev
        return job
