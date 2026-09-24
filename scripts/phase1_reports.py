"""Recomputable source inventory, field quality and frozen human-review samples."""
from __future__ import annotations
from collections import Counter, defaultdict
import csv
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
import discovery as d
import scout

LOGICAL_CHANNELS={"internship","campus_fulltime","social"}


def readonly(root):
    c=sqlite3.connect('file:'+(Path(root).resolve()/'state/jobs.sqlite3').as_posix()+'?mode=ro',uri=True,factory=scout.ClosingConnection)
    c.row_factory=sqlite3.Row
    return c


def rows(root):
    with readonly(root) as c:
        jobs=[{**dict(r),'job':json.loads(r['payload'])} for r in c.execute('SELECT * FROM discovery_jobs ORDER BY key')]
        sources={r['source_id']:dict(r) for r in c.execute('SELECT * FROM discovery_sources')}
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        fields=[dict(r) for r in c.execute('SELECT * FROM discovery_field_state')] if 'discovery_field_state' in tables else []
    return jobs,sources,fields


def safe_csv(path,headers,values):
    with Path(path).open('x',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(headers)
        for row in values:
            writer.writerow(["'"+str(v) if str(v).lstrip().startswith(('=','+','-','@')) else v for v in row])


def evidence_valid(root,ref):
    try:
        return scout.digest(scout.within(root,ref['path']).read_bytes())==ref['sha256']
    except (OSError,ValueError,KeyError):return False


def audit(root,output,asof=None):
    root=Path(root).resolve();out=Path(output).resolve()
    out.mkdir(parents=True,exist_ok=False)
    asof=scout.dt(asof) if asof else scout.now_utc()
    catalog=d.registry(root);jobs,health,states=rows(root)
    review_path=root/'sources/entity_review.json'
    review=scout.read_json(review_path) if review_path.exists() else {'entries':{},'source_reviews':{}}
    source_review=review.get('source_reviews',{})
    jobs_by_source=defaultdict(list)
    for row in jobs:jobs_by_source[row['source_id']].append(row)
    source_rows=[];entry_rows=[];unresolved=[];employers=defaultdict(list)
    domains=defaultdict(set)
    for company in catalog['companies']:
        info=review.get('entries',{}).get(company['id'],{})
        proven=info.get('review_status')=='confirmed' and info.get('employer_id') and info.get('evidence_refs') and all(evidence_valid(root,x) for x in info['evidence_refs'])
        if proven:employers[info['employer_id']].append(company['id'])
        else:unresolved.append(company['id'])
        entry_rows.append({'catalog_id':company['id'],'name':company['name'],'sector':company['sector'],
          'group_id':info.get('group_id'),'employer_id':info.get('employer_id') if proven else None,
          'brand_id':info.get('brand_id'),'relationship_status':'confirmed' if proven else 'pending',
          'size_status':info.get('size_status','pending'),'channel_inventory_status':info.get('channel_inventory_status','unknown'),
          'applicable_channels':info.get('applicable_channels',[]),'source_ids':[s['id'] for s in company['sources']]})
        for source in company['sources']:
            domains[urlsplit(source['url']).hostname].add(company['id'])
            h=health.get(source['id'],{});group=jobs_by_source[source['id']]
            conf=source_review.get(source['id'],{})
            src_proven=conf.get('review_status')=='confirmed' and conf.get('evidence_refs') and all(evidence_valid(root,x) for x in conf['evidence_refs'])
            src_proven=bool(src_proven and conf.get('verified_channels') and set(conf['verified_channels'])<=LOGICAL_CHANNELS)
            fresh=bool(h.get('updated') and timedelta(0)<=asof-scout.dt(h['updated'])<=timedelta(hours=72))
            source_rows.append({'source_id':source['id'],'catalog_id':company['id'],'name':company['name'],
              'url':source['url'],'adapter':source['adapter'],'configured_employment_types':source.get('channels',[]),
              'observed_recruitment_labels':sorted({r['job'].get('recruitment_type','') for r in group}-{''}),
              'verified_channels':conf.get('verified_channels',[]) if src_proven else [],
              'channel_review_status':'confirmed' if src_proven else 'pending','collection_mode':conf.get('collection_mode','unknown'),
              'collection_status':h.get('status','not_attempted'),'last_attempt':h.get('updated'),
              'fresh_72h':fresh,'latest_traversal_rows':h.get('collected'),'reported_total':h.get('total'),
              'stored_jobs':len(group),'description_nonempty':sum(bool(r['job'].get('description')) for r in group),
              'requirements_nonempty':sum(bool(r['job'].get('requirements')) for r in group),
              'both_nonempty':sum(bool(r['job'].get('description') and r['job'].get('requirements')) for r in group),
              'error':h.get('error','')})
    # Formal coverage uses reviewed independent entities and channels only.
    partial=set();sufficient=set()
    for employer,ids in employers.items():
        eligible=[s for s in source_rows if s['catalog_id'] in ids and s['channel_review_status']=='confirmed'
                  and s['collection_status']=='complete' and s['fresh_72h'] and s['collection_mode']=='automated']
        if eligible:partial.add(employer)
        entry_reviews=[review['entries'][i] for i in ids]
        inventories_known=all(x.get('channel_inventory_status')=='confirmed' and x.get('applicable_channels') and set(x['applicable_channels'])<=LOGICAL_CHANNELS for x in entry_reviews)
        applicable={ch for x in entry_reviews for ch in x.get('applicable_channels',[])}
        covered={ch for s in eligible for ch in s['verified_channels']}
        if inventories_known and applicable<=covered:sufficient.add(employer)
    n=len(jobs);nonempty={f:sum(bool(r['job'].get(f)) for r in jobs) for f in ('description','requirements')}
    quality={'denominator':'all stored unique job identities, including stale/out-of-default-region records',
      'jobs':n,'nonempty':nonempty,'nonempty_rates':{f:round(count/n,6) if n else None for f,count in nonempty.items()},
      'both_nonempty':sum(bool(r['job'].get('description') and r['job'].get('requirements')) for r in jobs),
      'missing_requirements':n-nonempty['requirements'],'field_states':{},'capture_completeness_rate':None,
      'field_accuracy_rate':None,'human_labels':'not_supplied','note':'Nonempty does not establish full capture or correctness.'}
    current=[r for r in jobs if r['job'].get('source_status') not in {'closed','expired'}]
    quality['source_closed_records']=sum(r['job'].get('source_status')=='closed' for r in jobs)
    quality['nonclosed_records']=len(current)
    quality['nonclosed_body_nonempty']={f:sum(bool(r['job'].get(f)) for r in current) for f in ('description','requirements')}
    for field in ('description','requirements'):
        found=[s for s in states if s['field']==field]
        counts=Counter(s['status'] for s in found)
        counts['uninstrumented']=n-len(found)
        quality['field_states'][field]={'value_status':dict(counts),'last_attempt_status':dict(Counter(s['last_attempt_status'] for s in found)),
          'cached_value_with_last_attempt_failure':sum(bool(s['value_text']) and s['last_attempt_status'] in {'fetch_failed','parse_failed'} for s in found)}
    total_known=len(employers);universe_resolved=not unresolved and total_known>0
    denominator_hash=scout.digest(catalog)
    report={'schema_version':1,'asof':scout.timestamp(asof),'catalog_version':catalog.get('version'),
      'catalog_sha256':denominator_hash,'review_sha256':scout.digest(review),'universe_id':'draft-'+scout.digest([catalog,review])[:12],
      'coverage':{'catalog_entries':len(catalog['companies']),'source_entries':len(source_rows),
        'entries_with_stored_jobs':len({r['company_id'] for r in jobs}),
        'sources_by_collection_status':dict(Counter(s['collection_status'] for s in source_rows)),
        'complete_empty_sources':[s['source_id'] for s in source_rows if s['collection_status']=='complete' and s['latest_traversal_rows']==0],
        'confirmed_employers':total_known,'unresolved_catalog_entries':len(unresolved),
        'partially_covered_confirmed_employers':len(partial),'sufficiently_covered_confirmed_employers':len(sufficient),
        'formal_denominator':total_known if universe_resolved else None,
        'partial_enterprise_coverage':len(partial)/total_known if universe_resolved else None,
        'sufficient_enterprise_coverage':len(sufficient)/total_known if universe_resolved else None,
        'national_coverage':None,'channel_applicability_unverified_sources':sum(s['channel_review_status']!='confirmed' for s in source_rows)},
      'quality':quality,'sources':source_rows,'entity_inventory':entry_rows,
      'relationship_review_candidates':[{'shared_host':host,'catalog_ids':sorted(ids),'status':'candidate_only'} for host,ids in sorted(domains.items()) if len(ids)>1]}
    scout.write_json(out/'report.json',report)
    scout.write_json(out/'catalog-snapshot.json',catalog)
    scout.write_json(out/'entity-review-snapshot.json',review)
    template={'version':1,'entries':{r['catalog_id']:{'review_status':'pending','group_id':None,'employer_id':None,'brand_id':None,'size_status':'pending','evidence_refs':[],'channel_inventory_status':'unknown','applicable_channels':[]} for r in entry_rows},
              'source_reviews':{r['source_id']:{'review_status':'pending','collection_mode':'unknown','verified_channels':[],'evidence_refs':[]} for r in source_rows}}
    scout.write_json(out/'entity-review-template.json',template)
    safe_csv(out/'entity-inventory.csv',list(entry_rows[0]) if entry_rows else [],
      [[json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else (v if v is not None else '') for v in r.values()] for r in entry_rows])
    safe_csv(out/'source-inventory.csv',list(source_rows[0]) if source_rows else [],
      [[json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else (v if v is not None else '') for v in r.values()] for r in source_rows])
    lines=['# 第一阶段覆盖与正文质量快照','',f"时间：{report['asof']}；目标集合：{report['universe_id']}",'',
      f"目录 {len(entry_rows)} 项；来源 {len(source_rows)} 个；已存岗位 {n} 条。",
      f"职责非空 {nonempty['description']}；要求非空 {nonempty['requirements']}；两者同时非空 {quality['both_nonempty']}。",'',
      f"独立招聘主体尚有 {len(unresolved)} 个目录项待核验，正式企业覆盖率暂不计算。来源遍历状态单独列示。",'',
      '| 来源 | 状态 | 台账岗位 | 职责非空 | 要求非空 | 同时非空 |','|---|---|---:|---:|---:|---:|']
    lines += [f"| {r['source_id']} | {r['collection_status']} | {r['stored_jobs']} | {r['description_nonempty']} | {r['requirements_nonempty']} | {r['both_nonempty']} |" for r in source_rows if r['stored_jobs'] or r['collection_status']!='not_attempted']
    lines += ['','字段采集完整率与准确率保留为空，等待实际人工标注。JSON及CSV保留明细，可独立复算。']
    (out/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return report


def sample(root,output,sources,per_source=30,seed='20260917',cohort=None):
    if per_source<1:raise ValueError('Positive sample size required')
    root=Path(root).resolve();out=Path(output).resolve();out.mkdir(parents=True,exist_ok=False)
    jobs,_,fields=rows(root);by_key=defaultdict(dict)
    for item in fields:by_key[item['job_key']][item['field']]=item
    selected=[];shortfalls={}
    for source in sources:
        groups=defaultdict(list)
        for row in jobs:
            if row['source_id']!=source:continue
            j=row['job'];missing='requirements_missing' if not j.get('requirements') else ('description_missing' if not j.get('description') else 'both_present')
            stratum=(missing,j.get('employment_type','unknown'),j.get('region','unknown'),bool(j.get('experience')))
            groups[stratum].append(row)
        for group in groups.values():group.sort(key=lambda r:scout.digest([seed,source,r['key']]))
        chosen=[]
        while any(groups.values()) and len(chosen)<per_source:
            for stratum in sorted(groups):
                if groups[stratum] and len(chosen)<per_source:
                    row=groups[stratum].pop(0);chosen.append((row,stratum))
        if len(chosen)<per_source:shortfalls[source]=per_source-len(chosen)
        selected.extend(chosen)
    original={}
    baseline_digest=None
    if cohort:
        frozen=scout.read_json(cohort)
        if scout.digest(frozen['records'])!=frozen['snapshot_digest']:raise ValueError('Frozen sample manifest changed; annotate review.csv instead')
        seed=frozen['seed'];per_source=frozen['requested_per_source'];baseline_digest=frozen['snapshot_digest']
        original={r['job_key']:r for r in frozen['records']};by_id={r['key']:r for r in jobs}
        missing=set(original)-set(by_id)
        if missing:raise ValueError('Frozen cohort jobs missing: '+str(len(missing)))
        selected=[(by_id[r['job_key']],tuple(r['stratum'])) for r in frozen['records']]
        shortfalls=frozen.get('shortfalls',{})
    records=[];refs={}
    for row,stratum in selected:
        j=row['job'];evidences=[json.loads(row['evidence'])]
        if j.get('closure_evidence',{}).get('response'):evidences.append(j['closure_evidence']['response'])
        for item in by_key[row['key']].values():
            if item['evidence']:evidences.append(json.loads(item['evidence']))
        unique={e['path']:e for e in evidences}
        for name,e in unique.items():
            if evidence_valid(root,e):
                source=scout.within(root,name);target=out/'evidence'/source.name
                if target.exists() and scout.digest(target.read_bytes())!=e['sha256']:
                    target=out/'evidence'/(scout.digest(name)[:8]+'-'+source.name)
                target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists():target.write_bytes(source.read_bytes())
                refs[name]={**e,'sample_path':target.relative_to(out).as_posix()}
        records.append({'sample_id':'sample_'+scout.digest([seed,row['key']])[:12],'job_key':row['key'],'source_id':row['source_id'],
          'stratum':list(stratum),'candidate':{k:j.get(k) for k in ('company','title','url','locations','employment_type','recruitment_type','degree','experience','description','requirements')},
          'first_seen':row['first_seen'],'last_seen':row['last_seen'],
          'baseline_candidate':original[row['key']]['candidate'] if row['key'] in original else None,
          'source_job_id':j.get('source_job_id'),'source_status':j.get('source_status'),
          'closure_evidence':j.get('closure_evidence'),
          'primary_evidence_ref':json.loads(row['evidence']),
          'field_evidence_refs':{f:json.loads(v['evidence']) for f,v in by_key[row['key']].items() if v['evidence']},
          'field_statuses':{f:{k:v[k] for k in ('status','completeness','acquired_at','last_attempt_at','last_attempt_status')} for f,v in by_key[row['key']].items()},
          'evidence_refs':[refs.get(e['path'],{**e,'sample_path':None,'status':'missing_or_hash_mismatch'}) for e in unique.values()],
          'review_status':'pending','gold':{'company':None,'title':None,'locations':None,'employment_type':None,'description_capture':None,'requirements_capture':None},
          'reviewer':None,'reviewed_at':None,'notes':''})
    manifest={'schema_version':1,'seed':seed,'sampling_method':'deterministic hash within strata, round-robin allocation by source',
      'created_at':scout.timestamp(),'source_counts':dict(Counter(r['source_id'] for r in records)),'requested_per_source':per_source,
      'samples':len(records),'shortfalls':shortfalls,'gold_labels_completed':0,
      'purpose':'development_pilot_not_a_holdout_accuracy_estimate','baseline_sample_digest':baseline_digest,
      'snapshot_digest':scout.digest(records),'records':records}
    scout.write_json(out/'samples.json',manifest)
    headers=['sample_id','job_key','source_id','title','url','locations','employment_type','candidate_description','candidate_requirements',
             'review_status','gold_company','gold_title','gold_locations','gold_employment_type','gold_description_capture','gold_requirements_capture','reviewer','reviewed_at','notes']
    safe_csv(out/'review.csv',headers,[[r['sample_id'],r['job_key'],r['source_id'],r['candidate']['title'],r['candidate']['url'],
      json.dumps(r['candidate']['locations'],ensure_ascii=False),r['candidate']['employment_type'],r['candidate']['description'],r['candidate']['requirements'],
      'pending','','','','','','','','',''] for r in records])
    (out/'README.md').write_text('''# 人工复核样本

本包是分层抽样的真实岗位候选，尚未完成标注。candidate字段来自系统，不是标准答案；review.csv的gold字段特意留空。

逐条查看关联的原始响应，必要时打开官方职位链接。职责和要求分别标记：完整取得／漏取／截断／解析错误／当前详情未公开／证据不足。仅有列表而没有完整详情的样本，应先补充详情证据，不能判定为未公开。

JSON记录抽样种子、样本摘要和证据SHA-256。原始采集时间与后续复核时间分开保存。单人复核如实填写复核者，不称作双人独立标注。
''',encoding='utf-8')
    return {k:v for k,v in manifest.items() if k!='records'}
