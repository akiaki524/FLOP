"""Async ledger barriers under the actual Node permission flags, using dummy state."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = """
import assert from 'node:assert/strict';
import { AsyncPilotLedger, readPublicState } from './src/collaboration_agent/pilot_ledger.mjs';
const did='did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL';
const root=process.argv[1]+'/ledger';
"""

class AsyncLedgerTest(unittest.TestCase):
    def execute(self, script):
        with tempfile.TemporaryDirectory(prefix='connection-ledger-') as directory:
            result = subprocess.run(['node', '--permission', '--disable-sigusr1',
                '--disallow-code-generation-from-strings', '--allow-fs-read=' + str(ROOT / 'src'),
                '--allow-fs-read=' + directory, '--allow-fs-write=' + directory,
                '--input-type=module', '-e', PRELUDE + script, directory], cwd=ROOT, capture_output=True)
            self.assertEqual(result.returncode, 0, 'ASYNC_LEDGER_TEST_FAILED')
            self.assertEqual(result.stdout, b'')
            self.assertEqual(result.stderr, b'')

    def test_durable_reopen_and_scoped_filesystem_denial(self):
        self.execute("""
const ledger=await AsyncPilotLedger.open(did,{root,create:true});
await ledger.append({event:'START_TEST_EPHEMERAL'});
assert.equal(readPublicState(ledger.path,did).revision,1);
await ledger.close();
const recovered=await AsyncPilotLedger.open(did,{root});
assert.equal(recovered.value.revision,1);
await recovered.close();
const {open}=await import('node:fs/promises');
await assert.rejects(open('/tmp/connection-ledger-forbidden','wx'),{code:'ERR_ACCESS_DENIED'});
assert.equal(process.permission.has('child'),false);
assert.equal(process.permission.has('worker'),false);
assert.equal(process.permission.has('addons'),false);
""")

    def test_failed_file_barrier_never_publishes_revision(self):
        self.execute("""
let fail=false;
const ledger=await AsyncPilotLedger.open(did,{root,create:true,testFault:phase=>{
 if(fail && phase==='file-fsync') throw new Error('INJECTED');
}});
fail=true;
await assert.rejects(ledger.append({event:'START_TEST_EPHEMERAL'}),{message:'INJECTED'});
assert.equal(readPublicState(ledger.path,did).revision,0);
await assert.rejects(ledger.append({event:'START_TEST_EPHEMERAL'}),{code:'LEDGER_BUSY_OR_BROKEN'});
await ledger.close();
""")

    def test_failed_directory_barrier_does_not_ack_or_rollback(self):
        self.execute("""
let fail=false;
const ledger=await AsyncPilotLedger.open(did,{root,create:true,testFault:phase=>{
 if(fail && phase==='directory-fsync') throw new Error('INJECTED');
}});
fail=true;
await assert.rejects(ledger.append({event:'START_TEST_EPHEMERAL'}),{message:'INJECTED'});
assert.equal(ledger.value.revision,0);
assert.equal(readPublicState(ledger.path,did).revision,1);
await ledger.close();
const recovered=await AsyncPilotLedger.open(did,{root});
assert.equal(recovered.value.revision,1);
await recovered.close();
""")

    def test_concurrent_append_and_duplicate_owner_refused(self):
        self.execute("""
const ledger=await AsyncPilotLedger.open(did,{root,create:true});
await assert.rejects(AsyncPilotLedger.open(did,{root}),{message:'LEDGER_LOCKED_RECONCILIATION_REQUIRED'});
const first=ledger.append({event:'START_TEST_EPHEMERAL'});
await assert.rejects(ledger.append({event:'START_TEST_EPHEMERAL'}),{code:'LEDGER_BUSY_OR_BROKEN'});
await first;
assert.equal(ledger.value.revision,1);
await ledger.close();
""")

if __name__ == '__main__':
    unittest.main()
