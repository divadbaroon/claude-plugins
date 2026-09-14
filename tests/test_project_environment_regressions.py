import json
import unittest
from test_project_environment import EnvironmentTests
from human_compact.trajectory import project_environment as E
from human_compact.trajectory.project_env_js import scan

class ExtractionTests(unittest.TestCase):
    def test_literals_comments_regex_multiline_and_destructuring(self):
        source='''const fake="process.env.STRING_FAKE";
/* process.env.COMMENT_FAKE */
const re=/process.env.REGEX_FAKE/;
const multi=process\n.env\n.MULTILINE;
const { OPTIONAL="fallback", OTHER: alias }=process.env;
const vite=import.meta.env["VITE_URL"];
'''
        rows={r[0]:r for r in scan(source)[0]}
        self.assertEqual({'MULTILINE','OPTIONAL','OTHER','VITE_URL'},set(rows))
        self.assertEqual('optional',rows['OPTIONAL'][3])
    def test_bounded_helper_and_defaults(self):
        text='''function read(name: string) { return import.meta.env[name]; }
const url=read("DYNAMIC_URL");
const port=process.env.PORT || "3000";
const token=process.env.TOKEN;
if (!token) throw new Error("missing");'''
        rows={r[0]:r for r in scan(text)[0]}
        self.assertEqual({'DYNAMIC_URL','PORT','TOKEN'},set(rows))
        self.assertEqual('required',rows['TOKEN'][3]);self.assertEqual('optional',rows['PORT'][3])
        self.assertTrue(scan(text)[1])
    def test_regex_quotes_and_spread_helper_call(self):
        text="""function setting(name: string): string[] {
          const value=import.meta.env[name];
          return value.split(',').map(v=>v.replace(/^['\"]|['\"]$/g,''));
        }
        const urls=[...setting('CALLBACKS')];
        const domain=import.meta.env.DOMAIN;
        """
        self.assertEqual({'CALLBACKS','DOMAIN'},{r[0] for r in scan(text)[0]})
    def test_mutated_parameter_does_not_resolve(self):
        text='function read(name) { name="OTHER"; return process.env[name]; } read("WRONG");'
        self.assertNotIn('WRONG',{r[0] for r in scan(text)[0]})
    def test_deploy_guard_and_echo_are_not_assignments(self):
        text='''[[ -z "${SITE_BUCKET:-}" ]] && missing+=("SITE_BUCKET")
echo "export SITE_BUCKET=example"
LOCAL=example
printf '%s' "${LOCAL:-fallback}"
'''
        self.assertEqual([('SITE_BUCKET',1,'required')],E.shell_inputs(text))

class InventoryTests(EnvironmentTests):
    def test_inventory_includes_nested_without_claiming_values_ready(self):
        self.write('nested/package.json','{}');self.write('nested/app.ts','process.env.NESTED')
        self.write('.env','NESTED=not-a-component-value')
        scoped=E.scan(str(self.root));inventory=E.scan(str(self.root),include_nested=True)
        self.assertEqual([],scoped['variables']);self.assertTrue(inventory['inventoryOnly'])
        self.assertEqual('NESTED',inventory['variables'][0]['name'])
        self.assertEqual('inventory',inventory['variables'][0]['status'])
        self.assertNotIn('not-a-component-value',str(inventory))
    def test_python_required_and_fallback(self):
        self.write('app.py','import os\na=os.environ["REQUIRED"]\nb=os.getenv("OPTIONAL", "fallback")')
        rows={r['name']:r for r in E.scan(str(self.root))['variables']}
        self.assertEqual('missing',rows['REQUIRED']['status']);self.assertEqual('optional',rows['OPTIONAL']['status'])

class RepositoryContextTests(EnvironmentTests):
    def test_component_check_searches_git_root_and_links_example_without_values(self):
        (self.root/'.git').mkdir()
        self.write('.env.example','VITE_VIEWER=example-sensitive\nADMIN_ONLY=another-sensitive\n')
        self.write('apps/viewer/package.json','{"dependencies":{"vite":"7"}}')
        self.write('apps/viewer/app.ts','import.meta.env.VITE_VIEWER')
        self.write('apps/admin/package.json','{}')
        self.write('apps/admin/app.ts','process.env.ADMIN_ONLY')
        r=E.scan(str(self.root/'apps/viewer'))
        self.assertEqual(['VITE_VIEWER'],[x['name'] for x in r['variables']])
        self.assertEqual({'VITE_VIEWER','ADMIN_ONLY'},{x['name'] for x in r['repositoryInventory']})
        self.assertEqual(['.env.example'],r['repositoryExamples'])
        self.assertTrue(any(e.get('scope')=='repository-example' for e in r['variables'][0]['evidence']))
        self.assertNotIn('sensitive',json.dumps(r))
    def test_explicit_root_without_git_and_invalid_boundary(self):
        self.write('.env.example','ROOT_ONLY=\n')
        self.write('app/package.json','{}')
        r=E.scan(str(self.root/'app'),repository_root=str(self.root))
        self.assertEqual('ROOT_ONLY',r['repositoryInventory'][0]['name'])
        with self.assertRaises(ValueError):E.scan(str(self.root),repository_root=str(self.root/'app'))

class GroupingTests(EnvironmentTests):
    def test_full_inventory_groups_and_continuation(self):
        (self.root/'.git').mkdir()
        self.write('apps/viewer/package.json','{"scripts":{"dev":"vite"}}')
        self.write('apps/viewer/app.ts', 'const base=import.meta.env.VITE_URL; if (!base) throw Error("missing"); const port=process.env.PORT || "3000";')
        self.write('apps/viewer/deploy_site.sh', ': "${DEPLOY_TOKEN:?required}"')
        self.write('apps/admin/package.json','{}')
        self.write('apps/admin/app.ts','process.env.ADMIN_TOKEN')
        self.write('packages/shared/package.json','{}')
        self.write('packages/shared/app.ts','process.env.SHARED_TOKEN')
        r=E.scan(str(self.root/'apps/viewer'))
        rows={v['name']:v for v in r['displayVariables']}
        self.assertEqual('required',rows['VITE_URL']['group']);self.assertTrue(rows['VITE_URL']['blocksContinuation'])
        self.assertEqual('optional',rows['PORT']['group'])
        self.assertEqual('other',rows['DEPLOY_TOKEN']['group']);self.assertFalse(rows['DEPLOY_TOKEN']['blocksContinuation'])
        self.assertEqual('other',rows['ADMIN_TOKEN']['group']);self.assertFalse(rows['ADMIN_TOKEN']['editable'])
        self.assertEqual('unresolved',rows['SHARED_TOKEN']['group'])
        E.save(str(self.root/'apps/viewer'),{'VITE_URL':'https://example.test'})
        r=E.scan(str(self.root/'apps/viewer'))
        self.assertTrue(all(not v['blocksContinuation'] for v in r['variables']))
    def test_deployment_script_used_by_start_stays_unresolved(self):
        self.write('package.json','{"scripts":{"start":"bash deploy_site.sh"}}')
        self.write('deploy_site.sh', ': "${TOKEN:?required}"')
        row=E.scan(str(self.root))['variables'][0]
        self.assertEqual('unresolved',row['group']);self.assertTrue(row['blocksContinuation'])
