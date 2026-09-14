import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from human_compact.trajectory import project_runtime as RT, project_run as R, project_setup as S
from test_project_setup import SetupTests

class RuntimeTests(SetupTests):
    def managed_plan(self):
        plan=self.plan()
        plan['pythonRuntimes']=[{'cwd':'.','version':f'{sys.version_info.major}.{sys.version_info.minor}'}]
        return plan

    def test_existing_interpreter_uses_fresh_managed_environment(self):
        plan=self.managed_plan();id=self.retained()
        marker=self.root/'.venv';marker.mkdir();(marker/'keep').write_text('unchanged')
        with mock.patch.object(RT,'find',return_value=sys.executable),mock.patch.object(S,'propose',return_value=plan):
            R.start(id)
            result=self.wait(id)
            self.assertEqual('running',result['status'],result)
            self.assertTrue((marker/'keep').exists())
            self.assertIn('project-environments',result['command'])
            self.assertNotIn('approval',result)
            self.assertEqual('.'.join(map(str,sys.version_info[:3])),result['attempts'][0]['executedRuntimes'][0]['actualVersion'])
            R.reset(id)

    def pending(self):
        plan=self.managed_plan();id=self.retained()
        with mock.patch.object(RT,'find',return_value=None),mock.patch.object(RT,'download_command',return_value=['uv','python','install','3.11','--no-bin','--no-registry']),mock.patch.object(S,'propose',return_value=plan):
            R.start(id)
            end=time.monotonic()+5
            while time.monotonic()<end:
                state=R.view(id)
                if state['status']=='awaiting_approval':return id,state
                time.sleep(.03)
        self.fail(str(state))

    def test_approval_survives_reload_and_decline_preserves_failure(self):
        id,state=self.pending();token=state['approval']['id']
        R._JOBS.pop(id)
        self.assertEqual('awaiting_approval',R.view(id)['status'])
        with mock.patch.object(R,'recover') as recover:
            result=R.decide_approval(id,token,False)
            self.assertEqual('needs_input',result['status']);recover.assert_not_called()
            self.assertEqual(state['failures'],result['failures'])
            R.decide_approval(id,token,True);recover.assert_not_called()

    def test_approve_once_and_no_extra_model_call(self):
        id,state=self.pending();token=state['approval']['id']
        with mock.patch.object(R,'recover') as recover:
            with self.assertRaises(ValueError):R.decide_approval(id,'outdated',True)
            R.decide_approval(id,token,True);R.decide_approval(id,token,True)
            end=time.monotonic()+2
            while not recover.called and time.monotonic()<end:time.sleep(.01)
            self.assertEqual(1,recover.call_count)
            self.assertEqual(state['approval']['proposal'],recover.call_args.kwargs['resume'])

    def test_approved_plan_executes_without_new_inference_after_reload(self):
        id,state=self.pending();R._JOBS.pop(id)
        with mock.patch.object(RT,'find',return_value=sys.executable),mock.patch.object(S,'propose',side_effect=AssertionError('No new model call')):
            R.decide_approval(id,state['approval']['id'],True)
            result=self.wait(id)
            self.assertEqual('running',result['status'],result)
            self.assertEqual('approved',result['approval']['status'])
            self.assertEqual(1,len(result['attempts']))
            R.reset(id)

    def test_approved_download_then_create_and_run_through_supervisor(self):
        id,state=self.pending()
        marker=Path(self.temp.name)/'downloaded'
        downloader=Path(self.temp.name)/'download.py'
        downloader.write_text("import os\nfrom pathlib import Path\nassert os.environ['UV_PYTHON_DOWNLOADS']=='automatic'\nassert 'ANTHROPIC_API_KEY' not in os.environ\nPath("+repr(str(marker))+").write_text('downloaded')\n")
        with mock.patch.object(RT,'find',side_effect=lambda version:sys.executable if marker.exists() else None),mock.patch.object(RT,'download_command',return_value=[sys.executable,str(downloader)]),mock.patch.object(S,'propose',side_effect=AssertionError('No inference')):
            R.decide_approval(id,state['approval']['id'],True)
            result=self.wait(id)
            self.assertEqual('running',result['status'],result)
            self.assertTrue(marker.exists())
            self.assertEqual('done',next(s for s in result['stages'] if s['stage']=='runtime-download-0')['status'])
            R.reset(id)

    def test_reset_after_reload_revokes_approval(self):
        id,state=self.pending();R._JOBS.pop(id);R.reset(id)
        self.assertEqual('cancelled',R.view(id)['approval']['status'])
        with mock.patch.object(R,'recover') as recover:
            R.decide_approval(id,state['approval']['id'],True)
            recover.assert_not_called()

    def test_reset_revokes_pending_approval(self):
        id,state=self.pending();R.reset(id)
        with self.assertRaises(ValueError):R.decide_approval(id,state['approval']['id'],True)

    def test_bad_runtime_requests_rejected(self):
        for request in ({'cwd':'..','version':'3.11'},{'cwd':'.','version':'3.11;rm'}, {'cwd':'.','version':'3.11','command':'anything'}):
            plan=self.plan();plan['pythonRuntimes']=[request]
            with self.assertRaises(ValueError):S.validate(self.root,plan)

    def test_tools_do_not_inherit_credentials_or_override_defaults(self):
        with mock.patch.dict(os.environ,{'ANTHROPIC_API_KEY':'secret','UV_PYTHON_INSTALL_DIR':'/bad','UV_PYTHON_MIRROR':'https://bad'}):
            env=RT.tool_env()
        self.assertNotIn('ANTHROPIC_API_KEY',env);self.assertNotIn('UV_PYTHON_MIRROR',env)
        self.assertTrue(env['UV_PYTHON_INSTALL_DIR'].startswith(str(RT.home())))
        with mock.patch.object(RT.shutil,'which',return_value='/tools/uv'):
            command=RT.download_command('3.11')
        self.assertIn('--no-bin',command);self.assertIn('--no-registry',command);self.assertNotIn('--default',command)
