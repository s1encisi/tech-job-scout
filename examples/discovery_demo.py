"""Offline discovery demo: pagination, deduplication, filtering and workbook export."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import discovery
from job_sources import Page, normalize
import scout

CATALOG={'companies':[{'id':'demo-tech','name':'虚构科技公司','sector':'游戏研发与发行','priority':1,
    'sources':[{'id':'demo-public','url':'https://jobs.example.test/','adapter':'offline-demo'}]}]}


class Client:
    def metrics(self):return {'requests':0,'retries':0}


class OfflineSource:
    def page(self,client,company,source,cursor,size):
        number=int(cursor or 1)
        rows=[('DEMO-1','软件开发实习生','internship'),('DEMO-2','游戏引擎工程师','fulltime')] if number==1 else [('DEMO-3','游戏场景美术','fulltime')]
        jobs=[normalize(company,source,ident,title,'https://jobs.example.test/'+ident,employment_type=kind,
            locations=['上海'],description='开发与维护游戏软件。',requirements='了解Python或C++。') for ident,title,kind in rows]
        raw=json.dumps(jobs,ensure_ascii=False).encode();path=client.root/'evidence'/('demo-page-'+str(number)+'.txt');path.write_bytes(raw)
        receipt={'path':path.relative_to(client.root).as_posix(),'url':'https://jobs.example.test/?page='+str(number),
            'captured_at':scout.timestamp(),'sha256':hashlib.sha256(raw).hexdigest()}
        return Page(jobs,2 if number==1 else None,3,receipt)


def run(output):
    root=Path(output).resolve()
    if root.exists():raise ValueError('Choose a new output directory')
    discovery.initialize(root);client=Client();client.root=root
    with mock.patch.object(discovery,'registry',return_value=CATALOG),mock.patch.dict(discovery.ADAPTERS,{'offline-demo':OfflineSource}),mock.patch('socket.socket',side_effect=AssertionError('Offline demo')):
        discovery.collect(root,max_pages=1,page_size=2,max_details=0,client=client)
        result=discovery.collect(root,max_pages=1,page_size=2,max_details=0,resume=True,client=client)
        summary={'notice':'FICTIONAL OFFLINE DEMO','jobs':len(discovery.search(root)),
            'internships':len(discovery.search(root,kind='internship')),'fulltime':len(discovery.search(root,kind='fulltime')),
            'status':result['status'],'resumed_total_pages':result['sources'][0]['pages'],'network_requests':0}
        if summary['jobs']!=3 or summary['status']!='complete':raise RuntimeError('Demo failed')
        scout.write_json(root/'demo-summary.json',summary)
        return summary

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',default='demo-tech-discovery')
    print(json.dumps(run(p.parse_args().output),ensure_ascii=False,indent=2))
