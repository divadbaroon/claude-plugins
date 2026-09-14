import unittest
from human_compact.trajectory.project_env_js import scan

class ClientContractTests(unittest.TestCase):
    def requirements(self,source):
        return {name:requirement for name,_,_,requirement in scan(source)[0]}

    def test_aliases_generics_and_bracket_access(self):
        source="""import { createBrowserClient, createServerClient as server } from '@supabase/ssr';
        import {createClient as service} from '@supabase/supabase-js';
        createBrowserClient<Database>(process.env.URL!,process.env.ANON!);
        server(process.env.URL,process.env['OTHER_KEY']);
        service<Database>(import.meta.env.URL,import.meta.env.SERVICE,{unused:process.env.EXTRA});"""
        r=self.requirements(source)
        for name in ('URL','ANON','OTHER_KEY','SERVICE'):self.assertEqual(r[name],'required')
        self.assertEqual(r['EXTRA'],'unknown')

    def test_non_null_assertions_and_unrelated_functions_are_not_contracts(self):
        r=self.requirements("import { createClient } from 'unrelated'; createClient(process.env.URL!,process.env.KEY!); const x=process.env.ASSERTED!;")
        self.assertTrue(all(v=='unknown' for v in r.values()))

    def test_fallbacks_and_shadowing_not_promoted(self):
        r=self.requirements("import {createClient} from '@supabase/supabase-js'; createClient(process.env.URL || 'https://default.example',process.env.KEY ?? 'default');")
        self.assertEqual(r,{'URL':'optional','KEY':'optional'})
        r=self.requirements("import {createClient} from '@supabase/supabase-js'; function test(createClient){return createClient(process.env.URL,process.env.KEY);}")
        self.assertEqual(r,{'URL':'unknown','KEY':'unknown'})
