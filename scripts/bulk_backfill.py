"""Approved, restartable bulk campaign with one durable cross-run HTTP budget."""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import time
from uuid import uuid4
import body_backfill as backfill
from public_http import PublicHTTP, BudgetExceeded
from run_daily import RunLock
import scout


class BudgetLedger:
    def __init__(self,path):
        self.path=Path(path);self.data=scout.read_json(path)
        for key,ceiling in [('http',4000),('detail',3500)]:
            limit=self.data['limits'][key];used=self.data['used'][key]
            if not isinstance(limit,int) or not isinstance(used,int) or not 0<=used<=limit<=ceiling:raise ValueError('Invalid persisted campaign budget')
    def reserve(self,url,kind):
        used=self.data['used'];limits=self.data['limits']
        if used['http']>=limits['http']:raise BudgetExceeded('Campaign HTTP budget exhausted')
        if kind=='detail' and used['detail']>=limits['detail']:raise BudgetExceeded('Campaign detail budget exhausted')
        used['http']+=1
        if kind=='detail':used['detail']+=1
        self.data['new_attempts_by_kind'][kind]=self.data['new_attempts_by_kind'].get(kind,0)+1
        self.data['last_reservation']={'at':scout.timestamp(),'url':url,'kind':kind}
        scout.write_json(self.path,self.data)
    def remaining(self):
        return {key:self.data['limits'][key]-self.data['used'][key] for key in ('http','detail')}


def campaign_dir(root,ident):
    if not ident or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for ch in ident):
        raise ValueError('Invalid campaign id')
    return scout.within(root,'reports/body-campaigns/'+ident)


def create(root,approval_note,max_http=4000,max_detail=3500,include_prior=True):
    if not 1<=max_http<=4000 or not 1<=max_detail<=3500:raise ValueError('Approved bulk ceiling is 4000 HTTP / 3500 detail attempts')
    root=backfill.setup(root)
    if (root/'state/body-campaign.lock').exists():raise RuntimeError('A bulk campaign is already active')
    ident='bulk-'+scout.now_utc().strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex[:6]
    out=campaign_dir(root,ident);out.mkdir(parents=True)
    with RunLock(root/'state/run.lock'):
        with scout.connect(root) as c:
            tasks=[dict(r) for r in c.execute("SELECT task_id,job_key,source_id FROM discovery_body_tasks WHERE status IN ('pending','retry','running') ORDER BY task_id")]
            runs=[json.loads(r[0]) for r in c.execute('SELECT payload FROM discovery_backfill_runs')] if include_prior else []
            prior={'http':sum(r.get('requests',0) for r in runs),'detail':sum(r.get('detail_attempts',0) for r in runs)}
            if prior['http']>max_http or prior['detail']>max_detail:raise ValueError('Prior usage already exceeds limits')
            import sqlite3
            with sqlite3.connect(out/'before.sqlite3',factory=scout.ClosingConnection) as dest:c.backup(dest)
        plan={'schema_version':1,'campaign_id':ident,'created_at':scout.timestamp(),'approval_note':approval_note,
              'limits':{'http':max_http,'detail':max_detail},'prior_usage':prior,
              'sources':sorted({t['source_id'] for t in tasks}),'task_ids':[t['task_id'] for t in tasks],
              'job_keys':[t['job_key'] for t in tasks],'task_count':len(tasks),
              'stop_rule':'Stop a source on access blocking; never reset campaign usage on resume.'}
        scout.write_json(out/'plan.json',plan)
        scout.write_json(out/'budget.json',{'limits':plan['limits'],'used':dict(prior),'prior_usage':prior,'new_attempts_by_kind':{}})
        scout.write_json(out/'blocks.json',{})
    return plan


def snapshot(root,plan):
    wanted=set(plan['task_ids'])
    with scout.connect(root) as c:rows=[dict(r) for r in c.execute('SELECT * FROM discovery_body_tasks') if r['task_id'] in wanted]
    counts=dict(Counter(r['status'] for r in rows));by_source={}
    for source in plan['sources']:by_source[source]=dict(Counter(r['status'] for r in rows if r['source_id']==source))
    return rows,{'counts':counts,'by_source':by_source,'scope_tasks':len(rows)}


def _run_locked(root,ident,interval=1.,timeout=20,retries=2):
    if interval<1:raise ValueError('Approved bulk mode requires at least one second between same-host requests')
    root=Path(root).resolve();out=campaign_dir(root,ident);plan=scout.read_json(out/'plan.json')
    ledger=BudgetLedger(out/'budget.json');blocks=scout.read_json(out/'blocks.json');blocked=set(blocks)
    remaining=ledger.remaining();started=time.monotonic();rounds=[];last_emit=0
    client=PublicHTTP(root,ident,max_requests=remaining['http'],max_detail_attempts=remaining['detail'],
                      interval=interval,timeout=timeout,retries=retries,on_attempt=ledger.reserve)
    def emit(status='running',force=False):
        nonlocal last_emit
        if not force and time.monotonic()-last_emit<30:return
        _,progress=snapshot(root,plan)
        progress.update(campaign_id=ident,status=status,at=scout.timestamp(),budget=ledger.data,
                        blocked_sources=blocks,elapsed_this_session_seconds=round(time.monotonic()-started,2))
        scout.write_json(out/'progress.json',progress)
        print(scout.dumps({'campaign_id':ident,'status':status,'counts':progress['counts'],
              'http_used_total':ledger.data['used']['http'],'detail_used_total':ledger.data['used']['detail'],
              'blocked_sources':list(blocks)}),flush=True)
        last_emit=time.monotonic()
    def block(source,error):
        blocks[source]={'at':scout.timestamp(),'reason':error};blocked.add(source)
        scout.write_json(out/'blocks.json',blocks)
        emit(force=True)
    emit(force=True)
    try:
        while True:
            rows,progress=snapshot(root,plan)
            active=[r for r in rows if r['source_id'] not in blocked and r['status'] in {'pending','retry','running'}]
            if not active:
                status='complete' if all(r['status'] in {'succeeded','source_closed'} for r in rows) else 'finished_with_issues';break
            left=ledger.remaining()
            if left['http']<=0 or left['detail']<=0:status='budget_exhausted';break
            now=scout.timestamp()
            due=[r for r in active if r['status']=='pending' or (r['status']=='retry' and (not r['next_attempt_at'] or r['next_attempt_at']<=now)) or (r['status']=='running' and r['lease_until'] and r['lease_until']<=now)]
            if not due:
                emit('waiting_for_retry');time.sleep(5);continue
            report=backfill.run(root,sources=plan['sources'],max_http_attempts=client.max_requests,
                max_detail_attempts=client.max_detail_attempts,max_tasks=plan['task_count'],client=client,
                interval=interval,timeout=timeout,retries=retries,budget_profile='approved_bulk',task_ids=plan['task_ids'],
                blocked_sources=blocked,on_progress=lambda event:emit(),on_block=block)
            rounds.append(report['run_id']);emit(force=True)
            if report['logical_tasks_attempted']==0 and not blocked:
                # Do not spin if no due task can actually run.
                status='needs_review';break
    except BaseException:
        emit('interrupted',force=True);raise
    emit(status,force=True)
    rows,progress=snapshot(root,plan)
    result={'campaign_id':ident,'status':status,'ended_at':scout.timestamp(),'scope':plan,
            'budget':ledger.data,'queue':progress,'blocked_sources':blocks,'run_ids_this_session':rounds,
            'unresolved_tasks':[r for r in rows if r['status'] not in {'succeeded','source_closed'}]}
    scout.write_json(out/'result.json',result)
    return result


def run(root,ident,interval=1.,timeout=20,retries=2):
    root=Path(root).resolve()
    with RunLock(root/'state/body-campaign.lock'):
        backfill.setup(root)
        return _run_locked(root,ident,interval,timeout,retries)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--workspace',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('create');p.add_argument('--approval-note',required=True);p.add_argument('--max-http',type=int,default=4000);p.add_argument('--max-detail',type=int,default=3500)
    p=sub.add_parser('run');p.add_argument('--campaign',required=True);p.add_argument('--interval',type=float,default=1.);p.add_argument('--timeout',type=float,default=20);p.add_argument('--retries',type=int,default=2)
    p=sub.add_parser('status');p.add_argument('--campaign',required=True)
    a=vars(parser.parse_args());root=a.pop('workspace');command=a.pop('command')
    if command=='create':result=create(root,**{k.replace('-','_'):v for k,v in a.items()})
    elif command=='run':result=run(root,a.pop('campaign'),**a);result={k:v for k,v in result.items() if k not in {'unresolved_tasks','scope'}}
    else:
        path=campaign_dir(root,a['campaign'])/'progress.json';result=scout.read_json(path)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
