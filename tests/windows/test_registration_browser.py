"""Offline checks for the migrated browser registration executor."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'windows'))
from apex_registration.flows.ea import ea_pw_register
class AttachBrowserTests(unittest.TestCase):
    def test_roxybrowser_connects_over_cdp_instead_of_launch(self):
        class FakePage:
            def set_default_timeout(self, _value):
                return None

        class FakeContext:
            pages = [FakePage()]

        class FakeBrowser:
            contexts = [FakeContext()]

        class Chromium:
            def launch(self, **_kwargs):
                raise AssertionError("chromium.launch must not run for RoxyBrowser")

            def connect_over_cdp(self, url):
                self.cdp = url
                return FakeBrowser()

        class Playwright:
            chromium = Chromium()

        with patch.dict(os.environ, {"GAME_ACCOUNTS_EA_REGISTER_ENGINE": "roxybrowser"}):
            with patch.object(ea_pw_register, "RoxyBrowserClient") as client_cls:
                client_cls.return_value.ensure_open.return_value = {
                    "id": "dir-1",
                    "cdp": "ws://127.0.0.1:40597/devtools/browser/x",
                    "name": "apex-ea-test",
                }
                browser, context, page, client, profile_id = ea_pw_register.attach_browser(
                    Playwright(),
                    proxy="http://user:pass@gw.dataimpulse.com:823",
                    seed="a@example.com",
                )
        self.assertEqual(profile_id, "dir-1")
        self.assertEqual(
            Playwright.chromium.cdp, "ws://127.0.0.1:40597/devtools/browser/x"
        )
        self.assertIs(page, FakeContext.pages[0])
        self.assertIs(client, client_cls.return_value)

    def test_roxybrowser_deletes_profile_when_cdp_connect_fails(self):
        class Chromium:
            def connect_over_cdp(self, _url):
                raise RuntimeError("browser has been closed")

        class Playwright:
            chromium = Chromium()

        with patch.dict(os.environ, {"GAME_ACCOUNTS_EA_REGISTER_ENGINE": "roxybrowser"}):
            with patch.object(ea_pw_register, "RoxyBrowserClient") as client_cls:
                client = client_cls.return_value
                client.ensure_open.return_value = {
                    "id": "dir-1",
                    "cdp": "ws://127.0.0.1:40597/devtools/browser/x",
                    "name": "apex-ea-test",
                }
                with self.assertRaises(RuntimeError):
                    ea_pw_register.attach_browser(
                        Playwright(),
                        proxy="http://user:pass@gw.dataimpulse.com:823",
                        seed="a@example.com",
                    )
        client.delete.assert_called_once_with("dir-1")

    def test_release_fingerprint_window_deletes_unless_keep(self):
        client = Mock()
        with patch.dict(os.environ, {"GAME_ACCOUNTS_ROXY_KEEP": "", "GAME_ACCOUNTS_FINGERPRINT_KEEP": ""}):
            ea_pw_register.release_fingerprint_window(client, "dir-1")
        client.delete.assert_called_once_with("dir-1")

        client.reset_mock()
        with patch.dict(os.environ, {"GAME_ACCOUNTS_ROXY_KEEP": "1"}):
            ea_pw_register.release_fingerprint_window(client, "dir-1")
        client.delete.assert_not_called()
        client.close.assert_not_called()


class HumanInputHelperTests(unittest.TestCase):
    def test_bezier_endpoints(self):
        p0, p3 = (0.0, 0.0), (100.0, 40.0)
        p1, p2 = (20.0, 30.0), (80.0, -10.0)
        self.assertEqual(ea_pw_register.cubic_bezier(p0, p1, p2, p3, 0.0), p0)
        self.assertEqual(ea_pw_register.cubic_bezier(p0, p1, p2, p3, 1.0), p3)
        mid = ea_pw_register.cubic_bezier(p0, p1, p2, p3, 0.5)
        self.assertGreater(mid[0], 0.0)
        self.assertLess(mid[0], 100.0)

    def test_locator_is_blocked_for_disabled_controls(self):
        class FakeLoc:
            def __init__(self, attrs, enabled=True):
                self.attrs = attrs
                self.enabled = enabled

            def get_attribute(self, name):
                return self.attrs.get(name)

            def is_enabled(self):
                return self.enabled

        self.assertTrue(
            ea_pw_register.locator_is_blocked(
                FakeLoc({"aria-disabled": "true", "class": "otkbtn"})
            )
        )
        self.assertTrue(
            ea_pw_register.locator_is_blocked(
                FakeLoc({"class": "otkbtn otkbtn-disabled"})
            )
        )
        self.assertFalse(
            ea_pw_register.locator_is_blocked(FakeLoc({"class": "otkbtn-primary"}))
        )

    def test_otp_boxes_are_typed_not_filled_first(self):
        calls = []

        class Box:
            def click(self, timeout=None):
                calls.append("click")

            def press_sequentially(self, ch, delay=None):
                calls.append(("type", ch, delay))

            def fill(self, ch):
                calls.append(("fill", ch))

        ea_pw_register._type_otp_char(Box(), "8")
        self.assertIn("click", calls)
        self.assertTrue(any(item[0] == "type" and item[1] == "8" for item in calls if isinstance(item, tuple)))
        self.assertFalse(any(item[0] == "fill" for item in calls if isinstance(item, tuple)))


class ConnectCookieCaptureTests(unittest.TestCase):
    def test_context_cookies_include_http_only_remid(self):
        class Ctx:
            def cookies(self, urls=None):
                return [
                    {"name": "sid", "value": "s1"},
                    {"name": "remid", "value": "r1", "expires": 1893456000},
                ]

        payload = ea_pw_register.capture_connect_cookies(Ctx())
        self.assertEqual(payload["sid"], "s1")
        self.assertEqual(payload["remid"], "r1")
        self.assertTrue(payload["remid_expires_at"])

    def test_drop_ea_auth_cookies_clears_ea_session_not_steam(self):
        class Ctx:
            def __init__(self):
                self.store = [
                    {"name": "sid", "value": "s1", "domain": ".ea.com", "path": "/"},
                    {"name": "remid", "value": "r1", "domain": ".ea.com", "path": "/"},
                    {"name": "sid", "value": "s2", "domain": "signin.ea.com", "path": "/"},
                    {"name": "PIM-SESSION-ID", "value": "p1", "domain": "myaccount.ea.com", "path": "/"},
                    {"name": "other", "value": "drop", "domain": ".ea.com", "path": "/"},
                    {"name": "ealocale", "value": "en_HK", "domain": ".ea.com", "path": "/"},
                    {"name": "sessionid", "value": "keep", "domain": "steamcommunity.com", "path": "/"},
                ]
                self.cleared = []

            def cookies(self, urls=None):
                return list(self.store)

            def clear_cookies(self, **kwargs):
                self.cleared.append(kwargs)
                self.store = [
                    cookie
                    for cookie in self.store
                    if not (
                        cookie.get("name") == kwargs.get("name")
                        and cookie.get("domain") == kwargs.get("domain")
                        and (cookie.get("path") or "/") == (kwargs.get("path") or "/")
                    )
                ]

        ctx = Ctx()
        self.assertEqual(ea_pw_register.drop_ea_auth_cookies(ctx), 5)
        names = sorted(cookie["name"] for cookie in ctx.store)
        self.assertEqual(names, ["ealocale", "sessionid"])

    def test_cdp_all_cookies_fill_in_missing_remid(self):
        class Session:
            def send(self, method):
                assert method == "Network.getAllCookies"
                return {
                    "cookies": [
                        {"name": "sid", "value": "s2"},
                        {"name": "remid", "value": "from-cdp", "expires": 1893456000},
                    ]
                }

        class Page:
            pass

        class Ctx:
            pages = []

            def cookies(self, urls=None):
                return [{"name": "sid", "value": "s2"}]

            def new_cdp_session(self, page):
                return Session()

        payload = ea_pw_register.capture_connect_cookies(Ctx(), page=Page())
        self.assertEqual(payload["remid"], "from-cdp")


class EaPwRegisterHelperTests(unittest.TestCase):
    def test_captcha_detection(self):
        self.assertTrue(ea_pw_register.looks_like_captcha('data-sitekey="6LeTest"'))
        self.assertTrue(ea_pw_register.looks_like_captcha("grecaptcha.execute"))
        self.assertFalse(ea_pw_register.looks_like_captcha("<form><input name=email>"))

    def test_juno_mev_email_collection_is_otp_not_captcha(self):
        step = "juno/mevEmailCollection"
        text = "驗證您的電子郵件\n輸入您的代碼\n下一步"
        html = (
            '<script src="https://www.google.com/recaptcha/api.js"></script>'
            '<input maxlength="1"><input maxlength="1">'
        )
        self.assertTrue(ea_pw_register.is_email_verify_step(step, text, html))
        self.assertFalse(
            ea_pw_register.should_abort_as_captcha(html, text=text, step=step)
        )

    def test_english_verify_copy_is_otp(self):
        self.assertTrue(
            ea_pw_register.is_email_verify_step(
                "juno/create", "Verify your email\nEnter your code"
            )
        )

    def test_already_registered_detection(self):
        self.assertTrue(
            ea_pw_register.looks_already_registered(
                "alreadyRegistered", "This email is already registered"
            )
        )
        self.assertTrue(ea_pw_register.looks_already_registered("", "該電郵已註冊"))
        self.assertFalse(ea_pw_register.looks_already_registered("<form>", "下一步"))

    def test_technical_issue_copy(self):
        self.assertTrue(
            ea_pw_register.looks_technical_issue(
                "",
                "發生錯誤… 我們遇到了錯誤，無法完成您的請求。請稍後再試。",
            )
        )
        self.assertFalse(ea_pw_register.looks_technical_issue("<form>", "下一步"))

    def test_hidden_country_select_still_sets_hong_kong(self):
        from playwright.sync_api import sync_playwright

        html = """
        <select name="country" id="country" style="display:none">
          <option value="JP" selected>日本</option>
          <option value="HK">香港</option>
        </select>
        <select name="dobYear"><option value="1999">1999</option></select>
        <button id="countryDobNextBtn">下一步</button>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                self.assertTrue(ea_pw_register.select_country(page, "HK"))
                self.assertEqual(page.locator("select[name=country]").input_value(), "HK")
            finally:
                browser.close()

    def test_visible_country_menu_is_clicked(self):
        from playwright.sync_api import sync_playwright

        html = """
        <div class="otkselect">
          <select name="country" id="country" style="display:none">
            <option value="JP" selected>日本</option>
            <option value="US">United States</option>
          </select>
          <button type="button" class="otkselect-label" id="country-trigger">日本</button>
          <ul id="country-menu" hidden>
            <li role="option" data-value="US">United States</li>
          </ul>
        </div>
        <script>
          document.getElementById('country-trigger').onclick = () => {
            document.getElementById('country-menu').hidden = false;
          };
          document.querySelector('[data-value="US"]').onclick = () => {
            const select = document.getElementById('country');
            select.value = 'US';
            document.getElementById('country-trigger').textContent = 'United States';
            document.getElementById('country-menu').hidden = true;
          };
        </script>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                self.assertTrue(ea_pw_register.select_country(page, "US"))
                self.assertEqual(page.locator("select[name=country]").input_value(), "US")
                self.assertEqual(
                    page.locator("#country-trigger").inner_text(),
                    "United States",
                )
            finally:
                browser.close()

    def test_otk_label_is_clicked_when_select_covers_it(self):
        from playwright.sync_api import sync_playwright

        html = """
        <span class="otkselect" style="position:relative;display:inline-block;width:180px;height:36px">
          <select name="country" id="country" class="otkselect-select"
                  style="position:absolute;inset:0;opacity:0;width:180px;height:36px">
            <option value="JP" selected>日本</option>
            <option value="US">United States</option>
          </select>
          <span class="otkselect-label" id="country-label"
                style="display:inline-block;width:180px;height:36px;line-height:36px">日本</span>
        </span>
        <ul id="country-menu" hidden>
          <li data-value="US">United States</li>
        </ul>
        <script>
          document.getElementById('country-label').addEventListener('click', () => {
            document.getElementById('country-menu').hidden = false;
          });
          document.querySelector('[data-value="US"]').addEventListener('click', () => {
            const select = document.getElementById('country');
            select.value = 'US';
            document.getElementById('country-label').textContent = 'United States';
            document.getElementById('country-menu').hidden = true;
          });
        </script>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                self.assertTrue(ea_pw_register.select_country(page, "US"))
                self.assertEqual(page.locator("select[name=country]").input_value(), "US")
                self.assertEqual(page.locator("#country-label").inner_text(), "United States")
            finally:
                browser.close()

    def test_overlay_select_types_into_the_browser_picker(self):
        from playwright.sync_api import sync_playwright

        html = """
        <span class="otkselect" style="position:relative;display:inline-block;width:220px;height:40px">
          <select name="country" style="position:absolute;inset:0;opacity:0;width:220px;height:40px">
            <option value="US" selected>United States of America</option>
            <option value="HK">Hong Kong</option>
          </select>
          <span class="otkselect-label" style="display:flex;width:220px;height:40px">United States of America</span>
        </span>
        <script>
          document.querySelector("select[name=country]").addEventListener("change", (event) => {
            const chosen = event.target.selectedOptions[0];
            document.querySelector(".otkselect-label").textContent = chosen ? chosen.text : "";
          });
        </script>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                self.assertTrue(ea_pw_register.select_country(page, "HK"))
                self.assertEqual(page.locator("select[name=country]").input_value(), "HK")
                self.assertIn("Hong Kong", page.locator(".otkselect-label").inner_text())
            finally:
                browser.close()

    def test_fill_register_country_dob_refuses_japan_only_select(self):
        class FakePage:
            url = "https://signin.ea.com/p/juno/create"

        with patch.object(ea_pw_register, "email_field_visible", return_value=False), patch.object(
            ea_pw_register, "country_dob_visible", return_value=True
        ), patch.object(
            ea_pw_register, "select_country", return_value=True
        ), patch.object(
            ea_pw_register, "selected_country_code", return_value="JP"
        ), patch.object(
            ea_pw_register, "human_delay"
        ), patch.object(
            ea_pw_register, "click_next", side_effect=AssertionError("must not submit")
        ):
            self.assertFalse(
                ea_pw_register.fill_register_country_dob(
                    FakePage(), "HK", "1", "1", "1999"
                )
            )

    def test_fill_register_country_dob_rejects_technical_issue_page(self):
        class FakePage:
            url = "https://signin.ea.com/p/juno/create"

        state = {"dob": True}

        def fake_country_dob(_page):
            return state["dob"]

        def fake_click_next(_page, timeout=8000):
            state["dob"] = False
            return True

        with patch.object(ea_pw_register, "email_field_visible", return_value=False), patch.object(
            ea_pw_register, "country_dob_visible", side_effect=fake_country_dob
        ), patch.object(
            ea_pw_register, "select_country", return_value=True
        ), patch.object(
            ea_pw_register, "selected_country_code", return_value="HK"
        ), patch.object(
            ea_pw_register, "choose_visible_select", return_value=True
        ), patch.object(
            ea_pw_register, "human_delay"
        ), patch.object(
            ea_pw_register, "click_next", side_effect=fake_click_next
        ), patch.object(
            ea_pw_register, "page_blob", return_value="<html></html>"
        ), patch.object(
            ea_pw_register,
            "visible_text",
            return_value="呃，發生了些意料之外的事情 我們遭遇到一些技術問題",
        ), patch.object(
            ea_pw_register, "looks_technical_issue", return_value=True
        ):
            self.assertFalse(
                ea_pw_register.fill_register_country_dob(
                    FakePage(), "HK", "1", "15", "1999"
                )
            )

    def test_next_register_mailbox_skips_burned_and_steam(self):
        pool = [
            {"email": "used@example.com", "refresh_token": "a"},
            {"email": "steam@example.com", "refresh_token": "b"},
            {"email": "next@example.com", "refresh_token": "c"},
        ]
        chosen = ea_pw_register.next_register_mailbox(
            pool,
            burned={"used@example.com"},
            current_email="used@example.com",
            skip_emails={"steam@example.com"},
        )
        self.assertEqual(chosen["email"], "next@example.com")
        self.assertIsNone(
            ea_pw_register.next_register_mailbox(
                pool,
                burned={"used@example.com", "next@example.com"},
                current_email="used@example.com",
                skip_emails={"steam@example.com"},
            )
        )

    def test_next_button_regex_covers_zh(self):
        self.assertTrue(ea_pw_register.NEXT_RE.search("下一步"))
        self.assertTrue(ea_pw_register.NEXT_RE.search("繼續"))
        self.assertTrue(ea_pw_register.NEXT_RE.search("Create Account"))
        self.assertFalse(ea_pw_register.NEXT_RE.search("Cancel"))

    def test_next_button_regex_covers_login_submit(self):
        self.assertTrue(ea_pw_register.NEXT_RE.search("登入"))
        self.assertTrue(ea_pw_register.NEXT_RE.search("登录"))
        self.assertTrue(ea_pw_register.NEXT_RE.search("Sign In"))


class EaLoginFormWaitTests(unittest.TestCase):
    def test_complete_ea_login_form_checks_remember_me_before_submit(self):
        from playwright.sync_api import sync_playwright

        html = """
        <input id="email" name="email">
        <input id="rememberMe" name="rememberMe" type="checkbox" hidden>
        <a id="logInBtn" role="button">下一步</a>
        <script>
          window.flags = [];
          document.getElementById('logInBtn').onclick = function () {
            window.flags.push(document.getElementById('rememberMe').checked);
            this.remove();
            document.getElementById('email').remove();
            const wrap = document.createElement('div');
            wrap.innerHTML = '<input id="password" name="password" type="password">'
              + '<input id="rememberMe" name="rememberMe" type="checkbox" hidden>'
              + '<button id="logInBtn" type="button">登入</button>';
            document.body.appendChild(wrap);
            document.getElementById('logInBtn').onclick = () => {
              window.flags.push(document.getElementById('rememberMe').checked);
              window.loggedIn = document.getElementById('password').value;
            };
          };
        </script>
        """
        with sync_playwright() as playwright, patch.object(
            ea_pw_register, "human_delay", lambda *_a, **_k: None
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                filled = ea_pw_register.complete_ea_login_form(
                    page, "sn@example.com", "Pw1!aaaaaa", timeout_ms=8000
                )
                self.assertTrue(filled)
                self.assertEqual(page.evaluate("window.loggedIn"), "Pw1!aaaaaa")
                self.assertEqual(page.evaluate("window.flags"), [True, True])
            finally:
                browser.close()

    def test_complete_ea_login_form_waits_for_delayed_password(self):
        from playwright.sync_api import sync_playwright

        html = """
        <input id="email" name="email">
        <a id="logInBtn" role="button" onclick="
          this.remove();
          document.getElementById('email').remove();
          setTimeout(() => {
            const wrap = document.createElement('div');
            wrap.innerHTML = '<input id=password name=password type=password>'
              + '<button id=logInBtn type=button>登入</button>';
            document.body.appendChild(wrap);
            document.getElementById('logInBtn').onclick = () => {
              window.loggedIn = document.getElementById('password').value;
            };
          }, 1200);
        ">下一步</a>
        """
        with sync_playwright() as playwright, patch.object(
            ea_pw_register, "human_delay", lambda *_a, **_k: None
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                filled = ea_pw_register.complete_ea_login_form(
                    page, "sn@example.com", "Pw1!aaaaaa", timeout_ms=8000
                )
                self.assertTrue(filled)
                self.assertEqual(page.evaluate("window.loggedIn"), "Pw1!aaaaaa")
            finally:
                browser.close()

    def test_complete_ea_login_form_keeps_symbol_password(self):
        from playwright.sync_api import sync_playwright

        secret = "Aa1!@#$%wxyz"
        html = f"""
        <input id="password" name="password" type="password">
        <button id="logInBtn" type="button">登入</button>
        <script>
          document.getElementById('logInBtn').onclick = () => {{
            window.loggedIn = document.getElementById('password').value;
          }};
        </script>
        """
        with sync_playwright() as playwright, patch.object(
            ea_pw_register, "human_delay", lambda *_a, **_k: None
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                filled = ea_pw_register.complete_ea_login_form(
                    page, "sn@example.com", secret, timeout_ms=5000
                )
                self.assertTrue(filled)
                self.assertEqual(page.evaluate("window.loggedIn"), secret)
            finally:
                browser.close()

    def test_complete_ea_login_form_retries_after_rejected_password(self):
        from playwright.sync_api import sync_playwright

        html = """
        <input id="password" name="password" type="password">
        <p id="err" class="otkinput-errormsg" style="display:none">
          您的憑證不正確或已過期。請再試一次或重設您的密碼。
        </p>
        <button id="logInBtn" type="button">登入</button>
        <script>
          let n = 0;
          document.getElementById('logInBtn').onclick = () => {
            n += 1;
            const err = document.getElementById('err');
            if (n === 1) { err.style.display = 'block'; return; }
            err.style.display = 'none';
            window.loggedIn = document.getElementById('password').value;
          };
        </script>
        """
        with sync_playwright() as playwright, patch.object(
            ea_pw_register, "human_delay", lambda *_a, **_k: None
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                filled = ea_pw_register.complete_ea_login_form(
                    page, "sn@example.com", "Pw1!aaaaaa", timeout_ms=8000
                )
                self.assertTrue(filled)
                self.assertEqual(page.evaluate("window.loggedIn"), "Pw1!aaaaaa")
            finally:
                browser.close()

    def test_complete_ea_login_form_clicks_continue_without_fields(self):
        from playwright.sync_api import sync_playwright

        html = """
        <a href="javascript:void(0);" role="button" id="continue">繼續</a>
        <script>
          document.getElementById('continue').onclick = function () {
            window.clicked = true;
            document.body.innerHTML = '<input id="password" name="password" type="password">'
              + '<button id="logInBtn" type="button">登入</button>';
            document.getElementById('logInBtn').onclick = function () {
              window.loggedIn = document.getElementById('password').value;
            };
          };
        </script>
        """
        with sync_playwright() as playwright, patch.object(
            ea_pw_register, "human_delay", lambda *_a, **_k: None
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                filled = ea_pw_register.complete_ea_login_form(
                    page, "hq@example.com", "Pw1!aaaaaa", timeout_ms=8000
                )
                self.assertTrue(filled)
                self.assertTrue(page.evaluate("window.clicked === true"))
                self.assertEqual(page.evaluate("window.loggedIn"), "Pw1!aaaaaa")
            finally:
                browser.close()


class LoginPasswordErrorTests(unittest.TestCase):
    def test_login_password_rejected_detects_hk_copy(self):
        class FakeLoc:
            def __init__(self, text="", visible=False):
                self._text = text
                self._visible = visible

            def count(self):
                return 1 if self._text or self._visible else 0

            def is_visible(self):
                return self._visible

            def inner_text(self):
                return self._text

            @property
            def first(self):
                return self

        class FakePage:
            def locator(self, selector):
                if "otkinput-errormsg" in selector:
                    return FakeLoc(
                        "您的憑證不正確或已過期。請再試一次或重設您的密碼。",
                        visible=True,
                    )
                return FakeLoc()

        self.assertTrue(ea_pw_register.login_password_rejected(FakePage()))

    def test_login_password_rejected_false_on_clean_form(self):
        class FakeLoc:
            def count(self):
                return 0

            def is_visible(self):
                return False

            def inner_text(self):
                return ""

            @property
            def first(self):
                return self

        class FakePage:
            def locator(self, _selector):
                return FakeLoc()

        with patch.object(ea_pw_register, "visible_text", return_value="輸入密碼"):
            self.assertFalse(ea_pw_register.login_password_rejected(FakePage()))


class JunoCreateNavigationTests(unittest.TestCase):
    def test_on_juno_create_url(self):
        self.assertTrue(
            ea_pw_register.on_juno_create("https://signin.ea.com/p/juno/create")
        )
        self.assertTrue(
            ea_pw_register.on_juno_create(
                "https://signin.ea.com/p/juno/create?execution=e1s1"
            )
        )
        self.assertFalse(ea_pw_register.on_juno_create("https://www.ea.com/"))
        self.assertFalse(
            ea_pw_register.on_juno_create("https://www.ea.com/login_check")
        )

    def test_goto_register_clicks_through_the_public_home(self):
        class Loc:
            def __init__(self, page, kind):
                self.page = page
                self.kind = kind

            def count(self):
                return 0 if self.kind == "empty" else 1

            def is_visible(self):
                return self.kind != "empty"

            def nth(self, _index):
                return self

            @property
            def first(self):
                return self

        class FakePage:
            def __init__(self):
                self.url = "about:blank"
                self.gotos = []

            def goto(self, url, wait_until=None, timeout=None):
                self.gotos.append(url)
                self.url = url

            def wait_for_timeout(self, _ms):
                return None

            def locator(self, _sel):
                return Loc(self, "empty")

            def get_by_role(self, role, name=None):
                pattern = getattr(name, "pattern", "")
                if "Create an account" in pattern:
                    return Loc(self, "create")
                return Loc(self, "account")

            def get_by_text(self, *_args, **_kwargs):
                return Loc(self, "empty")

        page = FakePage()

        def fake_click(target, loc, timeout=8000):
            del loc, timeout
            if getattr(target, "clicks", 0) >= 1:
                target.url = "https://signin.ea.com/p/juno/create?execution=e1"
            target.clicks = getattr(target, "clicks", 0) + 1
            return True

        with patch.object(ea_pw_register, "human_click", fake_click), patch.object(
            ea_pw_register, "human_delay", lambda *_args, **_kwargs: None
        ):
            ea_pw_register.goto_register(page, settle_ms=0)
        self.assertEqual(page.gotos, ["https://www.ea.com/"])
        self.assertTrue(ea_pw_register.on_juno_create(page.url))
        self.assertFalse(any("locale=zh_HK" in url for url in page.gotos))


class MailWaitAndReloadTests(unittest.TestCase):
    def test_mail_wait_default_is_ninety_and_caps_at_three_minutes(self):
        from apex_registration.flows.ea import try_ea_once as protocol

        from apex_registration.flows.steam import try_steam_once as steam_protocol

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GAME_ACCOUNTS_EMAIL_WAIT_SECONDS", None)
            self.assertEqual(protocol.mail_wait_seconds(), 90)
            self.assertEqual(steam_protocol.mail_wait_seconds(), 90)
        with patch.dict(os.environ, {"GAME_ACCOUNTS_EMAIL_WAIT_SECONDS": "180"}):
            self.assertEqual(protocol.mail_wait_seconds(), 180)
            self.assertEqual(steam_protocol.mail_wait_seconds(), 180)
        with patch.dict(os.environ, {"GAME_ACCOUNTS_EMAIL_WAIT_SECONDS": "900"}):
            self.assertEqual(protocol.mail_wait_seconds(), 180)
            self.assertEqual(steam_protocol.mail_wait_seconds(), 180)

    def test_wait_graph_code_resends_then_reloads_before_giving_up(self):
        events = []

        class FakeBtn:
            def count(self):
                return 1

            def is_visible(self):
                return True

            def click(self):
                events.append("resend")

        class FakeLocator:
            def __init__(self):
                self.first = FakeBtn()

        class FakePage:
            url = "https://signin.ea.com/p/juno/create"

            def locator(self, _sel):
                return FakeLocator()

            def reload(self, **kwargs):
                events.append(("reload", kwargs.get("wait_until")))

        clock = {"t": 0.0}

        def fake_monotonic():
            return clock["t"]

        def fake_sleep(seconds):
            clock["t"] += float(seconds)

        with patch.object(
            ea_pw_register.protocol, "mail_wait_seconds", return_value=90
        ), patch.object(
            ea_pw_register.protocol, "graph_access_token", return_value="tok"
        ), patch.object(
            ea_pw_register.protocol, "graph_find_ea_code", return_value=None
        ), patch.object(
            ea_pw_register.protocol, "cur_step", return_value="mevEmailCollection"
        ), patch.object(
            ea_pw_register, "visible_text", return_value="驗證您的電子郵件"
        ), patch.object(
            ea_pw_register, "page_blob", return_value="<html></html>"
        ), patch.object(
            ea_pw_register, "looks_rate_limited", return_value=False
        ), patch.object(
            ea_pw_register.time, "monotonic", fake_monotonic
        ), patch.object(
            ea_pw_register.time, "sleep", fake_sleep
        ):
            code = ea_pw_register.wait_graph_code(
                {"email": "x@example.com"}, 1.0, page=FakePage()
            )
        self.assertIsNone(code)
        self.assertIn("resend", events)
        self.assertTrue(
            any(item[0] == "reload" for item in events if isinstance(item, tuple))
        )

    def test_wait_graph_code_does_not_reload_or_resend_again_when_rate_limited(self):
        events = []
        state = {"limited": False}

        class FakeBtn:
            def count(self):
                return 1

            def is_visible(self):
                return True

            def click(self):
                events.append("resend")
                state["limited"] = True

        class FakeLocator:
            def __init__(self):
                self.first = FakeBtn()

        class FakePage:
            url = "https://signin.ea.com/p/juno/create"

            def locator(self, _sel):
                return FakeLocator()

            def reload(self, **kwargs):
                events.append(("reload", kwargs.get("wait_until")))

        clock = {"t": 0.0}

        def fake_monotonic():
            return clock["t"]

        def fake_sleep(seconds):
            clock["t"] += float(seconds)

        with patch.object(
            ea_pw_register.protocol, "mail_wait_seconds", return_value=90
        ), patch.object(
            ea_pw_register.protocol, "graph_access_token", return_value="tok"
        ), patch.object(
            ea_pw_register.protocol, "graph_find_ea_code", return_value=None
        ), patch.object(
            ea_pw_register.protocol, "cur_step", return_value="mevEmailCollection"
        ), patch.object(
            ea_pw_register, "visible_text", return_value="您提出請求次數過多"
        ), patch.object(
            ea_pw_register, "page_blob", return_value="<html>您提出請求次數過多</html>"
        ), patch.object(
            ea_pw_register, "looks_rate_limited", side_effect=lambda *_args: state["limited"]
        ), patch.object(
            ea_pw_register.time, "monotonic", fake_monotonic
        ), patch.object(
            ea_pw_register.time, "sleep", fake_sleep
        ):
            code = ea_pw_register.wait_graph_code(
                {"email": "x@example.com"}, 1.0, page=FakePage()
            )
        self.assertIsNone(code)
        self.assertEqual(events, ["resend"])


class HeadedLaunchTests(unittest.TestCase):
    def test_headed_roxy_still_connects_over_cdp(self):
        class FakePage:
            def set_default_timeout(self, _value):
                return None

            def wait_for_timeout(self, _value):
                return None

        class FakeContext:
            pages = [FakePage()]

        class FakeBrowser:
            contexts = [FakeContext()]

        class Chromium:
            def launch(self, **_kwargs):
                raise AssertionError("headed Roxy must not fall back to chromium.launch")

            def connect_over_cdp(self, url):
                self.cdp = url
                return FakeBrowser()

        class Playwright:
            chromium = Chromium()

        with patch.dict(
            os.environ,
            {
                "GAME_ACCOUNTS_EA_REGISTER_ENGINE": "roxybrowser",
                "GAME_ACCOUNTS_EA_REGISTER_HEADED": "1",
            },
        ):
            with patch.object(ea_pw_register, "RoxyBrowserClient") as client_cls:
                client_cls.return_value.ensure_open.return_value = {
                    "id": "dir-1",
                    "cdp": "ws://127.0.0.1:40597/devtools/browser/x",
                    "name": "apex-ea-test",
                }
                browser, context, page, client, profile_id = ea_pw_register.attach_browser(
                    Playwright(),
                    proxy="http://user:pass@gw.dataimpulse.com:823",
                    seed="a@example.com",
                )
        self.assertEqual(profile_id, "dir-1")
        self.assertIs(client, client_cls.return_value)
        self.assertEqual(
            Playwright.chromium.cdp, "ws://127.0.0.1:40597/devtools/browser/x"
        )

    def test_explicit_playwright_launches_chromium(self):
        captured = {}

        class FakePage:
            def set_default_timeout(self, _value):
                return None

        class FakeContext:
            pages = [FakePage()]

            def new_page(self):
                return self.pages[0]

        class FakeBrowser:
            contexts = [FakeContext()]

            def new_context(self, **_kwargs):
                return FakeContext()

        class Chromium:
            def launch(self, **kwargs):
                captured.update(kwargs)
                return FakeBrowser()

            def connect_over_cdp(self, _url):
                raise AssertionError("explicit Playwright must not open Roxy CDP")

        class Playwright:
            chromium = Chromium()

        with patch.dict(
            os.environ,
            {"GAME_ACCOUNTS_EA_REGISTER_ENGINE": "playwright"},
        ):
            browser, context, page, client, profile_id = ea_pw_register.attach_browser(
                Playwright(),
                proxy="http://user:pass@gw.dataimpulse.com:823",
                seed="a@example.com",
            )
        self.assertTrue(captured.get("headless", False))
        self.assertIsNone(client)
        self.assertIsNone(profile_id)
        self.assertIs(page, FakeContext.pages[0])
