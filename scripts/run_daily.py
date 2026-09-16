#!/usr/bin/env python3
"""Run one fresh Codex CLI session. Dry-run unless --execute is explicitly supplied."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
from datetime import datetime,timezone
import scout

class RunLock:
    def __init__(self,path):self.path=Path(path);self.fd=None;self.keep=False
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        try:self.fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError as ex:raise RuntimeError("Existing run.lock: do not steal it automatically; check PID/host before manual recovery") from ex
        os.write(self.fd,json.dumps({"pid":os.getpid(),"host":socket.gethostname(),"started":scout.timestamp()}).encode());os.fsync(self.fd)
        return self
    def __exit__(self,*args):
        if self.fd is not None:os.close(self.fd)
        if not self.keep:self.path.unlink(missing_ok=True)

class ProcessCleanupError(RuntimeError):
    """Child ownership could not be confirmed after termination; retain the run lock."""


def execute_captured(command, prompt, workspace, out, err, timeout):
    """Bound the owned Codex process tree. Never kill unrelated Codex processes."""
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err,
                            encoding="utf-8", cwd=workspace, **options)
    try:
        proc.communicate(prompt, timeout=timeout)
        return proc.returncode
    except BaseException:
        try:
            if os.name == "nt":
                result = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=20, check=False)
                if result.returncode != 0:
                    raise ProcessCleanupError("Windows process-tree cleanup is unconfirmed; inspect the retained lock/PID before restarting")
            else:
                # A new session separates this owned tree from the caller and unrelated tools.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait(timeout=10)
        except Exception as cleanup_error:
            raise ProcessCleanupError("Process cleanup is unconfirmed; do not export or launch another writer") from cleanup_error
        raise


def watched_hashes(workspace):
    paths=[workspace/"profile.json",workspace/"policy.json",workspace/"company_seeds.json"]
    paths.extend(p for p in scout.BASE.rglob("*") if p.is_file() and p.suffix in {".py",".md",".json",".yaml",".ps1"} and "__pycache__" not in str(p))
    return {str(p.resolve()):scout.digest(p.read_bytes()) for p in paths}

def build_prompt(workspace,rid):
    template=(scout.BASE/"templates/daily_prompt.txt").read_text(encoding="utf-8")
    return template.replace("{{WORKSPACE}}",str(workspace)).replace("{{RUN_ID}}",rid).replace("{{SKILL_ROOT}}",str(scout.BASE))

def cli_command(exe,workspace):
    # Avoid retired model names and dangerous full-access modes. Capability check precedes execution.
    return [exe,"--search","-a","never","exec","--sandbox","workspace-write","--skip-git-repo-check","--json","--cd",str(workspace),"-"]

def run(workspace,execute=False,timeout=5400):
    if timeout <= 0:raise ValueError("timeout must be positive")
    workspace=Path(workspace).resolve()
    scout.init_workspace(workspace)
    exe=shutil.which("codex")
    if not exe:raise RuntimeError("Codex CLI is not on PATH. Install/sign in through the official product; no credentials are read by this runner.")
    version=subprocess.run([exe,"--version"],capture_output=True,text=True,timeout=20,check=True).stdout.strip()
    helptext=subprocess.run([exe,"exec","--help"],capture_output=True,text=True,timeout=20,check=True).stdout
    for flag in ("--sandbox","--skip-git-repo-check","--json","--cd"):
        if flag not in helptext:raise RuntimeError("Installed Codex CLI lacks "+flag+"; review official docs instead of weakening sandbox")
    command=cli_command(exe,workspace)
    if not execute:return {"mode":"dry_run","codex_version":version,"command":command,"workspace":str(workspace),"scheduled":False}
    with RunLock(workspace/"state/run.lock") as lock:
        baseline=watched_hashes(workspace)
        rid=scout.begin(workspace)["run_id"]
        event_path=workspace/"reports"/(rid+".codex-events.jsonl")
        err_path=workspace/"reports"/(rid+".codex-stderr.txt")
        runtime={"run_id":rid,"codex_version":version,"command":command,"python":sys.version,"host":socket.gethostname(),"started":scout.timestamp(),"web_search_requested":"live"}
        scout.write_json(workspace/"reports"/(rid+".runtime.json"),runtime)
        status="completed"
        try:
            with event_path.open("w",encoding="utf-8") as out,err_path.open("w",encoding="utf-8") as err:
                returncode=execute_captured(command,build_prompt(workspace,rid),workspace,out,err,timeout)
            if returncode!=0:status="failed"
        except ProcessCleanupError:
            lock.keep=True
            raise
        except subprocess.TimeoutExpired:
            status="partial"
            runtime["error"]="Codex timeout; checkpoint/queue preserved. A new run must recheck evidence freshness."
        if watched_hashes(workspace)!=baseline:
            runtime["error"]="Protected source/config drift detected; no automatic export or authorization. Review changes."
            with scout.connect(workspace) as c:
                c.execute("UPDATE runs SET ended=?,status=?,stats=? WHERE id=?",(scout.timestamp(),"drift_blocked",scout.dumps(runtime),rid))
            scout.write_json(workspace/"reports"/(rid+".runtime.json"),runtime)
            raise RuntimeError(runtime["error"])
        # Agent is instructed not to finalize; the host verifies and exports once.
        report=scout.finalize(workspace,rid,status)
        runtime["ended"]=scout.timestamp();runtime["result"]=report["run_status"]
        scout.write_json(workspace/"reports"/(rid+".runtime.json"),runtime)
        return report

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--workspace",required=True);p.add_argument("--execute",action="store_true");p.add_argument("--timeout-seconds",type=int,default=5400)
    a=p.parse_args()
    try:
        r=run(a.workspace,a.execute,a.timeout_seconds);print(json.dumps(r,ensure_ascii=False,indent=2))
        return 0 if r.get("run_status") not in {"failed","export_failed","blocked"} else 2
    except Exception as ex:
        print(json.dumps({"error":type(ex).__name__,"detail":str(ex)},ensure_ascii=False),file=sys.stderr);return 2
if __name__=="__main__":raise SystemExit(main())
