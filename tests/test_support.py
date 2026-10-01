"""테스트 장치 자체의 동작 확인 (spec §11-1). `mock_transport`는 묶음 2에서 본격적으로 쓰인다."""

from __future__ import annotations

import logging
import sys
import unittest
from collections.abc import Callable
from pathlib import Path

import httpx2

from dooray_core import config
from support import FAKE_TOKEN, CoreTestCase, load_fixture, mock_transport


class MockTransportTest(CoreTestCase):
    def test_mock_transport_answers_the_table_and_records_requests(self) -> None:
        transport = mock_transport(
            {
                ("GET", "/common/v1/members/me"): (
                    200,
                    load_fixture("members_me"),
                    {"X-RateLimit-Remaining": "19"},
                )
            }
        )
        with httpx2.Client(transport=transport, base_url="http://test") as client:
            response = client.get("/common/v1/members/me", params={"a": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-RateLimit-Remaining"], "19")
        self.assertIn("result", response.json())

        sent = transport.requests
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].method, "GET")
        self.assertEqual(sent[0].url.path, "/common/v1/members/me")
        self.assertEqual(sent[0].url.params["a"], "1")

    def test_mock_transport_fails_loudly_on_an_unexpected_path(self) -> None:
        transport = mock_transport({("GET", "/common/v1/members/me"): (200, {}, None)})
        with httpx2.Client(transport=transport, base_url="http://test") as client:
            with self.assertRaises(AssertionError):
                client.get("/project/v1/projects")

    def test_mock_transport_walks_a_list_of_replies_in_order(self) -> None:
        # 재시도 테스트(T3~T6)에 필요하다 — 같은 경로가 호출마다 다른 응답을 준다.
        transport = mock_transport(
            {
                ("GET", "/retry"): [
                    (500, {"header": {}}, None),
                    (429, {"header": {}}, {"Retry-After": "3"}),
                    (200, {"result": "끝"}, None),
                ]
            }
        )
        with httpx2.Client(transport=transport, base_url="http://test") as client:
            first = client.get("/retry")
            second = client.get("/retry")
            third = client.get("/retry")

        self.assertEqual(first.status_code, 500)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.headers["Retry-After"], "3")
        self.assertEqual(third.status_code, 200)
        self.assertEqual(third.json()["result"], "끝")
        self.assertEqual(len(transport.requests), 3)

    def test_mock_transport_repeats_the_last_reply_of_a_list(self) -> None:
        transport = mock_transport({("GET", "/retry"): [(500, {}, None), (200, {}, None)]})
        with httpx2.Client(transport=transport, base_url="http://test") as client:
            codes = [client.get("/retry").status_code for _ in range(4)]
        self.assertEqual(codes, [500, 200, 200, 200])

    def test_mock_transport_keeps_the_single_reply_form(self) -> None:
        transport = mock_transport({("GET", "/once"): (200, {"result": 1}, None)})
        with httpx2.Client(transport=transport, base_url="http://test") as client:
            codes = [client.get("/once").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 200])

    def test_mock_transport_rejects_an_empty_reply_list(self) -> None:
        transport = mock_transport({("GET", "/none"): []})
        with httpx2.Client(transport=transport, base_url="http://test") as client:
            with self.assertRaises(AssertionError):
                client.get("/none")


class GuardTest(CoreTestCase):
    """완료 기준 2·3·4 — 바깥 세계로 나가는 길이 정말 막혀 있는지 본다."""

    def test_real_home_folder_is_out_of_reach(self) -> None:
        with self.assertRaises(AssertionError):
            Path.home()

    def test_real_network_is_out_of_reach(self) -> None:
        with self.assertRaises(AssertionError):
            httpx2.HTTPTransport().handle_request(
                httpx2.Request("GET", "http://127.0.0.1:1/")
            )

    def test_default_keychain_runner_is_out_of_reach(self) -> None:
        with self.assertRaises(AssertionError):
            config._run_security(["/usr/bin/security"])
        # tearDown의 "기본 실행기가 불렸다" 검사를 이 테스트만 면제한다.
        self.security.calls.clear()

    def test_the_temporary_home_is_what_config_sees(self) -> None:
        self.assertEqual(config.paths().home, self.home)
        self.assertNotIn(".dooray-bridge", str(self.home))


class FixtureTest(CoreTestCase):
    def test_load_fixture_reads_json_with_or_without_the_extension(self) -> None:
        self.assertEqual(load_fixture("members_me"), load_fixture("members_me.json"))
        self.assertIn("header", load_fixture("members_me"))


class WidenedTearDownTest(unittest.TestCase):
    """002 완료 기준 13·14·15 — 넓힌 `tearDown`이 **실제로 잡는지** 일부러 깨뜨려 본다.

    `CoreTestCase`를 상속하지 않는다. 안쪽 사례가 자기 장치를 세우고 무너뜨리는 것을
    바깥에서 지켜봐야 하기 때문이다 (001 묶음 1의 변이 검증과 같은 방식).
    """

    def outcome(self, leak: Callable[[CoreTestCase], None]) -> unittest.TestResult:
        """`leak`만 하는 사례를 하나 만들어 돌리고 결과를 돌려준다."""

        class Leaky(CoreTestCase):
            def runTest(inner) -> None:  # noqa: N805 — unittest가 부르는 이름이다
                leak(inner)

        return Leaky().run()

    def failure_text(self, result: unittest.TestResult) -> str:
        self.assertEqual(result.errors, [], result.errors)
        self.assertEqual(len(result.failures), 1, result.failures)
        return result.failures[0][1]

    def test_a_clean_case_passes(self) -> None:
        # 대조군. 아무것도 흘리지 않으면 넓힌 검사가 조용하다.
        result = self.outcome(lambda case: None)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)

    def test_a_token_written_to_stderr_is_caught(self) -> None:
        result = self.outcome(lambda case: sys.stderr.write(f"경고: {FAKE_TOKEN}\n"))
        self.assertIn("토큰이 stderr에 남았습니다.", self.failure_text(result))

    def test_a_token_logged_to_the_root_logger_is_caught(self) -> None:
        result = self.outcome(
            lambda case: logging.getLogger("mcp.test").error("토큰 %s", FAKE_TOKEN)
        )
        self.assertIn("토큰이 로그 레코드에 남았습니다.", self.failure_text(result))

    def test_a_token_in_a_logged_traceback_is_caught(self) -> None:
        # `Formatter().format(record)`가 `exc_info`까지 문자열로 만든다 (mcp-notes §5).
        def leak(case: CoreTestCase) -> None:
            try:
                raise ValueError(FAKE_TOKEN)
            except ValueError:
                logging.getLogger("mcp.test").exception("도구가 실패했습니다")

        self.assertIn("토큰이 로그 레코드에 남았습니다.", self.failure_text(self.outcome(leak)))

    def test_anything_written_to_stdout_is_caught(self) -> None:
        # 유출 검사가 아니라 프로토콜 채널 보호다 — 토큰이 아니어도 걸린다 (mcp-notes §6).
        result = self.outcome(lambda case: sys.stdout.write("아무 말"))
        self.assertIn("stdout에 출력이 있었습니다", self.failure_text(result))
