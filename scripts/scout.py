#!/usr/bin/env python3
"""Evidence and personal-eligibility ledger. Public collection lives in discovery.py."""
from __future__ import annotations
import argparse
import csv
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

VERSION = "2.0.0"
SCHEMA_VERSION = 1
UTC = timezone.utc
BASE = Path(__file__).resolve().parents[1]
CORE_AXES = {"degree_program", "major", "graduation_year"}
TRUSTED_KINDS = {"official_company", "official_ats", "official_notice", "government_notice"}
AXES = CORE_AXES | {"student_status", "experience_years", "internship_days", "internship_months", "start_date", "language", "work_authorization", "other"}
JOB_TYPES = {"internship", "campus", "entry", "experienced", "fulltime", "talent_pool", "unknown"}
STATES = {"open", "closed", "expired", "not_open", "talent_pool", "unknown"}
TRACKS = {"software", "ai_data", "game", "product_design", "transferable", "low"}
CLOSED = re.compile(r"已截止|已结束|停止招聘|职位已关闭|岗位已关闭|停止投递|已下线|不再接受|招聘结束|no longer accepting|position.{0,20}closed|applications? closed|job.{0,20}closed", re.I)
OPEN = re.compile(r"立即申请|立即投递|申请职位|在线申请|在线投递|投递简历|招聘中|报名入口|apply now|apply for (this|the) (job|position)|submit application", re.I)
LIMITS = {"file_bytes": 16 * 1024 * 1024, "batch": 200, "text": 4 * 1024 * 1024}

class ValidationError(ValueError):
    pass

class ClosingConnection(sqlite3.Connection):
    """sqlite3's default context manager commits but does not close the handle."""
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()

def now_utc():
    return datetime.now(UTC)

def timestamp(value=None):
    return (value or now_utc()).astimezone(UTC).isoformat(timespec="seconds")

def dt(value):
    if not isinstance(value, str):
        raise ValidationError("Timestamp must be an ISO string with a timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as ex:
        raise ValidationError("Invalid ISO timestamp") from ex
    if result.tzinfo is None:
        raise ValidationError("Naive timestamp rejected; include timezone")
    return result.astimezone(UTC)

def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest(data):
    if not isinstance(data, bytes):
        data = dumps(data).encode("utf-8")
    return hashlib.sha256(data).hexdigest()

def read_json(path):
    p = Path(path)
    if p.stat().st_size > LIMITS["file_bytes"]:
        raise ValidationError("JSON file too large; split the batch")
    return json.loads(p.read_text(encoding="utf-8-sig"))

def atomic_bytes(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, p)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def write_json(path, obj):
    atomic_bytes(path, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

def within(root, relative):
    r = Path(root).resolve()
    p = (r / relative).resolve()
    if not p.is_relative_to(r):
        raise ValidationError("Path escapes workspace")
    return p

def safe_url(value):
    """Syntax/private-literal guard. Browser/network sandbox remains the SSRF boundary."""
    if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\x00-\x20\\]", value):
        raise ValidationError("Malformed URL")
    p = urlsplit(value)
    if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password:
        raise ValidationError("Only public http(s) URLs without embedded credentials")
    host = p.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")) or "." not in host:
        raise ValidationError("Local/internal URL rejected")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise ValidationError("Non-public IP rejected")
    try:
        port = p.port
    except ValueError as ex:
        raise ValidationError("Invalid URL port") from ex
    if port not in {None, 80, 443}:
        raise ValidationError("Nonstandard URL port rejected")
    return value

def canonical_url(value):
    safe_url(value)
    p = urlsplit(value)
    # Keep meaningful IDs, tenants and hash-router fragments. Never strip all query parameters.
    args = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid", "msclkid"}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(args), p.fragment))

def under_prefix(url, prefix):
    a, b = urlsplit(safe_url(url)), urlsplit(safe_url(prefix))
    if (a.scheme, a.netloc.lower()) != (b.scheme, b.netloc.lower()):
        return False
    bp = b.path or "/"
    if a.path != bp and not a.path.startswith(bp.rstrip("/") + "/"):
        return False
    needed = set(parse_qsl(b.query, keep_blank_values=True))
    if not needed.issubset(set(parse_qsl(a.query, keep_blank_values=True))):
        return False
    return not b.fragment or a.fragment == b.fragment or a.fragment.startswith(b.fragment.rstrip("/") + "/")

def job_key(job):
    employer = job.get("employer_key", "").strip()
    campaign = job.get("campaign", "").strip()
    if not employer or not campaign:
        raise ValidationError("employer_key and campaign required; use an evidenced campaign or explicit unknown-cycle namespace")
    jid = str(job.get("job_id") or "").strip()
    identity = [employer, campaign, "id:" + jid if jid else "url:" + canonical_url(job["url"])]
    return "job_" + digest(identity)[:24]

def connect(root):
    p = Path(root) / "state" / "jobs.sqlite3"
    if not p.exists():
        raise ValidationError("Workspace not initialized")
    conn = sqlite3.connect(p, timeout=15, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.close()
        raise ValidationError("Schema version mismatch; no automatic migration")
    return conn

def init_workspace(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ("state", "evidence", "inbox", "reports", "exports", "backups", "memory", "quarantine"):
        (root / name).mkdir(exist_ok=True)
    for name in ("profile.json", "policy.json", "company_seeds.json"):
        target = root / name
        if not target.exists():
            shutil.copyfile(BASE / "templates" / name, target)
    db_path = root / "state/jobs.sqlite3"
    if db_path.exists():
        with connect(root) as c:
            return {"workspace": str(root), "status": "already_initialized", "jobs": c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]}
    c = sqlite3.connect(db_path)
    c.executescript("""
      PRAGMA foreign_keys=ON;
      PRAGMA journal_mode=WAL;
      CREATE TABLE runs(id TEXT PRIMARY KEY,started TEXT NOT NULL,ended TEXT,status TEXT NOT NULL,
        profile_hash TEXT NOT NULL,policy_hash TEXT NOT NULL,stats TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE evidence(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,url TEXT NOT NULL,captured TEXT NOT NULL,
        path TEXT NOT NULL,sha256 TEXT NOT NULL,method TEXT NOT NULL,tool_ref TEXT NOT NULL,
        completeness TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES runs(id));
      CREATE TABLE sources(id TEXT PRIMARY KEY,payload TEXT NOT NULL,updated TEXT NOT NULL);
      CREATE TABLE leads(id TEXT PRIMARY KEY,url TEXT NOT NULL,payload TEXT NOT NULL,first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,run_id TEXT NOT NULL,resolved_key TEXT,FOREIGN KEY(run_id) REFERENCES runs(id));
      CREATE TABLE jobs(key TEXT PRIMARY KEY,payload TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
        last_verified TEXT,last_attempt TEXT NOT NULL,probe_status TEXT NOT NULL DEFAULT 'ok',
        content_hash TEXT NOT NULL,run_id TEXT NOT NULL,FOREIGN KEY(run_id) REFERENCES runs(id));
      CREATE TABLE observations(id TEXT PRIMARY KEY,job_key TEXT NOT NULL,run_id TEXT NOT NULL,
        observed TEXT NOT NULL,payload TEXT NOT NULL,content_hash TEXT NOT NULL,
        FOREIGN KEY(job_key) REFERENCES jobs(key),FOREIGN KEY(run_id) REFERENCES runs(id));
      CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,job_key TEXT,
        at TEXT NOT NULL,kind TEXT NOT NULL,detail TEXT NOT NULL,FOREIGN KEY(run_id) REFERENCES runs(id));
      CREATE TABLE coverage(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,payload TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES runs(id));
      CREATE TABLE annotations(job_key TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT '',priority TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',updated TEXT NOT NULL,FOREIGN KEY(job_key) REFERENCES jobs(key));
      PRAGMA user_version=1;
    """)
    c.commit()
    c.close()
    write_json(root / "memory/checkpoint.json", {"schema_version": 1, "phase": "not_started", "run_id": None,
        "pending": [], "last_successful_run": None, "notice": "Derived restart index; not evidence or authorization."})
    return {"workspace": str(root), "status": "initialized", "jobs": 0}

def configs(root):
    profile = read_json(Path(root) / "profile.json")
    policy = read_json(Path(root) / "policy.json")
    if profile.get("schema_version") != 1 or policy.get("schema_version") != 1:
        raise ValidationError("Unsupported config schema")
    return profile, policy

def begin(root):
    profile, policy = configs(root)
    rid = now_utc().strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    with connect(root) as c:
        c.execute("INSERT INTO runs(id,started,status,profile_hash,policy_hash) VALUES(?,?,?,?,?)",
                  (rid, timestamp(), "running", digest(profile), digest(policy)))
    (Path(root) / "inbox" / rid).mkdir(parents=True, exist_ok=True)
    write_json(Path(root) / "memory/checkpoint.json", {"schema_version":1,"run_id":rid,"phase":"started","pending":[],
        "profile_hash":digest(profile),"policy_hash":digest(policy)})
    return {"run_id": rid, "profile_hash": digest(profile), "policy_hash": digest(policy)}

def assert_run(c, root, rid):
    run = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    if not run or run["status"] != "running":
        raise ValidationError("Run does not exist or is already finalized")
    profile, policy = configs(root)
    if (run["profile_hash"], run["policy_hash"]) != (digest(profile), digest(policy)):
        raise ValidationError("Profile/policy changed during run; stop and review instead of adapting silently")
    return run

def register_evidence(root, rid, file, url, captured, method, tool_ref, completeness):
    safe_url(url)
    when = dt(captured)
    if when > now_utc() + timedelta(minutes=5):
        raise ValidationError("Future evidence timestamp rejected")
    if method not in {"browser_export", "http_capture", "tool_export", "pdf_text"}:
        raise ValidationError("Unsupported capture method; model summaries/search snippets are not captures")
    if completeness not in {"full_detail", "partial", "snippet", "error"} or not tool_ref.strip():
        raise ValidationError("Completeness and actual tool reference required")
    p = Path(file).resolve()
    if not p.is_file() or p.stat().st_size > LIMITS["file_bytes"]:
        raise ValidationError("Capture missing or too large")
    data = p.read_bytes()
    # Text exports only. Original screenshots/PDFs may be retained next to them as supplementary files.
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as ex:
        raise ValidationError("Evidence text must be UTF-8, not synthesized from a binary file") from ex
    if not text.strip() or len(text) > LIMITS["text"]:
        raise ValidationError("Evidence text missing or too large")
    h = digest(data)
    eid = "ev_" + digest([rid, canonical_url(url), captured, h])[:24]
    rel = "evidence/" + rid + "/" + eid + ".txt"
    with connect(root) as c:
        assert_run(c, root, rid)
        destination = within(root, rel)
        if destination.exists() and digest(destination.read_bytes()) != h:
            raise ValidationError("Existing evidence cannot be overwritten")
        if not destination.exists():
            atomic_bytes(destination, data)
        c.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?,?,?)",
                  (eid, rid, url, timestamp(when), rel, h, method, tool_ref, completeness))
    return {"evidence_id": eid, "sha256": h, "path": rel}

def evidence_record(c, root, eid):
    row = c.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone()
    if not row:
        raise ValidationError("Evidence ID not found: " + str(eid))
    p = within(root, row["path"])
    raw = p.read_bytes()
    if digest(raw) != row["sha256"]:
        raise ValidationError("Evidence hash mismatch: " + eid)
    return dict(row), raw.decode("utf-8-sig")

def quote_check(c, root, reference, *, full=False):
    if not isinstance(reference, dict) or set(reference) != {"evidence_id", "quote"}:
        raise ValidationError("Evidence reference must contain exactly evidence_id and quote")
    quote = reference["quote"]
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 4000:
        raise ValidationError("Empty or overlong evidence quote")
    row, text = evidence_record(c, root, reference["evidence_id"])
    if quote not in text:
        raise ValidationError("Quote is not present verbatim in saved evidence")
    if full and row["completeness"] != "full_detail":
        raise ValidationError("Full detail required; snippets/partial/error pages cannot verify a job")
    return row

def register_source(root, rid, source):
    required = {"source_id", "employer_key", "company", "kind", "prefix", "identity_evidence", "authorization_evidence", "reviewed_at", "reviewer", "ownership", "ownership_evidence"}
    if set(source) != required:
        raise ValidationError("Source schema mismatch: " + str(sorted(required ^ set(source))))
    safe_url(source["prefix"])
    if source["kind"] not in TRUSTED_KINDS | {"university_repost", "third_party"}:
        raise ValidationError("Unknown source kind")
    if not all(isinstance(source[k], str) and source[k].strip() for k in ("source_id", "employer_key", "company", "reviewer")):
        raise ValidationError("Source identity fields required")
    if dt(source["reviewed_at"]) > now_utc() + timedelta(minutes=5):
        raise ValidationError("Future source review")
    with connect(root) as c:
        assert_run(c, root, rid)
        if source["kind"] in TRUSTED_KINDS:
            identity_row = quote_check(c, root, source["identity_evidence"], full=True)
            if source["company"] not in source["identity_evidence"]["quote"]:
                raise ValidationError("Company identity is absent from the identity evidence")
            auth = source["authorization_evidence"]
            auth_row = quote_check(c, root, auth, full=True)
            if dt(source["reviewed_at"]) > min(dt(identity_row["captured"]), dt(auth_row["captured"])) + timedelta(minutes=5):
                raise ValidationError("Cannot refresh source trust with old source evidence")
            # Official identity is an auditable review decision, not proof supplied by this substring test.
            if source["prefix"] not in auth["quote"]:
                raise ValidationError("Source authorization quote must preserve the exact linked origin/tenant prefix")
            if source["kind"] == "official_ats":
                p = urlsplit(source["prefix"])
                if p.path in {"", "/"} and not p.query and not p.fragment:
                    raise ValidationError("Shared ATS root is not an employer-specific allowlist")
        if source["ownership"] != "unknown":
            quote_check(c, root, source["ownership_evidence"], full=True)
        previous = c.execute("SELECT payload FROM sources WHERE id=?", (source["source_id"],)).fetchone()
        if previous and json.loads(previous[0])["employer_key"] != source["employer_key"]:
            raise ValidationError("Source ID cannot change employers")
        c.execute("INSERT INTO sources VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated",
                  (source["source_id"], dumps(source), timestamp()))
        c.execute("INSERT INTO events(run_id,at,kind,detail) VALUES(?,?,?,?)", (rid,timestamp(),"source_review",dumps(source)))
    return {"source_id":source["source_id"], "kind":source["kind"], "warning":"Source authenticity still depends on checking the linked official identity."}

def check_requirement(check, profile):
    axis = check["axis"]
    rule = check["rule"]
    fact = profile.get("facts", {}).get(axis, {})
    candidate = fact.get("value") if fact.get("status") == "confirmed" else None
    target = check.get("expected")
    if rule == "unpublished" or rule == "manual":
        return "unknown", axis + ": " + ("未公开" if rule == "unpublished" else "需人工判断")
    if rule == "unrestricted":
        return "pass", axis + ": 官方明确不限"
    if candidate is None:
        return "unknown", axis + ": 用户条件待确认"
    if rule == "one_of":
        if not isinstance(target, list) or not target:
            raise ValidationError("one_of requires a nonempty expected list")
        result = candidate in target
    elif rule in {"min", "max"}:
        if isinstance(candidate, bool) or isinstance(target, bool) or not isinstance(candidate, (int,float)) or not isinstance(target, (int,float)):
            return "unknown", axis + ": 数值条件无法比较"
        result = candidate >= target if rule == "min" else candidate <= target
    elif rule == "on_or_before":
        try:
            result = date.fromisoformat(candidate) <= date.fromisoformat(target)
        except (TypeError,ValueError):
            return "unknown", axis + ": 日期条件无法比较"
    else:
        raise ValidationError("Unsupported qualification rule")
    return ("pass" if result else "fail"), axis + (": 符合公开条件" if result else ": 明确不符")

def evaluate(job, profile, policy, *, asof=None, trusted=False, source_fresh=False, evidence_fresh=False, probe="ok"):
    asof = asof or now_utc()
    reasons = []
    results = [check_requirement(x, profile) for x in job["checks"]]
    if any(x[0] == "fail" for x in results):
        eligibility = "fail"
    elif any(x[0] == "unknown" for x in results) or not job["requirements_review"]["all_public_hard_requirements_captured"]:
        eligibility = "unknown"
    else:
        eligibility = "pass"
    reasons.extend(x[1] for x in results if x[0] != "pass")
    status = job["status"]
    if CLOSED.search(job["status_evidence"]["quote"]):
        status = "closed"
    deadline = job.get("deadline_at")
    if deadline and dt(deadline) < asof:
        status = "expired"
    if job.get("opens_at") and dt(job["opens_at"]) > asof:
        status = "not_open"
    if job["job_type"] == "talent_pool":
        status = "talent_pool"
    if status == "open":
        if not trusted or not source_fresh or not evidence_fresh or probe != "ok":
            status = "unverified"
            reasons.append("来源/正文/时效/最近访问未全部通过")
        elif not OPEN.search(job["status_evidence"]["quote"]) or not job["application_url"]:
            status = "unverified"
            reasons.append("缺少职位层面的在招证据或投递入口")
        elif job.get("date_conflict"):
            status = "unverified"
            reasons.append("招聘日期冲突")
    if not trusted:
        status = "unverified"
        reasons.append("来源身份或证据完整性未通过")
    if status in {"closed", "expired", "talent_pool", "not_open"} or eligibility == "fail" or job["match_track"] == "low":
        bucket = "history"
    elif status == "open" and eligibility == "pass" and job["job_type"] in {"internship","campus","entry","experienced","fulltime"}:
        bucket = "eligible"
    elif status == "open":
        bucket = "qualification_pending"
    else:
        bucket = "verification_pending"
    # Score cannot override a failed qualification. No invented probability of admission.
    base = {"software":90,"ai_data":90,"game":90,"product_design":85,"transferable":70,"low":20}.get(job["match_track"],65)
    score = min(100, base + min(10, len(job.get("matched_skills",[])) * 2))
    return {"status":status,"eligibility":eligibility,"bucket":bucket,"match_score":score,"risks":"；".join(reasons)}

def validate_job(c, root, job, *, asof=None):
    required = {"employer_key","company","source_id","job_id","campaign","title","locations","job_type","industry",
        "match_track","url","application_url","application_evidence","observed_at","status","status_evidence","identity_evidence","facts",
        "checks","deadline_at","opens_at","date_conflict","requirements_review","match_reason","matched_skills"}
    if not required.issubset(job):
        raise ValidationError("Job schema mismatch: " + str(sorted(required ^ set(job))))
    if job["job_type"] not in JOB_TYPES or job["status"] not in STATES or job["match_track"] not in TRACKS:
        raise ValidationError("Unknown job type/status/match track")
    if not isinstance(job["locations"], list) or not all(isinstance(v,str) for v in job["locations"]):
        raise ValidationError("locations must be a list; don't duplicate a single posting by city")
    if not isinstance(job["matched_skills"],list) or not isinstance(job["date_conflict"],bool):
        raise ValidationError("Invalid normalized field type")
    safe_url(job["url"])
    if job["application_url"]:
        safe_url(job["application_url"])
    current = asof or now_utc()
    if dt(job["observed_at"]) > current + timedelta(minutes=5):
        raise ValidationError("Future job observation")
    for k in ("deadline_at", "opens_at"):
        if job[k]:
            dt(job[k])
    source_row = c.execute("SELECT payload FROM sources WHERE id=?",(job["source_id"],)).fetchone()
    if not source_row:
        raise ValidationError("Unknown source; register as a lead or verify origin first")
    source = json.loads(source_row[0])
    if source["kind"] in TRUSTED_KINDS:
        quote_check(c, root, source["identity_evidence"], full=True)
        quote_check(c, root, source["authorization_evidence"], full=True)
    if source["ownership"] != "unknown":
        quote_check(c, root, source["ownership_evidence"], full=True)
    if (source["employer_key"],source["company"]) != (job["employer_key"],job["company"]):
        raise ValidationError("Employer/source identity mismatch")
    if not under_prefix(job["url"],source["prefix"]):
        raise ValidationError("Job URL is outside the registered employer/tenant prefix")
    title_ref = job["identity_evidence"]
    idrow = quote_check(c,root,title_ref,full=True)
    if job["title"] not in title_ref["quote"] or not job["title"].strip():
        raise ValidationError("Exact job title must occur in identity evidence")
    if canonical_url(idrow["url"]) != canonical_url(job["url"]):
        raise ValidationError("Identity evidence must come from the job URL, not another posting")
    if job["job_id"] and str(job["job_id"]) not in title_ref["quote"] and str(job["job_id"]) not in job["url"]:
        raise ValidationError("Job ID is not visible in evidence or URL")
    statusrow = quote_check(c,root,job["status_evidence"],full=True)
    if canonical_url(statusrow["url"]) != canonical_url(job["url"]):
        raise ValidationError("Status evidence belongs to a different job")
    known_closure = (job["status"] == "closed" or bool(CLOSED.search(job["status_evidence"]["quote"]))) and c.execute("SELECT 1 FROM jobs WHERE key=?", (job_key(job),)).fetchone()
    decisive_capture = dt(statusrow["captured"]) if known_closure else min(dt(idrow["captured"]), dt(statusrow["captured"]))
    if dt(job["observed_at"]) > decisive_capture + timedelta(minutes=5):
        raise ValidationError("Cannot relabel old evidence as a new observation")
    facts = job["facts"]
    required_facts = {"location_raw","recruitment_type_raw","duties_raw","degree_raw","major_raw","cohort_raw",
        "skills_raw","tools_raw","deadline_raw","salary_raw","experience_raw","campaign_raw"}
    for name in required_facts:
        facts.setdefault(name, None)
    for field,value in facts.items():
        if value is None:
            continue
        if not isinstance(value,dict) or set(value) != {"value","evidence"}:
            raise ValidationError("Raw fact must be null or {value,evidence}")
        quote_check(c,root,value["evidence"],full=True)
        if not isinstance(value["value"],str) or value["value"] not in value["evidence"]["quote"]:
            raise ValidationError("Raw fact must be quoted literally; put summaries in match_reason")
    for date_field in ("deadline_at", "opens_at"):
        if not job[date_field]:
            continue
        if facts["deadline_raw"] is None:
            raise ValidationError("Normalized date without raw recruitment-date evidence")
        local_date = datetime.fromisoformat(job[date_field].replace("Z", "+00:00")).date()
        source_dates = {(int(a),int(b),int(d)) for a,b,d in re.findall(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?", facts["deadline_raw"]["value"])}
        if (local_date.year, local_date.month, local_date.day) not in source_dates:
            raise ValidationError("Normalized date is not supported by an explicit full source date")
    type_patterns = {"internship":r"实习|intern", "campus":r"校招|校园|应届|graduate", "entry":r"应届|无工作经验|0年|经验不限|不限经验|entry.level", "talent_pool":r"人才库|人才储备|储备人才|talent.pool"}
    if job["job_type"] in type_patterns:
        type_fact = facts["recruitment_type_raw"]
        if not type_fact or not re.search(type_patterns[job["job_type"]], type_fact["value"], re.I):
            raise ValidationError("Recruitment type is not supported by raw source wording")
    if not job["campaign"].startswith("unknown-cycle:"):
        fact = facts["campaign_raw"]
        if not fact or job["campaign"] not in fact["value"]:
            raise ValidationError("Campaign/year cannot be inferred from today's date")
    if facts["location_raw"] is None and job["locations"]:
        raise ValidationError("Locations without raw evidence")
    if facts["location_raw"] and any(x not in facts["location_raw"]["value"] for x in job["locations"]):
        raise ValidationError("Normalized location does not occur in source")
    checks = job["checks"]
    if not isinstance(checks,list) or len(checks)>40:
        raise ValidationError("Invalid qualification checks")
    axes=[]
    for check in checks:
        if set(check)!={"axis","rule","expected","evidence"} or check["axis"] not in AXES:
            raise ValidationError("Unknown check schema or axis")
        axes.append(check["axis"])
        if check["rule"] == "unpublished":
            if check["evidence"] is not None:
                raise ValidationError("Unpublished condition must not invent a quote")
        else:
            quote_check(c,root,check["evidence"],full=True)
        if check["rule"] == "one_of":
            expected = check["expected"]
            if not isinstance(expected, list) or not expected:
                raise ValidationError("one_of requires a nonempty expected list")
            qtext = check["evidence"]["quote"]
            if re.search(r"不接受|不包括|不含|除外|排除|不得|not eligible|excluding|except", qtext, re.I):
                raise ValidationError("Exclusions require manual/compound review, not positive exact matching")
            aliases = {"master":r"硕士|研究生|master", "bachelor":r"本科|学士|bachelor", "doctor":r"博士|doctor|PhD", "enrolled":r"在读|在校|enrolled"}
            for target in expected:
                pattern = aliases.get(str(target), re.escape(str(target)))
                if not re.search(pattern, qtext, re.I):
                    raise ValidationError("Accepted qualification value is not supported by source wording")
            if check["axis"] == "major" and re.search(r"相关专业|related (major|discipline|field)", qtext, re.I) and any(str(t) not in qtext for t in expected):
                raise ValidationError("Related majors cannot be expanded without evidence")
        if check["rule"] in {"min", "max"} and str(check["expected"]) not in check["evidence"]["quote"]:
            raise ValidationError("Numeric qualification threshold absent from source wording")
        if check["rule"]=="unrestricted" and not re.search(r"不限|不限制|无.{0,8}限制|any major|all majors|no.{0,15}restriction|all (years|students)",check["evidence"]["quote"],re.I):
            raise ValidationError("Unrestricted is not equivalent to unstated")
    required_axes = CORE_AXES if job["job_type"] in {"campus", "internship"} else {"degree_program", "major"}
    if not required_axes.issubset(set(axes)):
        raise ValidationError("Missing applicable qualification axes")
    if len(axes) != len(set(axes)):
        raise ValidationError("Combine compound requirements explicitly; duplicate axes require manual review")
    review = job["requirements_review"]
    if set(review)!={"all_public_hard_requirements_captured","reviewer","tool_ref"} or not isinstance(review["all_public_hard_requirements_captured"],bool) or not review["tool_ref"] or not review["reviewer"]:
        raise ValidationError("Record the reviewer and capture reference")
    if job["application_url"]:
        application_row = quote_check(c, root, job["application_evidence"], full=True)
        if job["application_url"] not in job["application_evidence"]["quote"] and canonical_url(job["application_url"]) != canonical_url(application_row["url"]):
            raise ValidationError("Application URL is not present in captured link evidence or its final page URL")
        approved=[]
        for row in c.execute("SELECT payload FROM sources"):
            s=json.loads(row[0])
            if s["employer_key"]==job["employer_key"] and s["kind"] in TRUSTED_KINDS:
                approved.append(s["prefix"])
        if source["kind"] in TRUSTED_KINDS and not any(under_prefix(job["application_url"],p) for p in approved):
            raise ValidationError("Application URL is outside independently registered official prefixes")
    profile,policy=configs(root)
    confirmed_skills=set(profile.get("search_skills",[]))
    if not set(job["matched_skills"]).issubset(confirmed_skills):
        raise ValidationError("Matched skill not in user profile; cannot infer a new skill")
    check = evaluate(job,profile,policy,asof=current,trusted=source["kind"] in TRUSTED_KINDS,
        source_fresh=current-dt(source["reviewed_at"])<=timedelta(days=policy["source_review_days"]),
        evidence_fresh=current-dt(job["observed_at"])<=timedelta(hours=policy["fresh_hours"]))
    return job_key(job),check

def ingest(root,rid,batch):
    if not isinstance(batch,list) or len(batch)>LIMITS["batch"]:
        raise ValidationError("Batch must be a list of at most 200 observations")
    accepted=0; rejected=[]; new=0; changed=0
    with connect(root) as c:
        assert_run(c,root,rid)
        for index,job in enumerate(batch):
            try:
                key,assessment=validate_job(c,root,job)
                old=c.execute("SELECT * FROM jobs WHERE key=?",(key,)).fetchone()
                if old and dt(job["observed_at"])<dt(old["last_seen"]):
                    raise ValidationError("Out-of-order observation cannot overwrite newer data")
                content={k:v for k,v in job.items() if k not in {"observed_at","identity_evidence","status_evidence","application_evidence","requirements_review"}}
                # Evidence references inside facts are normalized out of business-change detection.
                content["facts"]={k:(v["value"] if v else None) for k,v in job["facts"].items()}
                content["checks"]=[{k:v for k,v in x.items() if k!="evidence"} for x in job["checks"]]
                h=digest(content)
                oid="obs_"+digest([key,rid,job["observed_at"],digest(job)])[:24]
                if c.execute("SELECT 1 FROM observations WHERE id=?",(oid,)).fetchone():
                    continue
                last_verified=job["observed_at"] if assessment["status"] in {"open","closed","expired","not_open","talent_pool"} else (old["last_verified"] if old else None)
                c.execute("""INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                    payload=excluded.payload,last_seen=excluded.last_seen,last_verified=excluded.last_verified,
                    last_attempt=excluded.last_attempt,probe_status=excluded.probe_status,content_hash=excluded.content_hash,run_id=excluded.run_id""",
                    (key,dumps(job),old["first_seen"] if old else timestamp(),job["observed_at"],last_verified,timestamp(),"ok",h,rid))
                c.execute("INSERT INTO observations VALUES(?,?,?,?,?,?)",(oid,key,rid,job["observed_at"],dumps(job),h))
                c.execute("UPDATE leads SET resolved_key=? WHERE url=?", (key, canonical_url(job["url"])))
                kind="new" if not old else ("changed" if old["content_hash"]!=h else "reverified")
                c.execute("INSERT INTO events(run_id,job_key,at,kind,detail) VALUES(?,?,?,?,?)",(rid,key,timestamp(),kind,dumps(assessment)))
                accepted+=1; new+=kind=="new"; changed+=kind=="changed"
            except (ValidationError,KeyError,TypeError,ValueError,OSError) as ex:
                rejected.append({"index":index,"reason":str(ex),"observation":job})
                c.execute("INSERT INTO events(run_id,at,kind,detail) VALUES(?,?,?,?)",(rid,timestamp(),"quarantined",dumps({"index":index,"reason":str(ex)})))
    if rejected:
        write_json(Path(root)/"quarantine"/(rid+"-"+uuid4().hex[:8]+".json"),rejected)
    return {"accepted":accepted,"new":new,"changed":changed,"quarantined":len(rejected),"errors":[{"index":x["index"],"reason":x["reason"]} for x in rejected]}

def probe(root,rid,key,status,detail):
    if status not in {"ok","http_404","http_410","http_403","http_429","timeout","login_required","captcha","parse_error","unknown"}:
        raise ValidationError("Unknown probe status")
    with connect(root) as c:
        assert_run(c,root,rid)
        if not c.execute("SELECT 1 FROM jobs WHERE key=?",(key,)).fetchone():
            raise ValidationError("Unknown job key")
        # A link test never refreshes last_seen or last_verified. A success needs a fresh observation to restore ok.
        if status!="ok":
            c.execute("UPDATE jobs SET last_attempt=?,probe_status=? WHERE key=?",(timestamp(),status,key))
        else:
            c.execute("UPDATE jobs SET last_attempt=? WHERE key=?",(timestamp(),key))
        c.execute("INSERT INTO events(run_id,job_key,at,kind,detail) VALUES(?,?,?,?,?)",(rid,key,timestamp(),"probe",dumps({"status":status,"detail":detail})))
    return {"job_key":key,"probe_status":status,"last_verified_refreshed":False}

def add_lead(root, rid, lead):
    required = {"url", "title_hint", "company_hint", "industry", "found_at", "tool_ref", "reason"}
    if not isinstance(lead, dict) or set(lead) != required:
        raise ValidationError("Lead schema mismatch; hints are not verified facts")
    url = canonical_url(lead["url"])
    if dt(lead["found_at"]) > now_utc() + timedelta(minutes=5) or not lead["tool_ref"]:
        raise ValidationError("A lead needs an actual discovery reference and a non-future date")
    ident = "lead_" + digest(url)[:24]
    with connect(root) as c:
        assert_run(c, root, rid)
        old = c.execute("SELECT first_seen FROM leads WHERE id=?", (ident,)).fetchone()
        c.execute("INSERT INTO leads VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,last_seen=excluded.last_seen,run_id=excluded.run_id",
            (ident,url,dumps(lead),old[0] if old else timestamp(),lead["found_at"],rid,None))
    return {"lead_id": ident, "counted_as_eligible": False}


def add_coverage(root,rid,entry):
    required={"task_id","industry","region","channel","query","result","tool_ref","pages_checked","next_cursor","notes"}
    if set(entry)!=required or entry["result"] not in {"done","blocked","partial","zero_found","not_attempted"}:
        raise ValidationError("Invalid coverage record")
    if entry["result"]!="not_attempted" and not entry["tool_ref"]:
        raise ValidationError("Coverage needs an actual search/browser tool reference")
    if isinstance(entry["pages_checked"],bool) or not isinstance(entry["pages_checked"],int) or entry["pages_checked"]<0:
        raise ValidationError("Invalid page count")
    with connect(root) as c:
        assert_run(c,root,rid)
        cid=digest([rid,entry["task_id"]])
        c.execute("INSERT INTO coverage VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",(cid,rid,dumps(entry)))
    return {"task_id":entry["task_id"],"result":entry["result"]}

def plan(root,on_date=None):
    profile,policy=configs(root)
    today=date.fromisoformat(on_date) if on_date else now_utc().date()
    # Date selects rotation, not a user's graduation year or an employer campaign.
    group=policy["regions"][today.toordinal()%len(policy["regions"])]
    tasks=[]
    for industry,terms in policy["industries"].items():
        for channel,roles in policy.get("channels", {"internship":"实习", "fulltime":"全职 校招 社招"}).items():
            tasks.append({"task_id":industry+":"+channel,"industry":industry,"region":"全国+"+group,"channel":channel,
                "query":f'{industry} {roles} 官方招聘',"result":"not_attempted","tool_ref":"","pages_checked":0,"next_cursor":None,"notes":"按企业目录拆分，先列举岗位，再用职类关键词补漏。"})
    with connect(root) as c:
        due=[]
        for row in c.execute("SELECT * FROM jobs"):
            j=json.loads(row["payload"])
            if j["status"] in {"closed","expired","talent_pool"}:
                continue
            due.append({"job_key":row["key"],"url":j["url"],"last_seen":row["last_seen"],"deadline_at":j["deadline_at"],"probe_status":row["probe_status"]})
        due.sort(key=lambda x:(x["deadline_at"] or "9999",x["last_seen"]))
    return {"date":today.isoformat(),"rotation_region":group,"budgets":policy["budgets"],"required_tasks":tasks,"reverify_due":due,
        "graduation_year":profile["facts"]["graduation_year"],"coverage_warning":"Known-plan coverage is not internet recall."}

def materialize(root,asof=None):
    profile,policy=configs(root); current=asof or now_utc(); output=[]
    with connect(root) as c:
        sources={r["id"]:json.loads(r["payload"]) for r in c.execute("SELECT * FROM sources")}
        annotations={r["job_key"]:dict(r) for r in c.execute("SELECT * FROM annotations")}
        for row in c.execute("SELECT * FROM jobs ORDER BY first_seen DESC,key"):
            j=json.loads(row["payload"]); s=sources.get(j["source_id"],{})
            integrity=True
            try:
                # Recheck evidence against disk before publishing, not only when ingesting.
                validate_job(c,root,j,asof=current)
            except (ValidationError,OSError,ValueError,TypeError,KeyError):
                integrity=False
            result=evaluate(j,profile,policy,asof=current,trusted=integrity and s.get("kind") in TRUSTED_KINDS,
                source_fresh=bool(s) and current-dt(s["reviewed_at"])<=timedelta(days=policy["source_review_days"]),
                evidence_fresh=current-dt(j["observed_at"])<=timedelta(hours=policy["fresh_hours"]),probe=row["probe_status"])
            output.append({**dict(row),"job":j,"assessment":result,"source":s,"annotation":annotations.get(row["key"],{})})
    return output

def annotations_from_rows(root,rows):
    statuses={"","未投递","已投递（用户记录）","笔试","面试","录用","拒绝","暂不投递"}
    with connect(root) as c:
        for row in rows:
            if len(row)<4 or not row[0]:continue
            key,status,priority,note=map(lambda v:"" if v is None else str(v),row[:4])
            if not c.execute("SELECT 1 FROM jobs WHERE key=?",(key,)).fetchone():
                raise ValidationError("Unknown key in follow-up sheet; refusing to guess identity")
            if status not in statuses or priority not in {"","高","中","低"}:
                raise ValidationError("Invalid follow-up value")
            if len(note) > 20000:
                raise ValidationError("User note exceeds 20000 characters; refusing silent truncation")
            c.execute("INSERT INTO annotations VALUES(?,?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET status=excluded.status,priority=excluded.priority,note=excluded.note,updated=excluded.updated",
                (key,status,priority,note[:20000],timestamp()))

def checkpoint(root,rid,value):
    allowed={"phase","completed_task_ids","pending_task_ids","next_actions","blockers","evidence_ids"}
    if set(value)!=allowed:
        raise ValidationError("Checkpoint schema mismatch")
    with connect(root) as c:assert_run(c,root,rid)
    # Bounded structured handoff; raw pages belong in the evidence store, not in memory.
    raw={"schema_version":1,"run_id":rid,**value,"written_at":timestamp()}
    if len(dumps(raw).encode())>24000:
        raise ValidationError("Checkpoint exceeds 24KB; store a pointer to the pending queue")
    write_json(Path(root)/"memory/checkpoint.json",raw)
    return {"checkpoint":"memory/checkpoint.json"}

def finalize(root,rid,requested_status="completed",export=True):
    root=Path(root)
    with connect(root) as c:
        run_record = assert_run(c,root,rid)
        unresolved_leads = c.execute("SELECT COUNT(*) FROM leads WHERE resolved_key IS NULL").fetchone()[0]
        coverage=[json.loads(r[0]) for r in c.execute("SELECT payload FROM coverage WHERE run_id=?",(rid,))]
        events={r[0]:r[1] for r in c.execute("SELECT kind,COUNT(*) FROM events WHERE run_id=? GROUP BY kind",(rid,))}
    rows=materialize(root)
    counts={b:sum(x["assessment"]["bucket"]==b for x in rows) for b in ("eligible","qualification_pending","verification_pending","history")}
    expected_ids = {industry + ":" + channel for industry in configs(root)[1]["industries"] for channel in configs(root)[1].get("channels", {"internship":"实习", "fulltime":"全职"})}
    expected = len(expected_ids)
    attempted = len(expected_ids & {x["task_id"] for x in coverage if x["result"] != "not_attempted"})
    complete = len(expected_ids & {x["task_id"] for x in coverage if x["result"] in {"done", "zero_found"}})
    pending_rechecks = sum(x["job"]["status"] == "open" and x["assessment"]["bucket"] != "history" and dt(x["last_attempt"]) < dt(run_record["started"]) for x in rows)
    status=requested_status
    if requested_status=="completed" and (pending_rechecks or complete<expected or events.get("quarantined",0) or any(x["result"] in {"blocked","partial"} for x in coverage)):
        status="partial"
    stats={**counts,"new":events.get("new",0),"changed":events.get("changed",0),"quarantined":events.get("quarantined",0),
        "unverified_leads":unresolved_leads,"pending_rechecks":pending_rechecks,"plan_tasks":expected,"attempted_tasks":attempted,"completed_tasks":complete,"coverage_denominator":"当前任务矩阵，不是互联网全部岗位", "asof":timestamp(),"run_status":status}
    export_error=None
    if export:
        try:
            from xlsx_writer import export_tracker
            exports=export_tracker(root,rid,rows,stats,coverage)
            stats["exports"]=exports
        except Exception as ex:
            export_error=f"{type(ex).__name__}: {ex}"
            stats["export_error"]=export_error
            status="export_failed";stats["run_status"]=status
    with connect(root) as c:
        assert_run(c,root,rid)
        c.execute("UPDATE runs SET ended=?,status=?,stats=? WHERE id=?",(timestamp(),status,dumps(stats),rid))
        c.commit()  # backup on the same connection must not run inside a pending write transaction.
        backup=root/"backups"/(rid+".sqlite3")
        with sqlite3.connect(backup, factory=ClosingConnection) as dst:c.backup(dst)
    write_json(root/"reports"/(rid+".json"),stats)
    summary=(f"# 岗位追踪日报\n\n运行：{rid}\n\n状态：{status}\n\n"
        f"符合公开条件且时效核验通过：{counts['eligible']}；资格待确认：{counts['qualification_pending']}；"
        f"待核实：{counts['verification_pending']}；历史与排除：{counts['history']}。\n\n"
        f"新增 {stats['new']}，变更 {stats['changed']}，隔离 {stats['quarantined']}。任务矩阵完成 {complete}/{expected}。"
        "该比例不代表互联网岗位召回率；零新增不代表没有新岗位。\n\n"
        f"导出：{dumps(stats.get('exports',{})) if not export_error else export_error}\n")
    atomic_bytes(root/"reports"/(rid+".md"),summary.encode())
    write_json(root/"memory/last_run.json",{"run_id":rid,"status":status,"report":"reports/"+rid+".json","stats":stats})
    return stats

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace",required=True)
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("init");sub.add_parser("begin");sub.add_parser("status")
    q=sub.add_parser("plan");q.add_argument("--date")
    q=sub.add_parser("evidence");q.add_argument("--run",required=True);q.add_argument("--file",required=True);q.add_argument("--url",required=True)
    q.add_argument("--captured-at",required=True);q.add_argument("--method",required=True);q.add_argument("--tool-ref",required=True);q.add_argument("--completeness",default="full_detail")
    for cmd in ("source","ingest","coverage","checkpoint","lead"):
        q=sub.add_parser(cmd);q.add_argument("--run",required=True);q.add_argument("--file",required=True)
    q=sub.add_parser("probe");q.add_argument("--run",required=True);q.add_argument("--key",required=True);q.add_argument("--status",required=True);q.add_argument("--detail",required=True)
    q=sub.add_parser("finalize");q.add_argument("--run",required=True);q.add_argument("--status",choices=["completed","partial","failed","blocked"],default="completed");q.add_argument("--no-export",action="store_true")
    a=p.parse_args(argv)
    try:
        if a.command=="init":out=init_workspace(a.workspace)
        elif a.command=="begin":out=begin(a.workspace)
        elif a.command=="plan":out=plan(a.workspace,a.date)
        elif a.command=="status":
            rows=materialize(a.workspace);out={"jobs":len(rows),"buckets":{b:sum(x["assessment"]["bucket"]==b for x in rows) for b in ("eligible","qualification_pending","verification_pending","history")}}
        elif a.command=="evidence":out=register_evidence(a.workspace,a.run,a.file,a.url,a.captured_at,a.method,a.tool_ref,a.completeness)
        elif a.command=="source":out=register_source(a.workspace,a.run,read_json(a.file))
        elif a.command=="ingest":out=ingest(a.workspace,a.run,read_json(a.file))
        elif a.command=="lead":out=add_lead(a.workspace,a.run,read_json(a.file))
        elif a.command=="coverage":out=add_coverage(a.workspace,a.run,read_json(a.file))
        elif a.command=="checkpoint":out=checkpoint(a.workspace,a.run,read_json(a.file))
        elif a.command=="probe":out=probe(a.workspace,a.run,a.key,a.status,a.detail)
        else:out=finalize(a.workspace,a.run,a.status,not a.no_export)
        print(json.dumps(out,ensure_ascii=False,indent=2))
        if out.get("quarantined",0) or out.get("run_status") in {"failed","blocked","export_failed"}:return 2
        return 0
    except (ValidationError,OSError,ValueError,sqlite3.Error) as ex:
        print(json.dumps({"error":type(ex).__name__,"detail":str(ex)},ensure_ascii=False),file=sys.stderr)
        return 2

if __name__=="__main__":
    raise SystemExit(main())
