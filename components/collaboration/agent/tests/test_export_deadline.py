"""Export-only bounded deadline; no broader Material or source permissions."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from collaboration_agent.material_fetch import transport,EXPORT_TIMEOUT,TIMEOUT,allowed_url
from collaboration_agent.model import Invalid
from readonly_intake import EXPORT,EXPORT_BYTES,public_read,SessionFetcher

class ExportDeadlineTests(unittest.TestCase):
    def test_total_deadline_is_export_only_and_get_remains_fixed_origin(self):
        budgets=[]; calls=[]
        @contextmanager
        def record_deadline(seconds): budgets.append(seconds); yield
        class Response:
            status=200
            def getheaders(self): return [('content-type','application/x-ndjson')]
            def read1(self,size): return b''
        class Connection:
            def __init__(self,*args): pass
            def request(self,method,target,headers): calls.append((method,target,headers))
            def getresponse(self): return Response()
            def close(self): pass
        with patch('collaboration_agent.material_fetch.deadline',record_deadline), \
             patch('collaboration_agent.material_fetch.public_addresses',return_value=[None]), \
             patch('collaboration_agent.material_fetch.PinnedHTTPS',Connection):
            self.assertIsNone(transport(EXPORT,EXPORT_BYTES,intake_export=True)[3])
            self.assertIsNone(transport('https://technocore.chat/llms.txt',100)[3])
            with self.assertRaises(Invalid): transport('https://evil.invalid/export',100,intake_export=True)
            with self.assertRaises(Invalid): transport(EXPORT,EXPORT_BYTES+1,intake_export=True)
            with self.assertRaises(Invalid): allowed_url(EXPORT)
        self.assertEqual((TIMEOUT,EXPORT_TIMEOUT),(15,30))
        self.assertEqual(budgets,[30,15])
        self.assertEqual([c[:2] for c in calls],[('GET','/r/tclk-offers/export'),('GET','/llms.txt')])
        self.assertTrue(all(not {'Cookie','Authorization','Proxy-Authorization'} & c[2].keys() for c in calls))

    def test_timeout_keeps_partial_bytes_without_retry_or_redirect(self):
        calls=[]; closed=[]
        class Response:
            status=200
            def __init__(self): self.reads=0
            def getheaders(self): return [('content-type','application/x-ndjson')]
            def read1(self,size):
                self.reads+=1
                if self.reads==1: return b'{"seq":1}\n'
                raise TimeoutError()
        class Connection:
            def __init__(self,*args): pass
            def request(self,*args,**kwargs): calls.append(args)
            def getresponse(self): return Response()
            def close(self): closed.append(True)
        with patch('collaboration_agent.material_fetch.public_addresses',return_value=[None]), \
             patch('collaboration_agent.material_fetch.PinnedHTTPS',Connection):
            status,_,raw,error=transport(EXPORT,EXPORT_BYTES,intake_export=True)
        self.assertEqual((status,raw,error),(200,b'{"seq":1}\n','TIMEOUT'))
        self.assertEqual(len(calls),1); self.assertEqual(closed,[True])

    def test_deadline_evidence_and_session_expiry_are_not_extended(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.local') as tmp:
            root=Path(tmp)
            with patch('readonly_intake.transport',return_value=(200,{},b'partial','TIMEOUT')) as send:
                result,raw=public_read(root,'export',EXPORT,EXPORT_BYTES,export=True)
            self.assertEqual(json.loads((root/'export-attempt.json').read_bytes())['deadline_seconds'],30)
            self.assertEqual(result['fetch_error'],'TIMEOUT')
            self.assertEqual((root/'export.bin').read_bytes(),raw)
            self.assertGreaterEqual(result['elapsed_seconds'],0)
            send.assert_called_once_with(EXPORT,EXPORT_BYTES,intake_export=True)
            expired=SessionFetcher(root/'fetch',clock=lambda:600,stop_at=600,
                                   send=lambda *args: self.fail('session deadline bypassed'))
            with self.assertRaisesRegex(Invalid,'SESSION_TIME_BUDGET_EXCEEDED'):
                expired.fetch('/kv/ns/key',[0])
            self.assertEqual(expired.count,0)

if __name__=='__main__': unittest.main()
