#!/usr/bin/env python3
"""Human/root-only synthetic crash rehearsal. Never reads a credential or seed.

Run only after review and explicit Host mutation approval. Failure leaves the
gate/evidence in place; it never blindly restores a changed crash configuration.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

_spec = importlib.util.spec_from_file_location(
    'signer_wsl_crash_gate', Path(__file__).resolve().with_name('wsl_crash_gate.py'))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

# Executed in a fresh interpreter: no inherited Python memory, credential or
# project seed. Nonzero limits ensure this does not merely test RLIMIT_CORE=0.
CRASH = '''import os, resource
resource.setrlimit(resource.RLIMIT_CORE, (8 * 1024 * 1024, 8 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
synthetic = bytearray(b"FLOP-SYNTHETIC-CRASH-ONLY" * 1024)
os.abort()
'''


def prove():
    gate.need(os.geteuid() == 0, 'ROOT_REQUIRED')
    if not gate.is_wsl():
        return {'isWsl': False, 'skipped': True, 'hostMutated': False}
    gate.need(gate.state_present() is False, 'CRASH_PROOF_EXISTING_GATE_REFUSED')
    gate.need(not os.path.lexists('/var/lib/flop-policy-signer-secret/project-seed.cred'),
              'CRASH_PROOF_EXISTING_CREDENTIAL_REFUSED')
    started_at = int(time.time() * 1000)
    original = gate.current()
    boot = gate.boot_id()
    gate.operate('arm')
    gate.require_safe()
    gate.need(gate.current() == gate.TARGET, 'WSL_CRASH_CAPTURE_UNSAFE')
    with tempfile.TemporaryDirectory(prefix='flop-synthetic-crash-') as directory:
        result = subprocess.run(
            [sys.executable, '-I', '-c', CRASH], cwd=directory,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10, check=False,
            env={'PATH': '/usr/bin:/bin'})
        gate.need(result.returncode == -signal.SIGABRT, 'CRASH_PROOF_SIGNAL_MISMATCH')
        gate.need(not list(Path(directory).iterdir()), 'CRASH_PROOF_LOCAL_CORE_FOUND')
    gate.need(gate.boot_id() == boot and gate.current() == gate.TARGET,
              'CRASH_PROOF_STATE_CHANGED')
    gate.operate('disarm')
    gate.need(gate.boot_id() == boot and gate.current() == original,
              'CRASH_PROOF_RESTORE_MISMATCH')
    return {
        'isWsl': True, 'syntheticOnly': True, 'signal': 'SIGABRT',
        'startedAtMs': started_at, 'finishedAtMs': int(time.time() * 1000),
        'nonPipeNoCoreBeforeAndAfterCrash': True, 'localCoreRetained': False,
        'originalRestoredExactly': True,
        # Kernel no-core contract supports non-invocation; this does NOT claim
        # independent observation of Windows-side capture activity.
        'handlerNonInvocationEvidence': 'LINUX_NO_CORE_CONFIGURATION_CONTRACT',
        'windowsCaptureObserved': 'NOT_CHECKED',
        'humanVerificationRequired': (
            'Compare Windows %TEMP%\\wsl-crashes metadata before/after this bounded '
            'test; confirm no new dump for its time window. Do not open/upload dumps.'),
    }


def main():
    try:
        gate.need(len(sys.argv) == 1, 'CRASH_PROOF_ARGUMENTS_DENIED')
        print(json.dumps(prove(), sort_keys=True))
        return 0
    except gate.GateError as error:
        print(json.dumps({'passed': False, 'code': str(error)}))
    except Exception:
        print(json.dumps({'passed': False, 'code': 'CRASH_PROOF_FAILED'}))
    return 70


if __name__ == '__main__':
    raise SystemExit(main())
