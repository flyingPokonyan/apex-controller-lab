import sys
import time
import unittest
import unittest.mock
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'windows'))
from apex_registration.flows.ea import ea_pw_link_2fa


class EaLoginActionMatchingTests(unittest.TestCase):
    def test_chinese_next_matches(self):
        self.assertTrue(ea_pw_link_2fa.login_action_name_matches("下一步"))

    def test_traditional_chinese_next_matches(self):
        self.assertTrue(ea_pw_link_2fa.login_action_name_matches("繼續"))

    def test_english_next_still_matches(self):
        self.assertTrue(ea_pw_link_2fa.login_action_name_matches("NEXT"))
        self.assertTrue(ea_pw_link_2fa.login_action_name_matches("Log In"))

    def test_create_account_is_not_a_login_action(self):
        self.assertFalse(ea_pw_link_2fa.login_action_name_matches("创建账号"))

    def test_japanese_steam_oauth_labels_match(self):
        self.assertIsNotNone(ea_pw_link_2fa.STEAM_OAUTH_CONSENT_RE.fullmatch("許可"))
        self.assertIsNotNone(ea_pw_link_2fa.STEAM_OAUTH_LOGIN_CONSENT_RE.fullmatch("サインイン"))
        self.assertIsNotNone(ea_pw_link_2fa.EA_LINK_ACCOUNTS_RE.fullmatch("アカウントをリンク"))
        self.assertIsNotNone(ea_pw_link_2fa.EA_LINK_ACCOUNTS_RE.fullmatch("绑定账户"))

    def test_stable_login_button_id_is_in_fallbacks(self):
        self.assertIn("#logInBtn", ea_pw_link_2fa.LOGIN_ACTION_SELECTORS)
        self.assertIn("#continue", ea_pw_link_2fa.LOGIN_ACTION_SELECTORS)


class LoginVerificationChooserTests(unittest.TestCase):
    def test_detects_traditional_chinese_method_dialog(self):
        self.assertTrue(
            ea_pw_link_2fa.login_verification_dialog_visible(
                html="",
                text="選擇您的驗證方法 這就是您用於二階段驗證的方式。 驗證器應用程式 電子郵件",
            )
        )

    def test_ignores_unrelated_security_page(self):
        self.assertFalse(
            ea_pw_link_2fa.login_verification_dialog_visible(
                html="",
                text="安全性與隱私 管理 2FA 設定 開啟",
            )
        )


class EaLoginOtpPageTests(unittest.TestCase):
    def test_chinese_identity_page_is_otp(self):
        self.assertTrue(
            ea_pw_link_2fa.is_login_otp_page(
                html="我们将向 hq*****@outlook.com 发送一段验证码。发送代码",
                url="https://signin.ea.com/p/juno/login",
                title="双重身份登录",
            )
        )

    def test_chinese_verify_identity_title_is_otp(self):
        self.assertTrue(
            ea_pw_link_2fa.is_login_otp_page(
                html="验证您的身份",
                url="https://signin.ea.com/p/juno/login",
                title="验证您的身份",
            )
        )

    def test_english_two_factor_page_is_otp(self):
        self.assertTrue(
            ea_pw_link_2fa.is_login_otp_page(
                html="Two Factor Log In Enter your code",
                url="https://signin.ea.com/p/juno/login",
                title="Two Factor Log In",
            )
        )

    def test_email_password_login_is_not_otp(self):
        self.assertFalse(
            ea_pw_link_2fa.is_login_otp_page(
                html="登录您的 EA账户 下一步 创建账号",
                url="https://signin.ea.com/p/juno/login",
                title="登录",
            )
        )

    def test_send_code_label_matches_chinese(self):
        self.assertTrue(ea_pw_link_2fa.send_code_label_matches("发送代码"))
        self.assertTrue(ea_pw_link_2fa.send_code_label_matches("Send Code"))
        self.assertFalse(ea_pw_link_2fa.send_code_label_matches("下一步"))

    def test_browser_locale_requests_hong_kong_ui(self):
        self.assertEqual(ea_pw_link_2fa.BROWSER_LOCALE, "zh-HK")
        self.assertIn("zh-HK", ea_pw_link_2fa.ACCEPT_LANGUAGE)

    def test_steam_modal_matches_traditional_chinese(self):
        self.assertTrue(
            ea_pw_link_2fa.STEAM_LINK_MODAL_RE.search("連結您的 Steam")
        )
        self.assertTrue(
            ea_pw_link_2fa.NOT_LINKED_RE.search("未連結")
        )

    def test_ea_link_accounts_matches_hong_kong_copy(self):
        self.assertTrue(ea_pw_link_2fa.ea_link_accounts_label_matches("LINK ACCOUNTS"))
        self.assertTrue(ea_pw_link_2fa.ea_link_accounts_label_matches("連結帳號"))
        self.assertTrue(ea_pw_link_2fa.ea_link_accounts_label_matches("連結帳戶"))
        self.assertFalse(ea_pw_link_2fa.ea_link_accounts_label_matches("繼續"))
        self.assertFalse(ea_pw_link_2fa.ea_link_accounts_label_matches("取消"))

    def test_continue_label_matches_english_and_chinese(self):
        self.assertTrue(ea_pw_link_2fa.continue_label_matches("Continue"))
        self.assertTrue(ea_pw_link_2fa.continue_label_matches("继续"))
        self.assertTrue(ea_pw_link_2fa.continue_label_matches("繼續"))
        self.assertFalse(ea_pw_link_2fa.continue_label_matches("Cancel"))
        self.assertFalse(ea_pw_link_2fa.continue_label_matches("创建账号"))

    def test_already_linked_elsewhere_page(self):
        text = (
            "Linked Accounts Found Your accounts can't be linked because your "
            "Steam account demainemisty699 already linked to your EA Account "
            "for li*****@hotmail.com."
        )
        self.assertEqual(
            ea_pw_link_2fa.classify_ea_link_rejection(text),
            "steam_already_linked_elsewhere",
        )
        self.assertEqual(
            ea_pw_link_2fa.extract_already_linked_ea_email(text),
            "li*****@hotmail.com",
        )

    def test_hong_kong_already_linked_elsewhere_page(self):
        text = (
            "找到連結的帳號 無法連結您的帳號，因為您的 Steam 帳號 "
            "colicchiogwynn2200 已連結至您 bg****@outlook.com 的 EA 帳號。"
        )
        self.assertEqual(
            ea_pw_link_2fa.classify_ea_link_rejection(text),
            "steam_already_linked_elsewhere",
        )
        self.assertEqual(
            ea_pw_link_2fa.extract_already_linked_ea_email(text),
            "bg****@outlook.com",
        )

    def test_masked_email_matches_unique_unlinked_candidate(self):
        emails = [
            "LimasWittstruck78@hotmail.com",
            "LinkkilaArin2532@hotmail.com",
            "hqjt7733@outlook.com",
        ]
        self.assertEqual(
            ea_pw_link_2fa.match_masked_email("li*****@hotmail.com", emails),
            [
                "LimasWittstruck78@hotmail.com",
                "LinkkilaArin2532@hotmail.com",
            ],
        )

    def test_normal_connected_accounts_is_not_rejection(self):
        self.assertIsNone(
            ea_pw_link_2fa.classify_ea_link_rejection(
                "Connected accounts Not linked Steam Discord"
            )
        )

    def test_steam_persona_matches_email_local_part(self):
        self.assertTrue(
            ea_pw_link_2fa.steam_persona_matches(
                "stumpequiggle5938",
                username="p8j5ww8900prhm",
                email="StumpeQuiggle5938@hotmail.com",
            )
        )
        self.assertFalse(
            ea_pw_link_2fa.steam_persona_matches(
                "stumpequiggle5938",
                username="btw5kmh7wcxo",
                email="iwno3348@outlook.com",
            )
        )

    def test_linked_steam_persona_from_current_ea_testid(self):
        html = (
            '<span data-testid="connected-accounts-linked-section-pre-title-Steam">'
            "Steam</span>"
            '<span id="connected-accounts-linked-section-title-carchididine29" '
            'data-testid="connected-accounts-linked-section-title-carchididine29">'
            "carchididine29</span>"
            '<button data-testid="unlink-external-button-Steam">Unlink</button>'
        )
        self.assertEqual(
            ea_pw_link_2fa.linked_steam_persona_from_html(html),
            "carchididine29",
        )

    def test_linked_steam_persona_ignores_title_steam_testid(self):
        html = (
            '<span data-testid="connected-accounts-linked-section-title-Steam">'
            "Steam</span>"
        )
        self.assertEqual(ea_pw_link_2fa.linked_steam_persona_from_html(html), "")


class AccountObservationTests(unittest.TestCase):
    def test_steam_name_is_scoped_to_steam_card(self):
        html = """<div><div><span data-testid="connected-accounts-linked-section-pre-title-Xbox">Xbox</span><span data-testid="connected-accounts-linked-section-title-wrong">wrong</span><button data-testid="unlink-external-button-Xbox">Unlink</button></div><div><span data-testid="connected-accounts-linked-section-pre-title-Steam">Steam</span><span data-testid="connected-accounts-linked-section-title-right">right</span><button data-testid="unlink-external-button-Steam">Unlink</button></div></div>"""
        self.assertEqual(ea_pw_link_2fa.linked_steam_persona_from_html(html), "right")

    def test_punctuation_is_not_discarded_when_matching_accounts(self):
        self.assertFalse(ea_pw_link_2fa.steam_persona_matches("a-b", username="ab", email="else@example.com"))

    def test_linked_without_display_name_is_still_linked(self):
        observed = ea_pw_link_2fa.steam_connection_from_payload({"result": {"extPtyCon": [
            {"extPtyConTyp": "STEAM", "idx": "STEAM_0", "disNm": None}]}})
        self.assertEqual(observed["status"], "linked")
        self.assertEqual(observed["persona"], "")

    def test_missing_provider_is_unknown_not_unlinked(self):
        self.assertEqual(ea_pw_link_2fa.steam_connection_from_payload({})["status"], "unknown")

    def test_identity_requires_observed_id_and_email(self):
        account = {"originId": "ea-id", "email": "ea@example.com"}
        self.assertTrue(ea_pw_link_2fa.account_identity_matches(
            {"result": {"eaid": "EA-ID", "em": "EA@example.com"}}, account))
        self.assertFalse(ea_pw_link_2fa.account_identity_matches(
            {"result": {"eaid": "another", "em": "ea@example.com"}}, account))
        self.assertFalse(ea_pw_link_2fa.account_identity_matches({}, account))

    def test_display_name_is_not_read_as_a_totp_secret(self):
        self.assertIsNone(ea_pw_link_2fa.extract_totp_secret(
            "<h1>tigerspiralstark</h1>", "tigerspiralstark 两步验证"))
        self.assertEqual(
            ea_pw_link_2fa.extract_totp_secret("", "M2PV SXXZ 7GY5 PHXA"),
            "M2PVSXXZ7GY5PHXA",
        )

    def test_2fa_success_requires_app_enabled_state(self):
        self.assertTrue(ea_pw_link_2fa.app_2fa_enabled({"result": {"twoFacInfo": {"sts": "ENABLED", "twoFacTyp": "APP"}}}))
        self.assertFalse(ea_pw_link_2fa.app_2fa_enabled({"result": {"twoFacInfo": {"sts": "DISABLED", "twoFacTyp": "APP"}}}))
        self.assertFalse(ea_pw_link_2fa.app_2fa_enabled({"result": {"twoFacInfo": {"sts": "ENABLED", "twoFacTyp": "EMAIL"}}}))


class RememberDeviceTests(unittest.TestCase):
    def test_hidden_remember_me_checkbox_is_checked(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(
                    """
                    <form>
                      <input id="email" name="email">
                      <input id="rememberMe" name="rememberMe" type="checkbox" hidden>
                      <label for="rememberMe">記住這台裝置</label>
                      <button type="submit">下一步</button>
                    </form>
                    """
                )
                self.assertTrue(ea_pw_link_2fa.ensure_remember_device(page))
                self.assertTrue(page.locator("#rememberMe").is_checked())
            finally:
                browser.close()

    def test_remember_me_checkbox_inside_iframe_is_checked(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(
                    '<iframe srcdoc="'
                    "<form>"
                    "<input id='rememberMe' name='rememberMe' type='checkbox'>"
                    "<label for='rememberMe'>Keep me signed in</label>"
                    "</form>"
                    '"></iframe>'
                )
                self.assertTrue(ea_pw_link_2fa.ensure_remember_device(page))
                frame = page.frames[-1]
                self.assertTrue(frame.locator("#rememberMe").is_checked())
            finally:
                browser.close()


class OAuthConsentControlTests(unittest.TestCase):
    def test_security_explanation_is_not_an_identity_challenge(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('''<p>This helps us quickly verify your identity when signing in.</p>
                    <h2 style="display:none">Verify your identity</h2>
                    <button>Turn on</button>''')
                self.assertFalse(ea_pw_link_2fa.identity_challenge_visible(page))
                page.locator('h2').evaluate('(el) => el.style.display = "block"')
                self.assertTrue(ea_pw_link_2fa.identity_challenge_visible(page))
            finally:
                browser.close()

    def test_security_settings_waits_for_delayed_visible_control(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('''<button data-testid="enter-tfa-button" style="display:none"
                    onclick="window.hiddenClicked=true">Hidden</button>
                    <script>setTimeout(() => {
                      const button = document.createElement('button');
                      button.dataset.testid = 'enter-tfa-button';
                      button.textContent = 'Manage 2FA settings';
                      button.onclick = () => window.openedSettings = true;
                      document.body.appendChild(button);
                    }, 500);</script>''')
                ea_pw_link_2fa.open_tfa_settings(page, timeout_ms=5000)
                self.assertTrue(page.evaluate('window.openedSettings === true'))
                self.assertFalse(page.evaluate('window.hiddenClicked === true'))
            finally:
                browser.close()

    def test_link_accounts_is_clicked_on_juno_login_callback(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                account = context.new_page()
                account.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html; charset=utf-8",
                        body="<h1>Connected accounts</h1>",
                    ),
                )
                account.goto("https://myaccount.ea.com/am/ui/connected-accounts")
                confirm = context.new_page()
                confirm.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html; charset=utf-8",
                        body=(
                            '<script>window.linked=false</script>'
                            '<a href="javascript:void(0);" role="button" id="continue" '
                            'onclick="window.linked=true">連結帳號</a>'
                        ),
                    ),
                )
                confirm.goto("https://signin.ea.com/p/juno/login")
                self.assertTrue(ea_pw_link_2fa.ea_login_form_url(confirm.url))
                self.assertTrue(
                    ea_pw_link_2fa.click_ea_link_accounts_anywhere(context)
                )
                self.assertTrue(confirm.evaluate("window.linked === true"))
            finally:
                browser.close()

    def test_link_modal_on_myaccount_is_clicked(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                account = context.new_page()
                account.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html; charset=utf-8",
                        body=(
                            '<script>window.clicked=false</script>'
                            '<button data-testid="connected-accounts-modal-continue-button" '
                            'onclick="window.clicked=true">連携する</button>'
                        ),
                    ),
                )
                account.goto("https://myaccount.ea.com/am/ui/connected-accounts")
                self.assertTrue(ea_pw_link_2fa.pending_ea_link_accounts(context))
                self.assertTrue(ea_pw_link_2fa.click_ea_link_accounts_anywhere(context))
                self.assertTrue(account.evaluate("window.clicked === true"))
            finally:
                browser.close()

    def test_link_accounts_does_not_click_continue_on_myaccount(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                account = context.new_page()
                account.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html; charset=utf-8",
                        body=(
                            '<script>window.clicked=false</script>'
                            '<a href="javascript:void(0);" role="button" id="continue" '
                            'onclick="window.clicked=true">繼續</a>'
                        ),
                    ),
                )
                account.goto("https://myaccount.ea.com/am/ui/connected-accounts")
                self.assertFalse(
                    ea_pw_link_2fa.click_ea_link_accounts_anywhere(context)
                )
                self.assertFalse(account.evaluate("window.clicked === true"))
            finally:
                browser.close()

    def test_link_accounts_is_clicked_on_a_later_ea_tab(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                main = context.new_page()
                main.set_content("<p>Connected accounts</p>")
                confirm = context.new_page()
                confirm.set_content(
                    '<button onclick="window.linked=true">連結帳號</button>'
                )
                self.assertTrue(
                    ea_pw_link_2fa.click_ea_link_accounts_anywhere(context)
                )
                self.assertTrue(confirm.evaluate("window.linked === true"))
            finally:
                browser.close()

    def test_rejection_is_found_on_a_later_ea_tab(self):
        class Page:
            def __init__(self, url, text):
                self.url = url
                self.text = text

            def is_closed(self):
                return False

            def inner_text(self, selector="body"):
                return self.text

        class Ctx:
            def __init__(self, pages):
                self.pages = pages

        ctx = Ctx(
            [
                Page("https://myaccount.ea.com/am/ui/connected-accounts", "Steam"),
                Page(
                    "https://signin.ea.com/p/juno/login",
                    "找到連結的帳號 無法連結您的帳號，因為您的 Steam 帳號 "
                    "colicchiogwynn2200 已連結至您 bg****@outlook.com 的 EA 帳號。",
                ),
            ]
        )
        code, masked = ea_pw_link_2fa.steam_link_rejection_from_context(ctx)
        self.assertEqual(code, "steam_already_linked_elsewhere")
        self.assertEqual(masked, "bg****@outlook.com")

    def test_ea_link_accounts_clicks_javascript_void_anchor(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(
                    '<script>window.linked=false</script>'
                    '<h1>將您的 EA 帳號連結到 Steam</h1>'
                    '<a href="javascript:void(0);" role="button" '
                    'onclick="window.linked=true">連結帳號</a>'
                )
                self.assertTrue(ea_pw_link_2fa.click_oauth_consent(page, on_ea=True))
                self.assertTrue(page.evaluate('window.linked === true'))
            finally:
                browser.close()

    def test_ea_link_accounts_force_clicks_through_overlay(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(
                    '<script>window.linked=false</script>'
                    '<a href="javascript:void(0);" role="button" id="continue" '
                    'onclick="window.linked=true">連結帳號</a>'
                    '<div style="position:fixed;inset:0;background:rgba(0,0,0,.4)"></div>'
                )
                self.assertTrue(ea_pw_link_2fa.click_oauth_consent(page, on_ea=True))
                self.assertTrue(page.evaluate('window.linked === true'))
            finally:
                browser.close()

    def test_ea_link_consent_does_not_click_juno_login_continue(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html; charset=utf-8",
                        body=(
                            '<script>window.clicked=false</script>'
                            '<input id="email" name="email">'
                            '<a href="javascript:void(0);" role="button" id="continue" '
                            'onclick="window.clicked=true">下一步</a>'
                        ),
                    ),
                )
                page.goto("https://signin.ea.com/p/juno/login")
                self.assertTrue(ea_pw_link_2fa.ea_login_form_url(page.url))
                self.assertFalse(ea_pw_link_2fa.click_oauth_consent(page, on_ea=True))
                self.assertFalse(page.evaluate("window.clicked === true"))
            finally:
                browser.close()

    def test_inspect_steam_link_state_detects_juno_login_as_logged_out(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html; charset=utf-8",
                        body="<h1>登入</h1><input id='email'>",
                    ),
                )
                page.goto("https://signin.ea.com/p/juno/login")
                started = __import__("time").monotonic()
                state = ea_pw_link_2fa.wait_for_steam_linked(page, timeout_ms=20000)
                self.assertEqual(state["status"], "logged_out")
                self.assertLess(__import__("time").monotonic() - started, 4)
            finally:
                browser.close()

    def test_ea_popup_closing_during_consent_is_not_a_runtime_failure(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                main = browser.new_page()
                main.set_content('<button onclick="window.open()">Open</button>')
                with main.expect_popup() as opened:
                    main.get_by_role('button', name='Open').click()
                popup = opened.value
                popup.set_content('<button onclick="window.close()">Link accounts</button>')
                checkpoint = []
                self.assertTrue(ea_pw_link_2fa.click_oauth_consent(
                    popup, on_ea=True, before_click=lambda: checkpoint.append(True)))
                self.assertTrue(popup.is_closed())
                self.assertEqual(checkpoint, [True])
                self.assertFalse(main.is_closed())
            finally:
                browser.close()

    def test_real_button_semantics_cover_legacy_input_and_modern_button(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                for control in [
                    '<input type="button" value="Allow" onclick="window.allowed=true">',
                    '<button onclick="window.allowed=true">Allow</button>',
                ]:
                    page.set_content('<script>window.allowed=false</script>' + control +
                        '<button onclick="window.denied=true">Deny</button>')
                    self.assertTrue(ea_pw_link_2fa.click_oauth_consent(page, on_ea=False))
                    self.assertTrue(page.evaluate('window.allowed === true'))
                    self.assertFalse(page.evaluate('window.denied === true'))
            finally:
                browser.close()

    def test_steam_credentials_are_not_entered_into_an_ea_relay(self):
        self.assertFalse(ea_pw_link_2fa.is_steam_page('https://signin.ea.com/p/juno/login'))
        self.assertTrue(ea_pw_link_2fa.is_steam_page('https://steamcommunity.com/oauth/login'))
        self.assertFalse(ea_pw_link_2fa.is_steam_page('https://steamcommunity.com.attacker.example/'))

    def test_resolve_steam_oauth_page_switches_off_ea_popup(self):
        class FakePage:
            def __init__(self, url):
                self.url = url

            def is_closed(self):
                return False

        class FakeContext:
            def __init__(self, pages):
                self.pages = pages

        ea_popup = FakePage("https://signin.ea.com/p/juno/login")
        steam_tab = FakePage("https://steamcommunity.com/oauth/login")
        ctx = FakeContext([ea_popup, steam_tab])
        self.assertIs(
            ea_pw_link_2fa.resolve_steam_oauth_page(ctx, ea_popup),
            steam_tab,
        )

    def test_await_steam_oauth_page_waits_for_late_steam_tab(self):
        class FakePage:
            def __init__(self, url):
                self.url = url

            def is_closed(self):
                return False

            def wait_for_timeout(self, _ms):
                return None

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage("https://signin.ea.com/p/juno/login")]
                self.ticks = 0

            def maybe_open_steam(self):
                self.ticks += 1
                if self.ticks >= 3 and len(self.pages) == 1:
                    self.pages.append(
                        FakePage("https://steamcommunity.com/oauth/login")
                    )

        ctx = FakeContext()
        original = ea_pw_link_2fa.resolve_steam_oauth_page

        def resolve(context, preferred=None):
            context.maybe_open_steam()
            return original(context, preferred)

        with unittest.mock.patch.object(
            ea_pw_link_2fa, "resolve_steam_oauth_page", side_effect=resolve
        ):
            found = ea_pw_link_2fa.await_steam_oauth_page(
                ctx, ctx.pages[0], timeout_ms=2000
            )
        self.assertEqual(found.url, "https://steamcommunity.com/oauth/login")

    def test_already_authed_steam_skips_credential_form(self):
        self.assertFalse(
            ea_pw_link_2fa.steam_credentials_required(None, None)
        )
        self.assertTrue(
            ea_pw_link_2fa.steam_credentials_required("user", "pass")
        )

    def test_steam_authenticated_state_requires_rendered_text(self):
        class Body:
            def __init__(self, text): self.text = text
            def inner_text(self, timeout=None): return self.text
        class Page:
            url = "https://steamcommunity.com/oauth/login"
            def __init__(self, text): self.body = Body(text)
            def locator(self, selector): return self.body
        self.assertTrue(ea_pw_link_2fa.steam_authenticated_or_consent(Page("Authorize with Steam")))
        self.assertTrue(ea_pw_link_2fa.steam_authenticated_or_consent(Page("Allow")))
        self.assertTrue(ea_pw_link_2fa.steam_authenticated_or_consent(Page("Steam Guard")))
        self.assertFalse(ea_pw_link_2fa.steam_authenticated_or_consent(Page("Loading...")))
        self.assertFalse(
            ea_pw_link_2fa.steam_authenticated_or_consent(
                Page("Sign in\nSteam Account\nForgot your password")
            )
        )

    def test_steam_auth_page_rejects_ea_json_relay(self):
        class Page:
            def __init__(self, url):
                self.url = url

            def is_closed(self):
                return False

        self.assertFalse(
            ea_pw_link_2fa.steam_auth_page_is_live(
                Page("https://myaccount.ea.com/am/data/1/link-external-party")
            )
        )
        self.assertTrue(
            ea_pw_link_2fa.steam_auth_page_is_live(
                Page("https://steamcommunity.com/oauth/login")
            )
        )
        self.assertTrue(ea_pw_link_2fa.steam_auth_page_is_live(Page("about:blank")))

    def test_steam_oauth_consent_clicks_login_when_already_signed_in(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(
                    '<script>window.signed=false</script>'
                    '<p>使用您的 Steam 帐户登录到 Electronic Arts</p>'
                    '<button onclick="window.signed=true">登录</button>'
                )
                self.assertTrue(ea_pw_link_2fa.click_oauth_consent(page, on_ea=False))
                self.assertTrue(page.evaluate('window.signed === true'))
            finally:
                browser.close()

    def test_steam_oauth_consent_ignores_login_when_password_is_visible(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(
                    '<script>window.signed=false</script>'
                    '<input type="password">'
                    '<button onclick="window.signed=true">登录</button>'
                )
                self.assertFalse(ea_pw_link_2fa.click_oauth_consent(page, on_ea=False))
                self.assertFalse(page.evaluate('window.signed === true'))
            finally:
                browser.close()

    def test_steam_oauth_consent_does_not_click_sign_in(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(
                    '<script>window.signed=false;window.allowed=false</script>'
                    '<button onclick="window.signed=true">Sign In</button>'
                    '<button onclick="window.allowed=true">Allow</button>'
                )
                self.assertTrue(ea_pw_link_2fa.click_oauth_consent(page, on_ea=False))
                self.assertTrue(page.evaluate('window.allowed === true'))
                self.assertFalse(page.evaluate('window.signed === true'))
            finally:
                browser.close()

    def test_steam_cookie_domain_is_limited_to_steam_hosts(self):
        self.assertTrue(ea_pw_link_2fa.steam_cookie_domain(".steamcommunity.com"))
        self.assertTrue(ea_pw_link_2fa.steam_cookie_domain("login.steampowered.com"))
        self.assertFalse(ea_pw_link_2fa.steam_cookie_domain("myaccount.ea.com"))
        self.assertFalse(
            ea_pw_link_2fa.steam_cookie_domain("steamcommunity.com.attacker.example")
        )

    def test_steam_login_sign_in_does_not_short_circuit_credential_wait(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                html = (
                    "<h1>Sign in</h1><p>Steam Account</p>"
                    "<button>Sign In</button>"
                )
                page.route(
                    "https://steamcommunity.com/oauth/login",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html", body=html
                    ),
                )
                page.goto("https://steamcommunity.com/oauth/login")
                started = time.monotonic()
                user, password = ea_pw_link_2fa.wait_for_steam_credentials(
                    page, timeout_ms=1800
                )
                elapsed = time.monotonic() - started
                self.assertIsNone(user)
                self.assertIsNone(password)
                self.assertGreaterEqual(elapsed, 1.0)
            finally:
                browser.close()

    def test_extract_steam_oauth_urls_from_am_json_and_escaped_text(self):
        payload = {
            "result": {
                "url": "https://steamcommunity.com/oauth/login?client_id=abc"
            },
            "errorCode": 601061,
        }
        self.assertEqual(
            ea_pw_link_2fa.extract_steam_oauth_urls(payload),
            ["https://steamcommunity.com/oauth/login?client_id=abc"],
        )
        escaped = r'{"redirect":"https:\/\/steamcommunity.com\/oauth\/login?x=1"}'
        self.assertEqual(
            ea_pw_link_2fa.extract_steam_oauth_urls(escaped),
            ["https://steamcommunity.com/oauth/login?x=1"],
        )
        self.assertEqual(
            ea_pw_link_2fa.extract_steam_oauth_urls(
                "https://api.steampowered.com/IClientMetricsService/ReportClientError/v1"
            ),
            [],
        )
        self.assertEqual(
            ea_pw_link_2fa.am_error_code_from_text(
                'Pretty-print\n{"errorCode":601061}'
            ),
            "601061",
        )

    def test_prefer_steam_oauth_url_ranks_login_over_loginform(self):
        chosen = ea_pw_link_2fa.prefer_steam_oauth_url(
            [
                "https://steamcommunity.com/oauth/loginform/",
                "https://steamcommunity.com/oauth/login?client_id=1",
            ]
        )
        self.assertIn("oauth/login?client_id=1", chosen)

    def test_hidden_steam_password_is_still_a_credential_form(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                html = (
                    '<input id="steamAccountName" style="display:none">'
                    '<input id="steamPassword" type="password" style="display:none">'
                    '<input id="imageLogin" type="submit" value="Sign In">'
                )
                page.route(
                    "https://steamcommunity.com/oauth/loginform/",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html", body=html
                    ),
                )
                page.goto("https://steamcommunity.com/oauth/loginform/")
                started = time.monotonic()
                user, password = ea_pw_link_2fa.wait_for_steam_credentials(
                    page, timeout_ms=1800
                )
                self.assertIsNotNone(user)
                self.assertIsNotNone(password)
                self.assertLess(time.monotonic() - started, 1.0)
            finally:
                browser.close()

    def test_steam_qr_gate_reveals_password_fields(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                html = '''
                  <h1>Sign in with QR</h1>
                  <canvas></canvas>
                  <a href="#" onclick="event.preventDefault();
                    document.body.insertAdjacentHTML('beforeend',
                      '<input id=steamAccountName><input id=steamPassword type=password>');">
                    Sign in with account name and password
                  </a>
                '''
                page.route(
                    "https://steamcommunity.com/oauth/loginform/",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html", body=html
                    ),
                )
                page.goto("https://steamcommunity.com/oauth/loginform/")
                started = time.monotonic()
                user, password = ea_pw_link_2fa.wait_for_steam_credentials(
                    page, timeout_ms=4000
                )
                self.assertIsNotNone(user)
                self.assertIsNotNone(password)
                self.assertLess(time.monotonic() - started, 3.5)
            finally:
                browser.close()

    def test_ensure_live_repairs_ea_relay_with_browser_navigation(self):
        class FakePage:
            def __init__(self, url, text=""):
                self.url = url
                self._text = text
                self.gotos = []

            def is_closed(self):
                return False

            def inner_text(self, selector="body", timeout=None):
                return self._text

            def content(self):
                return self._text

            def evaluate(self, *_args, **_kwargs):
                return {}

            def goto(self, url, wait_until=None, timeout=None):
                self.gotos.append(url)
                self.url = 'https://steamcommunity.com/oauth/login?client_id=1'

            def wait_for_timeout(self, _ms):
                return None

        class FakeContext:
            def __init__(self, pages):
                self.pages = pages

            def new_page(self):
                page = FakePage("about:blank")
                self.pages.append(page)
                return page

        parent = FakePage("https://myaccount.ea.com/am/ui/connected-accounts")
        popup = FakePage(
            "https://myaccount.ea.com/am/data/1/link-external-party",
            '{"errorCode":601061}',
        )
        ctx = FakeContext([parent, popup])
        oauth = "https://steamcommunity.com/oauth/login?client_id=1"

        def am_json(*args, **kwargs):
            self.fail('The redirect endpoint must be opened as a document, not fetched as JSON')

        found = ea_pw_link_2fa.ensure_live_steam_oauth_page(
            ctx, parent, popup, [], am_json=am_json, timeout_ms=200
        )
        self.assertEqual(found.url, oauth)
        self.assertEqual(popup.gotos, [
            'https://myaccount.ea.com/am/data/1/link-external-party?externalParty=STEAM'
        ])
