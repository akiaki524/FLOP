"""Rootless Gate A checks for production-anchor mismatch and write-off refusal."""
import json
import os
import select
import socket
import tempfile
import time
import unittest

from connection_offline import OfflineFixture, NODE


PROJECT_DID = 'did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL'


class GateADiagnosticTest(unittest.TestCase):
    def real_fixture(self, root, credential=b'A' * 32):
        fixture = OfflineFixture(root)
        dummy = fixture.credentials / 'dummy'
        if credential is None:
            dummy.unlink()
        else:
            dummy.write_bytes(credential)
            dummy.rename(fixture.credentials / 'project-seed')
        (fixture.config / 'config.json').write_text(json.dumps({
            'mode': 'REAL_ACCEPT_PREPARATION',
            'projectDid': PROJECT_DID,
            'externalWriteEnabled': False,
        }))
        return fixture

    def signer_failure(self, credential):
        with tempfile.TemporaryDirectory(prefix='gate-a-diagnostic-') as root:
            fixture = self.real_fixture(root, credential)
            try:
                child = fixture.launch('signer')
                self.assertEqual(child.wait(timeout=15), 70)
                self.assertEqual(child.stdout.read(), b'')
                self.assertEqual(child.stderr.read(), b'')
                diagnostic = json.loads((fixture.state / 'startup-diagnostic.json').read_text())
                self.assertEqual(diagnostic['phase'], 'CREDENTIAL')
                serialized = json.dumps(diagnostic)
                self.assertNotIn((b'A' * 32).hex(), serialized)
                self.assertNotIn('AAAAAAAA', serialized)
                return diagnostic['code']
            finally:
                fixture.close()

    def test_fake_seed_reaches_production_anchor_and_reports_did_mismatch(self):
        self.assertIsNotNone(NODE, 'NODE_V22_REQUIRED')
        self.assertEqual(self.signer_failure(b'A' * 32), 'REAL_CREDENTIAL_DID_MISMATCH')

    def test_missing_and_malformed_delivery_have_distinct_closed_diagnostics(self):
        self.assertEqual(self.signer_failure(None), 'REAL_CREDENTIAL_UNAVAILABLE')
        self.assertEqual(self.signer_failure(b'A' * 31), 'REAL_CREDENTIAL_MALFORMED')

    def test_transport_send_is_policy_denied_before_payload_validation(self):
        with tempfile.TemporaryDirectory(prefix='gate-a-transport-') as root:
            fixture = self.real_fixture(root)
            services = fixture.source / 'connection_services.mjs'
            source = services.read_text()
            needle = "      validateAcceptHandoff(request.value, { t, signing });"
            self.assertIn(needle, source)
            source = source.replace(needle,
                "      process.stdout.write('VALIDATION_REACHED\\n');\n" + needle)
            services.write_text(source)
            try:
                child = fixture.launch('transport')
                deadline = time.monotonic() + 5
                while child.poll() is None and time.monotonic() < deadline:
                    try:
                        with socket.socket(socket.AF_UNIX) as client:
                            client.settimeout(.2)
                            client.connect(str(fixture.paths['transport']))
                            client.sendall(b'{"operation":"sendAccept","value":{}}\n')
                            reply = json.loads(client.makefile('rb').readline())
                        break
                    except (ConnectionRefusedError, socket.timeout):
                        time.sleep(.02)
                else:
                    self.fail('TRANSPORT_DID_NOT_ACCEPT_REQUEST')
                self.assertEqual(reply, {'ok': False, 'code': 'CONNECTION_REQUEST_DENIED'})
                readable, _, _ = select.select([child.stdout], [], [], .2)
                reached = os.read(child.stdout.fileno(), 4096) if readable else b''
                self.assertEqual(reached, b'', 'DISABLED_SEND_REACHED_PAYLOAD_VALIDATION')
                self.assertIsNone(child.poll(), 'TRANSPORT_EXITED_AFTER_POLICY_REFUSAL')
            finally:
                fixture.close()


if __name__ == '__main__':
    unittest.main()
