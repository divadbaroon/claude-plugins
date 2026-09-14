"""Real browser/controller/runner round trip; remote checkout alone is substituted.

The curated CSV supplies real metadata, but the two executed applications here
are disposable static fixtures. Their startup results are not corpus scores.
"""
import csv
import io
import json
import time
from pathlib import Path
from unittest import mock

from test_goal_page import BrowserCase, ROOT, server_for
from human_compact.trajectory import project_checkout, project_run


class BenchmarkBrowserTests(BrowserCase):
    def test_upload_filter_run_two_export_and_reload(self):
        corpus = ROOT / 'benchmarks/paper-repositories/corpus.csv'
        rows = list(csv.DictReader(io.StringIO(corpus.read_text(encoding='utf-8'))))
        selected_urls = [rows[0]['Git repo URL'], rows[1]['Git repo URL']]
        fixtures = {}
        for index, repo_url in enumerate(selected_urls):
            folder = self.root / ('static-fixture-' + str(index))
            folder.mkdir()
            (folder / 'index.html').write_text('<!doctype html><title>Benchmark fixture</title><p>Ready</p>')
            fixtures[repo_url] = folder

        def fixture_checkout(value):
            self.assertIn(value, fixtures, 'An unselected repository was launched')
            return {'path': str(fixtures[value]), 'source': project_checkout.identity(value)}

        prior_runs = set(project_run._JOBS)
        try:
            with mock.patch.object(project_checkout, 'resolve', side_effect=fixture_checkout) as checkout:
                with server_for(self.chat) as url, self.page_on(url) as (page, errors):
                    page.get_by_role('button', name='New Project', exact=True).click()
                    page.get_by_role('tab', name='Benchmark CSV', exact=True).click()
                    page.get_by_label('Upload benchmark CSV').set_input_files(str(corpus))
                    self.expect(page.locator('.project-benchmark tbody tr')).to_have_count(40)
                    self.assertEqual(checkout.call_count, 0)

                    page.get_by_label('Dependency', exact=True).select_option('python')
                    page.get_by_label('Artifact type', exact=True).select_option('system')
                    page.get_by_label('Select ' + selected_urls[0], exact=True).check()
                    page.get_by_role('searchbox', name='Search', exact=True).fill('hypocompass')
                    page.get_by_label('Select ' + selected_urls[1], exact=True).check()
                    self.expect(page.get_by_text('1 visible · 2 selected · 1 selected hidden by filters', exact=True)).to_be_visible()
                    page.get_by_role('button', name='Run selected (2)', exact=True).click()

                    # These commands run the real server-owned native static runner.
                    deadline = time.monotonic() + 30
                    report = None
                    while time.monotonic() < deadline:
                        page.get_by_role('button', name='Refresh outcomes', exact=True).click()
                        report = page.evaluate('window.engelbart.store.get().projectBenchmark.batch')
                        if report.get('summary', {}).get('counts', {}).get('healthy_startup') == 2:
                            break
                        page.wait_for_timeout(200)
                    self.assertEqual(report['summary']['counts']['healthy_startup'], 2, report)
                    self.assertEqual(checkout.call_count, 2)
                    self.assertEqual({call.args[0] for call in checkout.call_args_list}, set(selected_urls))

                    with page.expect_download() as download:
                        page.get_by_role('button', name='Download diagnostic JSON', exact=True).click()
                    exported = json.loads(Path(download.value.path()).read_text(encoding='utf-8'))
                    self.assertEqual(exported['summary']['denominator'], 2)
                    self.assertEqual({case['repoUrl'] for case in exported['cases']}, set(selected_urls))
                    self.assertTrue(all(case['events'] for case in exported['cases']))
                    self.assertIn('hc-static', json.dumps(exported))
                    batch_id = exported['id']

                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='New Project', exact=True).click()
                    page.get_by_role('tab', name='Benchmark CSV', exact=True).click()
                    self.expect(page.get_by_text('Batch ' + batch_id + ' · fixed denominator 2', exact=True)).to_be_visible()
                    self.expect(page.get_by_role('button', name='Run selected (2)', exact=True)).to_be_disabled()
                    self.assertEqual(checkout.call_count, 2)
                    self.assertEqual(errors, [])
        finally:
            for run_id in set(project_run._JOBS) - prior_runs:
                project_run.reset(run_id)
                project_run._JOBS.pop(run_id, None)
