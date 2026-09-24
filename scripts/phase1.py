"""Phase 1 baseline, body recovery, bounded backfill and human-review sampling."""
import argparse
import json
from pathlib import Path
import body_backfill as backfill
import phase1_reports as reports
import scout


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--workspace',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    q=sub.add_parser('audit');q.add_argument('--output',required=True);q.add_argument('--asof')
    q=sub.add_parser('prepare');q.add_argument('--sources',nargs='*')
    sub.add_parser('queue')
    q=sub.add_parser('backfill');q.add_argument('--sources',nargs='*')
    q.add_argument('--max-http-attempts',type=int,default=100);q.add_argument('--max-detail-attempts',type=int,default=30)
    q.add_argument('--max-tasks',type=int,default=30);q.add_argument('--interval',type=float,default=1.)
    q.add_argument('--review-sample');q.add_argument('--timeout',type=float,default=20);q.add_argument('--retries',type=int,default=2)
    q=sub.add_parser('sample');q.add_argument('--output',required=True);q.add_argument('--sources',nargs='+',required=True)
    q.add_argument('--cohort');q.add_argument('--per-source',type=int,default=30);q.add_argument('--seed',default='20260917')
    args=vars(parser.parse_args());root=Path(args.pop('workspace')).resolve();command=args.pop('command')
    if command=='audit':result=reports.audit(root,**args);result={'asof':result['asof'],'coverage':result['coverage'],'quality':result['quality']}
    elif command=='prepare':result=backfill.prepare(root,**args)
    elif command=='backfill':
        sample=args.pop('review_sample')
        priorities=[r['job_key'] for r in scout.read_json(sample)['records']] if sample else None
        result=backfill.run(root,prioritize_keys=priorities,**args)
    elif command=='sample':result=reports.sample(root,**args)
    else:backfill.setup(root);result=backfill.queue_status(root)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
