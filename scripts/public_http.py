"""Public HTTP reads with evidence files, request budgets and per-host pacing."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.robotparser import RobotFileParser
import scout


class FetchError(RuntimeError): pass
class BudgetExceeded(FetchError): pass


class PublicRedirect(HTTPRedirectHandler):
    def __init__(self, before_redirect=None):
        super().__init__();self.before_redirect=before_redirect
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scout.safe_url(newurl)
        redirected=super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and self.before_redirect:self.before_redirect(newurl)
        return redirected


class PublicHTTP:
    def __init__(self, root, run_id, max_requests=200, interval=1., timeout=20, retries=2, max_detail_attempts=None, on_attempt=None, headers=None):
        self.root = Path(root); self.folder = self.root / 'evidence' / run_id
        self.folder.mkdir(parents=True, exist_ok=True)
        self.max_requests = max_requests; self.interval = interval; self.timeout = timeout
        self.retries = retries; self.requests = 0; self.retry_count = 0
        self.latencies = []; self.last = {}; self.robots = {}
        self.max_detail_attempts=max_detail_attempts;self.detail_attempts=0
        self.extra_headers=dict(headers or {})
        self.on_attempt=on_attempt
        self.request_kind='list';self._active_kind='list';self.attempts_by_kind={};self.redirects=0;self.response_bytes=0
        self.opener = build_opener(PublicRedirect(self._redirect_attempt))
    def _before_attempt(self,url,kind,redirect=False):
        scout.safe_url(url)
        if self.requests>=self.max_requests:raise BudgetExceeded('HTTP attempt budget reached')
        if kind=='detail' and self.max_detail_attempts is not None and self.detail_attempts>=self.max_detail_attempts:
            raise BudgetExceeded('Detail HTTP attempt budget reached')
        host=urlsplit(url).netloc
        wait=self.interval-(time.monotonic()-self.last.get(host,0))
        if wait>0:time.sleep(wait)
        if self.on_attempt:self.on_attempt(url,kind)
        self.requests+=1;self.attempts_by_kind[kind]=self.attempts_by_kind.get(kind,0)+1
        if kind=='detail':self.detail_attempts+=1
        if redirect:self.redirects+=1
        self.last[host]=time.monotonic()
    def _redirect_attempt(self,url):
        self._before_attempt(url,self._active_kind,redirect=True)
    def _read(self, url, body=None, kind=None):
        scout.safe_url(url); host = urlsplit(url).netloc
        kind=kind or self.request_kind
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        headers = {'User-Agent': 'TechJobScout/2.0 (public recruitment research)',
                   'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
                   'Referer': urlsplit(url).scheme + '://' + host + '/'}
        if body is not None: headers['Content-Type'] = 'application/json'
        headers.update(self.extra_headers)
        for attempt in range(self.retries + 1):
            self._before_attempt(url,kind)
            previous_kind=self._active_kind;self._active_kind=kind;start=time.monotonic()
            try:
                with self.opener.open(Request(url, data=payload, headers=headers), timeout=self.timeout) as response:
                    raw = response.read(16 * 1024 * 1024 + 1)
                    if len(raw) > 16 * 1024 * 1024: raise FetchError('Response exceeds 16 MiB')
                    self.response_bytes+=len(raw)
                    return raw, response.headers.get_content_charset() or 'utf-8', response.url
            except HTTPError as error:
                capture_error=None
                try:error_raw=error.read(256*1024)
                except (OSError,ValueError) as read_error:error_raw=b'';capture_error=str(read_error)
                finally:error.close()
                error_key=hashlib.sha256((url+json.dumps(body,sort_keys=True)).encode()).hexdigest()[:12]
                error_sha=hashlib.sha256(error_raw).hexdigest()
                error_file=self.folder/(error_key+'-'+error_sha[:12]+'-error-'+str(self.requests)+'.txt')
                error_file.write_bytes(error_raw)
                error_receipt={'url':url,'final_url':error.geturl(),'http_status':error.code,'request_body':body,'captured_at':scout.timestamp(),
                               'sha256':error_sha,'path':error_file.relative_to(self.root).as_posix(),'body_capture_error':capture_error,'body_limit_bytes':256*1024}
                scout.write_json(error_file.with_suffix('.meta.json'),error_receipt)
                if error.code not in {429, 500, 502, 503, 504} or attempt == self.retries:
                    failure=FetchError(f'HTTP {error.code}: {url}');failure.receipt=error_receipt
                    raise failure from error
                retry = error.headers.get('Retry-After', '')
                try: delay = float(retry)
                except ValueError:
                    try: delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError): delay = 2 ** attempt
                if delay > 30: raise FetchError(f'Host requested retry after {delay:.0f}s') from error
                self.retry_count += 1; time.sleep(max(0, delay))
            except (URLError, TimeoutError) as error:
                if attempt == self.retries: raise FetchError(f'Connection failed: {url}') from error
                self.retry_count += 1; time.sleep(2 ** attempt)
            finally:
                self._active_kind=previous_kind
                self.last[host] = time.monotonic(); self.latencies.append(time.monotonic() - start)
    def _allowed(self, url):
        parts = urlsplit(url); origin = parts.scheme + '://' + parts.netloc
        if origin not in self.robots:
            parser = RobotFileParser()
            try:
                raw, charset, _ = self._read(origin + '/robots.txt',kind='robots')
                parser.parse(raw.decode(charset, 'replace').splitlines())
            except BudgetExceeded: raise
            except FetchError as error:
                if 'HTTP 404:' in str(error): parser.parse([])
                else: raise FetchError('robots.txt unavailable: ' + str(error)) from error
            self.robots[origin] = parser
        if not self.robots[origin].can_fetch('TechJobScout', url):
            raise FetchError('robots.txt disallows this path; browser handoff needed')
    def text(self, url, body=None):
        self._allowed(url); raw, charset, final = self._read(url, body)
        sha = hashlib.sha256(raw).hexdigest()
        key = hashlib.sha256((url + json.dumps(body, sort_keys=True)).encode()).hexdigest()[:12]
        file = self.folder / (key + '-' + sha[:12] + '.txt'); file.write_bytes(raw)
        receipt = {'url': url, 'final_url': final, 'request_body': body,
                   'captured_at': scout.timestamp(), 'sha256': sha, 'path': file.relative_to(self.root).as_posix()}
        scout.write_json(file.with_suffix('.meta.json'), receipt)
        return raw.decode(charset, 'replace'), receipt
    def json(self, url, body=None):
        text, receipt = self.text(url, body)
        try: return json.loads(text), receipt
        except ValueError as error: raise FetchError('Expected JSON; use browser handoff') from error
    def metrics(self):
        values = sorted(self.latencies)
        def percentile(p): return round(values[min(len(values)-1, int((len(values)-1)*p))], 3) if values else None
        return {'requests': self.requests, 'retries': self.retry_count, 'detail_attempts':self.detail_attempts,
                'attempts_by_kind':self.attempts_by_kind,'redirects':self.redirects,'successful_response_bytes':self.response_bytes,
                'latency_unit':'request_opener_chain_including_redirects',
                'latency_p50_seconds': percentile(.5), 'latency_p95_seconds': percentile(.95)}
