"""Local Unix socket tests for the production adapter; no credentials or signatures."""
import json
from pathlib import Path
import select
import socket
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ConnectionIPCTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='collab-ipc-')
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'test.sock')
        self.listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.listener.close)
        self.listener.bind(self.path)
        self.listener.listen(4)
        script = '''import {serveSocket} from './src/collaboration_agent/connection_runtime.mjs';
const server=serveSocket(Number(process.argv[1]),async value=>{
 if(value.method!=='status') throw new Error('DENIED');
 await new Promise(r=>setTimeout(r,20)); return {ready:true};
});
server.on('listening',()=>process.stdout.write('READY\\n'));
'''
        self.child = subprocess.Popen(['node', '--input-type=module', '-e', script, str(self.listener.fileno())],
                                      cwd=ROOT, pass_fds=[self.listener.fileno()], stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        self.assertTrue(select.select([self.child.stdout], [], [], 5)[0], 'server did not start')
        self.assertEqual(self.child.stdout.readline(), b'READY\n')

    def stop(self):
        self.child.terminate()
        self.child.wait(timeout=5)
        self.child.stdout.close()

    def request(self, data):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(6)
            client.connect(self.path)
            client.sendall(data)
            client.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                part = client.recv(4096)
                if not part:
                    break
                chunks.append(part)
            return b''.join(chunks)

    def test_async_reply_survives_client_half_close(self):
        self.assertEqual(json.loads(self.request(b'{"method":"status"}\n')),
                         {'ok': True, 'result': {'ready': True}})

    def test_denial_does_not_reflect_input(self):
        raw = self.request(b'{"method":"DO_NOT_ECHO_THIS"}\n')
        self.assertNotIn(b'DO_NOT_ECHO_THIS', raw)
        self.assertEqual(json.loads(raw), {'ok': False, 'code': 'CONNECTION_REQUEST_DENIED'})

    def test_oversized_request_closes_without_reflection(self):
        self.assertEqual(self.request(b'x' * 65537 + b'\n'), b'')

    def test_real_client_adapter(self):
        script = '''import {callSocket} from './src/collaboration_agent/connection_runtime.mjs';
const value=await callSocket(process.argv[1],{method:'status'});
if(value.ready!==true) process.exit(1);
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, self.path], cwd=ROOT,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, 'client adapter failed')
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')

if __name__ == '__main__':
    unittest.main()
