"""Bounded recovery of freshly verified EA credentials.

All actions use controls from the current EA window. In particular, SUBMIT
is located after focusing/typing: EA expands its password rules and moves it.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import re
import time

from .ea_app import EaAppAutomationError, EaCaptchaRequired, EaOtpUnavailable
from .ea_pages import EaPage, password_page_blocker, phrase_point


class EaPasswordRecoveryMixin:
    def configure_password_recovery(self, handler):
        self._password_recovery_handler = handler

    def _recovery_credentials(self, action, credentials):
        handler = getattr(self, "_password_recovery_handler", None)
        if not callable(handler):
            raise EaAppAutomationError("密码恢复凭据服务不可用")
        updated = handler(action, credentials)
        if updated.login_identifier.casefold() != credentials.login_identifier.casefold():
            raise EaAppAutomationError("密码恢复凭据与本次账号不匹配")
        if action == "PREPARE" and not updated.password_reset_id:
            raise EaAppAutomationError("新密码没有持久保存，已停止")
        if self.evidence is not None:
            self.evidence.protect(updated.login_identifier, updated.password)
        return updated

    def _leave_recovery_captcha(self, hwnd):
        # This is still the rejected account's pre-login recovery flow. Try
        # the visible BACK before handing the account back; never submit a
        # captcha or claim sign-out from an unknown screen.
        for _ in range(4):
            observation = self._observe(hwnd)
            if observation.page is EaPage.EMAIL:
                return
            if observation.page is not EaPage.CAPTCHA:
                self._return_to_account_page(hwnd, observation)
                return
            back = self._login_back_point(observation)
            if back is None:
                return
            self._click_point(hwnd, *back)
            self.sleep(1.0)

    @staticmethod
    def _reset_password_rejected(observation):
        # Positive server validation only. A timeout/network failure can mean
        # the change succeeded, so it must not invent another password.
        text = "".join(observation.normalized)
        return observation.page is EaPage.RESET_PASSWORD and any(term in text for term in (
            "previouslyusedpassword", "passwordhasbeenused", "chooseadifferentpassword",
            "passworddoesnotmeet", "passwordcannotbethesame", "passwordcanotbethesame",
            "passwordmustbedifferent", "invalidpassword", "passwordisnotvalid",
            "passwordhasalreadybeenused", "cantusethesamepassword",
        ))

    @staticmethod
    def _reset_password_valid(password):
        return (8 <= len(password) <= 64 and re.search(r"[a-z]", password)
            and re.search(r"[A-Z]", password) and re.search(r"[0-9]", password))

    def _reset_result(self, hwnd):
        # Do not treat the first unchanged frame after SUBMIT as a rejection.
        deadline = min(time.monotonic() + 25, self._password_recovery_deadline)
        while time.monotonic() < deadline:
            observation = self._observe(hwnd)
            if observation.page is EaPage.CAPTCHA:
                raise EaCaptchaRequired("EA 密码恢复出现 Captcha")
            self._raise_if_account_banned(hwnd, observation)
            if observation.page is EaPage.RESET_SUCCESS or self._reset_password_rejected(observation):
                return observation
            self.sleep(0.5)
        raise EaAppAutomationError("EA 未确认密码重设结果")

    def _reset_submit(self, hwnd, observation, password):
        # Filled forms have no placeholder. The PASSWORD label is above the
        # input, so use the earlier field's box only to replace its contents.
        point = self._recovery_control(observation, ("enteryourpassword",), y_range=(0.35, 0.80))
        self._click_point(hwnd, *point)
        self._clear_focused_field()
        self._type_secret(password)
        self.sleep(0.8)
        typed = self._observe(hwnd)
        if typed.page is not EaPage.RESET_PASSWORD or time.monotonic() >= self._password_recovery_deadline:
            raise EaAppAutomationError("EA 设置密码页面发生变化，已停止提交")
        self._record("password-recovery-password-typed", typed)
        self._click_point(hwnd, *self._recovery_control(typed, ("submit",), y_range=(0.45, 0.95)))
        return self._reset_result(hwnd)

    def _recovery_control(self, observation, terms, *, y_range=(0.20, 0.95), exact=True):
        placed = replace(observation, tokens=tuple(
            token for token in observation.tokens if token.confidence >= 0.85
        ))
        point = self._anchor(placed, terms, exact=exact, x_range=(0.05, 0.95), y_range=y_range)
        if point is None and exact:
            for term in terms:
                point = phrase_point(placed.tokens, placed.rect, term,
                                     x_range=(0.05, 0.95), y_range=y_range)
                if point is not None:
                    break
        if point is None:
            raise EaAppAutomationError("EA 密码恢复未找到可信操作控件")
        return point

    def _recovery_wait(self, hwnd, pages, *, timeout_s=25.0):
        remaining = self._password_recovery_deadline - time.monotonic()
        if remaining <= 0:
            raise EaAppAutomationError("EA 密码恢复超时")
        observation = self._wait_for_page(
            hwnd, (*pages, EaPage.CAPTCHA, EaPage.BANNED, EaPage.EXPIRED_SESSION),
            timeout_s=min(timeout_s, remaining),
        )
        if observation.page is EaPage.CAPTCHA:
            raise EaCaptchaRequired("EA 密码恢复出现 Captcha，已暂停")
        self._raise_if_account_banned(hwnd, observation)
        if observation.page not in pages:
            raise EaAppAutomationError("EA 密码恢复未到达预期页面")
        return observation

    def _recover_password(self, hwnd, credentials, otp_supplier):
        """Recover once after a rejection bound to this call's identifier.

The caller keeps the lease active and renews it during email/OTP waits.
A new candidate is persisted encrypted before changing the UI password.
"""
        self._password_recovery_deadline = time.monotonic() + 240.0
        rejected = self._observe(hwnd)
        if (rejected.page is not EaPage.PASSWORD
                or not rejected.has_login_error()
                or not getattr(self, "_login_identifier_verified", False)):
            raise EaAppAutomationError("EA 密码恢复缺少本次账号的凭据拒绝证据")
        # Prefer the separate link at the bottom over the inline error copy.
        point = self._recovery_control(
            rejected, ("forgotyourpassword", "resetyourpassword", "忘记密码", "重置密码"),
            y_range=(0.45, 0.95), exact=False,
        )
        self._record("password-recovery-start", rejected)
        self.notify("EA 拒绝本次账号凭据，尝试重设原密码")
        self._click_point(hwnd, *point)
        account = self._recovery_wait(hwnd, (EaPage.RECOVERY_ACCOUNT,))
        point = self._recovery_control(account, ("enteryouremailoreaid",), y_range=(0.35, 0.80))
        self._click_point(hwnd, *point)
        self._clear_focused_field()
        self._type_secret(credentials.login_identifier)
        self.sleep(0.8)
        typed = self._observe(hwnd)
        if (typed.page is not EaPage.RECOVERY_ACCOUNT
                or not self._verified_identifier_echo(typed, credentials.login_identifier)):
            raise EaAppAutomationError("EA 密码恢复未核实本次账号输入，已停止")
        self._record("password-recovery-account-verified", typed, identifierVerified=True)
        challenge_started_at = datetime.now(timezone.utc)
        self._click_point(hwnd, *self._recovery_control(typed, ("next",), y_range=(0.45, 0.95)))

        selected_method = None
        observation = self._recovery_wait(hwnd, (EaPage.OTP_METHOD, EaPage.OTP))
        if observation.has_login_error():
            raise EaOtpUnavailable("EA 密码恢复验证未通过")
        if observation.page is EaPage.OTP_METHOD:
            selected_method, challenge_started_at = self._choose_otp_method(
                hwnd, observation, credentials.otp_methods,
            )
            observation = self._recovery_wait(hwnd, (EaPage.OTP,))
        method = self._otp_page_method(observation, credentials.otp_methods, selected_method)
        self._submit_otp(hwnd, observation, otp_supplier, method=method,
                         challenge_started_at=challenge_started_at)
        observation = self._recovery_wait(hwnd, (EaPage.RESET_PASSWORD,), timeout_s=20.0)
        self._record("password-recovery-verified", observation)
        field_observation = observation
        success = None
        if self._reset_password_valid(credentials.password):
            success = self._reset_submit(hwnd, field_observation, credentials.password)
        if success is None or self._reset_password_rejected(success):
            # Original fails local rules or is explicitly refused by EA.
            # Only one generated replacement is attempted in this recovery.
            credentials = self._recovery_credentials("PREPARE", credentials)
            if not self._reset_password_valid(credentials.password):
                raise EaAppAutomationError("服务端新密码不满足 EA 要求")
            self._record("password-recovery-new-password")
            self.notify("原密码无法重设，使用已加密保存的新密码")
            # Re-observe before replacing. The input remains focused after the
            # first attempt: select-all avoids guessing a hidden placeholder.
            if success is None:
                success = self._reset_submit(hwnd, field_observation, credentials.password)
            else:
                current = self._observe(hwnd)
                if not self._reset_password_rejected(current):
                    raise EaAppAutomationError("EA 新密码提交前页面发生变化")
                # Anchor on the current PASSWORD label and the original input
                # offset: the requirements panel only moves SUBMIT, not input.
                label = self._recovery_control(current, ("password",), y_range=(0.35, 0.75))
                rect = current.rect
                self._click_point(hwnd, label[0], label[1] + int((rect[3] - rect[1]) * 0.045))
                self._clear_focused_field()
                self._type_secret(credentials.password)
                self.sleep(0.8)
                typed = self._observe(hwnd)
                if typed.page is not EaPage.RESET_PASSWORD or time.monotonic() >= self._password_recovery_deadline:
                    raise EaAppAutomationError("EA 新密码设置页发生变化")
                self._record("password-recovery-password-typed", typed)
                self._click_point(hwnd, *self._recovery_control(typed, ("submit",), y_range=(0.45, 0.95)))
                success = self._reset_result(hwnd)
            if success.page is not EaPage.RESET_SUCCESS:
                raise EaAppAutomationError("EA 新密码也未被接受")
        if credentials.password_reset_id is not None:
            credentials = self._recovery_credentials("COMMIT", credentials)
        self._record("password-recovery-success", success)
        self.notify("EA 密码重设成功，重新登录本次账号")
        self._click_point(hwnd, *self._recovery_control(success, ("signin",), y_range=(0.25, 0.80)))
        account = self._recovery_wait(hwnd, (EaPage.EMAIL,))
        if password_page_blocker(account.normalized) != "STILL_ON_ACCOUNT_PAGE":
            raise EaAppAutomationError("EA 重设后未返回清晰登录账号页")
        self._record("password-recovery-login-ready", account)
        return credentials
