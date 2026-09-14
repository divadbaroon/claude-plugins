import json
from pathlib import Path
import tempfile
import unittest
from human_compact.trajectory import project_components as C

class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
    def write(self,p,text):
        path=self.root/p;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    def test_hypocompass_relationships_not_start_dependencies(self):
        self.write('system/frontend/package.json',json.dumps({'scripts':{'start':'PORT=8080 react-scripts start','backend':'cd ../backend && .venv/bin/python base.py'},'proxy':'http://localhost:8090'}))
        self.write('system/backend/requirements.txt','flask\n')
        self.write('system/backend/base.py','app.run(host="0.0.0.0", port=8090)')
        self.write('system/README.md','Run backend first, then frontend.')
        self.write('system/frontend/node_modules/ignore/package.json','{}')
        r=C.discover(str(self.root))
        self.assertEqual(['system/backend','system/frontend'],[c['id'] for c in r['components']])
        self.assertEqual([],r['dependencies'])
        proxy=next(h for h in r['relationships'] if h['kind']=='functional-proxy')
        self.assertEqual(['system/backend'],proxy['dependencies'])
        self.assertEqual(['system/README.md'],r['documentation'])
    def test_compose_conditions_order_parallel_groups(self):
        self.write('compose.yaml','''services:
  db:
    image: postgres
  cache:
    image: redis
  api:
    image: api
    depends_on:
      db:
        condition: service_healthy
  web:
    image: web
    depends_on: [api]
''')
        r=C.discover(str(self.root));levels=r['order']['levels']
        self.assertEqual(['compose.yaml#cache','compose.yaml#db'],levels[0])
        self.assertEqual(['compose.yaml#api'],levels[1]);self.assertEqual(['compose.yaml#web'],levels[2])
        self.assertEqual('service_healthy',r['dependencies'][0]['condition'])
    def test_cycle_and_unresolved_dependency(self):
        nodes=[{'id':'a'},{'id':'b'},{'id':'c'}]
        graph=C.order(nodes,[{'service':'a','dependency':'b'},{'service':'b','dependency':'a'},{'service':'c','dependency':'missing'}])
        self.assertEqual(['a','b','c'],graph['blocked']);self.assertEqual([],graph['levels'])
    def test_symlink_and_unsupported_yaml_are_not_followed(self):
        self.write('outside/package.json','{}')
        (self.root/'link').symlink_to(self.root/'outside',target_is_directory=True)
        self.write('compose.yml','services: &a {x: *a}')
        r=C.discover(str(self.root))
        self.assertEqual(['outside'],[c['id'] for c in r['components']]);self.assertTrue(r['warnings'])
    def test_source_unchanged_and_single_root(self):
        self.write('package.json','{}')
        before=(self.root/'package.json').read_bytes();r=C.discover(str(self.root))
        self.assertEqual('.',r['components'][0]['id']);self.assertEqual(before,(self.root/'package.json').read_bytes())
