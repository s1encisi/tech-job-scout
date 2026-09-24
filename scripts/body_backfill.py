"""Persistent, cache-first body backfill; no model or browser dependency."""
from __future__ import annotations
from collections import defaultdict, deque
from datetime import timedelta
import json
from pathlib import Path
import time
from uuid import uuid4
import body_fields as bf
import discovery as d
from job_sources import ADAPTERS,PostingClosed
from public_http import PublicHTTP, FetchError, BudgetExceeded
from run_daily import RunLock
import scout


def setup(root):
    root=d.initialize(root)
    with scout.connect(root) as c:bf.ensure_schema(c)
    return root


def unresolved(c,key):
    job_row=c.execute('SELECT payload FROM discovery_jobs WHERE key=?',(key,)).fetchone()
    if job_row and json.loads(job_row[0]).get('source_status')=='closed':return []
    rows={r['field']:r for r in c.execute('SELECT * FROM discovery_field_state WHERE job_key=?',(key,))}
    return [f for f in bf.FIELDS if f not in rows or rows[f]['status'] not in {'obtained','not_disclosed'}
            or rows[f]['completeness'] in {'summary','suspected_truncated'}]


def enqueue(c,key,source,reason,can_fetch,when):
    old=c.execute('SELECT * FROM discovery_body_tasks WHERE job_key=?',(key,)).fetchone()
    if old:
        # Preserve prior attempts, blocked state and backoff across repeated preparation.
        if old['status']=='succeeded':
            c.execute("UPDATE discovery_body_tasks SET status=?,reason=?,next_attempt_at=?,updated_at=? WHERE job_key=?",
                ('pending' if can_fetch else 'manual',reason,when,when,key))
        return
    c.execute('INSERT INTO discovery_body_tasks(task_id,job_key,source_id,status,reason,attempts,http_attempts,next_attempt_at,lease_owner,lease_until,created_at,updated_at,last_error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('body_'+scout.digest(key)[:24],key,source,'pending' if can_fetch else 'manual',reason,0,0,when,None,None,when,when,''))


def prepare(root, sources=None):
    root=setup(root);catalog=d.registry(root);selected=set(sources or [])
    source_map={s['id']:s for co in catalog['companies'] for s in co['sources']}
    if selected-set(source_map):raise ValueError('Unknown sources: '+','.join(sorted(selected-set(source_map))))
    run_id='cache-'+uuid4().hex[:10];when=scout.timestamp();restored=[];attempted=0
    with RunLock(root/'state/run.lock'):
        index,cache_report=bf.cache_index(root,catalog)
        with scout.connect(root) as c:
            rows=c.execute('SELECT * FROM discovery_jobs ORDER BY key').fetchall()
            for row in rows:
                if selected and row['source_id'] not in selected:continue
                job=json.loads(row['payload']);source=source_map.get(row['source_id'])
                adapter=source['adapter'] if source else None
                before={f:job.get(f,'') for f in bf.FIELDS};attempted+=1
                for receipt,kind,record in index.get((adapter,job['source_job_id']),[]):
                    bf.apply(c,root,row['key'],bf.extract(adapter,record,kind),receipt,kind,run_id,when)
                after=json.loads(c.execute('SELECT payload FROM discovery_jobs WHERE key=?',(row['key'],)).fetchone()[0])
                if any(not before[f] and after.get(f) for f in bf.FIELDS):restored.append(row['key'])
                missing=unresolved(c,row['key'])
                if missing:
                    can_fetch=source is not None and hasattr(ADAPTERS[source['adapter']](),'detail')
                    enqueue(c,row['key'],row['source_id'],','.join(missing),can_fetch,when)
                else:
                    state='source_closed' if after.get('source_status')=='closed' else 'succeeded'
                    c.execute("UPDATE discovery_body_tasks SET status=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_key=? AND status!='running'",(state,when,row['key']))
    report={'run_id':run_id,'at':when,'examined_jobs':attempted,'restored_jobs':len(restored),
            'restored_job_keys':restored,'network_requests':0,'cache':cache_report,'queue':queue_status(root)}
    scout.write_json(root/'reports/phase1'/(run_id+'.json'),report)
    return report


def queue_status(root):
    with scout.connect(root) as c:
        counts={r[0]:r[1] for r in c.execute('SELECT status,COUNT(*) FROM discovery_body_tasks GROUP BY status')}
        by_source=[dict(r) for r in c.execute('SELECT source_id,status,COUNT(*) AS count,SUM(attempts) AS task_attempts,SUM(http_attempts) AS http_attempts,SUM(detail_attempts) AS detail_attempts FROM discovery_body_tasks GROUP BY source_id,status ORDER BY source_id,status')]
    return {'counts':counts,'by_source':by_source}


def candidates(c,when,sources=None,prioritize_keys=None):
    # A process interrupted after claim can be resumed after its lease expires.
    c.execute("UPDATE discovery_body_tasks SET status='retry',lease_owner=NULL,lease_until=NULL,next_attempt_at=?,updated_at=?,last_error='Expired worker lease' WHERE status='running' AND lease_until<=?",(when,when,when))
    rows=c.execute("SELECT * FROM discovery_body_tasks WHERE status IN ('pending','retry') AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY attempts,created_at,task_id",(when,)).fetchall()
    groups=defaultdict(deque)
    priority=set(prioritize_keys or [])
    rows=sorted(rows,key=lambda r:(r['job_key'] not in priority,r['attempts'],r['created_at'],r['task_id']))
    for row in rows:
        if not sources or row['source_id'] in sources:groups[row['source_id']].append(dict(row))
    result=[]
    while any(groups.values()):
        for source in sorted(groups):
            if groups[source]:result.append(groups[source].popleft())
    return result


def run(root,sources=None,max_http_attempts=100,max_detail_attempts=30,max_tasks=30,
        interval=1.,timeout=20,retries=2,client=None,now=None,prioritize_keys=None,budget_profile='smoke',task_ids=None,blocked_sources=None,on_progress=None,on_block=None):
    ceilings={'smoke':(100,30),'approved_bulk':(4000,3500)}
    if budget_profile not in ceilings:raise ValueError('Unknown budget profile')
    http_ceiling,detail_ceiling=ceilings[budget_profile]
    if not 1<=max_http_attempts<=http_ceiling or not 0<=max_detail_attempts<=detail_ceiling or max_tasks<1:
        raise ValueError(f'{budget_profile} limits: HTTP <= {http_ceiling}, detail <= {detail_ceiling}; positive task count')
    if interval<0 or timeout<=0 or not 0<=retries<=2:raise ValueError('Invalid transport settings')
    root=setup(root);source_map={s['id']:s for co in d.registry(root)['companies'] for s in co['sources']}
    selected=set(sources or [])
    if selected-set(source_map):raise ValueError('Unknown source selection')
    run_id='backfill-'+scout.now_utc().strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex[:6]
    client=client or PublicHTTP(root,run_id,max_requests=max_http_attempts,max_detail_attempts=max_detail_attempts,
                                interval=interval,timeout=timeout,retries=retries)
    before_metrics=json.loads(json.dumps(client.metrics()));latency_start=len(getattr(client,'latencies',[]))
    blocked_sources=blocked_sources if blocked_sources is not None else set()
    allowed_tasks=set(task_ids) if task_ids is not None else None
    client.request_kind='detail';when=scout.timestamp(now);start=time.monotonic();results=[];interrupted=False;claimed_tasks=0;active_task=None
    config={'budget_profile':budget_profile,'max_http_attempts':max_http_attempts,'max_detail_attempts':max_detail_attempts,
            'max_tasks':max_tasks,'interval_seconds':interval,'timeout_seconds':timeout,'retries':retries,
            'source_selection':sorted(selected),'priority_job_keys_sha256':scout.digest(sorted(prioritize_keys or [])),'max_task_attempts':3,'lease_seconds':300}
    with RunLock(root/'state/run.lock'):
        with scout.connect(root) as c:tasks=candidates(c,when,selected,prioritize_keys)
        try:
            for task in tasks:
                if (allowed_tasks is not None and task['task_id'] not in allowed_tasks) or task['source_id'] in blocked_sources:continue
                if claimed_tasks>=max_tasks:break
                if client.requests>=max_http_attempts or client.detail_attempts>=max_detail_attempts:break
                source=source_map.get(task['source_id']);adapter=ADAPTERS[source['adapter']]() if source else None
                before_detail=client.detail_attempts;before_http=client.requests
                with scout.connect(root) as c:
                    if not adapter or not hasattr(adapter,'detail'):
                        c.execute("UPDATE discovery_body_tasks SET status='manual',updated_at=?,last_error='No detail adapter' WHERE task_id=?",(scout.timestamp(),task['task_id']));continue
                    if task['attempts']>=3:
                        c.execute("UPDATE discovery_body_tasks SET status='exhausted',updated_at=? WHERE task_id=?",(when,task['task_id']));continue
                    row=c.execute('SELECT * FROM discovery_jobs WHERE key=?',(task['job_key'],)).fetchone()
                    if row is None:
                        c.execute("UPDATE discovery_body_tasks SET status='manual',last_error='Missing job',updated_at=? WHERE task_id=?",(when,task['task_id']));continue
                    if not unresolved(c,task['job_key']):
                        state='source_closed' if json.loads(row['payload']).get('source_status')=='closed' else 'succeeded'
                        c.execute("UPDATE discovery_body_tasks SET status=?,updated_at=? WHERE task_id=?",(state,when,task['task_id']));continue
                    job=json.loads(row['payload'])
                    c.execute("UPDATE discovery_body_tasks SET status='running',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE task_id=?",(run_id,scout.timestamp((now or scout.now_utc())+timedelta(seconds=300)),when,task['task_id']))
                claimed_tasks+=1;active_task=(task,before_detail,before_http)
                outcome='succeeded';error='';receipt=None;fields=None;fetch_status=None
                try:
                    _,receipt=adapter.detail(client,job)
                    raw=scout.read_json(bf.checked_evidence(root,receipt))
                    records,kind=bf.decode_response(source['adapter'],raw)
                    if kind!='detail' or job['source_job_id'] not in records:raise ValueError('Detail response identity/kind mismatch')
                    fields=bf.extract(source['adapter'],records[job['source_job_id']],kind)
                    if any(v['status']=='parse_failed' for v in fields.values()):
                        outcome='retry';error='Detail field mapping incomplete';fetch_status='parse_failed'
                    elif any(v['completeness']=='suspected_truncated' for v in fields.values()):
                        outcome='manual';error='Detail text may be truncated; review source wording'
                except PostingClosed as ex:
                    outcome='source_closed';receipt=ex.receipt;error=str(ex)
                except BudgetExceeded as ex:
                    outcome='pending';error=str(ex)
                except FetchError as ex:
                    error=str(ex);fetch_status='fetch_failed'
                    outcome='blocked' if any(x in error for x in ('HTTP 401','HTTP 403','HTTP 405','HTTP 429','robots.txt disallows','Host requested retry','robots.txt unavailable')) else 'retry'
                except (ValueError,KeyError,TypeError,OSError) as ex:
                    outcome='retry';error=str(ex);fetch_status='parse_failed'
                if outcome=='blocked':
                    blocked_sources.add(task['source_id'])
                    if on_block:on_block(task['source_id'],error)
                detail_used=client.detail_attempts-before_detail;http_used=client.requests-before_http
                attempt_increment=int(detail_used>0 or outcome not in {'pending'})
                attempts=task['attempts']+attempt_increment
                if outcome=='retry' and attempts>=3:outcome='exhausted'
                finished=scout.timestamp();next_at=scout.timestamp(scout.now_utc()+timedelta(seconds=min(3600,60*2**max(0,attempts-1)))) if outcome=='retry' else finished
                with scout.connect(root) as c:
                    if outcome=='source_closed':bf.mark_tencent_closed(c,root,task['job_key'],receipt,run_id)
                    elif fields is not None:
                        bf.apply(c,root,task['job_key'],fields,receipt,'detail',run_id,finished)
                        if outcome=='succeeded' and unresolved(c,task['job_key']):outcome='manual';error='Conflicting field values need review'
                    elif fetch_status:bf.failure(c,task['job_key'],fetch_status,error,finished)
                    c.execute('''UPDATE discovery_body_tasks SET status=?,attempts=attempts+?,http_attempts=http_attempts+?,detail_attempts=detail_attempts+?,
                      next_attempt_at=?,lease_owner=NULL,lease_until=NULL,updated_at=?,last_error=? WHERE task_id=?''',
                      (outcome,attempt_increment-1,http_used,detail_used,next_at,finished,error,task['task_id']))
                results.append({'task_id':task['task_id'],'job_key':task['job_key'],'source_id':task['source_id'],
                                'status':outcome,'http_attempts':http_used,'detail_attempts':detail_used,'error':error})
                active_task=None
                if on_progress:on_progress({'task_id':task['task_id'],'source_id':task['source_id'],'status':outcome,'claimed_tasks':claimed_tasks,**client.metrics()})
                if outcome=='pending' and error:break
        except BaseException:
            interrupted=True
            raise
        finally:
            with scout.connect(root) as c:
                if active_task:
                    active,detail_before,http_before=active_task
                    c.execute('UPDATE discovery_body_tasks SET http_attempts=http_attempts+?,detail_attempts=detail_attempts+? WHERE task_id=? AND lease_owner=?',(client.requests-http_before,client.detail_attempts-detail_before,active['task_id'],run_id))
                c.execute("UPDATE discovery_body_tasks SET status='retry',lease_owner=NULL,lease_until=NULL,next_attempt_at=?,updated_at=?,last_error='Interrupted; eligible to resume' WHERE lease_owner=?",(scout.timestamp(),scout.timestamp(),run_id))
            metrics=client.metrics()
            for name in ('requests','detail_attempts','retries','redirects','successful_response_bytes'):
                if name in metrics:metrics[name]-=before_metrics.get(name,0)
            if 'attempts_by_kind' in metrics:
                metrics['attempts_by_kind']={k:v-before_metrics.get('attempts_by_kind',{}).get(k,0) for k,v in metrics['attempts_by_kind'].items()}
            values=sorted(getattr(client,'latencies',[])[latency_start:])
            if values:
                for key,p in [('latency_p50_seconds',.5),('latency_p95_seconds',.95)]:metrics[key]=round(values[int((len(values)-1)*p)],3)
            report={'run_id':run_id,'started_at':when,'ended_at':scout.timestamp(),'interrupted':interrupted,
                    'effective_config':config,'config_fingerprint':scout.digest(config),'results':results,
                    'logical_tasks_attempted':claimed_tasks,'elapsed_seconds':round(time.monotonic()-start,3),
                    **metrics,'blocked_sources':sorted(blocked_sources),'queue':queue_status(root)}
            report['status']='interrupted' if interrupted else ('complete' if not any(report['queue']['counts'].get(s,0) for s in ('pending','retry','running','blocked','manual','exhausted')) else 'partial')
            with scout.connect(root) as c:c.execute('INSERT INTO discovery_backfill_runs VALUES(?,?)',(run_id,scout.dumps(report)))
            scout.write_json(root/'reports/phase1'/(run_id+'.json'),report)
    return report
