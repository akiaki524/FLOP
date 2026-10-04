"""Rootless four-role DUMMY_OFFLINE IPC and crash-recovery integration.

This exercises inherited Unix listeners and the production adapters.  It does
not claim to reproduce systemd UID, credential, or unit hardening.
"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from connection_startup import classify_failure

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

WRAPPER = r"""
import ctypes, errno, os, socket, sys
sys.excepthook=lambda kind,value,trace: os._exit({IndexError:131,ValueError:132,OSError:133,TypeError:134,AttributeError:135}.get(kind,139))
class C(ctypes.Structure):
 _fields_=[('arg',ctypes.c_uint),('op',ctypes.c_int),('a',ctypes.c_uint64),('b',ctypes.c_uint64)]
count=int(sys.argv[1]); deny=int(sys.argv[2]); first=3; node=first+count
if ctypes.CDLL(None,use_errno=True).prctl(38,1,0,0,0): os._exit(120)
if deny:
 lib=ctypes.CDLL('libseccomp.so.2',use_errno=True); lib.seccomp_init.restype=ctypes.c_void_p
 lib.seccomp_init.argtypes=[ctypes.c_uint32]
 lib.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p]
 lib.seccomp_syscall_resolve_name.restype=ctypes.c_int; lib.seccomp_rule_add.restype=ctypes.c_int
 lib.seccomp_rule_add.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint]
 lib.seccomp_load.argtypes=[ctypes.c_void_p]; lib.seccomp_release.argtypes=[ctypes.c_void_p]
 ctx=lib.seccomp_init(0x7fff0000); nr=lib.seccomp_syscall_resolve_name(b'socket')
 if not ctx or nr<0: os._exit(121)
 try:
  for family in (socket.AF_INET,socket.AF_INET6):
   if lib.seccomp_rule_add(ctx,0x00050000|errno.EAFNOSUPPORT,nr,1,C(0,4,family,0)): os._exit(122)
  if lib.seccomp_load(ctx): os._exit(123)
 finally: lib.seccomp_release(ctx)
for offset in range(count): os.dup2(int(sys.argv[first+offset]),3+offset,inheritable=True)
os.environ['LISTEN_PID']=str(os.getpid())
os.execve(sys.argv[node],sys.argv[node:],os.environ)
"""

class OfflineFixture:
    def __init__(self, root):
        self.root = Path(root); self.deploy = self.root / "deployment"
        self.source = self.deploy / "src/collaboration_agent"
        self.config = self.root / "config"; self.credentials = self.root / "credentials"
        self.state = self.root / "state"; self.tests = self.deploy / "tests"
        self.children = []
        for path in (self.source, self.config, self.credentials, self.state, self.tests): path.mkdir(parents=True)
        for path in (ROOT / "src/collaboration_agent").glob("*.mjs"): shutil.copyfile(path, self.source / path.name)
        shutil.copyfile(ROOT / "src/collaboration_agent/tclk_pin.json", self.source / "tclk_pin.json")
        pin = json.loads((self.source / "tclk_pin.json").read_text())
        runtime = self.deploy / ".local/batch16/official-runtime"
        for relative in pin["files"]:
            target = runtime / relative; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / ".local/batch16/official-runtime" / relative, target)
        self.paths = {name: self.root / (name + ".sock") for name in
                      ("worker", "gate", "human", "transport", "worker-control")}
        for filename in ("connection_runtime.mjs", "connection_services.mjs"):
            path = self.source / filename; text = path.read_text()
            text = text.replace("/etc/collab-connection/config.json", str(self.config / "config.json"))
            text = text.replace("/var/lib/collab-signer", str(self.state))
            for name, endpoint in self.paths.items():
                text = text.replace("/run/collab-connection/" + name + ".sock", str(endpoint))
            path.write_text(text)
        smoke = (ROOT / "tests/connection_smoke.mjs").read_text()
        smoke = smoke.replace(".local/batch17a/candidate.json", str(ROOT / ".local/batch17a/candidate.json"))
        smoke = smoke.replace("/run/collab-connection/", str(self.root) + "/")
        for name, endpoint in self.paths.items():
            smoke = smoke.replace("/run/collab-connection/" + name + ".sock", str(endpoint))
        (self.tests / "connection_smoke.mjs").write_text(smoke)
        seed = b"A" * 32; self.seed=seed; (self.credentials / "dummy").write_bytes(seed)
        (self.config / "config.json").write_text(json.dumps({"mode":"DUMMY_OFFLINE",
            "projectDid":"did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL",
            "dummyCredentialSha256":hashlib.sha256(seed).hexdigest()}))
        self.listeners = {}
        for name, path in self.paths.items():
            listener=socket.socket(socket.AF_UNIX); listener.bind(str(path)); listener.listen(8)
            self.listeners[name]=listener

    def launch(self, role):
        # The fd-name mapping must also work when systemd supplies the reverse order.
        names = {"signer": ["gate", "worker"], "worker": ["worker-control"],
                 "approval": ["human"], "transport": ["transport"]}[role]
        passed = [os.dup(self.listeners[name].fileno()) for name in names]
        env = os.environ.copy(); env.update({"LISTEN_FDS":str(len(names)),
            "LISTEN_FDNAMES":":".join(names), "CREDENTIALS_DIRECTORY":str(self.credentials)})
        fs = ["--allow-fs-read="+str(self.deploy), "--allow-fs-read="+str(self.config)]
        if role == "signer": fs += ["--allow-fs-read="+str(self.credentials),
            "--allow-fs-read="+str(self.state), "--allow-fs-write="+str(self.state)]
        entry = [str(self.source / "pilot_signer.mjs"), "--offline-connection"] if role == "signer" else [
            str(self.source / "connection_services.mjs"), role]
        argv = [sys.executable,"-I","-c",WRAPPER,str(len(passed)),"0" if role == "transport" else "1",
                *map(str,passed),NODE,"--permission","--disable-sigusr1",
                "--disallow-code-generation-from-strings",*fs,*entry]
        child=subprocess.Popen(argv,cwd=self.deploy,env=env,pass_fds=tuple(passed),
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        for fd in passed: os.close(fd)
        self.children.append((role,child)); return child

    def smoke(self, recovery=False):
        command=[NODE,str(self.tests/"connection_smoke.mjs")]
        if recovery: command.append("recovery")
        result=subprocess.run(command,cwd=self.deploy,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        try: value=json.loads(result.stdout)
        except Exception: value={}
        if result.returncode or value.get("failed") != 0:
            passed=value.get("passed") if isinstance(value.get("passed"),int) else "UNKNOWN"
            dead=",".join(role for role,child in self.children if child.poll() is not None) or "NONE"
            diagnostic=self.state/"startup-diagnostic.json"
            phase="NONE"
            if diagnostic.is_file():
                detail=json.loads(diagnostic.read_text()); phase=detail.get("phase","UNKNOWN")+":"+detail.get("code","UNKNOWN")
            raise AssertionError("OFFLINE_SMOKE_FAILED_AFTER_"+str(passed)+":DEAD_"+dead+":DIAG_"+phase)
        if value.get("externalWrites") != 0 or value.get("realSecrets") is not False:
            raise AssertionError("OFFLINE_SMOKE_BOUNDARY_FAILED")
        if result.stderr: raise AssertionError("OFFLINE_SMOKE_STDERR")
        return value

    def close(self):
        noisy=False
        for _,child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
            if child.stdout:
                noisy = noisy or bool(child.stdout.read()); child.stdout.close()
            if child.stderr:
                noisy = noisy or bool(child.stderr.read()); child.stderr.close()
        for listener in self.listeners.values(): listener.close()
        if noisy: raise AssertionError("SERVICE_OUTPUT_NOT_SILENT")

class ConnectionOfflineTest(unittest.TestCase):
    def test_four_role_smoke_and_signer_crash_recovery(self):
        self.assertIsNotNone(NODE,"NODE_V22_REQUIRED")
        with tempfile.TemporaryDirectory(prefix="collab-offline-") as raw:
            f=OfflineFixture(raw)
            try:
                for role in ("transport","signer","approval","worker"): f.launch(role)
                time.sleep(.4)
                dead=[role for role,child in f.children if child.poll() is not None]
                failures=[]
                for role,child in f.children:
                    if child.poll() is not None:
                        failures.append(role+":"+classify_failure(child.returncode,child.stderr.read()))
                self.assertEqual(dead,[],"SERVICE_START_FAILED:"+",".join(failures))
                forms=(f.seed,f.seed.hex().encode())
                for _,child in f.children:
                    proc=Path("/proc")/str(child.pid)
                    raw=(proc/"environ").read_bytes()+(proc/"cmdline").read_bytes()
                    self.assertFalse(any(value in raw for value in forms),"DUMMY_PRESENT_ENV_OR_ARGV")
                    self.assertIn("NoNewPrivs:\t1",(proc/"status").read_text())
                first=f.smoke(); self.assertEqual(first["passed"],13)
                signer=next(child for role,child in f.children if role=="signer")
                custody=f.state/"custody"
                before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in custody.iterdir()
                        if p.is_file() and p.name!="connection-store.lock"}
                signer.kill(); signer.wait(timeout=5)
                locks=list((f.state/"ledger").glob("identity-*.json.lock"))+[custody/"connection-store.lock"]
                self.assertEqual(len(locks),2)
                for lock in locks:
                    self.assertEqual(json.loads(lock.read_text())["pid"],signer.pid); lock.unlink()
                    fd=os.open(lock.parent,os.O_RDONLY|os.O_DIRECTORY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
                f.launch("signer"); time.sleep(.4)
                self.assertIsNone(f.children[-1][1].poll(),"SIGNER_RESTART_FAILED")
                recovery=f.smoke(True); self.assertEqual(recovery["passed"],3)
                after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in custody.iterdir()
                       if p.is_file() and p.name!="connection-store.lock"}
                self.assertEqual(before,after)
            finally: f.close()

if __name__=="__main__": unittest.main()
