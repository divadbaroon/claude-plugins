"""Production credit-source menu uses the existing machine account boundary."""
from unittest import mock
from test_goal_page import BrowserCase, seed_design, bind_project, server_for
from human_compact import claude_account

class ApiCreditsBrowserTests(BrowserCase):
    def test_balance_switch_refresh_and_close(self):
        seed_design(self.chat);bind_project(self)
        state={'ok':True,'using':'engelbart','available':True,'budget_usd':20.0,'spend_usd':3.25}
        def switch(use):
            state['using']=use
            return dict(state)
        with mock.patch.object(claude_account,'status',side_effect=lambda **kw:dict(state)) as status, mock.patch.object(claude_account,'switch',side_effect=switch) as switching, server_for(self.chat) as url,self.page_on(url) as (page,errors):
            self.expect(page.get_by_role('menuitem',name='API',exact=True)).to_have_count(0)
            page.locator('.account-btn').click()
            page.get_by_role('menuitem',name='API',exact=True).click()
            dialog=page.get_by_role('group',name='API and credits')
            self.expect(dialog).to_contain_text('$16.75 left of $20.00')
            self.expect(dialog).to_contain_text('shared across sessions')
            self.assertTrue(any(call.kwargs.get('fresh') for call in status.call_args_list))
            dialog.get_by_role('button',name='My Claude',exact=True).click()
            self.expect(dialog.get_by_role('button',name='My Claude Active')).to_have_attribute('aria-pressed','true')
            self.assertEqual('own',switching.call_args.args[0])
            state['spend_usd']=4.5
            dialog.get_by_role('button',name='Refresh',exact=True).click()
            self.expect(dialog).to_contain_text('$15.50 left')
            dialog.get_by_role('button',name='Engelbart',exact=True).click()
            self.expect(dialog.get_by_role('button',name='Engelbart Active')).to_have_attribute('aria-pressed','true')
            self.assertEqual('engelbart',switching.call_args.args[0])
            page.keyboard.press('Escape');self.expect(dialog).to_have_count(0)
            self.assertEqual([],errors)

    def test_no_fake_balance_and_refused_switch_keeps_current_source(self):
        seed_design(self.chat);bind_project(self)
        state={'ok':True,'using':'engelbart','available':True}
        with mock.patch.object(claude_account,'status',return_value=state), mock.patch.object(claude_account,'switch',return_value={'ok':False,'error':'Settings could not be changed.'}), server_for(self.chat) as url,self.page_on(url) as (page,errors):
            self.expect(page.get_by_role('menuitem',name='API',exact=True)).to_have_count(0)
            page.locator('.account-btn').click()
            page.get_by_role('menuitem',name='API',exact=True).click()
            dialog=page.get_by_role('group',name='API and credits')
            self.expect(dialog).to_contain_text('Balance unavailable')
            self.expect(dialog).not_to_contain_text('$0.00')
            dialog.get_by_role('button',name='My Claude',exact=True).click()
            self.expect(dialog.get_by_role('alert')).to_have_text('Settings could not be changed.')
            self.expect(dialog.get_by_role('button',name='Engelbart Active')).to_have_attribute('aria-pressed','true')
            page.get_by_role('tab',name='Bart',exact=True).click();self.expect(dialog).to_have_count(0)
            self.assertEqual([],errors)
