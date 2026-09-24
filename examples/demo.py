#!/usr/bin/env python3
"""Reproduce a fictional evidence-to-Excel pipeline without network or Codex."""
from __future__ import annotations

import argparse
import copy
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'scripts'))
sys.path.insert(0, str(BASE / 'tests'))
import scout
from fixtures import FictionalJobFixture


def run(output):
    root = Path(output).resolve()
    if root.exists():
        raise ValueError('Output already exists; choose a new directory to preserve prior data.')
    scout.init_workspace(root)
    profile = scout.read_json(root / 'profile.json')
    profile['display_name'] = '虚构演示用户（非真实个人）'
    for axis, value in [('degree_program', 'master'), ('major', '计算机科学与技术'),
                        ('graduation_year', 2027), ('student_status', 'enrolled')]:
        profile['facts'][axis] = {'value': value, 'status': 'confirmed',
                                 'source': 'FICTIONAL DEMO ONLY', 'confirmed_at': '2026-09-16'}
    scout.write_json(root / 'profile.json', profile)
    rid = scout.begin(root)['run_id']
    fixture = FictionalJobFixture()
    fixture.root, fixture.rid = root, rid
    fixture.company = '虚构演示公司（不代表真实招聘）'
    fixture.prefix = 'https://jobs.example.test/acme'
    fixture.title = '机器学习算法工程师（虚构演示）'
    job = fixture.make_job()
    first = scout.ingest(root, rid, [job])
    repeated = scout.ingest(root, rid, [job])
    forged = copy.deepcopy(job)
    forged['identity_evidence']['quote'] = 'THIS QUOTE DOES NOT EXIST IN THE EVIDENCE'
    rejected = scout.ingest(root, rid, [forged])
    scout.add_lead(root, rid, {
        'url': 'https://jobs.example.test/unverified', 'title_hint': '虚构待核实入口',
        'company_hint': '未知', 'industry': '人工智能', 'found_at': scout.timestamp(),
        'tool_ref': 'FICTIONAL DEMO ONLY', 'reason': '演示：尚无岗位正文证据',
    })
    stats = scout.finalize(root, rid)
    if not (first['accepted'] == 1 and repeated['accepted'] == 0
            and rejected['quarantined'] == 1 and len(scout.materialize(root)) == 1
            and stats['eligible'] == 1 and stats['new'] == 1
            and stats['exports']['sheets'] == 13 and stats['run_status'] == 'partial'):
        raise RuntimeError('Demo invariants failed; inspect the generated report.')
    summary = {
        'notice': 'FICTIONAL OFFLINE DEMO — not real job search results',
        'unique_jobs': 1, 'eligible_fixture_jobs': stats['eligible'],
        'duplicate_ingest_new_jobs': repeated['new'], 'quarantined_observations': stats['quarantined'],
        'unverified_leads': stats['unverified_leads'], 'workbook_sheets': stats['exports']['sheets'],
        'run_status': stats['run_status'], 'completed_search_tasks': stats['completed_tasks'],
        'planned_search_tasks': stats['plan_tasks'],
    }
    scout.write_json(root / 'demo-summary.json', summary)
    scout.atomic_bytes(root / 'README.txt',
        'FICTIONAL OFFLINE DEMO. All jobs, employers, profiles and evidence are synthetic.\n'
        'No network requests or applications were made.\n'.encode('utf-8'))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='demo-output')
    args = parser.parse_args()
    print(scout.dumps(run(args.output)))
