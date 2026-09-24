"""Offline evidence agreement and explicitly attributed sample-label evaluation."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from html import unescape
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs,urlsplit
import scout
from phase1_reports import safe_csv

FIELDS=('title','locations','description','requirements')
CAPTURE_LABELS={'complete','omitted','truncated','parse_error','not_disclosed','insufficient_evidence'}


def load_sample(path):
    path=Path(path).resolve();data=scout.read_json(path)
    if scout.digest(data['records'])!=data['snapshot_digest']:raise ValueError('Sample snapshot digest mismatch')
    if len({r['sample_id'] for r in data['records']})!=len(data['records']):raise ValueError('Duplicate sample identities')
    return path,data


def norm(value):
    return re.sub(r'\s+',' ',unicodedata.normalize('NFC',unescape(value or ''))).strip()


def job_id(record):
    if record.get('source_job_id'):return str(record['source_job_id'])
    parsed=urlsplit(record['candidate']['url']);query=parse_qs(parsed.query)
    for key in ('postId','id'):
        if query.get(key):return query[key][0]
    return parsed.path.rstrip('/').split('/')[-1]


def inspect_payload(payload,source,ident):
    """Independent reader of saved API records; does not call the production extractor."""
    if source.startswith('tencent'):
        if payload.get('Code')!=200:raise ValueError('Source response was not successful')
        root=payload['Data'];items=root.get('Posts') if 'Posts' in root else [root]
        item=next((v for v in items if str(v.get('PostId'))==ident),None)
        keys={'title':'RecruitPostName','description':'Responsibility','requirements':'Requirement'}
        locations=[item.get('LocationName','')] if item else []
        kind='list' if 'Posts' in root else 'detail'
    elif source.startswith('netease'):
        if payload.get('code')!=200:raise ValueError('Source response was not successful')
        root=payload['data'];items=root.get('list') if 'list' in root else [root]
        item=next((v for v in items if str(v.get('id'))==ident),None)
        keys={'title':'name','description':'description','requirements':'requirement'}
        locations=item.get('workPlaceNameList',[]) if item else []
        kind='list' if 'list' in root else 'detail'
    elif source.startswith('mihoyo'):
        if payload.get('code')!=0:raise ValueError('Source response was not successful')
        root=payload['data'];items=root.get('list') if 'list' in root else [root]
        item=next((v for v in items if str(v.get('id'))==ident),None)
        kind='list' if 'list' in root else 'detail'
        keys={'title':'title','description':'jobSummary' if kind=='list' else 'description','requirements':'jobRequire'}
        locations=[v.get('addressDetail','') for v in item.get('addressDetailList',[])] if item else []
    else:raise ValueError('No independent reader for this source')
    if item is None:raise ValueError('Job identity not found in saved response')
    return {**{k:{'present':v in item,'value':item.get(v)} for k,v in keys.items()},
            'locations':{'present':bool(locations),'value':locations},'kind':kind}


def evidence_check(sample,output):
    file,data=load_sample(sample);out=Path(output).resolve();out.mkdir(parents=True,exist_ok=False)
    checks=[]
    for record in data['records']:
        captures=[];problems=[];closure_verified=False
        for reference in record['evidence_refs']:
            try:
                if not reference.get('sample_path'):raise ValueError('Evidence was not bundled')
                path=scout.within(file.parent,reference['sample_path'])
                if scout.digest(path.read_bytes())!=reference['sha256']:raise ValueError('Evidence hash mismatch')
                payload=scout.read_json(path)
                closure=record.get('closure_evidence') or {}
                if payload.get('Code')==500 and payload.get('Data')=='E1005' and record.get('source_status')=='closed':
                    url=urlsplit(reference['url'])
                    closure_verified=(url.hostname=='careers.tencent.com' and parse_qs(url.query).get('postId')==[job_id(record)] and closure.get('definition',{}).get('meaning')=='closed')
                    continue
                capture=inspect_payload(payload,record['source_id'],job_id(record))
                captures.append((reference,capture))
            except (ValueError,KeyError,TypeError,OSError) as error:problems.append(str(error))
        for field in FIELDS:
            bound=record.get('field_evidence_refs',{}).get(field)
            eligible=[(ref,cap) for ref,cap in captures if cap[field]['present']]
            if bound:eligible=[(ref,cap) for ref,cap in eligible if ref['sha256']==bound['sha256'] and ref['path']==bound['path']]
            # Title/location belong to the frozen primary observation; body fields prefer their exact bound receipt.
            if not bound and field in {'title','locations'} and record.get('primary_evidence_ref'):
                primary=record['primary_evidence_ref'];eligible=[(ref,cap) for ref,cap in eligible if ref['path']==primary['path'] and ref['sha256']==primary['sha256']]
            eligible.sort(key=lambda pair:scout.dt(pair[0]['captured_at']),reverse=True)
            candidate=record['candidate'].get(field)
            status='no_source_field';reference=None;raw=None
            if eligible:
                reference,capture=eligible[0];raw=capture[field]['value']
                if field=='locations':
                    status='agrees_with_saved_value' if isinstance(candidate,list) and sorted(candidate)==sorted(raw) else 'normalization_difference'
                elif not isinstance(raw,(str,type(None))):status='source_type_unsupported'
                elif not norm(raw):status='source_empty' if not norm(candidate) else 'retained_value_needs_review'
                elif not norm(candidate):status='candidate_missing'
                elif re.search(r'</?[A-Za-z][^>]*>',raw):status='requires_rendered_review'
                else:status='agrees_with_saved_value' if norm(candidate)==norm(raw) else 'text_difference'
            if status=='no_source_field' and closure_verified:status='officially_closed_no_current_body'
            checks.append({'sample_id':record['sample_id'],'job_key':record['job_key'],'source_id':record['source_id'],
                'field':field,'status':status,'source_status':record.get('source_status'),'closure_verified':closure_verified,'candidate':candidate,'source_value':raw,'evidence':reference,
                'evidence_problems':problems,'field_state':record.get('field_statuses',{}).get(field),
                'review_origin':'automated_evidence_check'})
    result={'sample_digest':data['snapshot_digest'],'at':scout.timestamp(),'sample_count':len(data['records']),
      'review_origin':'automated_evidence_check','scope':'Agreement with saved response values only; no human labels or live-site completeness claim.',
      'counts_by_field':{f:dict(Counter(r['status'] for r in checks if r['field']==f)) for f in FIELDS},
      'human_accuracy_rate':None,'capture_completeness_rate':None,'checks':checks}
    scout.write_json(out/'evidence-check.json',result)
    safe_csv(out/'evidence-check.csv',['sample_id','source_id','field','status','candidate','source_value','evidence_path','capture_time'],
      [[r['sample_id'],r['source_id'],r['field'],r['status'],json.dumps(r['candidate'],ensure_ascii=False),
       json.dumps(r['source_value'],ensure_ascii=False),(r['evidence'] or {}).get('sample_path',''),(r['evidence'] or {}).get('captured_at','')] for r in checks])
    return {k:v for k,v in result.items() if k!='checks'}


def evaluate(sample,labels,output,review_origin):
    if review_origin not in {'human','assistant','automated'}:raise ValueError('Review origin must be explicit')
    file,data=load_sample(sample);out=Path(output).resolve();out.mkdir(parents=True,exist_ok=False)
    expected={r['sample_id']:r for r in data['records']}
    with Path(labels).open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    ids=[r.get('sample_id') for r in rows]
    if len(ids)!=len(set(ids)) or set(ids)!=set(expected):raise ValueError('Labels must contain every frozen sample exactly once')
    errors=[];results=[];counts=Counter()
    for row in rows:
        ref=expected[row['sample_id']];status=row.get('review_status','')
        if row.get('job_key')!=ref['job_key']:errors.append({'sample_id':row['sample_id'],'error':'Job key changed'});continue
        if status not in {'pending','reviewed','needs_evidence'}:errors.append({'sample_id':row['sample_id'],'error':'Unknown review status'});continue
        counts[status]+=1
        if status!='reviewed':continue
        if not row.get('reviewer') or not row.get('reviewed_at'):
            errors.append({'sample_id':row['sample_id'],'error':'Reviewer and review time required'});continue
        try:scout.dt(row['reviewed_at'])
        except ValueError:errors.append({'sample_id':row['sample_id'],'error':'Review time needs timezone'});continue
        required=['gold_company','gold_title','gold_locations','gold_employment_type','gold_description_capture','gold_requirements_capture']
        if any(not row.get(k) for k in required):errors.append({'sample_id':row['sample_id'],'error':'Reviewed row has unlabelled gold fields'});continue
        if any(row[k] not in CAPTURE_LABELS for k in ['gold_description_capture','gold_requirements_capture']):
            errors.append({'sample_id':row['sample_id'],'error':'Unknown capture label'});continue
        try:
            locations=json.loads(row['gold_locations'])
            if not isinstance(locations,list) or not all(isinstance(v,str) for v in locations):raise ValueError()
        except ValueError:errors.append({'sample_id':row['sample_id'],'error':'Gold locations must be a JSON array'});continue
        if row['gold_employment_type'] not in {'internship','fulltime','unknown'}:errors.append({'sample_id':row['sample_id'],'error':'Invalid employment type label'});continue
        comparisons={'company':norm(row['gold_company'])==norm(ref['candidate'].get('company')),
          'title':norm(row['gold_title'])==norm(ref['candidate'].get('title')),
          'locations':sorted(locations)==sorted(ref['candidate'].get('locations') or []),
          'employment_type':row['gold_employment_type']==ref['candidate'].get('employment_type')}
        results.append({'sample_id':row['sample_id'],'source_id':ref['source_id'],'comparisons':comparisons,
          'capture_labels':{k:row['gold_'+k+'_capture'] for k in ('description','requirements')},'reviewer':row['reviewer'],'reviewed_at':row['reviewed_at']})
    per_field={}
    for field in ('company','title','locations','employment_type'):
        numerator=sum(r['comparisons'][field] for r in results);denominator=len(results)
        per_source={}
        for source in sorted({r['source_id'] for r in results}):
            part=[r for r in results if r['source_id']==source];per_source[source]={'n':len(part),'matches':sum(r['comparisons'][field] for r in part)}
        per_field[field]={'matches':numerator,'reviewed_denominator':denominator,'sample_agreement':numerator/denominator if denominator else None,'by_source':per_source}
    capture_metrics={}
    for field in ('description','requirements'):
        counts_field=Counter(r['capture_labels'][field] for r in results)
        disclosed=sum(counts_field[k] for k in ('complete','omitted','truncated','parse_error'))
        capture_metrics[field]={'label_counts':dict(counts_field),'reviewed_source_disclosed_denominator':disclosed,
           'descriptive_sample_complete_rate':counts_field['complete']/disclosed if disclosed else None,
           'insufficient_evidence':counts_field['insufficient_evidence']}
    result={'sample_digest':data['snapshot_digest'],'labels_sha256':scout.digest(Path(labels).read_bytes()),'review_origin':review_origin,
      'total_samples':len(expected),'review_status_counts':dict(counts),'valid_reviewed_rows':len(results),'errors':errors,
      'sample_metrics':per_field,'capture_sample_metrics':capture_metrics,'labelled_by_human':review_origin=='human','independent_validation_established':False,
      'eligible_for_release_acceptance':False,'generalization_accuracy':None,
      'reason':'This is a development pilot; reviewed sample agreement is not an independent holdout or national-population accuracy.',
      'human_review_complete':review_origin=='human' and len(results)==len(expected) and not errors}
    scout.write_json(out/'evaluation.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('check-evidence');q.add_argument('--samples',required=True);q.add_argument('--output',required=True)
    q=sub.add_parser('evaluate');q.add_argument('--samples',required=True);q.add_argument('--labels',required=True);q.add_argument('--output',required=True);q.add_argument('--review-origin',choices=['human','assistant','automated'],required=True)
    a=vars(p.parse_args());command=a.pop('command');sample=a.pop('samples')
    result=evidence_check(sample,**a) if command=='check-evidence' else evaluate(sample,**a)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
