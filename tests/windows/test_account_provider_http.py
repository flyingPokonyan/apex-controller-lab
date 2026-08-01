from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.account_provider import (
    CleanupEvidence,
    CompletionEvidence,
    HttpAccountProvider,
    IdempotencyConflictError,
    LeaseProviderError,
    LeaseStaleError,
    LeaseState,
    ProviderHttpResponse,
)


NOW = datetime.now(timezone.utc)


def timestamp(offset_s: int = 0) -> str:
    return (NOW + timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")


def lease_payload(status: str = "CLAIMED", **changes) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "leaseId": "lease_1",
        "leaseFence": 7,
        "accountId": "acct_1",
        "status": status,
        "targetLevel": 20,
        "expectedIdentity": {"platform": "ea", "accountId": "ea_1"},
        "issuedAt": timestamp(),
        "renewAfter": timestamp(30),
        "expiresAt": timestamp(120),
        "serverTime": timestamp(),
    }
    payload.update(changes)
    return payload


def response(
    payload: dict[str, object] | None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> ProviderHttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return ProviderHttpResponse(status, headers or {}, body)


class FakeTransport:
    def __init__(self, *responses: ProviderHttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, headers, body, timeout_s):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json": None if body is None else json.loads(body),
                "timeout": timeout_s,
            }
        )
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class HttpAccountProviderTest(unittest.TestCase):
    def provider(self, transport: FakeTransport) -> HttpAccountProvider:
        return HttpAccountProvider(
            "https://runner.example/v1/runner/account-leases",
            "provider-secret-token",
            client_version="0.5.0",
            transport=transport,
        )

    def test_private_ip_http_is_allowed_but_public_http_is_rejected(self) -> None:
        provider = HttpAccountProvider(
            "http://192.168.31.200/v1/runner/account-leases",
            "provider-secret-token",
            client_version="0.5.0",
            transport=FakeTransport(response(None, status=204)),
        )
        self.assertEqual(provider.lease_url, "http://192.168.31.200/v1/runner/account-leases")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            HttpAccountProvider(
                "http://runner.example/v1/runner/account-leases",
                "provider-secret-token",
                client_version="0.5.0",
            )

    def test_claim_uses_contract_and_keeps_token_out_of_repr(self) -> None:
        transport = FakeTransport(response(lease_payload()))
        provider = self.provider(transport)

        lease = provider.claim("claim_1", "LEVEL_TO_TARGET")

        self.assertEqual(lease.expected_ea_account_id, "ea_1")
        self.assertEqual(lease.target_level, 20)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["json"],
            {
                "schemaVersion": 1,
                "taskType": "LEVEL_TO_TARGET",
                "clientVersion": "0.5.0",
            },
        )
        self.assertEqual(call["headers"]["Idempotency-Key"], "claim_1")
        self.assertEqual(
            call["headers"]["Authorization"], "Bearer provider-secret-token"
        )
        self.assertNotIn("provider-secret-token", repr(provider))

    def test_claim_204_honors_retry_after(self) -> None:
        provider = self.provider(
            FakeTransport(response(None, status=204, headers={"Retry-After": "45"}))
        )

        self.assertIsNone(provider.claim("claim_1", "LEVEL_TO_TARGET"))
        self.assertEqual(provider.claim_retry_after_s, 45.0)

    def test_status_mapping_and_account_binding_are_fail_closed(self) -> None:
        statuses = [
            "CLAIMED",
            "RUNNING",
            "COMPLETION_PENDING",
            "EXPIRED_UNCONFIRMED",
            "COMPLETED",
            "FAILED",
            "RELEASED",
        ]
        transport = FakeTransport(
            response(lease_payload()),
            *(response(lease_payload(item)) for item in statuses),
            response(lease_payload("RUNNING", accountId="acct_other")),
        )
        provider = self.provider(transport)
        provider.claim("claim_1", "LEVEL_TO_TARGET")

        states = [provider.status("lease_1", 7).state for _ in statuses]

        self.assertEqual(
            states,
            [
                LeaseState.ACTIVE,
                LeaseState.ACTIVE,
                LeaseState.COMPLETION_PENDING,
                LeaseState.EXPIRED_UNCONFIRMED,
                LeaseState.COMPLETED,
                LeaseState.CLOSED,
                LeaseState.CLOSED,
            ],
        )
        with self.assertRaises(LeaseStaleError):
            provider.status("lease_1", 7)

    def test_credentials_renew_otp_and_close_bodies_match_server(self) -> None:
        otp_payload = {
            "code": "123456",
            "receivedAt": timestamp(5),
            "expiresAt": timestamp(35),
            "challengeId": "challenge_1",
        }
        transport = FakeTransport(
            response({"loginIdentifier": "ea@example.com", "password": "secret"}),
            response(lease_payload("RUNNING")),
            response(otp_payload),
            response(lease_payload("COMPLETION_PENDING"), status=202),
        )
        provider = self.provider(transport)

        credentials = provider.credentials("lease_1", 7, "credentials_1")
        renewed = provider.renew(
            "lease_1", 7, "renew_1", "APEX_PLAYING", "run_1"
        )
        otp = provider.request_otp(
            "lease_1", 7, "otp_1", "challenge_1", NOW
        )
        closed = provider.close(
            "lease_1",
            7,
            "close_1",
            "TARGET_REACHED",
            "run_1",
            CompletionEvidence("run_1", 20, 42, 43),
            "TARGET_LEVEL_CONFIRMED",
            CleanupEvidence(True, True, True),
        )

        self.assertNotIn("ea@example.com", repr(credentials))
        self.assertNotIn("secret", repr(credentials))
        self.assertEqual(renewed.state, LeaseState.ACTIVE)
        self.assertNotIn("123456", repr(otp))
        self.assertEqual(closed.state, LeaseState.COMPLETION_PENDING)
        self.assertEqual(
            transport.calls[1]["json"],
            {
                "schemaVersion": 1,
                "leaseFence": 7,
                "phase": "APEX_PLAYING",
                "runId": "run_1",
            },
        )
        close_body = transport.calls[3]["json"]
        self.assertEqual(close_body["outcome"], "TARGET_REACHED")
        self.assertEqual(close_body["lobbyProgressSeq"], 42)
        self.assertEqual(close_body["runFinishedSeq"], 43)
        self.assertNotIn("level", close_body)
        for index, operation_id in enumerate(
            ("credentials_1", "renew_1", "otp_1", "close_1")
        ):
            self.assertEqual(
                transport.calls[index]["headers"]["Idempotency-Key"],
                operation_id,
            )

    def test_error_classification_and_expected_identity(self) -> None:
        stale = response(
            {
                "error": {
                    "code": "STALE_LEASE",
                    "message": "stale",
                    "retryable": False,
                }
            },
            status=409,
        )
        conflict = response(
            {
                "error": {
                    "code": "IDEMPOTENCY_CONFLICT",
                    "message": "conflict",
                    "retryable": False,
                }
            },
            status=409,
        )
        retryable = response(
            {"error": {"code": "BUSY", "message": "busy", "retryable": False}},
            status=429,
        )
        wrong_identity = response(
            lease_payload(expectedIdentity={"platform": "steam", "accountId": "steam_1"})
        )
        provider = self.provider(
            FakeTransport(stale, conflict, retryable, wrong_identity, OSError("offline"))
        )

        with self.assertRaises(LeaseStaleError):
            provider.renew("lease_1", 7, "renew_1", "EA_STARTING", None)
        with self.assertRaises(IdempotencyConflictError):
            provider.claim("claim_1", "LEVEL_TO_TARGET")
        with self.assertRaises(LeaseProviderError) as busy:
            provider.current()
        self.assertTrue(busy.exception.retryable)
        with self.assertRaisesRegex(LeaseProviderError, "EA 身份"):
            provider.claim("claim_2", "LEVEL_TO_TARGET")
        with self.assertRaises(LeaseProviderError) as offline:
            provider.current()
        self.assertTrue(offline.exception.retryable)


if __name__ == "__main__":
    unittest.main()
