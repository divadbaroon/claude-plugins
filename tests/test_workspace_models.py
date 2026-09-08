"""Model choices persist through the existing machine settings and provider boundary."""
import os
from unittest import mock
from test_goal_page import BrowserCase, seed_design, bind_project, server_for, ChatCase
from human_compact.trajectory import build, setup_chat, preview, providers
from human_compact.trajectory.agents import chat

class ModelSettingsTests(ChatCase):
    def test_defaults_and_persisted_models_reach_preview_provider(self):
        with mock.patch.dict(os.environ, {'HUMAN_COMPACT_HOME': str(self.root), 'HC_BUILD_MODEL': ''}):
            self.assertEqual('opus', setup_chat.workspace_model(self.root))
            self.assertEqual('sonnet', setup_chat.workspace_model(self.root, 'preview'))
            self.assertEqual('sonnet', setup_chat.setup_model(self.root))  # Onboarding unchanged.
            with mock.patch.object(providers, 'make') as make:
                make.return_value.generate_json.return_value = {'say':'Hello','todos':[]}
                self.assertTrue(chat.ask([{'role':'user','text':'hello'}],root=self.root)['ok'])
                self.assertEqual('opus',make.call_args.args[2])
            saved = build.save_settings('chat', self.root, {'interface_model':'sonnet','model':'opus','quick_model':'opus'})
            self.assertTrue(saved['ok'])
            self.assertEqual('sonnet', setup_chat.workspace_model(self.root))
            self.assertEqual('opus', setup_chat.workspace_model(self.root, 'preview'))
            self.assertEqual('opus', build.load_settings('other-chat', self.root)['quick_model'])
            with mock.patch.object(providers, 'make') as make:
                preview._engine('synthesize', 30, root=self.root)
                self.assertEqual('opus', make.call_args.args[2])
            self.assertFalse(build.save_settings('chat',self.root,{'interface_model':'opus; shell'})['ok'])
            self.assertEqual('sonnet',build.load_settings('chat',self.root)['interface_model'])

class ModelSettingsBrowserTests(BrowserCase):
    def test_production_settings_save_both_roles_and_reload(self):
        seed_design(self.chat);bind_project(self)
        with mock.patch.object(build,'_cli_binary',return_value=None), server_for(self.chat) as url, self.page_on(url) as (page,errors):
            def menu():
                page.locator('.account-btn').click()
                page.get_by_role('menuitem',name='Models',exact=True).click()
            menu()
            interface=page.get_by_label('Engelbart model',exact=True)
            app=page.get_by_label('Live Preview model',exact=True)
            self.expect(interface).to_have_value('opus')
            self.expect(app).to_have_value('sonnet')
            self.expect(interface).to_be_enabled()
            interface.select_option('sonnet')
            self.expect(app).to_be_enabled()
            app.select_option('opus')
            self.expect(app).to_be_enabled()
            settings=build.load_settings('chat',self.root)
            self.assertEqual(('sonnet','opus','opus'),tuple(settings[k] for k in ('interface_model','model','quick_model')))
            page.reload();menu()
            self.expect(interface).to_have_value('sonnet');self.expect(app).to_have_value('opus')
            self.assertEqual([],errors)
