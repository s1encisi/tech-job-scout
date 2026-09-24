"""Field-level body provenance shared by cached recovery and detail backfill."""
from __future__ import annotations
import json
from datetime import timedelta
from pathlib import Path
import scout
from job_sources import plain

FIELDS = ('description', 'requirements')
EXTRACTOR_VERSION = 'body-fields/1'
SCHEMA = {
    'tencent': {'description':'Responsibility','requirements':'Requirement'},
    'netease': {'description':'description','requirements':'requirement'},
    'mihoyo': {'description':'description','requirements':'jobRequire'},
}


def ensure_schema(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS discovery_field_state(
      job_key TEXT NOT NULL,field TEXT NOT NULL,status TEXT NOT NULL,value_text TEXT,
      raw_text TEXT,evidence TEXT,acquired_at TEXT,capture_kind TEXT,completeness TEXT,
      last_attempt_at TEXT,last_attempt_status TEXT,extractor_version TEXT,reason TEXT,
      PRIMARY KEY(job_key,field));
    CREATE TABLE IF NOT EXISTS discovery_field_observations(
      id TEXT PRIMARY KEY,job_key TEXT NOT NULL,field TEXT NOT NULL,status TEXT NOT NULL,
      value_text TEXT,evidence TEXT,at TEXT NOT NULL,extractor_version TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS discovery_body_tasks(
      task_id TEXT PRIMARY KEY,job_key TEXT UNIQUE NOT NULL,source_id TEXT NOT NULL,
      status TEXT NOT NULL,reason TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
      http_attempts INTEGER NOT NULL DEFAULT 0,detail_attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at TEXT,lease_owner TEXT,
      lease_until TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS discovery_backfill_runs(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS discovery_phase_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL);
    ''')
    columns={r[1] for r in c.execute('PRAGMA table_info(discovery_body_tasks)')}
    if 'detail_attempts' not in columns:c.execute('ALTER TABLE discovery_body_tasks ADD COLUMN detail_attempts INTEGER NOT NULL DEFAULT 0')
    c.execute('INSERT OR IGNORE INTO discovery_phase_migrations VALUES(?,?)',('phase1-body-v1',scout.timestamp()))


def decode_response(adapter, payload):
    """Return source records and capture kind without fabricating an empty success."""
    success_key = 'Code' if adapter == 'tencent' else 'code'
    expected = 0 if adapter == 'mihoyo' else 200
    if adapter not in SCHEMA or not isinstance(payload,dict) or payload.get(success_key) != expected:
        raise ValueError('Unsupported adapter or unsuccessful saved response')
    data=payload.get('Data' if adapter=='tencent' else 'data')
    if not isinstance(data,dict):raise ValueError('Missing response data object')
    list_key='Posts' if adapter=='tencent' else 'list'
    id_key='PostId' if adapter=='tencent' else 'id'
    kind='list' if list_key in data else 'detail'
    records=data[list_key] if kind=='list' else [data]
    if not isinstance(records,list):raise ValueError('Invalid record list')
    found={}
    for item in records:
        if not isinstance(item,dict) or item.get(id_key) is None:raise ValueError('Record identity missing')
        found[str(item[id_key])]=item
    return found,kind


def extract(adapter, record, kind):
    mappings=dict(SCHEMA[adapter])
    if adapter=='mihoyo' and kind=='list':mappings['description']='jobSummary'
    result={}
    for field,key in mappings.items():
        raw=record.get(key)
        if key not in record or not isinstance(raw,(str,type(None))):
            status='parse_failed' if kind=='detail' else 'not_requested'
            result[field]={'status':status,'value':None,'raw':None,'completeness':'unknown',
                           'reason':'Expected field missing' if kind=='detail' else 'Not present in listing'}
            continue
        value=plain(raw)
        status='obtained' if value else ('not_disclosed' if kind=='detail' else 'not_requested')
        completeness='summary' if adapter=='mihoyo' and kind=='list' else ('detail_field' if kind=='detail' else 'unassessed')
        if value.rstrip().endswith(('...','…','[展开]')):completeness='suspected_truncated'
        result[field]={'status':status,'value':value if value else None,'raw':raw,'completeness':completeness,
                       'reason':'Empty field in checked detail response' if status=='not_disclosed' else ''}
    return result


def checked_evidence(root, receipt):
    path=scout.within(root,receipt['path'])
    if scout.digest(path.read_bytes()) != receipt['sha256']:raise ValueError('Evidence integrity mismatch: '+receipt['path'])
    if scout.dt(receipt['captured_at'])>scout.now_utc()+timedelta(minutes=5):raise ValueError('Future capture timestamp')
    return path


def apply(c, root, job_key, fields, receipt, kind, run_id, attempt_at=None, verified=False):
    """Update body/provenance atomically; keep job keys, notes and list timestamps."""
    if not verified:checked_evidence(root,receipt)
    row=c.execute('SELECT * FROM discovery_jobs WHERE key=?',(job_key,)).fetchone()
    if row is None:raise ValueError('Unknown job key')
    job=json.loads(row['payload']);before={f:job.get(f,'') for f in FIELDS}
    capture=receipt['captured_at'];attempt_at=attempt_at or scout.timestamp()
    for field,observed in fields.items():
        if field not in FIELDS:raise ValueError('Unexpected body field')
        old=c.execute('SELECT * FROM discovery_field_state WHERE job_key=? AND field=?',(job_key,field)).fetchone()
        observation_id=scout.digest([job_key,field,receipt['path'],receipt['sha256'],observed,EXTRACTOR_VERSION])
        c.execute('INSERT OR IGNORE INTO discovery_field_observations VALUES(?,?,?,?,?,?,?,?)',
                  (observation_id,job_key,field,observed['status'],observed['value'],scout.dumps(receipt),capture,EXTRACTOR_VERSION))
        newer=not old or not old['acquired_at'] or scout.dt(capture)>=scout.dt(old['acquired_at'])
        keep_detail=old and old['value_text'] and old['capture_kind']=='detail' and kind=='list'
        # A listing summary or a failed/empty response cannot erase the last useful body.
        replace=(observed['status']=='obtained' and newer and not keep_detail)
        if replace:
            job[field]=observed['value']
            c.execute('''INSERT OR REPLACE INTO discovery_field_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (job_key,field,'obtained',observed['value'],observed['raw'],scout.dumps(receipt),capture,kind,
               observed['completeness'],attempt_at,observed['status'],EXTRACTOR_VERSION,observed['reason']))
        elif old and old['value_text']:
            if kind=='detail' and observed['status']=='not_disclosed' and newer:
                reason='Latest detail has no value; previous body retained for review'
                c.execute('UPDATE discovery_field_state SET status=?,last_attempt_at=?,last_attempt_status=?,reason=? WHERE job_key=? AND field=?',
                          ('conflict',attempt_at,'not_disclosed',reason,job_key,field))
            elif kind=='detail':
                c.execute('UPDATE discovery_field_state SET last_attempt_at=?,last_attempt_status=?,reason=? WHERE job_key=? AND field=?',
                          (attempt_at,observed['status'],observed['reason'],job_key,field))
        elif not old or newer:
            # Legacy text is preserved until its specific source is recovered.
            retained=job.get(field) or (old['value_text'] if old else None)
            state='conflict' if retained and observed['status']=='not_disclosed' else observed['status']
            c.execute('INSERT OR REPLACE INTO discovery_field_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (job_key,field,state,retained,observed['raw'],scout.dumps(receipt),capture,kind,
               observed['completeness'],attempt_at,observed['status'],EXTRACTOR_VERSION,observed['reason']))
    states={r['field']:dict(r) for r in c.execute('SELECT * FROM discovery_field_state WHERE job_key=?',(job_key,))}
    job['field_evidence']={f:{**json.loads(s['evidence']),'capture_kind':s['capture_kind']} for f,s in states.items() if s['value_text'] and s['evidence']}
    if kind=='detail' and all(v['status'] in {'obtained','not_disclosed'} for v in fields.values()):
        job['detail_evidence']=receipt
    job['body_status']='full_description' if job.get('description') and job.get('requirements') else ('description_available' if job.get('description') else 'listing_only')
    fingerprint=scout.digest({k:v for k,v in job.items() if k not in {'raw','updated_at','detail_evidence','field_evidence'}})
    c.execute('UPDATE discovery_jobs SET payload=?,content_hash=? WHERE key=?',(scout.dumps(job),fingerprint,job_key))
    changed=any(before[f] != job.get(f,'') for f in FIELDS)
    if changed:c.execute('INSERT INTO discovery_events(run_id,job_key,kind,at) VALUES(?,?,?,?)',(run_id,job_key,'body_updated',attempt_at))
    return changed


def failure(c, job_key, status, message, when=None):
    when=when or scout.timestamp()
    for field in FIELDS:
        old=c.execute('SELECT * FROM discovery_field_state WHERE job_key=? AND field=?',(job_key,field)).fetchone()
        if old:
            value_status=old['status'] if old['value_text'] else status
            c.execute('UPDATE discovery_field_state SET status=?,last_attempt_at=?,last_attempt_status=?,reason=? WHERE job_key=? AND field=?',
                      (value_status,when,status,message,job_key,field))
        else:
            c.execute('INSERT INTO discovery_field_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (job_key,field,status,None,None,None,None,None,'unknown',when,status,EXTRACTOR_VERSION,message))


def cache_index(root, catalog):
    """Verify and index previously captured responses once, with no network requests."""
    hosts={}
    for company in catalog['companies']:
        for source in company['sources']:
            if source['adapter'] in SCHEMA:
                from urllib.parse import urlsplit
                hosts[urlsplit(source['api_url']).hostname]=source['adapter']
    items={};errors=[];verified=0
    for path in sorted((Path(root)/'evidence').rglob('*.meta.json')):
        try:
            receipt=scout.read_json(path)
            if receipt.get('http_status',200)>=400:continue
            from urllib.parse import urlsplit
            adapter=hosts.get(urlsplit(receipt['url']).hostname)
            if not adapter:continue
            data=checked_evidence(root,receipt);records,kind=decode_response(adapter,scout.read_json(data));verified+=1
            for ident,record in records.items():items.setdefault((adapter,ident),[]).append((receipt,kind,record))
        except (OSError,ValueError,KeyError,TypeError) as error:errors.append({'file':path.relative_to(root).as_posix(),'error':str(error)})
    for group in items.values():group.sort(key=lambda item:(scout.dt(item[0]['captured_at']),item[1]=='detail'))
    return items,{'verified_responses':verified,'errors':errors}


def mark_tencent_closed(c,root,job_key,receipt,run_id):
    from urllib.parse import urlsplit,parse_qs
    from job_sources import tencent_closed_payload
    path=checked_evidence(root,receipt);payload=scout.read_json(path)
    if not tencent_closed_payload(payload):raise ValueError('No explicit Tencent closure code')
    row=c.execute('SELECT payload FROM discovery_jobs WHERE key=?',(job_key,)).fetchone()
    if row is None:raise ValueError('Unknown job identity')
    job=json.loads(row['payload'])
    url=urlsplit(receipt['url'])
    if urlsplit(job['url']).hostname not in {'careers.tencent.com','jobs.tencent.com'}:raise ValueError('Job is not from Tencent portal')
    if url.hostname!='careers.tencent.com' or url.path!='/tencentcareer/api/post/ByPostId' or parse_qs(url.query).get('postId')!=[job['source_job_id']]:
        raise ValueError('Closure response does not identify this posting')
    definition=scout.read_json(Path(__file__).resolve().parents[1]/'references/provider-status-codes.json')['tencent']['E1005']
    job['source_status']='closed';job['closure_observed_at']=receipt['captured_at']
    job['closure_evidence']={'response':receipt,'code':'E1005','definition':definition}
    fingerprint=scout.digest({k:v for k,v in job.items() if k not in {'raw','updated_at','detail_evidence','field_evidence'}})
    c.execute('UPDATE discovery_jobs SET payload=?,content_hash=? WHERE key=?',(scout.dumps(job),fingerprint,job_key))
    c.execute('INSERT INTO discovery_events(run_id,job_key,kind,at) VALUES(?,?,?,?)',(run_id,job_key,'source_closed',scout.timestamp()))
    c.execute("UPDATE discovery_body_tasks SET status='source_closed',lease_owner=NULL,lease_until=NULL,updated_at=?,last_error='Official Tencent E1005: posting closed' WHERE job_key=?",(scout.timestamp(),job_key))
