"""Public recruitment adapters with normalized records and response evidence."""
from dataclasses import dataclass
from pathlib import Path
from html import unescape
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlencode, urljoin


class BodyText(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.hidden+=1
        if not self.hidden and tag in {'p','div','li','br','section','h1','h2','h3','tr'}:self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in {'script','style'} and self.hidden:self.hidden-=1
        if not self.hidden and tag in {'p','div','li','section','tr'}:self.parts.append('\n')
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)


def plain(value):
    parser=BodyText();parser.feed(str(value or ''));parser.close()
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def employment(title, raw=''):
    text = title + ' ' + str(raw)
    if re.search(r'实习|intern', text, re.I): return 'internship'
    if re.search(r'全职|社招|社会招聘|校招|校园|应届|graduate|full.?time|experienced', text, re.I): return 'fulltime'
    return 'unknown'


def normalize(company, source, ident, title, url, **fields):
    if not ident or not str(title).strip(): raise ValueError('Missing job identity/title')
    row = dict(company_id=company['id'], company=company['name'], source_id=source['id'],
               source_job_id=str(ident), title=plain(title), url=url,
               sector=company['sector'], locations=[], employment_type='unknown',
               recruitment_type='', description='', requirements='', degree='',
               experience='', updated_at='', source_status='listed', raw={})
    row.update(fields)
    return row


@dataclass
class Page:
    jobs: list
    next_cursor: object = None
    total: int | None = None
    evidence: dict | None = None
    outcome: str = 'ok'
    links: list | None = None


class PostingClosed(ValueError):
    def __init__(self,receipt,code='E1005'):
        super().__init__('Tencent official status '+code+': posting closed')
        self.receipt=receipt;self.code=code


def tencent_closed_payload(payload):
    return isinstance(payload,dict) and payload.get('Code')==500 and payload.get('Data')=='E1005'


def check_tencent_error(client,error):
    from public_http import FetchError
    import scout
    receipt=getattr(error,'receipt',None)
    if isinstance(error,FetchError) and receipt and receipt.get('http_status')==500:
        path=scout.within(client.root,receipt['path'])
        if scout.digest(path.read_bytes())==receipt['sha256']:
            try:payload=scout.read_json(path)
            except ValueError:payload=None
            if tencent_closed_payload(payload):raise PostingClosed(receipt) from error
    raise error


class Tencent:
    def page(self, client, company, source, cursor, size):
        number = int(cursor or 1)
        url = source['api_url'] + '?' + urlencode(dict(pageIndex=number, pageSize=size, language='zh-cn', area='cn'))
        data, receipt = client.json(url)
        if data.get('Code') != 200 or not isinstance(data.get('Data', {}).get('Posts'), list):
            raise ValueError('Tencent list schema changed')
        payload = data['Data']; jobs = []
        for item in payload['Posts']:
            ident = item['PostId']
            jobs.append(normalize(company, source, ident, item['RecruitPostName'],
                'https://careers.tencent.com/jobdesc.html?' + urlencode({'postId': ident}),
                locations=[item.get('LocationName', '')], country=item.get('CountryName', ''),
                employment_type=employment(item['RecruitPostName'], '社会招聘'),
                recruitment_type='社会招聘', description=plain(item.get('Responsibility')),
                requirements=plain(item.get('Requirement')), role_raw=item.get('CategoryName', ''),
                updated_at=item.get('LastUpdateTime', ''), raw=item))
        total = int(payload['Count'])
        return Page(jobs, number + 1 if number * size < total else None, total, receipt)

    def detail(self, client, job):
        url = 'https://careers.tencent.com/tencentcareer/api/post/ByPostId?' + urlencode(
            {'postId': job['source_job_id'], 'language': 'zh-cn'})
        try:data, receipt = client.json(url)
        except Exception as error:check_tencent_error(client,error)
        if tencent_closed_payload(data):raise PostingClosed(receipt)
        if data.get('Code') != 200 or not isinstance(data.get('Data'),dict): raise ValueError('Tencent detail unavailable')
        item = data['Data']
        if str(item.get('PostId')) != job['source_job_id']: raise ValueError('Detail identity mismatch')
        job.update(description=plain(item.get('Responsibility')), requirements=plain(item.get('Requirement')), raw=item)
        return job, receipt


class Netease:
    def page(self, client, company, source, cursor, size):
        number = int(cursor or 1)
        data, receipt = client.json(source['api_url'], {'currentPage': number, 'pageSize': size})
        if data.get('code') != 200 or not isinstance(data.get('data', {}).get('list'), list):
            raise ValueError('NetEase list schema changed')
        payload = data['data']; jobs = []
        for item in payload['list']:
            label = item.get('workTypeName') or {'0':'全职','1':'实习','2':'劳务派遣'}.get(str(item.get('workType')), '')
            kind = employment(item['name'], label)
            if kind == 'unknown' and str(item.get('workType')) == '0': kind = 'fulltime'
            jobs.append(normalize(company, source, item['id'], item['name'],
                'https://hr.163.com/api/hr163/position/query?' + urlencode({'id': item['id']}),
                locations=item.get('workPlaceNameList', []), employment_type=kind,
                recruitment_type=label, portal_url=source['url'],
                description=plain(item.get('description')), requirements=plain(item.get('requirement')),
                degree=item.get('reqEducationName', ''), experience=item.get('reqWorkYearsName', ''),
                role_raw=item.get('firstPostTypeName', ''), updated_at=str(item.get('updateTime', '')), raw=item))
        return Page(jobs, number + 1 if number < int(payload['pages']) else None, int(payload['total']), receipt)


    def detail(self,client,job):
        url='https://hr.163.com/api/hr163/position/query?'+urlencode({'id':job['source_job_id']})
        data,receipt=client.json(url);item=data.get('data') or {}
        if data.get('code')!=200 or str(item.get('id'))!=job['source_job_id']:raise ValueError('NetEase detail unavailable')
        job.update(description=plain(item.get('description')),requirements=plain(item.get('requirement')),raw=item)
        return job,receipt


class Feishu:
    def page(self, client, company, source, cursor, size):
        offset = int(cursor or 0)
        body = dict(keyword='', limit=size, offset=offset, portal_type=source.get('portal_type', 1),
                    job_category_id_list=[], location_code_list=[], subject_id_list=[], recruitment_id_list=[])
        data, receipt = client.json(source['api_url'], body)
        payload = data.get('data') or {}
        if not isinstance(payload.get('job_post_list'), list) or 'count' not in payload:
            raise ValueError('ATS response changed; browser handoff needed')
        jobs = []
        for item in payload['job_post_list']:
            recruit = (item.get('recruit_type') or {}).get('name', '')
            jobs.append(normalize(company, source, item['id'], item['title'],
                source['url'].rstrip('/') + '/detail/' + str(item['id']),
                locations=[x.get('name') or x.get('cn_name', '') for x in item.get('city_info_list', [])],
                employment_type=employment(item['title'], recruit), recruitment_type=recruit,
                description=plain(item.get('description')), requirements=plain(item.get('requirement')), raw=item))
        total = int(payload['count'])
        return Page(jobs, offset + len(jobs) if offset + len(jobs) < total else None, total, receipt)


class RecruitmentHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.scripts = []; self.links = []; self.active = False
        self.buffer = []; self.anchor = None; self.anchor_text = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'script' and a.get('type', '').lower() == 'application/ld+json':
            self.active = True; self.buffer = []
        if tag == 'a' and a.get('href'): self.anchor = a; self.anchor_text = []
    def handle_data(self, data):
        if self.active: self.buffer.append(data)
        if self.anchor: self.anchor_text.append(data)
    def handle_endtag(self, tag):
        if tag == 'script' and self.active:
            try: self.scripts.append(json.loads(''.join(self.buffer)))
            except ValueError: pass
            self.active = False
        if tag == 'a' and self.anchor:
            self.links.append({**self.anchor, 'text': ''.join(self.anchor_text).strip()}); self.anchor = None


def postings(value):
    if isinstance(value, list):
        for entry in value: yield from postings(entry)
    elif isinstance(value, dict):
        kinds = value.get('@type', [])
        if 'JobPosting' in ([kinds] if isinstance(kinds, str) else kinds): yield value
        for key in ('@graph', 'mainEntity', 'itemListElement', 'item'):
            if key in value: yield from postings(value[key])


class StructuredHTML:
    def page(self, client, company, source, cursor, size):
        url = cursor or source['url']; html, receipt = client.text(url)
        parser = RecruitmentHTML(); parser.feed(html); jobs = []
        for item in postings(parser.scripts):
            target = urljoin(url, item.get('url', url)); ident = item.get('identifier') or target
            if isinstance(ident, dict): ident = ident.get('value', target)
            places = item.get('jobLocation') or []
            if isinstance(places, dict): places = [places]
            locations = [plain((p.get('address') or {}).get('addressLocality', '')) for p in places]
            jobs.append(normalize(company, source, ident, item.get('title', ''), target,
                locations=locations, employment_type=employment(item.get('title', ''), str(item.get('employmentType', ''))),
                recruitment_type=str(item.get('employmentType', '')), description=plain(item.get('description')),
                requirements=plain(item.get('qualifications')), updated_at=item.get('datePosted', ''),
                valid_through=item.get('validThrough', ''), raw=item))
        next_url = None; links = []
        for link in parser.links:
            href = urljoin(url, link['href'])
            if 'next' in link.get('rel', '').split() or link['text'] in {'下一页', 'Next', 'Next page'}: next_url = href
            if re.search(r'招聘|职位|实习|校招|job|career|position', link['text'] + href, re.I):
                links.append({'url': href, 'title': link['text']})
        return Page(jobs, next_url, None, receipt, 'ok' if jobs else 'browser_required', links[:100])


ADAPTERS = {'tencent': Tencent, 'netease': Netease, 'feishu': Feishu, 'html': StructuredHTML}

class Mihoyo:
    def page(self, client, company, source, cursor, size):
        number=int(cursor or 1); hire=source['hire_type']
        body={'pageNo':number,'pageSize':size,'hireType':hire,'channelDetailIds':[1]}
        data,receipt=client.json(source['api_url'],body)
        if data.get('code')!=0 or not isinstance(data.get('data',{}).get('list'),list):
            raise ValueError('miHoYo list schema changed')
        payload=data['data'];jobs=[]
        for item in payload['list']:
            nature=item.get('jobNature','')
            jobs.append(normalize(company,source,item['id'],item['title'],
                'https://jobs.mihoyo.com/'+('campus/' if hire else '')+'position/'+str(item['id']),
                locations=[a['addressDetail'] for a in item.get('addressDetailList',[])],
                employment_type=employment(item['title'],nature),recruitment_type=item.get('projectName',''),
                contract_type=nature,role_raw=item.get('competencyType',''),hire_type=hire,
                description=plain(item.get('jobSummary')),raw=item))
        total=int(payload['total'])
        if int(payload['pageNo'])!=number:raise ValueError('Server returned a different page')
        return Page(jobs,number+1 if number*size<total else None,total,receipt)

    def detail(self,client,job):
        body={'id':job['source_job_id'],'hireType':job['hire_type'],'channelDetailIds':[1]}
        data,receipt=client.json('https://ats.openout.mihoyo.com/ats-portal/v1/job/info',body)
        item=data.get('data') or {}
        if data.get('code')!=0 or str(item.get('id'))!=job['source_job_id']:raise ValueError('miHoYo detail unavailable')
        job.update(description=plain(item.get('description')),requirements=plain(item.get('jobRequire')),raw=item)
        return job,receipt

ADAPTERS['mihoyo']=Mihoyo
