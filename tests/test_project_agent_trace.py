import unittest
from unittest import mock
from human_compact.trajectory import project_agent_trace as T

class TraceTests(unittest.TestCase):
    def test_redacted_actual_prompt_and_bounded_response(self):
        events=[];engine=mock.Mock(model='sonnet',kind='claude')
        engine.generate_plain.return_value='SECRET'+('x'*25000)
        redact=lambda s:s.replace('SECRET','[redacted]')
        raw=T.call(engine,'policy SECRET',redact,events.append)
        self.assertEqual('policy [redacted]',engine.generate_plain.call_args.args[0])
        self.assertEqual(engine.generate_plain.call_args.args[0],events[0]['prompt'])
        self.assertEqual('running',events[0]['status']);self.assertEqual('done',events[1]['status'])
        self.assertNotIn('SECRET',str(events));self.assertEqual(24000,len(events[1]['response']))
        self.assertTrue(events[1]['responseTruncated']);self.assertTrue(raw.startswith('SECRET'))

    def test_redaction_preserves_routes_through_json_encoding(self):
        import json
        from human_compact.trajectory import project_setup as S
        code='app.secret_key = "private-value"\n\n@app.route("/")\ndef index(): return "OK"\n\n@app.route("/post", methods=["POST"])\n'
        brief={'requestedFiles':[{'text':S.scrub(code,str)}]}
        result=json.loads(json.dumps(S.scrub_tree(brief,str)))['requestedFiles'][0]['text']
        self.assertNotIn('private-value',result)
        self.assertIn('@app.route("/")',result)
        self.assertIn('def index(): return "OK"',result)
        self.assertEqual(code.count('\n'),result.count('\n'))

    def test_structured_call_outcome_and_supplied_evidence(self):
        events=[];engine=mock.Mock(model='sonnet',kind='claude')
        engine.generate_plain.return_value='Reading the file.\n{"status":"read_more","files":["backend/app.py"]}'
        T.call(engine,'Policy\nEvidence JSON:\n{"requestedFiles":[{"path":"README.md","text":"excerpt"}]}',str,events.append)
        self.assertEqual(['README.md'],events[0]['suppliedFiles'])
        self.assertEqual('read_more',events[-1]['outcome'])
        self.assertEqual(['backend/app.py'],events[-1]['requestedFiles'])
