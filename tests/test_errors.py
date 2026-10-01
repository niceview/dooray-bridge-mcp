"""spec §2(`errors.py`) — 예외 계층과 §2-1 표의 문장."""

from __future__ import annotations

import unittest

from dooray_core import errors


class HierarchyTest(unittest.TestCase):
    def test_every_error_descends_from_dooray_error(self) -> None:
        for name in (
            "ConfigError",
            "TokenMissingError",
            "NetworkError",
            "CallLimitError",
            "WriteNotAllowedError",
            "ApiError",
        ):
            with self.subTest(name=name):
                self.assertTrue(issubclass(getattr(errors, name), errors.DoorayError))

    def test_status_errors_descend_from_api_error(self) -> None:
        for name in (
            "AuthError",
            "ForbiddenError",
            "NotFoundError",
            "RateLimitError",
            "ServerError",
            "RedirectError",
        ):
            with self.subTest(name=name):
                self.assertTrue(issubclass(getattr(errors, name), errors.ApiError))

    def test_forbidden_error_is_not_the_builtin(self) -> None:
        self.assertIsNot(errors.ForbiddenError, PermissionError)
        self.assertFalse(issubclass(errors.ForbiddenError, PermissionError))

    def test_message_is_the_only_string(self) -> None:
        exc = errors.DoorayError("문장 하나")
        self.assertEqual(str(exc), "문장 하나")
        self.assertEqual(exc.message, "문장 하나")

    def test_api_error_carries_status(self) -> None:
        exc = errors.ApiError("문장", 404)
        self.assertEqual(exc.status, 404)

    def test_call_limit_error_carries_limit_and_used(self) -> None:
        exc = errors.CallLimitError(50, 51)
        self.assertEqual((exc.limit, exc.used), (50, 51))
        self.assertIn("50", str(exc))


class MessageTest(unittest.TestCase):
    """§2-1 표. 문장에 URL·쿼리·요청 헤더·응답 본문이 들어가지 않는다."""

    def test_401(self) -> None:
        self.assertIn("토큰이 유효하지 않습니다", errors.api_message(401))
        self.assertIn("DOORAY_API_TOKEN", errors.api_message(401))

    def test_403(self) -> None:
        text = errors.api_message(403)
        self.assertIn("권한이 없습니다", text)
        self.assertIn("IP 접근 제어", text)

    def test_404(self) -> None:
        text = errors.api_message(404)
        self.assertIn("찾을 수 없습니다(상태 404)", text)

    def test_429(self) -> None:
        self.assertIn("요청 제한에 걸렸습니다", errors.api_message(429))

    def test_5xx_carries_the_status(self) -> None:
        self.assertIn("(상태 503)", errors.api_message(503))
        self.assertIn("일시적으로 응답하지 못했습니다", errors.api_message(503))

    def test_3xx_says_it_did_not_follow(self) -> None:
        text = errors.api_message(302)
        self.assertIn("(상태 302)", text)
        self.assertIn("따라가지 않았습니다", text)

    def test_other_4xx(self) -> None:
        text = errors.api_message(400)
        self.assertIn("요청을 거절했습니다(상태 400)", text)

    def test_fixed_sentences_exist(self) -> None:
        self.assertIn("해석하지 못했습니다(상태 500)", errors.not_json_message(500))
        self.assertIn("연결하지 못했습니다", errors.NETWORK_MESSAGE)
        self.assertIn("토큰을 찾지 못했습니다", errors.TOKEN_MISSING_MESSAGE)
        self.assertIn("상한 7회", errors.CALL_LIMIT_MESSAGE.format(limit=7))

    def test_no_message_leaks_a_url_or_body(self) -> None:
        for status in (302, 400, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                text = errors.api_message(status)
                self.assertNotIn("://", text)
                self.assertNotIn("Authorization", text)
