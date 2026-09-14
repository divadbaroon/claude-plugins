import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from human_compact.trajectory import project_compatibility as C, project_run as R, project_setup as S, project_runtime as RT, preview as PV
from test_project_setup import SetupTests


def release(version='cp311',requires=None,source_only=False):
    return {'info':{'requires_python':requires},'urls':[] if source_only else [
        {'packagetype':'bdist_wheel','filename':f'example-1.0-{version}-{version}-macosx_11_0_arm64.whl'}]}

class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.backend=self.root/'backend';self.backend.mkdir()
        (self.backend/'requirements.txt').write_text('example==1.0\n')
    def assess(self,lookup=lambda *args:release(),preferred='3.14'):
        with mock.patch.object(C,'sys_tags',return_value=[mock.Mock(platform='macosx_11_0_arm64')]):
            return C.assess(self.root,self.backend,preferred,['3.12','3.14'],lookup=lookup)
    def test_wheel_coverage_beats_installed_runtime_and_does_not_claim_incompatibility(self):
        result=self.assess()
        self.assertEqual('3.11',result['recommendedVersion'])
        candidate=next(c for c in result['candidates'] if c['version']=='3.14')
        self.assertEqual('partial',candidate['status']);self.assertEqual(1,len(candidate['wheelGaps']))
    def test_minor_constraint_and_actual_patch_are_checked_separately(self):
        self.assertTrue(C.allows('>3.12.2,<3.12.5','3.12'))
        with self.assertRaisesRegex(ValueError,'violates'):
            C.verify_actual({'constraint':'>=3.12.5'},'3.12.1')
        C.verify_actual({'constraint':'>=3.12.5'},'3.12.6')

    def test_requires_python_excludes_candidate(self):
        (self.backend/'pyproject.toml').write_text('[project]\nrequires-python=">=3.10,<3.12"\n')
        result=self.assess()
        self.assertEqual('excluded',result['candidates'][0]['status'])
        self.assertEqual('3.11',result['recommendedVersion'])
    def test_exact_declaration_is_not_overridden_by_wheel_coverage(self):
        (self.backend/'.python-version').write_text('3.12.5\n')
        result=self.assess()
        self.assertEqual('3.12.5',result['recommendedVersion'])
    def test_release_requires_python_exclusion(self):
        result=self.assess(lambda *args:release('cp311','<3.12'))
        self.assertEqual('excluded',result['candidates'][0]['status'])
    def test_unavailable_metadata_and_source_only_are_unknown(self):
        for lookup in (lambda *args:None,lambda *args:release(source_only=True)):
            result=self.assess(lookup)
            self.assertEqual('3.14',result['recommendedVersion'])
            self.assertTrue(all(c['unknown'] for c in result['candidates']))
            self.assertTrue(all(c['status']!='excluded' for c in result['candidates']))
    def test_dependency_markers_use_candidate_version(self):
        (self.backend/'requirements.txt').write_text('example==1.0; python_version < "3.12"\n')
        result=self.assess()
        self.assertEqual([],result['candidates'][0]['wheelGaps'])
    def test_nested_requirements_are_fingerprinted_and_escape_rejected(self):
        (self.backend/'requirements.txt').write_text('-r base.txt\n')
        (self.backend/'base.txt').write_text('example==1.0\n')
        first=C.snapshot(self.root,self.backend)
        (self.backend/'base.txt').write_text('example==2.0\n')
        self.assertNotEqual(first['fingerprint'],C.snapshot(self.root,self.backend)['fingerprint'])
        (self.backend/'requirements.txt').write_text('-r ../../outside.txt\n')
        with self.assertRaises(ValueError):C.snapshot(self.root,self.backend)
    def test_labels_and_unrelated_changes_cannot_repeat_failed_install(self):
        step={'id':'deps','cwd':str(self.backend),'argv':['.venv/bin/python','-m','pip','install','-r','requirements.txt'],'env':{}}
        requests=[{'cwd':str(self.backend),'version':'3.12'}]
        failed=C.configuration(self.root,step,requests)
        previous=[{'failure':{'configuration':failed,'exitCode':1}}]
        renamed={**step,'id':'different-label'}
        plan={'preparation':[renamed],'services':[]}
        (self.root/'frontend').mkdir();(self.root/'frontend'/'package.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'failed component'):C.validate_repair(self.root,plan,requests,previous)
        alias={**renamed,'argv':['python3']+renamed['argv'][1:]}
        with self.assertRaises(ValueError):C.validate_repair(self.root,{'preparation':[alias],'services':[]},requests,previous)
        different=[{'cwd':str(self.backend),'version':'3.11'}]
        self.assertTrue(C.validate_repair(self.root,plan,different,previous))
        (self.backend/'requirements.txt').write_text('example==2.0\n')
        self.assertTrue(C.validate_repair(self.root,plan,requests,previous))
    def test_adding_a_service_dependency_is_a_relevant_change(self):
        backend={'id':'backend','cwd':str(self.backend),'argv':['npm','start'],'dependsOn':[]}
        database={'id':'database','cwd':str(self.backend),'argv':['npm','run','database']}
        previous=[{'failure':{'configuration':C.configuration(self.root,backend,[],[backend,database]),'exitCode':1}}]
        repaired={**backend,'dependsOn':['database']}
        C.validate_repair(self.root,{'preparation':[],'services':[database,repaired]},[],previous)

    def test_minor_and_patch_alias_cannot_bypass_guard(self):
        step={'cwd':str(self.backend),'argv':['python3','-m','pip','install','-r','requirements.txt']}
        prior=[{'failure':{'configuration':C.configuration(self.root,step,[{'cwd':str(self.backend),'version':'3.12'}])}}]
        with self.assertRaises(ValueError):C.validate_repair(self.root,{'preparation':[step],'services':[]},[{'cwd':str(self.backend),'version':'3.12.5'}],prior)

class CompatibilityExecutionTests(SetupTests):
    def test_excluded_runtime_never_creates_environment_or_installs(self):
        (self.root/'pyproject.toml').write_text('[project]\nrequires-python="<3.12"\n')
        plan=self.plan();plan['pythonRuntimes']=[{'cwd':'.','version':'3.14'}]
        with mock.patch.object(S,'propose',side_effect=[plan,{'status':'needs_input','reason':'Select compatible runtime'}]),mock.patch.object(RT,'inventory',return_value={'python':[]}),mock.patch.object(PV,'start_plan_process') as launch:
            id=self.retained();R.start(id);result=self.wait(id)
            self.assertEqual('needs_input',result['status'],result);launch.assert_not_called()
            self.assertEqual('excluded',result['attempts'][0]['compatibility'][0]['candidates'][0]['status'])
    def test_initial_selection_requests_download_before_execution(self):
        (self.root/'requirements.txt').write_text('example==1.0\n')
        plan=self.plan();plan['preparation']=[{'id':'venv','cwd':'.','argv':['python3','-m','venv','.venv']}]
        id=self.retained();record=R.read(id);record['orderPlan']=plan;R.write(id,record)
        def assessment(root,cwd,preferred,installed):
            with mock.patch.object(C,'sys_tags',return_value=[mock.Mock(platform='macosx_11_0_arm64')]):
                return original(root,cwd,preferred,installed,lookup=lambda *args:release())
        original=C.assess
        with mock.patch.object(C,'assess',side_effect=assessment),mock.patch.object(RT,'initial_requests',return_value=[{'cwd':str(self.root.resolve()),'version':'3.14'}]),mock.patch.object(RT,'inventory',return_value={'python':[{'version':'3.14'}]}),mock.patch.object(RT,'find',return_value=None),mock.patch.object(RT,'download_command',return_value=['uv','python','install','3.11','--no-bin','--no-registry']),mock.patch.object(PV,'start_plan_process') as launch:
            R.start(id)
            import time
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                result=R.view(id)
                if result['status'] in ('awaiting_approval','failed'):break
                time.sleep(.03)
            self.assertEqual('awaiting_approval',result['status'],result)
            self.assertEqual(['3.11'],result['approval']['downloadVersions'])
            launch.assert_not_called()

    def test_renamed_failed_service_is_not_executed_again(self):
        first=self.plan(fail=True);second=copy.deepcopy(first)
        second['services'][0]['id']='renamed';second['services'][1]['dependsOn']=['renamed']
        with mock.patch.object(S,'propose',side_effect=[first]+[second]*(S.MAX_ATTEMPTS-1)),mock.patch.object(PV,'start_plan_process',wraps=PV.start_plan_process) as launch:
            id=self.retained();R.start(id);result=self.wait(id)
            self.assertEqual('failed',result['status']);self.assertEqual(1,launch.call_count)
            self.assertIn('failed component configuration',result['reason'])

class NativeBuildRepairTests(CompatibilityTests):
    def test_runtime_switch_must_improve_native_build_evidence(self):
        old=self.assess();new=self.assess(preferred='3.12')
        step={'cwd':str(self.backend),'argv':['.venv/bin/python','-m','pip','install','-r','requirements.txt']}
        plan={'preparation':[step],'services':[]}
        prior=[{'failure':{'configuration':C.configuration(self.root,step,[{'cwd':str(self.backend),'version':'3.14'}]),
                           'stdout':'Failed to build example','stderr':'cpython: error: missing member',
                           'compatibility':[old],'exitCode':1}}]
        with self.assertRaisesRegex(ValueError,'no demonstrated improvement'):
            C.validate_repair(self.root,plan,[{'cwd':str(self.backend),'version':'3.12'}],prior,[new])
        C.validate_repair(self.root,plan,[{'cwd':str(self.backend),'version':'3.11'}],prior,[new])
    def test_removing_failed_component_is_not_a_repair(self):
        step={'cwd':str(self.backend),'argv':['npm','install']}
        prior=[{'failure':{'configuration':C.configuration(self.root,step,[])}}]
        with self.assertRaisesRegex(ValueError,'removes the failed component'):
            C.validate_repair(self.root,{'preparation':[],'services':[]},[],prior)
