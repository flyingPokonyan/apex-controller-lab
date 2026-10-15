"""Bounded recovery of one assigned task; never infer a commit from credentials."""

MAX_ATTEMPTS = 3
SUBMISSION_FLAGS = (
    'steam_oauth_account_login_submitted', 'steam_oauth_authorize_submitted',
    'ea_link_consent_submitted',
)
CHECKPOINT_KEYS = (
    'ea_email', 'ea_password', 'ea_origin_id', 'ea_creation_submitted',
    'ea_registered', 'account_login_verified', 'account_profile_completed',
    'steam_user', 'steam_email', 'steam_link', 'steam_link_attempted',
    *SUBMISSION_FLAGS, 'linked_steam_persona', 'totp', 'totp_secret',
    'tfa_existing_enabled', 'backup_codes', 'connect_cookies',
    'skipped_ea_emails', 'already_registered_ea_emails',
)
RETRYABLE = frozenset({
    'browser_unavailable', 'ea_network_error', 'ea_proxy_error', 'ea_action_timeout', 'ea_step_timeout',
    'myaccount_session_failed', 'ea_login_otp_missing', 'ea_login_otp_send_failed',
    'ea_login_otp_submit_failed', 'ea_create_submit_unconfirmed',
    'ea_register_failed', 'ea_technical_issue', 'ea_rate_limited',
    'steam_login_form_missing', 'steam_oauth_page_missing', 'steam_oauth_not_opened',
    'steam_login_transition_missing', 'steam_guard_code_missing',
    'steam_guard_unconfirmed', 'steam_link_not_committed', 'steam_link_unconfirmed',
    'ea_link_session_unconfirmed', 'steam_link_status_unavailable',
    'steam_link_commit_unconfirmed', 'tfa_status_unavailable',
    'tfa_settings_open_failed', 'tfa_control_timeout', 'tfa_identity_code_missing',
    'tfa_identity_send_failed', 'tfa_secret_not_found', 'tfa_activation_unconfirmed',
})


def transport_failure(exc):
    # Element/action timeouts are UI recovery, not proof of a bad proxy.
    text = str(exc).casefold()
    return any(marker in text for marker in (
        'err_connection_', 'err_proxy_', 'err_tunnel_', 'err_socks_',
        'err_name_not_resolved', 'err_empty_response', 'err_network_',
        'err_timed_out', 'connection timed out', 'read timed out',
        'connection reset', 'connection refused', 'socket disconnected',
        'tls connection', 'unexpected eof', 'remote disconnected',
        'browser has been closed', 'target closed',
    ))


def resume_saved_ea(result):
    return bool(result.get('ea_password') and any(result.get(key) is True for key in (
        'ea_creation_submitted', 'ea_registered', 'account_login_verified',
    )))


def completed(result):
    return (result.get('stage') == 'complete' and not result.get('error_code')
            and not result.get('needs_review') and bool(result.get('totp_secret'))
            and all(result.get(key) is True for key in (
                'ea_registered', 'account_login_verified', 'account_profile_completed',
                'steam_link', 'totp')))


def recovery_action(result, *, interrupted=False):
    """Return the only safe next action after a child exits or is stopped."""
    if completed(result):
        return 'upload'
    code = result.get('error_code')
    if code not in RETRYABLE and not (interrupted and not code):
        return 'stop'
    if resume_saved_ea(result):
        return 'resume'
    # Explicit false flags from the flow prove it had not dispatched creation.
    # Missing state, Steam signup interruption, or missing saved credentials do not.
    if (result.get('ea_creation_submitted') is False
            and result.get('ea_registered') is False
            and not result.get('account_login_verified')
            and result.get('stage') in {'init', 'open_register', 'submit_email', 'email_verify', 'create_account'}):
        return 'create'
    return 'stop'


def restore_checkpoint(result, previous):
    for key in CHECKPOINT_KEYS:
        if key in previous:
            result[key] = previous[key]
