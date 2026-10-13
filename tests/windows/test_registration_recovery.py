import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'windows'))
from apex_registration.recovery import recovery_action, resume_saved_ea
from apex_registration.flows.ea import ea_pw_full_setup as flow
from apex_registration.flows.ea import ea_pw_register as register


class RecoveryPolicyTests(unittest.TestCase):
    def test_generated_credentials_are_not_a_submitted_creation(self):
        state = dict(ea_password='saved', ea_origin_id='player', ea_creation_submitted=False,
                     ea_registered=False, stage='open_register', error_code='ea_network_error')
        self.assertFalse(resume_saved_ea(state))
        self.assertEqual(recovery_action(state), 'create')
        state['ea_creation_submitted'] = True
        self.assertTrue(resume_saved_ea(state))
        self.assertEqual(recovery_action(state), 'resume')
        state.pop('ea_password')
        self.assertEqual(recovery_action(state), 'stop')

    def test_semantic_rejections_and_unknown_interruptions_are_not_retried(self):
        for code in ('ea_login_credentials_rejected', 'steam_already_linked_elsewhere',
                     'ea_email_already_registered', 'tfa_activation_rejected', 'ea_full_setup_crashed'):
            self.assertEqual(recovery_action(dict(ea_password='saved', ea_registered=True, error_code=code)), 'stop')
        self.assertEqual(recovery_action({}, interrupted=True), 'stop')
        self.assertEqual(recovery_action({'stage': 'steam_register'}, interrupted=True), 'stop')

    def test_connection_reset_uses_remaining_login_attempts(self):
        page = Mock()
        page.goto.side_effect = RuntimeError('net::ERR_CONNECTION_RESET')
        with patch.object(flow, 'log'), patch.object(flow, 'screenshot'):
            self.assertFalse(flow.ensure_myaccount_session(page, Mock(), {'email':'test@example.test'}, 'saved', 'player'))
        self.assertEqual(page.goto.call_count, 2)

    def test_real_credential_error_is_recognized(self):
        with patch.object(register, 'visible_text', return_value='Your credentials are incorrect or have expired. Please try again or reset your password.'):
            self.assertTrue(register.login_password_rejected(Mock()))


class ResumeFlowTests(unittest.TestCase):
    """Exercise the real phase controller with offline browser and RPC fixtures."""
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {'EA_EMAIL':'test@example.test', 'EA_PROXY':'http://proxy.test',
            'SKIP_EA_REGISTER':'1', 'SKIP_STEAM_LINK':'0', 'SKIP_2FA_SETUP':'0',
            'EA_PASSWORD':'saved', 'EA_ORIGIN_ID':'player'}))
        for name, file in (('OUT_RESULT','result.json'), ('OUT_EA_RESULT','ea.json'),
                           ('OUT_LINK_RESULT','link.json'), ('OUT_LOG','run.log')):
            self.enterContext(patch.object(flow, name, self.root / file))
        self.enterContext(patch.object(flow, 'ARTIFACT_ROOT', self.root))
        self.enterContext(patch.object(flow, 'RUNTIME', MagicMock()))
        self.enterContext(patch.object(flow, 'sync_playwright', return_value=MagicMock()))
        self.enterContext(patch.object(flow, 'load_pool_mailboxes', return_value={'test@example.test':{'email':'test@example.test'}}))
        self.enterContext(patch.object(flow, 'load_steam_info', return_value={'username':'same-steam','email':'steam@example.test','password':'saved'}))
        self.enterContext(patch.object(flow, 'pick_register_country', return_value='HK'))
        for name in ('log', 'screenshot', 'close_task_browser', 'refresh_completed_session'):
            self.enterContext(patch.object(flow, name))
        self.enterContext(patch.object(flow, 'ensure_myaccount_session', return_value=True))
        self.enterContext(patch.object(flow, 'capture_remembered_session', return_value={'remid':'fixture'}))
        self.page = MagicMock()
        self.enterContext(patch.object(register, 'attach_browser', return_value=(Mock(), MagicMock(), self.page, Mock(), 'owned')))
        self.enterContext(patch.object(register, '_register_engine_name', return_value='bitbrowser'))
        self.enterContext(patch.object(register, 'release_fingerprint_window'))
        self.enterContext(patch.object(flow.pw_link, 'purge_steam_session', return_value=0))
        self.enterContext(patch.object(flow.pw_link, 'dismiss_cookies'))
        self.create = self.enterContext(patch.object(flow, 'submit_ea_creation', side_effect=AssertionError('must not create twice')))
        self.tile = self.enterContext(patch.object(flow.pw_link, 'click_steam_tile', side_effect=AssertionError('must not authorize twice')))
        self.new_secret = self.enterContext(patch.object(flow.pw_link, 'open_tfa_settings', side_effect=AssertionError('must not replace pending key')))
        self.state = dict(ea_email='test@example.test', ea_origin_id='player', ea_password='saved',
            ea_creation_submitted=True, ea_registered=True, account_login_verified=True, account_profile_completed=True,
            steam_user='same-steam', steam_email='steam@example.test', steam_link=False, totp=False)

    def run_flow(self, reply):
        flow.OUT_RESULT.write_text(json.dumps(self.state))
        with patch.object(flow, 'account_json', return_value=reply):
            code = flow.main()
        return code, json.loads(flow.OUT_RESULT.read_text())

    def test_authorization_in_flight_only_reads_final_state(self):
        self.state.update(steam_link_attempted=True, steam_oauth_authorize_submitted=True)
        with patch.object(flow.pw_link, 'wait_for_steam_linked', return_value={'status':'unlinked'}):
            code, result = self.run_flow({'ok':True,'payload':{'result':{'extPtyCon':[]}}})
        self.assertEqual(code, 1)
        self.assertEqual(result['error_code'], 'steam_link_commit_unconfirmed')
        self.assertTrue(result['steam_oauth_authorize_submitted'])
        self.create.assert_not_called()
        self.tile.assert_not_called()

    def test_session_failure_does_not_erase_verified_ea(self):
        with patch.object(flow, 'ensure_myaccount_session', return_value=False):
            code, result = self.run_flow({'ok':False, 'payload':{}})
        self.assertEqual(code, 3)
        self.assertEqual(result['error_code'], 'myaccount_session_failed')
        self.assertTrue(result['ea_registered'])
        self.assertTrue(result['account_login_verified'])
        self.create.assert_not_called()

    def test_pending_secret_is_kept_when_activation_cannot_be_confirmed(self):
        self.state.update(steam_link=True, totp_secret='PRESERVEDKEY')
        code, result = self.run_flow({'ok':True,'payload':{'result':{'twoFacInfo':{'sts':'DISABLED','twoFacTyp':'APP'}}}})
        self.assertEqual(code, 1)
        self.assertEqual(result['error_code'], 'tfa_activation_unconfirmed')
        self.assertEqual(result['totp_secret'], 'PRESERVEDKEY')
        self.new_secret.assert_not_called()

    def test_lost_activation_response_can_complete_by_reading_status(self):
        self.state.update(steam_link=True, totp_secret='PRESERVEDKEY')
        code, result = self.run_flow({'ok':True,'payload':{'result':{'twoFacInfo':{'sts':'ENABLED','twoFacTyp':'APP'}}}})
        self.assertEqual(code, 0)
        self.assertTrue(result['totp'])
        self.assertEqual(result['stage'], 'complete')
        self.new_secret.assert_not_called()

    def test_failed_status_read_cannot_start_new_2fa_setup(self):
        self.state.update(steam_link=True)
        code, result = self.run_flow({'ok':False, 'payload':{}})
        self.assertEqual(code, 1)
        self.assertEqual(result['error_code'], 'tfa_status_unavailable')
        self.new_secret.assert_not_called()
