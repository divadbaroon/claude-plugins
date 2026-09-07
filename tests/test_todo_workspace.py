"""Single-TODO contracts, clean communication and bounded Preview supervision."""
import contextlib
import http.server
import json
import os
import threading
import unittest
from unittest import mock
from test_agents import AgentCase, ROWS, PIECE, Recorder, _answer
from human_compact.trajectory import build, preview, chat_state as CS
from human_compact.trajectory.agents import acceptance as A, artifacts as ART, runtime as RT, presentation as P, communication as C


@contextlib.contextmanager
def page_server(directory):
    requests=[]
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(directory),**kwargs)
        def log_message(self,fmt,*args): requests.append(fmt % args)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try: yield 'http://127.0.0.1:%s/' % server.server_port,requests
    finally: server.shutdown();server.server_close();thread.join(5)


def contract(checks, prose='Both labeled textboxes populate with the expected values.'):
    return {'criterion':prose,'coverage':'complete','checks':checks}


def value(name,match='equals',value='instruction',steps=None):
    return {'kind':'control_value','role':'textbox','name':name,'match':match,'value':value,'steps':steps or []}


class TodoContracts(AgentCase):
    def test_one_concrete_row_is_quick_without_magic_ui_keywords(self):
        with mock.patch.dict(os.environ,{'HC_BUILD_LANE':'auto'}):
            self.assertTrue(build.prefer_quick([{'text':'Write a sample therapist instruction and matching software files.'}]))
            for text in ['Fix it','Rewrite the entire project','Add authentication','Delete the database','Explore possible research directions']:
                self.assertFalse(build.prefer_quick([{'text':text}]),text)

    def test_greeting_cannot_propose_or_resume_background_work(self):
        for greeting in ['hello','thanks','cool']:
            chat=Recorder(_answer('Hello! g11 is ready.',todos=['Write the file','Another task']))
            result=self.orchestrator(chat=chat).bart_message(self.held(),[{'role':'you','text':greeting}])
            self.assertEqual(1,len(result['replies']))
            self.assertEqual('text',result['replies'][0]['kind'])
            self.assertNotIn('g11',result['replies'][0]['text'])
            self.assertEqual([],self.runtime.builds)

    def test_next_step_allows_unique_proposals_and_hides_duplicates(self):
        chat=Recorder(_answer('Try this.',todos=['Write the file!','Write something new','Write something new.']))
        result=self.orchestrator(chat=chat).bart_message(self.held(),[{'role':'you','text':'What should I work on next?'}])
        proposals=[r['text'] for r in result['replies'] if r['kind']=='proposal']
        self.assertEqual(['Write something new'],proposals)

    def test_prose_has_no_ids_or_midword_cutoff(self):
        text=P.text('The g11 todo t0d6b928a is ready. '+('A complete sentence. '*100),100)
        self.assertNotIn('g11',text);self.assertNotIn('t0d6b928a',text)
        self.assertLessEqual(len(text),100);self.assertTrue(text.endswith('.'))
        self.assertEqual('one two…',P.text('one two extraordinarilylongword',12))

    def test_lifecycle_channel_and_long_ids_survive_stale_saves(self):
        ident='m-'+('a'*60)
        CS.save_bart_chat(self.session,PIECE,[{'id':ident,'who':'you','kind':'text','text':'hello','createdAt':'2026-09-07T00:00:00Z'}],self.root)
        C.publish(self.session,self.root,PIECE,'repair','The check found a problem. I am fixing it.')
        before=CS.load_bart_chats(self.session,self.root)[PIECE]
        self.assertEqual(ident,before[0]['id']);self.assertEqual('lifecycle',before[1]['channel'])
        for _ in range(3): CS.save_bart_chat(self.session,PIECE,before[:1],self.root,merge=True)
        self.assertEqual(2,len(CS.load_bart_chats(self.session,self.root)[PIECE]))

    def test_legacy_acceptance_is_upgraded_once_before_build(self):
        goals, important = CS.load_goals(self.session, self.root)
        from human_compact.trajectory import goals as GM
        rows = GM.by_id(goals, PIECE)['todo_items']
        for row in rows:
            row['acceptance'] = {'criterion':'Two empty textboxes', 'checks':[{'kind':'text','text':'Therapist Instruction'}]}
        CS.save_goals(self.session, goals, important, self.root)
        upgraded=contract([value('Therapist Instruction','empty'),value('Generated Software','empty')], 'Two empty textboxes')
        with mock.patch.object(A,'derive',return_value={rid:upgraded for rid in ROWS}) as derive:
            first=A.ensure(self.session,self.root,PIECE,ROWS)
            self.assertEqual(first,A.ensure(self.session,self.root,PIECE,ROWS))
            derive.assert_called_once()
            self.assertTrue(all(A.checks_cover(c) for c in first.values()))

    def test_empty_contains_is_invalid_but_explicit_file_checks_work(self):
        self.assertIsNone(A.normalize(contract([{'kind':'file','path':'software.txt','contains':''}])))
        self.assertIsNone(A.normalize(contract([value('Software','equals','x'*4001)])))
        self.assertIsNone(A.normalize(contract([{'kind':'file','path':'software.txt','contains':'  '}])))
        (self.project/'software.txt').write_text('matching software')
        runtime=RT.LocalRuntime(str(self.project),self.root)
        for kind in ['file_exists','file_nonempty','file']:
            check={'kind':kind,'path':'software.txt'}
            if kind=='file':check['contains']='matching'
            verdict=ART.verify(runtime,{'row':contract([check],'software file is available')},{})
            self.assertTrue(verdict['passed'],verdict)
        (self.project/'software.txt').write_text('')
        self.assertFalse(ART.verify(runtime,{'row':contract([{'kind':'file_nonempty','path':'software.txt'}],'software is not empty')},{})['passed'])

    def test_weak_labels_do_not_establish_empty_side_by_side_textboxes(self):
        weak=contract([{'kind':'control','role':'textbox','name':name} for name in ['Therapist Instruction','Generated Software']],
                      'Two empty textboxes side by side')
        self.assertFalse(A.checks_cover(A.normalize(weak)))
        legacy={'criterion':'an unsupported property','checks':[{'kind':'file_exists','path':'software.txt'}]}
        (self.project/'software.txt').write_text('data')
        model=mock.Mock();model.generate_json.return_value={'passed':True,'evidence':[{'todoId':'different-row','passed':True}]}
        result=ART.verify(RT.LocalRuntime(str(self.project),self.root),{'row':legacy},{},model)
        self.assertFalse(result['passed']);model.generate_json.assert_called_once()

    def test_terminal_keeps_concise_checks_and_private_evidence_stays_private(self):
        evidence={'rows':{ROWS[0]:'done'},'acceptance':{ROWS[0]:{'criterion':'raw contract'}},'artifact':{
            'page':{'checks':[{'expected':value('Therapist Instruction'),'passed':True}, {'expected':value('Generated Software'),'passed':False}]},
            'files':[{'path':'software.txt','expected':{'kind':'file_nonempty','path':'software.txt'},'passed':True}]}}
        C.evidence_to_terminal(self.session,self.root,PIECE,evidence)
        text='\n'.join(l['text'] for l in C.terminal_lines(build.load_activity(self.session,self.root,PIECE)))
        self.assertIn('✓ Therapist Instruction',text);self.assertIn('✗ Generated Software',text)
        self.assertNotIn(ROWS[0],text);self.assertNotIn('acceptance',text);self.assertNotIn('{',text)
        lines=C.terminal_lines([{'kind':'error','text':'app.js failed to import library'}, {'kind':'verify','text':'check evidence: {raw}'}, {'kind':'verify','text':'repair 2 of 3: g11 has failed'}])
        self.assertEqual(2,len(lines));self.assertIn('app.js',lines[0]['text']);self.assertNotIn('g11',lines[1]['text'])

    def test_healthy_preview_backs_off_http_but_detects_exit(self):
        proc=preview.Proc(str(self.project),{},None);proc.url='http://127.0.0.1:8765/'
        proc.process=mock.Mock();proc.process.poll.return_value=None
        response=mock.MagicMock();response.__enter__.return_value.headers={}
        with mock.patch.object(preview.time,'time',return_value=100) as now, mock.patch.object(preview.urllib.request,'urlopen',return_value=response) as http, mock.patch('socket.create_connection',return_value=mock.MagicMock()):
            proc.probe();self.assertTrue(proc.healthy);self.assertEqual('HEAD',http.call_args.args[0].method)
            for second in range(102,160,2):now.return_value=second;proc.snapshot()
            self.assertEqual(1,http.call_count)
            now.return_value=161;proc.probe();self.assertEqual(2,http.call_count)
            proc.process.poll.return_value=1;now.return_value=162;proc.snapshot();self.assertFalse(proc.healthy)

    def test_starting_preview_probes_and_socket_loss_is_detected(self):
        proc=preview.Proc(str(self.project),{},None);proc.url='http://127.0.0.1:8765/'
        response=mock.MagicMock();response.__enter__.return_value.headers={}
        with mock.patch.object(preview.time,'time',return_value=100) as now, mock.patch.object(preview.urllib.request,'urlopen',side_effect=[OSError('starting'),response]) as http:
            proc.probe();now.return_value=101;proc.probe();self.assertEqual(1,http.call_count)
            now.return_value=103;proc.probe();self.assertTrue(proc.healthy)
            with mock.patch('socket.create_connection',side_effect=OSError('lost listener')):
                now.return_value=106;proc.probe();self.assertFalse(proc.healthy)

    def test_health_output_filter_preserves_real_app_errors(self):
        proc=preview.Proc(str(self.project),{},None)
        for line in ['GET / Engelbart-Preview-Health/1','127.0.0.1 "GET /favicon.ico HTTP/1.1" 404 -','GET /important HTTP/1.1 404','user clicked Load Example','Traceback: application failure']:
            proc._keep(line)
        self.assertEqual(['GET /important HTTP/1.1 404','user clicked Load Example','Traceback: application failure'],list(proc.lines))


class TextboxBrowserContracts(AgentCase):
    def setUp(self):
        super().setUp()
        try: import playwright.sync_api
        except ImportError:self.skipTest('Covered in browser-enabled release job')

    def test_textarea_values_empty_layout_and_both_sides_of_load(self):
        (self.project/'index.html').write_text('''<style>main{display:flex;gap:12px}</style><main>
<label>Therapist Instruction<textarea id="left"></textarea></label>
<label>Generated Software<textarea id="right"></textarea></label></main>
<button onclick="document.querySelector('#left').value='Raise your right arm';document.querySelector('#right').value='arm.raise()'">Load Example</button>''')
        empty=[value(n,'empty') for n in ['Therapist Instruction','Generated Software']]
        layout={'kind':'layout','relation':'side_by_side','controls':[{'role':'textbox','name':n} for n in ['Therapist Instruction','Generated Software']]}
        steps=[{'action':'click','role':'button','name':'Load Example'}]
        checks=[value('Therapist Instruction','contains','Raise your right arm',steps),value('Generated Software','equals','arm.raise()',steps)]
        with page_server(self.project) as (url,requests):
            self.assertTrue(ART.inspect_page(url,A.normalize(contract(empty+[layout],'Two empty textboxes side by side'))['checks'])['passed'])
            result=ART.verify(RT.LocalRuntime(str(self.project),self.root),{'row':contract(checks)},{'url':url})
            self.assertTrue(result['passed'],result)
            (self.project/'instruction.txt').write_text('Raise your right arm')
            (self.project/'software.txt').write_text('arm.raise()')
            file_checks=[dict(c,from_file=f) for c,f in zip(checks,['instruction.txt','software.txt'])]
            for check in file_checks: check.pop('value')
            self.assertTrue(ART.verify(RT.LocalRuntime(str(self.project),self.root),{'row':contract(file_checks)},{'url':url})['passed'])
            (self.project/'software.txt').write_text('different software')
            self.assertFalse(ART.verify(RT.LocalRuntime(str(self.project),self.root),{'row':contract(file_checks)},{'url':url})['passed'])
            checks[1]['value']='wrong software'
            failed=ART.inspect_page(url,A.normalize(contract(checks))['checks'])
            self.assertFalse(failed['passed']);self.assertTrue(failed['checks'][0]['passed']);self.assertFalse(failed['checks'][1]['passed'])
        self.assertIn('<textarea',(self.project/'index.html').read_text());self.assertNotIn('<pre>',(self.project/'index.html').read_text())
