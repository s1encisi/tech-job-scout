"""Collect, search and export public tech recruitment independently of an LLM."""
from __future__ import annotations
import argparse
from functools import lru_cache
import csv
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4
import scout
from job_sources import ADAPTERS, normalize
from public_http import PublicHTTP, FetchError, BudgetExceeded
from run_daily import RunLock

BASE = Path(__file__).resolve().parents[1]


def registry(root=None):
    data = scout.read_json(BASE / 'templates/company_registry.json')
    overlay=Path(root)/'sources/registry.json' if root else None
    if overlay and overlay.exists():
        companies={c['id']:c for c in data['companies']}
        for entry in scout.read_json(overlay)['companies']:
            current=companies.setdefault(entry['id'],entry)
            merged={source['id']:source for source in current['sources']}
            merged.update({source['id']:source for source in entry['sources']})
            current['sources']=list(merged.values())
        data['companies']=list(companies.values())
    ids = [c['id'] for c in data['companies']]
    if len(ids) != len(set(ids)): raise ValueError('Duplicate company id')
    return data


def initialize(root):
    root = Path(root).resolve(); scout.init_workspace(root)
    with scout.connect(root) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS discovery_jobs(
          key TEXT PRIMARY KEY,company_id TEXT NOT NULL,source_id TEXT NOT NULL,payload TEXT NOT NULL,
          content_hash TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,evidence TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS discovery_sources(
          source_id TEXT PRIMARY KEY,company_id TEXT NOT NULL,updated TEXT NOT NULL,status TEXT NOT NULL,
          cursor TEXT,total INTEGER,pages INTEGER NOT NULL DEFAULT 0,collected INTEGER NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT '',last_signature TEXT);
        CREATE TABLE IF NOT EXISTS discovery_events(
          id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,job_key TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS discovery_runs(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS discovery_notes(key TEXT PRIMARY KEY,status TEXT,priority TEXT,note TEXT);
        CREATE TABLE IF NOT EXISTS discovery_handoffs(source_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS discovery_identity(company_id TEXT,namespace TEXT,source_job_id TEXT,job_key TEXT,PRIMARY KEY(company_id,namespace,source_job_id));
        ''')
        for row in c.execute('SELECT key,payload FROM discovery_jobs ORDER BY first_seen,key').fetchall():
            job=json.loads(row['payload'])
            c.execute('INSERT OR IGNORE INTO discovery_identity VALUES(?,?,?,?)',(job['company_id'],urlsplit(job['url']).netloc,job['source_job_id'],row['key']))
    return root


@lru_cache(maxsize=1)
def taxonomy():
    return scout.read_json(BASE / "templates/search_taxonomy.json")


def role_matches(term,text):
    if re.search(r"[A-Za-z]",term):
        return bool(re.search(r"(?<![A-Za-z0-9])"+re.escape(term)+r"(?![A-Za-z0-9])",text,re.I))
    return term in text


def classify(job):
    text = job['title'] + ' ' + job.get('role_raw', '')
    rules = taxonomy()
    job['roles'] = [role for role, terms in rules['roles'].items()
                    if any(role_matches(term,text) for term in terms)] or ['其他职类']
    job['scope'] = 'excluded' if any(re.search(x, job['title'], re.I) for x in rules['excluded_title_patterns']) else 'included'
    country = job.get('country', '')
    domestic = rules['mainland_locations']
    known_china = country in {'中国', 'China', '中国大陆', '中华人民共和国'}
    domestic_city = any(any(city in location for city in domestic) for location in job.get('locations', []))
    outside_city=any(any(place.lower() in location.lower() for place in rules['outside_default_locations']) for location in job.get('locations',[]))
    job['region']='mainland' if known_china or (not country and domestic_city) else ('outside_default' if country or outside_city else 'unknown')
    if job['scope'] != 'excluded' and job['region'] == 'outside_default':job['scope']='outside_default_region'
    job['body_status'] = 'full_description' if job.get('description') and job.get('requirements') else ('description_available' if job.get('description') else 'listing_only')
    deadline=job.get('valid_through')
    if deadline:
        try:
            when=scout.dt(deadline)
            if when<scout.now_utc():job['source_status']='expired'
        except ValueError:
            job['date_review']='Check source deadline timezone'
    job['eligibility'] = 'not_reviewed'
    return job


def save_jobs(c, root, run_id, jobs, evidence, capture_kind="list"):
    path = scout.within(root, evidence['path'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != evidence['sha256']: raise ValueError('Evidence hash mismatch')
    counts = {'new': 0, 'changed': 0, 'unchanged': 0}
    track_fields=bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_field_state'").fetchone())
    source_map={s['id']:s for co in registry(root)['companies'] for s in co['sources']} if track_fields else {}
    for job in jobs:
        scout.safe_url(job['url']); classify(job)
        identity=(job['company_id'],urlsplit(job['url']).netloc,job['source_job_id'])
        mapped=c.execute('SELECT job_key FROM discovery_identity WHERE company_id=? AND namespace=? AND source_job_id=?',identity).fetchone()
        key=mapped['job_key'] if mapped else 'discovery_'+scout.digest(identity)[:24]
        c.execute('INSERT OR IGNORE INTO discovery_identity VALUES(?,?,?,?)',(*identity,key))
        fingerprint = scout.digest({k:v for k,v in job.items() if k not in {'raw', 'updated_at', 'detail_evidence'}})
        previous = c.execute('SELECT * FROM discovery_jobs WHERE key=?', (key,)).fetchone()
        kind = 'new' if previous is None else ('changed' if previous['content_hash'] != fingerprint else 'unchanged')
        now = evidence['captured_at']
        if previous and scout.dt(now) < scout.dt(previous['last_seen']): continue
        # Keep validated detail bodies when a later list is empty or only a summary.
        if previous:
            old_job=json.loads(previous['payload'])
            if old_job.get('source_status')=='closed' and capture_kind!='detail':
                for name in ('source_status','closure_observed_at','closure_evidence'):
                    if name in old_job:job[name]=old_job[name]
            for field in ('description','requirements'):
                old_value=old_job.get(field)
                field_receipt=old_job.get('field_evidence',{}).get(field) or old_job.get('detail_evidence')
                incoming_detail=capture_kind=='detail'
                old_detail=bool(field_receipt and (field_receipt.get('capture_kind')=='detail' or field_receipt==old_job.get('detail_evidence')))
                if old_value and (not job.get(field) or (old_detail and not incoming_detail)):
                    job[field]=old_value
                    if field_receipt:job.setdefault('field_evidence',{})[field]=field_receipt
            if old_job.get('detail_evidence') and not job.get('detail_evidence'):
                job['detail_evidence']=old_job['detail_evidence']
        classify(job)
        fingerprint=scout.digest({k:v for k,v in job.items() if k not in {'raw','updated_at','detail_evidence','field_evidence'}})
        kind='new' if previous is None else ('changed' if previous['content_hash']!=fingerprint else 'unchanged')
        c.execute('''INSERT INTO discovery_jobs VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
          payload=excluded.payload,content_hash=excluded.content_hash,last_seen=excluded.last_seen,evidence=excluded.evidence''',
          (key,job['company_id'],job['source_id'],scout.dumps(job),fingerprint,
           previous['first_seen'] if previous else now,now,scout.dumps(evidence)))
        if kind != 'unchanged': c.execute('INSERT INTO discovery_events(run_id,job_key,kind,at) VALUES(?,?,?,?)',(run_id,key,kind,now))
        counts[kind] += 1
        if track_fields:
            import body_fields as bf
            adapter=source_map.get(job['source_id'],{}).get('adapter')
            raw=job.get('raw') or {}
            if adapter in bf.SCHEMA and ('id' in raw or 'PostId' in raw):
                bf.apply(c,root,key,bf.extract(adapter,raw,capture_kind),evidence,capture_kind,run_id,verified=True)
    return counts


def make_plan(root, company_ids=None):
    catalog = registry(root); selected = set(company_ids or [])
    taxonomy = scout.read_json(BASE / 'templates/search_taxonomy.json')
    with scout.connect(root) as c:
        health = {r['source_id']:dict(r) for r in c.execute('SELECT * FROM discovery_sources')}
        handoffs = {r['source_id']:json.loads(r['payload']) for r in c.execute('SELECT * FROM discovery_handoffs')}
    tasks = []
    for company in catalog['companies']:
        if selected and company['id'] not in selected: continue
        domain = urlsplit(company['sources'][0]['url']).hostname if company['sources'] else ''
        for kind, words in [('internship','实习 日常实习 暑期实习'), ('fulltime','校园招聘 社会招聘 全职')]:
            previous = [health[s['id']]['updated'] for s in company['sources'] if s['id'] in health]
            tasks.append({'task_id':company['id']+':'+kind,'company_id':company['id'],'company':company['name'],
                'sector':company['sector'],'employment_type':kind,'priority':company['priority'],
                'queries':[f'{company["name"]} {term} 招聘' for term in words.split()]
                    + ([f'site:{domain} {words.split()[0]}'] if domain else []),
                'entry_urls':[s['url'] for s in company['sources']],
                'browser_tasks':[handoffs[s['id']] for s in company['sources'] if s['id'] in handoffs],
                'source_status':{s['id']:health.get(s['id'],{}).get('status','not_collected') for s in company['sources']},
                'last_attempt':min(previous) if previous else ''})
    tasks.sort(key=lambda t:(bool(t['last_attempt']),t['last_attempt'],t['priority'],t['company_id'],t['employment_type']))
    return {'companies':len({t['company_id'] for t in tasks}),'tasks':len(tasks),'queue':tasks,
            'role_expansions':{k:v[:3] for k,v in taxonomy['roles'].items()},
            'counting_basis':'Registered companies x internship/fulltime. Source collection and eligibility are tracked separately.'}


def save_source(c, company, source, status, cursor, total, pages, collected, error, signature, page_size=None):
    saved_cursor={'position':cursor,'page_size':page_size} if cursor is not None and page_size else cursor
    c.execute('''INSERT INTO discovery_sources VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
      updated=excluded.updated,status=excluded.status,cursor=excluded.cursor,total=excluded.total,
      pages=excluded.pages,collected=excluded.collected,error=excluded.error,last_signature=excluded.last_signature''',
      (source['id'],company['id'],scout.timestamp(),status,json.dumps(saved_cursor),total,pages,collected,error,signature))


def collect(root, companies=None, max_pages=3, page_size=50, max_requests=200, max_details=20,
            resume=False, interval=1., source_limit=12, client=None):
    if min(max_pages,page_size,max_requests,source_limit) < 1 or max_details < 0 or interval < 0:
        raise ValueError('Budgets must be positive; details and interval may be zero')
    if page_size > 100: raise ValueError('Page size must be at most 100')
    root = initialize(root); selected = set(companies or [])
    catalog = registry(root); known = {c['id'] for c in catalog['companies']}
    if selected - known: raise ValueError('Unknown company ids: '+','.join(sorted(selected-known)))
    run_id = 'collect-' + scout.now_utc().strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:6]
    client = client or PublicHTTP(root,run_id,max_requests=max_requests,interval=interval,max_detail_attempts=max_details)
    start = time.monotonic(); results = []; stopped = False
    with RunLock(root / 'state/run.lock'):
        with scout.connect(root) as c:
            old_sources = {r['source_id']:dict(r) for r in c.execute('SELECT * FROM discovery_sources')}
        work = [(co,s) for co in catalog['companies'] if not selected or co['id'] in selected for s in co['sources']]
        work.sort(key=lambda p:(old_sources.get(p[1]['id'],{}).get('updated',''),p[0]['priority'],p[1]['adapter']=='html',p[1]['id']))
        for company, source in work[:source_limit]:
            old = old_sources.get(source['id'],{})
            cursor = json.loads(old.get('cursor') or 'null') if resume and old.get('status')=='partial' else None
            source_page_size=page_size
            if isinstance(cursor,dict) and 'position' in cursor:
                source_page_size=cursor['page_size'];cursor=cursor['position']
            pages = old.get('pages',0) if cursor is not None else 0
            count = old.get('collected',0) if cursor is not None else 0
            signature = old.get('last_signature') if cursor is not None else None
            total = old.get('total') if cursor is not None else None
            status = 'partial'; error = ''; links = []; adapter = ADAPTERS[source['adapter']]()
            try:
                for _ in range(max_pages):
                    page = adapter.page(client,company,source,cursor,source_page_size)
                    if page.outcome == 'browser_required':
                        status='browser_required'; links=page.links or []; pages+=1; break
                    current_signature = scout.digest([j['source_job_id'] for j in page.jobs])
                    if page.jobs and current_signature == signature:
                        error='Repeated page; cursor kept for inspection'; break
                    if not page.jobs and (page.next_cursor is not None or (page.total and count < page.total)):
                        error='Empty page before advertised end'; break
                    with scout.connect(root) as c:
                        save_jobs(c,root,run_id,page.jobs,page.evidence)
                        pages+=1; count+=len(page.jobs); total=page.total; cursor=page.next_cursor
                        signature=current_signature; status='complete' if cursor is None and (total is None or count>=total) else 'partial'
                        save_source(c,company,source,status,cursor,total,pages,count,'',signature,source_page_size)
                    if cursor is None: break
            except BudgetExceeded as ex:
                error=str(ex); status='partial'; stopped=True
            except (FetchError,ValueError,KeyError,TypeError) as ex:
                error=str(ex); status='partial' if pages else 'blocked'
            with scout.connect(root) as c:
                save_source(c,company,source,status,cursor,total,pages,count,error,signature,source_page_size)
                if status in {'browser_required','blocked'}:
                    task={'company_id':company['id'],'source_id':source['id'],'entry_url':source['url'],
                          'reason':error or 'Rendered browser page required','links':links,'at':scout.timestamp()}
                    c.execute('INSERT OR REPLACE INTO discovery_handoffs VALUES(?,?)',(source['id'],scout.dumps(task)))
            results.append({'company':company['name'],'source':source['id'],'status':status,
                            'pages':pages,'collected':count,'reported_total':total,'next_cursor':cursor,
                            'page_size':source_page_size,'error':error,'discovered_links':links})
            if stopped: break
        # Detail enrichment is a separate budget so list pagination can progress first.
        details=0; detail_errors=[];detail_tasks_attempted=0
        with scout.connect(root) as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM discovery_jobs ORDER BY last_seen DESC')]
        source_map={s['id']:s for co in catalog['companies'] for s in co['sources']}
        for row in rows:
            if detail_tasks_attempted >= max_details: break
            if selected and row['company_id'] not in selected: continue
            job=json.loads(row['payload'])
            if job.get('source_status')=='closed':continue
            adapter=ADAPTERS[source_map[job['source_id']]['adapter']]()
            if not hasattr(adapter,'detail'): continue
            detail_evidence=job.get('detail_evidence') or json.loads(row['evidence'])
            if job.get('requirements') and scout.now_utc()-scout.dt(detail_evidence['captured_at'])<timedelta(hours=72): continue
            try:
                client.request_kind='detail';detail_tasks_attempted+=1
                job, receipt=adapter.detail(client,job); job['detail_evidence']=receipt
                with scout.connect(root) as c: save_jobs(c,root,run_id,[job],receipt,capture_kind='detail')
                details+=1
            except BudgetExceeded: break
            except (FetchError,ValueError,KeyError) as ex:
                detail_errors.append(str(ex))
                if len(detail_errors)>=3: break
            finally:
                client.request_kind='list'
        report={'run_id':run_id,'at':scout.timestamp(),'sources':results,'detail_pages':details,
                'detail_errors':detail_errors,'detail_tasks_attempted':detail_tasks_attempted,'scheduled_sources':len(work),'attempted_sources':len(results),
                'collection_seconds':round(time.monotonic()-start,3),
                'budgets':{'max_requests':max_requests,'max_pages_per_source':max_pages,'max_detail_pages':max_details,'interval_seconds':interval},**client.metrics()}
        report['status']='complete' if len(results)==len(work) and all(r['status']=='complete' for r in results) else 'partial'
        with scout.connect(root) as c: c.execute('INSERT INTO discovery_runs VALUES(?,?)',(run_id,scout.dumps(report)))
        scout.write_json(root/'reports'/(run_id+'.json'),report)
        scout.write_json(root/'memory/discovery-last-run.json',report)
        export(root)
    return report


def search(root, keyword='', company='', kind='', city='', role='', experience='', include_stale=False, include_excluded=False, limit=100):
    if limit < 1: raise ValueError('limit must be positive')
    now=scout.now_utc(); output=[]
    with scout.connect(root) as c:
        for row in c.execute('SELECT * FROM discovery_jobs ORDER BY last_seen DESC,key'):
            job=classify(json.loads(row['payload'])); stale=now-scout.dt(row['last_seen'])>timedelta(hours=72)
            text=' '.join([job['title'],job.get('description',''),job.get('requirements','')]).lower()
            if keyword and not all(word.lower() in text for word in keyword.split()): continue
            if company and company.lower() not in (job['company_id']+' '+job['company']).lower(): continue
            if kind and job['employment_type']!=kind: continue
            if city and not any(city.lower() in p.lower() for p in job['locations']): continue
            if role and role not in job.get('roles',[]): continue
            if experience and experience.lower() not in (job.get('experience','')+' '+job.get('requirements','')).lower(): continue
            if stale and not include_stale: continue
            if (job.get('scope')!='included' or job.get('source_status') in {'expired','closed'}) and not include_excluded: continue
            output.append({**job,'key':row['key'],'first_seen':row['first_seen'],'last_seen':row['last_seen'],
                           'freshness':'stale' if stale else 'recent',
                           'detail_freshness':'stale' if job.get('detail_evidence') and now-scout.dt(job['detail_evidence']['captured_at'])>timedelta(hours=72) else 'recent',
                           'evidence':json.loads(row['evidence'])})
            if len(output)>=limit: break
    return output


def status(root):
    catalog=registry(root)
    with scout.connect(root) as c:
        sources=[dict(r) for r in c.execute('SELECT * FROM discovery_sources ORDER BY updated DESC')]
        jobs=c.execute('SELECT COUNT(*) FROM discovery_jobs').fetchone()[0]
    return {'registered_companies':len(catalog['companies']),
            'companies_with_entries':sum(bool(co['sources']) for co in catalog['companies']),
            'attempted_companies':len({s['company_id'] for s in sources}),
            'complete_sources':sum(s['status']=='complete' for s in sources),
            'stored_jobs':jobs,'sources':sources}


def export(root):
    from xlsx_writer import write_xlsx, read_followup
    root=Path(root); out=root/'exports'; out.mkdir(exist_ok=True)
    jobs=search(root,include_stale=True,limit=1000000); health=status(root)
    target=out/'科技岗位发现_latest.xlsx'
    original_hash=scout.digest(target.read_bytes()) if target.exists() else None
    if target.exists():
        notes=read_followup(target)
        with scout.connect(root) as c:
            known={r['key'] for r in c.execute('SELECT key FROM discovery_jobs')}
            for row in notes:
                if row[0] in known: c.execute('INSERT OR REPLACE INTO discovery_notes VALUES(?,?,?,?)',tuple((row+['']*4)[:4]))
        backup=root/'backups'/('discovery-'+scout.now_utc().strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:6]+'.xlsx')
        shutil.copy2(target,backup)
    with scout.connect(root) as c: notes={r['key']:dict(r) for r in c.execute('SELECT * FROM discovery_notes')}
    headers=['岗位键','公司','岗位','城市','用工类型','招聘通道','职类','学历','经验','职责','要求','来源链接','最近采集','正文状态','新鲜度','资格匹配','地区判定']
    def cells(j): return [j['key'],j['company'],j['title'],' / '.join(j['locations']),j['employment_type'],
        j['recruitment_type'],' / '.join(j.get('roles',[])),j.get('degree',''),j.get('experience',''),
        j['description'],j['requirements'],j['url'],j['last_seen'],j['body_status'],j['freshness'],j['eligibility'],j.get('region','unknown')]
    sheets=[('总览',[['指标','值'],['目录企业',health['registered_companies']],['已尝试企业',health['attempted_companies']],
        ['已完成来源',health['complete_sources']],['已收录岗位',len(jobs)],['最近采集',scout.timestamp()],
        ['用途','公开岗位目录；个人资格见独立核验台账']]),('全部岗位',[headers]+[cells(j) for j in jobs])]
    for name,kind in [('实习','internship'),('全职','fulltime'),('类型待确认','unknown')]:
        sheets.append((name,[headers]+[cells(j) for j in jobs if j['employment_type']==kind]))
    sheets.append(('来源覆盖',[['来源','企业ID','状态','页数','已采集','源报告总量','错误']]+
        [[s['source_id'],s['company_id'],s['status'],s['pages'],s['collected'],s['total'],s['error']] for s in health['sources']]))
    sheets.append(('人工跟进',[['岗位唯一键','人工投递状态','人工优先级','人工备注']]+
        [[j['key'],notes.get(j['key'],{}).get('status',''),notes.get(j['key'],{}).get('priority',''),notes.get(j['key'],{}).get('note','')] for j in jobs]))
    temp=out/('.discovery-'+uuid4().hex+'.xlsx'); write_xlsx(temp,sheets)
    if (scout.digest(target.read_bytes()) if target.exists() else None)!=original_hash:
        raise ValueError('Workbook changed during export; new output kept at '+str(temp))
    temp.replace(target)
    scout.write_json(out/'tech-jobs.json',jobs)
    with (out/'tech-jobs.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f); writer.writerow(headers)
        for job in jobs:
            writer.writerow([("'"+str(v)) if str(v).lstrip().startswith(('=','+','-','@')) else v for v in cells(job)])
    return {'xlsx':str(target),'jobs':len(jobs),'sheets':len(sheets)}


def import_capture(root, file):
    """Import a browser/tool export, preserving its actual evidence and source URL."""
    payload=scout.read_json(file); catalog=registry(root)
    company=next(c for c in catalog['companies'] if c['id']==payload['company_id'])
    source=next(s for s in company['sources'] if s['id']==payload.get('source_id') or s['url']==payload.get('source_url'))
    evidence=payload['evidence']; scout.dt(evidence['captured_at'])
    if scout.dt(evidence['captured_at'])>scout.now_utc()+timedelta(minutes=5): raise ValueError('Future capture')
    path=scout.within(root,evidence['path']); text=path.read_text(encoding='utf-8')
    jobs=[]
    for entry in payload['jobs']:
        if urlsplit(entry['url']).hostname != urlsplit(source['url']).hostname:
            raise ValueError('Job outside registered portal')
        if any(host in urlsplit(source['url']).hostname for host in ('mokahr.com','feishu.cn','z.zhipin.com')) and not scout.under_prefix(entry['url'],source['url']):
            raise ValueError('Job outside registered ATS tenant')
        if entry['url'] not in text and scout.canonical_url(entry['url'])!=scout.canonical_url(evidence['url']):
            raise ValueError('Job URL missing from captured links')
        if str(entry['source_job_id']) not in text and str(entry['source_job_id']) not in entry['url']:
            raise ValueError('Job ID missing from capture')
        if not isinstance(entry.get('locations',[]),list) or any(not isinstance(v,str) or v not in text for v in entry.get('locations',[])):
            raise ValueError('Locations must occur in captured text')
        if entry.get('employment_type','unknown') not in {'internship','fulltime','unknown'}:
            raise ValueError('Invalid employment type')
        for key in ('title','description','requirements'):
            if entry.get(key) and entry[key] not in text: raise ValueError('Job field missing from captured text: '+key)
        jobs.append(normalize(company,source,entry['source_job_id'],entry['title'],entry['url'],
            **{k:v for k,v in entry.items() if k not in {'source_job_id','title','url','company_id','source_id','company'}}))
    with RunLock(Path(root)/'state/run.lock'):
        with scout.connect(root) as c:
            result=save_jobs(c,root,'browser-'+uuid4().hex[:8],jobs,evidence)
            complete=payload.get('pagination_complete') is True
            save_source(c,company,source,'complete' if complete else 'partial',payload.get('next_cursor'),payload.get('total'),int(payload.get('pages',1)),len(jobs),'',None)
        export(root)
    return result


def add_source(root,company,name,sector,url,official_page,capture):
    """Add a runtime entry discovered through an official public link."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,60}",company):raise ValueError("Use an ASCII company id")
    scout.safe_url(url);scout.safe_url(official_page)
    file=scout.within(root,capture);text=file.read_text(encoding="utf-8")
    if url not in text or name not in text:raise ValueError("Capture must contain company name and target link")
    source={"id":company+"-linked-"+scout.digest(url)[:8],"url":url,"adapter":"html",
            "channels":["internship","fulltime"],"entry_status":"official_link_captured",
            "provenance":{"official_page":official_page,"capture":capture,"sha256":scout.digest(file.read_bytes()),"at":scout.timestamp()}}
    path=Path(root)/"sources/registry.json"
    with RunLock(Path(root)/"state/run.lock"):
        data=scout.read_json(path) if path.exists() else {"companies":[]}
        entry=next((c for c in data["companies"] if c["id"]==company),None)
        if entry is None:
            entry={"id":company,"name":name,"sector":sector,"priority":2,"sources":[]};data["companies"].append(entry)
        entry["sources"]=[s for s in entry["sources"] if s["id"]!=source["id"]]+[source]
        scout.write_json(path,data)
    return source


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--workspace',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('status'); sub.add_parser('export')
    plan=sub.add_parser('plan'); plan.add_argument('--companies',nargs='*'); plan.add_argument('--limit',type=int,default=24);plan.add_argument('--offset',type=int,default=0)
    run=sub.add_parser('collect'); run.add_argument('--companies',nargs='*'); run.add_argument('--resume',action='store_true')
    for name,default in [('max-pages',3),('page-size',50),('max-requests',200),('max-details',20),('source-limit',12)]:run.add_argument('--'+name,type=int,default=default)
    run.add_argument('--interval',type=float,default=1.)
    q=sub.add_parser('search')
    for name in ('keyword','company','city','role','experience'): q.add_argument('--'+name,default='')
    q.add_argument('--type',dest='kind',choices=['internship','fulltime','unknown'],default='')
    q.add_argument('--limit',type=int,default=100);q.add_argument('--include-stale',action='store_true');q.add_argument('--include-excluded',action='store_true')
    imp=sub.add_parser('import');imp.add_argument('--file',required=True)
    add=sub.add_parser('source-add')
    for name in ('company','name','sector','url','official-page','capture'):add.add_argument('--'+name,required=True)
    a=vars(p.parse_args());root=initialize(a.pop('workspace'));command=a.pop('command')
    if command=='source-add':result=add_source(root,**a)
    elif command=='collect':result=collect(root,**a)
    elif command=='plan':
        result=make_plan(root,a['companies']);scout.write_json(root/'reports/search-plan.json',result)
        if a['limit']<1 or a['offset']<0:raise ValueError('Use a positive plan limit and nonnegative offset')
        result={**result,'queue':result['queue'][a['offset']:a['offset']+a['limit']],'offset':a['offset']}
    elif command=='search':result=search(root,**a)
    elif command=='status':result=status(root)
    elif command=='import':result=import_capture(root,a['file'])
    else:
        with RunLock(root/'state/run.lock'):result=export(root)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,FetchError) as error:print(str(error),file=sys.stderr);raise SystemExit(2)
