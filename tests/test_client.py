"""spec §4·§5·§6(`client.py`) — §11-3의 T·D 케이스."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import threading
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx2

from dooray_core import client, config, errors, models
from dooray_core.errors import (
    ApiError,
    AuthError,
    CallLimitError,
    ConfigError,
    ForbiddenError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RedirectError,
    ServerError,
    WriteNotAllowedError,
)
from support import FAKE_TOKEN, CoreTestCase, load_fixture, mock_transport

KST = timezone(timedelta(hours=9))
BASE = config.ALLOWED_API_BASES[0]
ME = "/common/v1/members/me"
NOW = datetime(2026, 9, 20, 14, 3, 11, tzinfo=KST)

# 감사 로그 한 줄의 필드. 이 목록이 전부다 (spec §5-3).
AUDIT_FIELD_NAMES = {
    "kind",
    "ts",
    "tool",
    "mode",
    "method",
    "path",
    "project_id",
    "post_id",
    "status",
    "ok",
    "error",
    "ms",
    "attempt",
    "calls",
    "remaining",  # 003 완료 기준 27. 응답의 `X-RateLimit-Remaining` 또는 null
    "comment_id",
    "parent_id",  # 010 spec §4-6. 도구 줄에만 값이 들어가고 api 줄은 언제나 null
    "changed",  # 012 spec §5-6. 도구 줄에만 이름 목록이 들어가고 api 줄은 언제나 null
}


class FakeSleep:
    """실제로 자지 않는 대역. 무엇을 얼마나 기다렸는지 기록한다."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class TickingSleep(FakeSleep):
    """자는 대신 시계를 그만큼 앞으로 돌린다 (003 완료 기준 23).

    실제 `time.sleep`도 실제 시계도 쓰지 않는다 — 버킷이 시간과 함께 다시 차는 것을
    흔들림 없이 재는 유일한 방법이다. `clock=lambda: sleep.now`로 함께 넘긴다.
    """

    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0

    def __call__(self, seconds: float) -> None:
        super().__call__(seconds)
        self.now += seconds


def failing_transport(error: Exception | None = None) -> httpx2.MockTransport:
    """httpx2 예외를 던지는 가짜 전송 계층."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise error or httpx2.ConnectError("연결 실패", request=request)

    transport = httpx2.MockTransport(handler)
    transport.requests = []  # type: ignore[attr-defined]
    return transport


def me_routes() -> dict:
    return {("GET", ME): (200, load_fixture("members_me"), None)}


class TransportCase(CoreTestCase):
    """전송 계층 테스트의 공통 장치."""

    def setUp(self) -> None:
        super().setUp()
        self.sleep = FakeSleep()
        # 오래된 로그 훑기와 경고는 프로세스당 한 번이다 (§5-3). 테스트마다 처음 상태로 되돌린다.
        self.enterContext(mock.patch.object(client, "_audit_swept", False))
        self.enterContext(mock.patch.object(client, "_audit_warned", False))

    def make(self, routes: object, **kwargs: object) -> tuple[client.Transport, object]:
        http = routes if isinstance(routes, httpx2.MockTransport) else mock_transport(routes)
        kwargs.setdefault("now", lambda: NOW)
        kwargs.setdefault("tool", "test")
        # 고정 시계: 흐른 시간이 0이라 호출 사이 대기가 정확히 최소 간격이 된다 (003 spec §1).
        kwargs.setdefault("clock", lambda: 0.0)
        transport = client.Transport(
            FAKE_TOKEN, BASE, http_transport=http, sleep=self.sleep, **kwargs
        )
        self.addCleanup(transport.close)
        return transport, http

    def temp_log_dir(self) -> Path:
        """두 번째 임시 홈의 `logs/`. 패턴에 맞지 않는 파일을 놓아 볼 수 있다.

        삭제는 **기본 로그 폴더에서만** 일어나므로(묶음 2 검토) 홈 자체를 옮긴다.
        tearDown의 파일 검사는 첫 번째 임시 홈만 보므로 여기에는 아무 파일이나 둘 수 있다.
        """
        second = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="dooray-home2-")))
        self.enterContext(mock.patch.dict(os.environ, {"DOORAY_BRIDGE_HOME": str(second)}))
        folder = second.resolve() / "logs"
        folder.mkdir(mode=0o700)
        return folder

    def audit_lines(self, path: Path | None = None) -> list[dict]:
        path = path or (self.home / "logs" / "audit-2026-09-20.jsonl")
        text = path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


class TokenLeakTest(TransportCase):
    """완료 기준 57과 T2. **구현보다 먼저 쓴 테스트다.**"""

    def test_T2_no_token_in_the_transport_repr_or_its_client_headers(self) -> None:
        transport, _ = self.make(me_routes())
        transport.request("GET", ME)

        self.record(transport)
        self.assertNotIn(FAKE_TOKEN, repr(transport))
        self.assertNotIn(FAKE_TOKEN, str(transport))
        # httpx가 Authorization을 가려 준다는 동작에 기댄다. 바뀌면 이 줄이 잡는다.
        self.assertNotIn(FAKE_TOKEN, repr(transport._client.headers))
        self.assertNotIn(FAKE_TOKEN, str(transport._client.headers))
        self.assertNotIn(FAKE_TOKEN, repr(transport._client))
        # 토큰은 인스턴스 속성으로 남지 않는다 (§2-2의 4).
        self.assertNotIn(FAKE_TOKEN, repr(vars(transport)))

    def test_T2_no_url_query_or_token_in_an_api_exception(self) -> None:
        routes = {("GET", "/project/v1/projects"): (403, load_fixture("error_400"), None)}
        transport, _ = self.make(routes)
        with self.assertRaises(ForbiddenError) as caught:
            transport.request("GET", "/project/v1/projects", params={"member": "me"})

        exc = self.record(caught.exception)
        for text in (str(exc), repr(exc)):
            self.assertNotIn(FAKE_TOKEN, text)
            self.assertNotIn("/project/v1/projects", text)
            self.assertNotIn("member=me", text)
            self.assertNotIn("api.dooray.com", text)
        # 응답 본문도 문장에 넣지 않는다 (§2-2의 3).
        self.assertNotIn("Query params can not be empty", str(exc))
        self.assertEqual(str(exc), errors.FORBIDDEN_MESSAGE)

    def test_T2_a_network_error_carries_no_token_in_its_traceback(self) -> None:
        transport, _ = self.make(failing_transport())
        with self.assertRaises(NetworkError) as caught:
            transport.request("GET", ME)

        exc = self.record(caught.exception)
        # except 블록 밖에서 던지므로 원인도 문맥도 없다 (§2-2의 2, 묶음 2 검토에서 강화).
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)
        printed = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.assertNotIn(FAKE_TOKEN, printed)
        self.assertNotIn("api.dooray.com", str(exc))
        self.assertEqual(str(exc), errors.NETWORK_MESSAGE)

    def test_T2_a_network_error_carries_no_httpx_exception_at_all(self) -> None:
        # `raise ... from None`은 __suppress_context__만 세우고 __context__ 객체는 남긴다.
        # 그 httpx2 예외는 요청 객체를 들고 있고, 헤더의 원본 바이트에는 토큰이 그대로 있다.
        # 그래서 except 블록 **밖에서** 던진다 — 딸려 갈 객체가 애초에 없다 (묶음 2 검토).
        transport, _ = self.make(failing_transport())
        with self.assertRaises(NetworkError) as caught:
            transport.request("GET", ME)

        exc = self.record(caught.exception)
        self.assertIsNone(exc.__context__)
        self.assertIsNone(exc.__cause__)
        self.assertNotIn(FAKE_TOKEN, "".join(traceback.format_exception(exc)))

    def test_T8_a_redirect_is_not_followed_to_a_second_host(self) -> None:
        routes = {
            ("GET", ME): (
                302,
                {"header": {}},
                {"Location": "https://evil.example/common/v1/members/me"},
            )
        }
        transport, http = self.make(routes)
        with self.assertRaises(RedirectError) as caught:
            transport.request("GET", ME)

        exc = self.record(caught.exception)
        self.assertEqual(exc.status, 302)
        # 요청은 한 번뿐이고 두 번째 호스트로 가지 않았다.
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(http.requests[0].url.host, "api.dooray.com")
        self.assertEqual([r.url.host for r in http.requests], ["api.dooray.com"])
        # Location을 문장에 넣지 않는다 (§5-1, U-5).
        self.assertNotIn("evil.example", str(exc))
        self.assertNotIn("evil.example", repr(exc))
        self.assertEqual(transport.calls, 1)

    def test_T8_follow_redirects_and_trust_env_stay_off_under_a_fake_transport(self) -> None:
        # 테스트는 http_transport만 갈아 끼운다. 보안 설정은 그대로 적용돼야 한다 (§11-1).
        transport, _ = self.make(me_routes())
        self.assertFalse(transport._client.follow_redirects)
        self.assertFalse(transport._client.trust_env)

    def test_T2_no_token_in_the_reader_repr_or_its_client_headers(self) -> None:
        reader = client.DoorayReader(
            FAKE_TOKEN, BASE, tool="test", http_transport=mock_transport(me_routes()),
            sleep=self.sleep,
        )
        self.addCleanup(reader.close)
        reader.me()

        self.record(reader)
        self.assertNotIn(FAKE_TOKEN, repr(reader))
        self.assertNotIn(FAKE_TOKEN, str(reader))
        self.assertNotIn(FAKE_TOKEN, repr(vars(reader)))
        self.assertNotIn(FAKE_TOKEN, repr(reader._t._client.headers))
        self.assertNotIn(FAKE_TOKEN, str(reader._t._client.headers))
        self.assertNotIn(FAKE_TOKEN, repr(reader.names))


class AuditRetentionTest(TransportCase):
    """T18~T21. 이 도구가 하는 유일한 삭제다 (§5-3, plan 위험 6번). **구현보다 먼저 쓴 테스트다.**"""

    def sweep_with(self, folder: Path, today: datetime) -> None:
        transport, _ = self.make(me_routes(), now=lambda: today)
        transport.request("GET", ME)

    def test_T18_only_files_older_than_the_window_are_deleted(self) -> None:
        folder = self.temp_log_dir()
        today = datetime(2026, 9, 20, 14, 0, tzinfo=KST)
        made = {}
        for days in (91, 90, 89):
            stamp = (today.date() - timedelta(days=days)).isoformat()
            path = folder / f"audit-{stamp}.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            made[days] = path

        self.sweep_with(folder, today)

        self.assertEqual(client.AUDIT_RETENTION_DAYS, 90)
        self.assertFalse(made[91].exists(), "91일 전 로그가 남았습니다.")
        # 경계(정확히 90일 전)는 "이른" 파일이 아니므로 남는다.
        self.assertTrue(made[90].exists())
        self.assertTrue(made[89].exists())

    def test_T19_names_that_do_not_match_the_pattern_are_left_alone(self) -> None:
        folder = self.temp_log_dir()
        today = datetime(2026, 9, 20, 14, 0, tzinfo=KST)
        keep = [
            folder / "audit-2026-13-99.jsonl",  # 패턴에는 맞지만 날짜를 못 읽는다
            folder / "audit-old.jsonl",
            folder / "notes.txt",
            folder / "audit-2000-01-01.jsonl.bak",
        ]
        for path in keep:
            path.write_text("남아야 한다\n", encoding="utf-8")
        nested = folder / "keep"
        nested.mkdir()
        buried = nested / "audit-2000-01-01.jsonl"
        buried.write_text("하위 폴더는 건드리지 않는다\n", encoding="utf-8")

        self.sweep_with(folder, today)

        for path in keep:
            self.assertTrue(path.exists(), f"지우면 안 되는 파일이 지워졌습니다: {path.name}")
        self.assertTrue(buried.exists(), "하위 폴더의 파일이 지워졌습니다.")

    def test_T19_a_symlink_is_not_followed(self) -> None:
        folder = self.temp_log_dir()
        today = datetime(2026, 9, 20, 14, 0, tzinfo=KST)
        outside = self.temp_log_dir() / "소중한-파일.jsonl"
        outside.write_text("남의 파일\n", encoding="utf-8")
        stamp = (today.date() - timedelta(days=200)).isoformat()
        link = folder / f"audit-{stamp}.jsonl"
        link.symlink_to(outside)

        self.sweep_with(folder, today)

        self.assertTrue(outside.exists(), "심볼릭 링크를 따라가 남의 파일을 지웠습니다.")
        self.assertTrue(link.is_symlink())

    def test_T20_the_folder_is_listed_once_per_process(self) -> None:
        folder = self.temp_log_dir()
        today = datetime(2026, 9, 20, 14, 0, tzinfo=KST)
        listed: list[Path] = []
        real_iterdir = Path.iterdir

        def counting(self: Path):
            listed.append(self)
            return real_iterdir(self)

        transport, _ = self.make(
            {("GET", ME): [(200, load_fixture("members_me"), None)]},
            now=lambda: today,
            call_limit=50,
        )
        with mock.patch.object(Path, "iterdir", counting):
            for _ in range(10):
                transport.request("GET", ME)

        self.assertEqual(len(self.audit_lines(folder / "audit-2026-09-20.jsonl")), 10)
        self.assertEqual(len(listed), 1, f"폴더 목록 조회가 여러 번 일어났습니다: {listed}")

    def test_T21_a_failed_delete_does_not_fail_the_api_call(self) -> None:
        folder = self.temp_log_dir()
        today = datetime(2026, 9, 20, 14, 0, tzinfo=KST)
        stamp = (today.date() - timedelta(days=200)).isoformat()
        stubborn = folder / f"audit-{stamp}.jsonl"
        stubborn.write_text("{}\n", encoding="utf-8")

        with mock.patch.object(Path, "unlink", side_effect=PermissionError("막혔다")):
            transport, _ = self.make(
                me_routes(),
                now=lambda: today,
            )
            data = transport.request("GET", ME)

        self.assertEqual(data["result"]["id"], "1000000000000000001")
        self.assertTrue(stubborn.exists())

    def test_T22_an_injected_audit_path_never_triggers_deletion(self) -> None:
        # 삭제는 기본 로그 폴더에서만 일어난다. 누가 `audit_path`로 다른 폴더를 가리켜도
        # 그 폴더의 파일은 지워지지 않는다 (묶음 2 검토 — 삭제 표면을 좁힌다).
        elsewhere = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="dooray-else-")))
        today = datetime(2026, 9, 20, 14, 0, tzinfo=KST)
        old = elsewhere / f"audit-{(today.date() - timedelta(days=400)).isoformat()}.jsonl"
        old.write_text("{}\n", encoding="utf-8")

        transport, _ = self.make(
            me_routes(), audit_path=elsewhere / "audit-2026-09-20.jsonl", now=lambda: today
        )
        transport.request("GET", ME)

        self.assertTrue(old.exists())


class AuditHygieneTest(TransportCase):
    """리뷰 M4·L2. `tool`과 `error`는 호출부가 주는 문자열이다. 본문이 실려 들어올 자리를 코어에서 막는다."""

    def test_T27_a_tool_name_that_is_not_a_plain_label_is_replaced(self) -> None:
        transport, _ = self.make(me_routes(), tool="get_thread: 업무 제목이 여기 들어왔다")
        transport.request("GET", ME)
        line = self.audit_lines()[-1]
        self.assertEqual(line["tool"], "unknown")

    def test_T27_audit_tool_call_cleans_tool_and_error(self) -> None:
        client.audit_tool_call(
            "list_my_mentions 본문",
            mode="read",
            ok=False,
            error="ValueError: 댓글 본문 어쩌고",
            now=NOW,
        )
        line = self.audit_lines()[-1]
        self.assertEqual(line["tool"], "unknown")
        self.assertEqual(line["error"], "Error")
        client.audit_tool_call(
            "list_my_mentions", mode="read", ok=False, error="NetworkError", now=NOW
        )
        line = self.audit_lines()[-1]
        self.assertEqual((line["tool"], line["error"]), ("list_my_mentions", "NetworkError"))

    def test_T28_a_blocked_write_leaves_a_line_without_the_path(self) -> None:
        # "AI가 댓글 등록이 아닌 쓰기를 시도했다"는 공동대표가 로그에서 가장 보고 싶어 할 사건이다.
        # 경로는 사용자 작성 텍스트일 수 있으므로 **적지 않는다.**
        transport, http = self.make(me_routes(), allow_write=True)
        with self.assertRaises(WriteNotAllowedError):
            # 012: 완료 처리가 표에 들어왔다 — 막힌 예는 여전히 열지 않은 `move`다 (spec §10-2 #8).
            transport.request("POST", "/project/v1/projects/10/posts/20/move", body={})
        with self.assertRaises(errors.InvalidIdError):
            transport.request("GET", "/project/v1/posts/20/../../x?본문=비밀")
        self.assertEqual(http.requests, [])

        blocked = self.audit_lines()[-2:]
        self.assertEqual([l["error"] for l in blocked], ["WriteNotAllowedError", "InvalidIdError"])
        self.assertEqual([l["method"] for l in blocked], ["POST", "GET"])
        for line in blocked:
            self.assertIsNone(line["path"])
            self.assertIsNone(line["status"])
            self.assertFalse(line["ok"])
        self.assertNotIn("비밀", (self.home / "logs" / "audit-2026-09-20.jsonl").read_text("utf-8"))

    def test_T28_an_odd_method_is_not_echoed(self) -> None:
        transport, _ = self.make(me_routes(), allow_write=True)
        with self.assertRaises(WriteNotAllowedError):
            transport.request("본문", "/common/v1/members/me")
        self.assertEqual(self.audit_lines()[-1]["method"], "OTHER")


class HomeFolderModeTest(TransportCase):
    def test_T26_the_audit_log_creates_the_home_folder_as_0700(self) -> None:
        # 감사 로그가 홈 폴더를 처음 만드는 주체일 수 있다(설정을 저장한 적이 없는 경우).
        # `mkdir(parents=True)`는 부모를 기본 권한으로 만든다 — 실제 점검에서 홈이 0o755로 생겼다.
        fresh = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="dooray-fresh-"))) / "home"
        self.enterContext(mock.patch.dict(os.environ, {"DOORAY_BRIDGE_HOME": str(fresh)}))
        transport, _ = self.make(me_routes())
        transport.request("GET", ME)

        self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((fresh / "logs").stat().st_mode), 0o700)
        logs = list((fresh / "logs").glob("audit-*.jsonl"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(stat.S_IMODE(logs[0].stat().st_mode), 0o600)


class PathSafetyTest(TransportCase):
    """경로는 우리가 조립한다. ID 자리에 경로 조각이 끼어들면 요청이 나가지 않는다 (묶음 2 검토).

    002에서 ID는 AI가 채우는 도구 인자다. 두레이 댓글에 심은 지시문이 AI를 속여
    `20/set-done?x=` 같은 값을 넘기게 하면, 검증이 없을 때 댓글 등록이 아니라
    업무 완료 처리 API로 POST가 나간다. 제약 3("쓰기는 셋뿐")이 뚫린다.
    """

    BAD_IDS = (
        "20/set-done?x=",
        "20/../../../../common/v1/incoming-hooks#",
        "../drive/v1/drives",
        "20?x=1",
        "20#frag",
        "20 21",
        "20%2F21",
        "20\\21",
        "..",
        ".",
        "",
        "a" * 65,
    )

    def test_T23_path_id_accepts_what_dooray_sends(self) -> None:
        self.assertEqual(client.path_id("1000000000000000001"), "1000000000000000001")
        self.assertEqual(client.path_id(1000000000000000001), "1000000000000000001")

    def test_T23_path_id_rejects_anything_that_could_bend_the_path(self) -> None:
        for bad in self.BAD_IDS:
            with self.subTest(bad=bad):
                with self.assertRaises(errors.InvalidIdError) as caught:
                    client.path_id(bad)
                # 받은 값을 문장에 되풀이하지 않는다 — 사용자 작성 텍스트일 수 있다.
                if len(bad) > 2:  # 한두 글자(`.`, `..`)는 고정 문장의 마침표와 겹친다
                    self.assertNotIn(bad, str(self.record(caught.exception)))

    def test_T24_the_transport_refuses_a_bent_path_before_sending(self) -> None:
        transport, http = self.make(me_routes())
        for path in (
            "/project/v1/posts/20/../../common/v1/members",
            "/project/v1/posts/20?x=1",
            "/project/v1/posts/20#x",
            "/project/v1//posts/20",
            "project/v1/posts/20",
            "/project/v1/posts/20/",
            "/project/v1/posts/2 0",
            "/project/v1/posts/%2e%2e",
        ):
            with self.subTest(path=path):
                with self.assertRaises(errors.InvalidIdError):
                    transport.request("GET", path)
        self.assertEqual(http.requests, [])


class WritePathTest(TransportCase):
    """010 spec §9-1의 W-a~W-f, 012 spec §10-1의 WP-a~WP-g — 쓰기가 허용된 전송 계층이 내보낼 수 있는 여덟 행.

    거절은 **요청이 나가지 않았음**으로 증명한다(`http.requests`의 길이).
    예외 종류만 보면 "보내고 나서 거절"과 구분되지 않는다.
    """

    COMMENT = "/project/v1/projects/10/posts/20/logs"
    CREATE = "/project/v1/projects/10/posts"
    PARENT = "/project/v1/projects/10/posts/20/set-parent-post"
    UPDATE = "/project/v1/projects/10/posts/20"
    EDIT_COMMENT = "/project/v1/projects/10/posts/20/logs/30"
    WORKFLOW = "/project/v1/projects/10/posts/20/set-workflow"
    TO_ME = "/project/v1/projects/10/posts/20/to/me"
    TO_MEMBER = "/project/v1/projects/10/posts/20/to/40"
    DONE = "/project/v1/projects/10/posts/20/set-done"

    # 행마다 (동사, 경로, 통과하는 본문). 완료 처리는 본문이 없다.
    ALLOWED = (
        ("POST", COMMENT, {"body": {"content": "네"}}),
        ("POST", CREATE, {"subject": "제목", "body": {}}),
        ("POST", PARENT, {"parentPostId": "30"}),
        ("PUT", UPDATE, {"subject": "제목"}),
        ("PUT", EDIT_COMMENT, {"body": {"mimeType": "text/x-markdown", "content": "네"}}),
        ("POST", WORKFLOW, {"workflowId": "7"}),
        ("PUT", TO_ME, {"workflowId": "7"}),
        ("PUT", TO_MEMBER, {"workflowId": "7"}),
        ("POST", DONE, None),
    )

    def allowed_routes(self) -> dict:
        ok = (200, {"result": {"id": "9"}}, None)
        return {(verb, path): ok for verb, path, _ in self.ALLOWED}

    def test_W_a_WP_a_the_eight_allowed_writes_go_out(self) -> None:
        transport, http = self.make(self.allowed_routes(), allow_write=True)
        for verb, path, body in self.ALLOWED:
            transport.request(verb, path, body=body)
        self.assertEqual(
            [(row.method, row.url.path) for row in http.requests],
            [(verb, path) for verb, path, _ in self.ALLOWED],
        )
        # 완료 처리는 본문 없이 나간다.
        self.assertEqual(http.requests[-1].content, b"")

    def test_WP_a_every_allowed_key_passes_alone(self) -> None:
        transport, http = self.make(self.allowed_routes(), allow_write=True)
        sent = 0
        for verb, pattern, allowed in client._WRITE_PATHS:
            path = next(p for v, p, _ in self.ALLOWED if v == verb and pattern.fullmatch(p))
            for key in sorted(allowed):
                with self.subTest(verb=verb, path=path, key=key):
                    transport.request(verb, path, body={key: "1"})
                    sent += 1
        self.assertEqual(len(http.requests), sent)

    def test_W_b_nothing_outside_the_table_leaves_the_process(self) -> None:
        transport, http = self.make(self.allowed_routes(), allow_write=True)
        for verb, path in (
            # 표에 없는 (동사, 경로)는 전부 거절이다.
            ("PATCH", "/project/v1/projects/10/posts/20"),
            ("DELETE", "/project/v1/projects/10/posts/20/logs/30"),
            ("PUT", self.COMMENT),
            ("PATCH", self.COMMENT),
            ("PUT", self.CREATE),
            ("DELETE", self.PARENT),
            # 완료 처리는 표에 있지만 본문이 있으면 막힌다 (012 spec §5-2).
            ("POST", "/project/v1/projects/10/posts/20/set-done"),
            ("POST", "/project/v1/projects/10/posts/20/move"),
            ("POST", "/project/v1/projects/10/posts/20/to/40"),
            ("POST", "/common/v1/incoming-hooks"),
        ):
            with self.subTest(verb=verb, path=path):
                with self.assertRaises(WriteNotAllowedError) as caught:
                    transport.request(verb, path, body={})
                self.record(caught.exception)
        self.assertEqual(http.requests, [], "거절인데 요청이 나갔습니다.")

    def test_WP_b_requests_outside_the_eight_rows_never_leave(self) -> None:
        transport, http = self.make(self.allowed_routes(), allow_write=True)
        cases = (
            ("POST", self.TO_ME),
            ("PUT", "/project/v1/projects/10/posts/20/to/40/x"),
            ("POST", self.UPDATE),
            ("PUT", self.CREATE),
            ("PUT", self.COMMENT),
            ("POST", self.EDIT_COMMENT),
            ("DELETE", self.EDIT_COMMENT),
            ("DELETE", self.UPDATE),
            ("PATCH", self.UPDATE),
            ("PUT", self.DONE),
            ("POST", "/project/v1/projects/10/posts/20/move"),
            ("PUT", "/project/v1/projects/10/posts/20/logs/30/reactions"),
        )
        # 본문 모양 때문이 아니라 (동사, 경로) 때문에 막힌다는 것을 보이려고 여러 본문으로 부른다.
        bodies = (None, {"workflowId": "7"}, {"body": {"content": "네"}}, {"subject": "제목"})
        for verb, path in cases:
            for body in bodies:
                with self.subTest(verb=verb, path=path, body=body):
                    with self.assertRaises(WriteNotAllowedError) as caught:
                        transport.request(verb, path, body=body)
                    self.record(caught.exception)
        self.assertEqual(http.requests, [], "거절인데 요청이 나갔습니다.")

    def test_W_c_a_prefix_match_is_not_enough(self) -> None:
        # 생성 경로가 `/…/posts`로 끝나므로 완전 일치가 아니면 한 칸 더 붙은 경로가 통과한다.
        transport, http = self.make(self.allowed_routes(), allow_write=True)
        for path in (
            "/project/v1/projects/10/posts/20",
            "/project/v1/projects/10/posts/20/logs/30",
            "/project/v1/projects/10/posts/20/set-parent-post/40",
            "/project/v1/projects/10/posts/20/logs/30/reactions",
        ):
            with self.subTest(path=path):
                with self.assertRaises(WriteNotAllowedError):
                    transport.request("POST", path, body={})
        self.assertEqual(http.requests, [])

    def test_W_d_a_body_key_outside_the_set_is_refused(self) -> None:
        transport, http = self.make(self.allowed_routes(), allow_write=True)
        cases = (
            (self.PARENT, {"parentPostId": "30", "subject": "바뀐 제목"}),
            (self.PARENT, {"parentPostId": "30", "users": {}}),
            (self.PARENT, {"workflowId": "9"}),
            (self.CREATE, {"subject": "제목", "users": {"to": []}}),
            (self.CREATE, {"subject": "제목", "milestoneId": "7"}),
            (self.CREATE, {"subject": "제목", "priority": "highest"}),
            (self.COMMENT, {"body": {}, "attachFileIds": ["1"]}),
        )
        for path, body in cases:
            with self.subTest(path=path, keys=sorted(body)):
                with self.assertRaises(WriteNotAllowedError):
                    transport.request("POST", path, body=body)
        self.assertEqual(http.requests, [])

    def test_WP_c_keys_and_body_shape_are_checked_per_row(self) -> None:
        transport, http = self.make(self.allowed_routes(), allow_write=True)
        cases = []
        # 업무 수정에 열지 않은 키 — 혼자서도, 열린 키와 함께여도 막힌다.
        for key in ("parentPostId", "version", "estimatedDays", "workflowId"):
            cases.append(("PUT", self.UPDATE, {key: "1"}))
            cases.append(("PUT", self.UPDATE, {"subject": "제목", key: "1"}))
        cases += [
            ("PUT", self.EDIT_COMMENT, {"body": {}, "attachFileIds": ["1"]}),
            ("POST", self.WORKFLOW, {"workflowId": "7", "subject": "제목"}),
            ("PUT", self.TO_ME, {"workflowId": "7", "users": {}}),
            ("PUT", self.TO_MEMBER, {"users": {"to": []}}),
            # 완료 처리는 본문이 없어야 한다 — 빈 본문도 본문이다.
            ("POST", self.DONE, {}),
            ("POST", self.DONE, {"workflowId": "7"}),
        ]
        # 키가 있는 행에 본문이 없거나 비었거나 Mapping이 아니면 막힌다 (리뷰 L7).
        for verb, path, body in self.ALLOWED:
            if body is None:
                continue
            for bad in (None, {}, [("subject", "제목")], "subject"):
                cases.append((verb, path, bad))
        for verb, path, body in cases:
            with self.subTest(verb=verb, path=path, body=body):
                with self.assertRaises(WriteNotAllowedError):
                    transport.request(verb, path, body=body)  # type: ignore[arg-type]
        self.assertEqual(http.requests, [])

    def test_W_e_WP_d_a_reading_transport_refuses_all_eight(self) -> None:
        transport, http = self.make(self.allowed_routes())
        for verb, path, body in self.ALLOWED:
            with self.subTest(verb=verb, path=path):
                with self.assertRaises(WriteNotAllowedError):
                    transport.request(verb, path, body=body)
        self.assertEqual(http.requests, [])

    def test_W_f_a_blocked_attempt_is_logged_without_the_path(self) -> None:
        transport, _ = self.make(self.allowed_routes(), allow_write=True)
        with self.assertRaises(WriteNotAllowedError):
            # 012: 완료 처리가 표에 들어왔다 — 막힌 예는 열지 않은 `move`다 (spec §10-2 #8).
            transport.request("POST", "/project/v1/projects/10/posts/20/move", body={})
        line = self.audit_lines()[0]
        self.assertEqual(line["kind"], "api")
        self.assertEqual(line["method"], "POST")
        self.assertEqual(line["error"], "WriteNotAllowedError")
        self.assertIsNone(line["path"])
        self.assertIsNone(line["status"])
        self.assertIs(line["ok"], False)

    def test_W_f_an_unknown_verb_is_logged_as_other(self) -> None:
        transport, _ = self.make(self.allowed_routes(), allow_write=True)
        with self.assertRaises(WriteNotAllowedError):
            transport.request("BREW", self.COMMENT, body={})
        self.assertEqual(self.audit_lines()[0]["method"], "OTHER")

    def test_WP_e_a_blocked_delete_is_logged_as_delete_without_the_path(self) -> None:
        transport, http = self.make(self.allowed_routes(), allow_write=True)
        with self.assertRaises(WriteNotAllowedError):
            transport.request("DELETE", self.EDIT_COMMENT)
        self.assertEqual(http.requests, [])
        line = self.audit_lines()[0]
        self.assertEqual(line["method"], "DELETE")
        self.assertEqual(line["mode"], "write")
        self.assertIsNone(line["path"])
        self.assertIsNone(line["status"])
        self.assertIs(line["ok"], False)
        self.assertEqual(line["error"], "WriteNotAllowedError")

    def test_WP_f_a_put_is_not_retried_after_a_5xx(self) -> None:
        routes = {
            ("PUT", self.UPDATE): [
                (503, load_fixture("error_404"), None),
                (200, {"result": None}, None),
            ]
        }
        transport, http = self.make(routes, allow_write=True)
        with self.assertRaises(ServerError) as caught:
            transport.request("PUT", self.UPDATE, body={"subject": "제목"})
        self.record(caught.exception)
        self.assertEqual(len(http.requests), 1, "고치기 요청이 두 번 나갔습니다.")
        self.assertEqual(len(self.audit_lines()), 1)

    def test_WP_g_only_the_two_creating_rows_log_a_created_id(self) -> None:
        # 응답에 `result.id`가 있어도 만드는 두 행이 아니면 적지 않는다 (012 리뷰 L4).
        transport, _ = self.make(self.allowed_routes(), allow_write=True)
        for verb, path, body in self.ALLOWED:
            transport.request(verb, path, body=body)
        logged = [(line["method"], line["path"], line["comment_id"]) for line in self.audit_lines()]
        expected = [
            (verb, path, "9" if path in (self.COMMENT, self.CREATE) else None)
            for verb, path, _ in self.ALLOWED
        ]
        self.assertEqual(logged, expected)

    def test_WP_g_a_get_on_the_creating_paths_logs_no_id(self) -> None:
        # 업무 목록·댓글 목록은 만드는 행과 경로가 같다. GET은 무엇도 만들지 않았다.
        ok = (200, {"result": {"id": "9"}}, None)
        transport, _ = self.make({("GET", self.CREATE): ok, ("GET", self.COMMENT): ok})
        transport.request("GET", self.CREATE)
        transport.request("GET", self.COMMENT)
        self.assertEqual([line["comment_id"] for line in self.audit_lines()], [None, None])

    def test_W_the_table_holds_exactly_eight_rows(self) -> None:
        self.assertEqual(len(client._WRITE_PATHS), 8)


class TransportStatusTest(TransportCase):
    def test_T1_each_status_becomes_its_own_exception_and_sentence(self) -> None:
        cases = [
            (401, AuthError, errors.AUTH_MESSAGE),
            (403, ForbiddenError, errors.FORBIDDEN_MESSAGE),
            (404, NotFoundError, errors.NOT_FOUND_MESSAGE),
            (429, RateLimitError, errors.RATE_LIMIT_MESSAGE),
            (503, ServerError, errors.SERVER_MESSAGE.format(status=503)),
            (400, ApiError, errors.OTHER_CLIENT_MESSAGE.format(status=400)),
        ]
        for status, kind, sentence in cases:
            with self.subTest(status=status):
                transport, _ = self.make(
                    {("GET", ME): (status, load_fixture("error_404"), None)}, max_retries=0
                )
                with self.assertRaises(kind) as caught:
                    transport.request("GET", ME)
                exc = self.record(caught.exception)
                self.assertEqual(str(exc), sentence)
                self.assertEqual(exc.status, status)

    def test_T1_a_network_failure_becomes_network_error(self) -> None:
        transport, _ = self.make(failing_transport())
        with self.assertRaises(NetworkError) as caught:
            transport.request("GET", ME)
        self.assertEqual(str(self.record(caught.exception)), errors.NETWORK_MESSAGE)
        self.assertEqual(transport.calls, 1)

    def test_a_timeout_also_becomes_network_error(self) -> None:
        transport, _ = self.make(failing_transport(httpx2.ReadTimeout("느림")))
        with self.assertRaises(NetworkError):
            transport.request("GET", ME)

    def test_a_2xx_body_that_is_not_json_becomes_an_api_error(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, text="<html>점검 중</html>")

        transport, _ = self.make(httpx2.MockTransport(handler))
        with self.assertRaises(ApiError) as caught:
            transport.request("GET", ME)
        exc = self.record(caught.exception)
        self.assertEqual(str(exc), errors.NOT_JSON_MESSAGE.format(status=200))
        self.assertNotIn("점검 중", str(exc))


class TransportRetryTest(TransportCase):
    def test_T3_a_429_is_retried_after_a_backoff(self) -> None:
        transport, http = self.make(
            {
                ("GET", ME): [
                    (429, {"header": {}}, None),
                    (200, load_fixture("members_me"), None),
                ]
            }
        )
        data = transport.request("GET", ME)

        self.assertEqual(data["result"]["id"], "1000000000000000001")
        self.assertEqual(len(http.requests), 2)
        # 지수 백오프 0.5초뿐이다 — 재시도도 버스트 안이라 간격 대기가 0이다 (003 spec §7-1).
        self.assertEqual(self.sleep.waits, [0.5])
        self.assertEqual(transport.calls, 2)

    def test_T4_four_failures_raise_server_error_and_count_four_requests(self) -> None:
        transport, http = self.make({("GET", ME): (500, {"header": {}}, None)})
        with self.assertRaises(ServerError) as caught:
            transport.request("GET", ME)

        self.record(caught.exception)
        self.assertEqual(len(http.requests), 4, "최대 3회 재시도 = 총 4회 시도")
        self.assertEqual(transport.calls, 4, "재시도도 호출 상한에 센다")
        self.assertEqual([w for w in self.sleep.waits if w != 0.2], [0.5, 1.0, 2.0])

    def test_T5_retry_after_is_honoured_추정(self) -> None:
        # 429에 Retry-After가 오는지는 미확인이다 (api-notes §5 U-5). 있으면 따른다.
        transport, _ = self.make(
            {
                ("GET", ME): [
                    (429, {"header": {}}, {"Retry-After": "3"}),
                    (200, load_fixture("members_me"), None),
                ]
            }
        )
        transport.request("GET", ME)
        self.assertIn(3.0, self.sleep.waits)

    def test_T5_a_huge_retry_after_is_capped_at_thirty_seconds_추정(self) -> None:
        transport, _ = self.make(
            {
                ("GET", ME): [
                    (429, {"header": {}}, {"Retry-After": "9999"}),
                    (200, load_fixture("members_me"), None),
                ]
            }
        )
        transport.request("GET", ME)
        self.assertIn(30.0, self.sleep.waits)

    def test_T5_a_nonsense_retry_after_falls_back_to_the_exponential_wait(self) -> None:
        transport, _ = self.make(
            {
                ("GET", ME): [
                    (429, {"header": {}}, {"Retry-After": "tomorrow"}),
                    (200, load_fixture("members_me"), None),
                ]
            }
        )
        transport.request("GET", ME)
        self.assertEqual(self.sleep.waits, [0.5])

    def test_T6_a_post_is_never_retried(self) -> None:
        path = "/project/v1/projects/1/posts/2/logs"
        transport, http = self.make(
            {("POST", path): (503, {"header": {}}, None)}, allow_write=True
        )
        with self.assertRaises(ServerError):
            transport.request("POST", path, body={"body": {"content": "안녕"}})

        # 댓글 등록은 멱등하지 않다. 재시도가 중복 댓글을 만든다 (§5-1).
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(self.sleep.waits, [])

    def test_a_post_that_hits_429_is_not_retried_either(self) -> None:
        path = "/project/v1/projects/1/posts/2/logs"
        transport, http = self.make(
            {("POST", path): (429, {"header": {}}, None)}, allow_write=True
        )
        with self.assertRaises(RateLimitError):
            transport.request("POST", path, body={"body": {"content": "안녕"}})
        self.assertEqual(len(http.requests), 1)


class TransportGuardTest(TransportCase):
    def test_T7_the_call_limit_stops_further_requests(self) -> None:
        transport, http = self.make(me_routes(), call_limit=2)
        transport.request("GET", ME)
        transport.request("GET", ME)
        self.assertEqual(transport.calls_left, 0)

        with self.assertRaises(CallLimitError) as caught:
            transport.request("GET", ME)

        exc = self.record(caught.exception)
        self.assertEqual(exc.limit, 2)
        self.assertEqual(exc.used, 2)
        self.assertIn("2회", str(exc))
        self.assertEqual(len(http.requests), 2, "상한을 넘은 뒤로는 요청이 나가지 않는다")

    def test_T9_the_authorization_header_is_attached(self) -> None:
        transport, http = self.make(me_routes())
        transport.request("GET", ME)
        sent = http.requests[0]
        self.assertEqual(sent.headers["Authorization"], f"dooray-api {FAKE_TOKEN}")
        self.assertEqual(sent.headers["Accept"], "application/json")

    def test_T10_an_api_base_outside_the_list_is_rejected_before_any_request(self) -> None:
        http = mock_transport(me_routes())
        with self.assertRaises(ConfigError) as caught:
            client.Transport(
                FAKE_TOKEN,
                "https://api.example.com",
                tool="test",
                http_transport=http,
                sleep=self.sleep,
            )
        exc = self.record(caught.exception)
        # 받은 값을 되풀이하지 않는다 (005 리뷰 H1).
        self.assertNotIn("api.example.com", str(exc))
        self.assertTrue(str(exc).startswith("허용되지 않은 두레이 API 주소입니다."))
        self.assertEqual(len(http.requests), 0)

    def test_T13_a_write_on_a_read_only_transport_never_leaves(self) -> None:
        transport, http = self.make({("POST", "/anything"): (200, {}, None)})
        with self.assertRaises(WriteNotAllowedError) as caught:
            transport.request("POST", "/anything", body={"a": 1})

        self.record(caught.exception)
        self.assertEqual(len(http.requests), 0)
        self.assertEqual(transport.calls, 0)
        for method in ("PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                with self.assertRaises(WriteNotAllowedError):
                    transport.request(method, "/anything")
        self.assertEqual(len(http.requests), 0)


class TransportPacingTest(TransportCase):
    def test_T11_three_calls_inside_the_burst_do_not_sleep(self) -> None:
        # 003 spec §7-1이 이 기대를 바꿨다: 무조건 간격이 아니라 버스트 10개를 먼저 쓴다.
        transport, _ = self.make(me_routes())
        for _ in range(3):
            transport.request("GET", ME)
        self.assertEqual(self.sleep.waits, [])

    def test_T11_only_the_missing_part_of_the_interval_is_slept(self) -> None:
        # 003 완료 기준 1은 §7-1의 버킷 규칙 **안에서** 그대로다: 버스트를 다 쓴 뒤로는
        # 응답이 간격보다 오래 걸렸으면 쉬지 않고, 곧바로 이어졌으면 모자란 만큼만 쉰다.
        #   1~10번째: 버스트. 대기 0. 다음 토큰 시각 2.00, 시계 0.00
        #   11번째:   0.20 쉼(시계 0.20). 다음 토큰 2.20
        #   그 뒤 응답이 0.50초 걸렸다 → 시계 0.70
        #   12번째:   토큰 2.20 − 앞당겨도 되는 1.80 = 0.40 ≤ 0.70 → 쉬지 않음. 다음 토큰 2.40
        #   13번째:   2.40 − 1.80 = 0.60 ≤ 0.70 → 쉬지 않음. 다음 토큰 2.60
        #   14번째:   2.60 − 1.80 = 0.80 → 모자란 0.10만 쉰다
        self.sleep = TickingSleep()
        transport, _ = self.make(me_routes(), clock=lambda: self.sleep.now)
        for _ in range(client.BURST + 1):
            transport.request("GET", ME)
        self.assertEqual([round(wait, 6) for wait in self.sleep.waits], [0.2])

        self.sleep.now += 0.5  # 응답이 간격보다 오래 걸렸다
        self.sleep.waits.clear()
        for _ in range(3):
            transport.request("GET", ME)
        self.assertEqual([round(wait, 6) for wait in self.sleep.waits], [0.1])

    def test_T11_the_interval_is_a_fifth_of_a_second(self) -> None:
        # 실측 한도가 초당 5회다 (api-notes §1-4). 값이 조용히 줄지 않게 묶어 둔다.
        self.assertEqual(client.MIN_INTERVAL, 0.2)
        self.assertEqual(client.DEFAULT_DELAY, client.MIN_INTERVAL)

    def test_T12_a_low_remaining_header_buys_extra_rest(self) -> None:
        transport, _ = self.make(
            {
                ("GET", ME): (
                    200,
                    load_fixture("members_me"),
                    {"X-RateLimit-Remaining": "2", "X-RateLimit-Replenish-Rate": "5"},
                )
            }
        )
        transport.request("GET", ME)
        transport.request("GET", ME)
        # 버스트 안이라 간격 대기는 0이다. 추가 대기 min(2.0, (5-2)/5) = 0.6초만 남는다.
        self.assertEqual(self.sleep.waits, [0.6])

    def test_a_healthy_remaining_header_buys_nothing(self) -> None:
        transport, _ = self.make(
            {("GET", ME): (200, load_fixture("members_me"), {"X-RateLimit-Remaining": "19"})}
        )
        transport.request("GET", ME)
        transport.request("GET", ME)
        self.assertEqual(self.sleep.waits, [])

    def test_a_nonsense_remaining_header_is_ignored(self) -> None:
        transport, _ = self.make(
            {("GET", ME): (200, load_fixture("members_me"), {"X-RateLimit-Remaining": "plenty"})}
        )
        transport.request("GET", ME)
        transport.request("GET", ME)
        self.assertEqual(self.sleep.waits, [])

    def test_the_extra_wait_is_capped_at_two_seconds(self) -> None:
        transport, _ = self.make(
            {
                ("GET", ME): (
                    200,
                    load_fixture("members_me"),
                    {"X-RateLimit-Remaining": "0", "X-RateLimit-Replenish-Rate": "1"},
                )
            }
        )
        transport.request("GET", ME)
        transport.request("GET", ME)
        # 버스트 안이라 간격 대기는 0이다. 남은 토큰이 바닥나 얻은 추가 대기만 남는다.
        self.assertEqual(self.sleep.waits, [2.0])


class TransportBucketTest(TransportCase):
    """003 완료 기준 23 — 속도 제한이 토큰 버킷이다 (spec §7-1).

    용량 `BURST`, `delay`초에 하나 보충, 가득 찬 채로 시작한다.
    """

    def test_기준23_the_burst_is_half_the_measured_one(self) -> None:
        # 실측 버스트 20의 절반 (api-notes §1-4). 이 한도는 토큰당이라 다른 프로세스와 나눠 쓴다.
        self.assertEqual(client.BURST, 10)

    def test_기준23_the_first_ten_calls_do_not_wait(self) -> None:
        transport, http = self.make(me_routes())
        for _ in range(client.BURST):
            transport.request("GET", ME)
        self.assertEqual(self.sleep.waits, [])
        self.assertEqual(len(http.requests), client.BURST)

    def test_기준23_the_eleventh_call_waits_one_interval(self) -> None:
        transport, _ = self.make(me_routes())  # 고정 시계 — 흐른 시간이 0이다
        for _ in range(client.BURST + 1):
            transport.request("GET", ME)
        self.assertEqual([round(wait, 6) for wait in self.sleep.waits], [0.2])

    def test_기준23_after_the_burst_the_calls_are_one_interval_apart(self) -> None:
        self.sleep = TickingSleep()
        transport, _ = self.make(me_routes(), clock=lambda: self.sleep.now)
        for _ in range(client.BURST + 3):
            transport.request("GET", ME)
        self.assertEqual([round(wait, 6) for wait in self.sleep.waits], [0.2, 0.2, 0.2])

    def test_기준23_an_idle_stretch_refills_the_bucket_to_its_cap(self) -> None:
        self.sleep = TickingSleep()
        transport, _ = self.make(me_routes(), clock=lambda: self.sleep.now)
        for _ in range(client.BURST + 1):
            transport.request("GET", ME)

        self.sleep.now += 60  # 1분을 쉬었다
        self.sleep.waits.clear()
        for _ in range(client.BURST):
            transport.request("GET", ME)
        self.assertEqual(self.sleep.waits, [], "버킷이 다시 가득 찼다")

        transport.request("GET", ME)
        self.assertEqual(
            [round(wait, 6) for wait in self.sleep.waits], [0.2], "상한은 그대로 10이다"
        )

    def test_기준23_delay_zero_turns_the_pacing_off(self) -> None:
        transport, _ = self.make(me_routes(), delay=0)
        for _ in range(client.BURST + 5):
            transport.request("GET", ME)
        self.assertEqual(self.sleep.waits, [])


class PacerTest(unittest.TestCase):
    """속도 제한 상태는 `Transport` 여럿이 나눠 쓴다 (003 spec §7-1).

    실측: 모델이 `whoami`와 `list_projects`를 동시에 불러 같은 초에 21회가 나가 429를 받았다 —
    버킷이 `Transport`마다 따로였다.
    """

    def test_two_users_of_one_pacer_share_the_burst(self) -> None:
        pacer = client.Pacer()
        waits = [pacer.reserve(0.0) for _ in range(client.BURST + 2)]
        self.assertEqual(waits[: client.BURST], [0.0] * client.BURST)
        self.assertEqual([round(w, 6) for w in waits[client.BURST :]], [0.2, 0.4])

    def test_a_hold_makes_everyone_wait_and_then_there_is_no_burst(self) -> None:
        # 리뷰 H3. 스레드마다 같은 시간을 동시에 자면 처리량이 줄지 않는다 — 멈춤은 모두의 것이다.
        pacer = client.Pacer()
        pacer.hold(0.0, 0.6)
        self.assertEqual(
            [round(pacer.reserve(0.0), 6) for _ in range(3)], [0.6, 0.8, 1.0]
        )

    def test_a_longer_hold_wins_and_a_shorter_one_changes_nothing(self) -> None:
        pacer = client.Pacer()
        pacer.hold(0.0, 2.0)
        pacer.hold(0.0, 0.5)
        self.assertEqual(round(pacer.reserve(0.0), 6), 2.0)

    def test_a_low_remaining_header_becomes_a_hold(self) -> None:
        pacer = client.Pacer()
        pacer.note_remaining(0.0, 2, 5)  # (5 − 2) / 5 = 0.6초
        self.assertEqual(round(pacer.reserve(0.0), 6), 0.6)
        healthy = client.Pacer()
        healthy.note_remaining(0.0, 5, 5)
        healthy.note_remaining(0.0, None, None)
        self.assertEqual(healthy.reserve(0.0), 0.0)

    def test_the_hold_applies_even_when_the_bucket_is_off(self) -> None:
        pacer = client.Pacer(delay=0)
        self.assertEqual(pacer.reserve(0.0), 0.0)
        pacer.hold(0.0, 1.0)
        self.assertEqual(pacer.reserve(0.25), 0.75)
        self.assertEqual(pacer.reserve(1.5), 0.0)


class SharedPacerTest(TransportCase):
    def test_two_transports_with_one_pacer_spend_one_burst(self) -> None:
        pacer = client.Pacer()
        first, _ = self.make(me_routes(), pacer=pacer)
        second, _ = self.make(me_routes(), pacer=pacer)
        for _ in range(client.BURST // 2):
            first.request("GET", ME)
            second.request("GET", ME)
        self.assertEqual(self.sleep.waits, [])
        first.request("GET", ME)
        second.request("GET", ME)
        self.assertEqual([round(w, 6) for w in self.sleep.waits], [0.2, 0.4])

    def test_a_429_on_one_transport_holds_the_other(self) -> None:
        pacer = client.Pacer()
        limited, _ = self.make(
            {("GET", ME): [(429, load_fixture("error_404"), None), (200, load_fixture("members_me"), None)]},
            pacer=pacer,
        )
        other, _ = self.make(me_routes(), pacer=pacer)
        limited.request("GET", ME)  # 429 → 백오프 0.5초를 모두가 기다린다
        self.assertEqual(self.sleep.waits, [0.5])
        other.request("GET", ME)
        self.assertEqual(len(self.sleep.waits), 2)
        self.assertGreater(self.sleep.waits[1], 0.5)  # 멈춤 뒤에는 버스트 없이 간격대로


class BatchHaltTest(TransportCase):
    """리뷰 H1 — 병렬 묶음 안에서 429·5xx가 나면 **아직 시작하지 않은** 호출은 나가지 않는다."""

    def test_after_a_failure_the_rest_of_the_batch_is_not_sent(self) -> None:
        transport, http = self.make({("GET", ME): (429, load_fixture("error_404"), None)})
        with transport.batch():
            with self.assertRaises(errors.RateLimitError) as first:
                transport.request("GET", ME)
            self.record(first.exception)
            sent = len(http.requests)
            self.assertEqual(sent, 4)  # 1 + 재시도 3
            with self.assertRaises(errors.RateLimitError) as second:
                transport.request("GET", ME)
            self.record(second.exception)
            self.assertIsNot(second.exception, first.exception)  # 예외 객체를 나눠 갖지 않는다
            self.assertEqual(len(http.requests), sent)
            self.assertEqual(transport.calls, sent)  # 보내지 않은 것은 세지 않는다

    def test_the_halt_ends_with_the_batch(self) -> None:
        transport, http = self.make(
            {("GET", ME): [(500, load_fixture("error_404"), None)] * 4 + [(200, load_fixture("members_me"), None)]}
        )
        with transport.batch():
            with self.assertRaises(errors.ServerError) as caught:
                transport.request("GET", ME)
            self.record(caught.exception)
        transport.request("GET", ME)  # 묶음 밖 — 순차 호출의 뜻은 바뀌지 않는다
        self.assertEqual(len(http.requests), 5)

    def test_outside_a_batch_a_failure_stops_nothing(self) -> None:
        transport, http = self.make(
            {("GET", ME): [(500, load_fixture("error_404"), None)] * 4 + [(200, load_fixture("members_me"), None)]}
        )
        with self.assertRaises(errors.ServerError) as caught:
            transport.request("GET", ME)
        self.record(caught.exception)
        transport.request("GET", ME)
        self.assertEqual(len(http.requests), 5)

    def test_a_forbidden_answer_does_not_halt_the_batch(self) -> None:
        # 403·404는 그 항목만의 일이다. 묶음을 세우는 것은 429·5xx·네트워크뿐이다.
        transport, http = self.make(
            {("GET", ME): [(403, load_fixture("error_404"), None), (200, load_fixture("members_me"), None)]}
        )
        with transport.batch():
            with self.assertRaises(errors.ForbiddenError) as caught:
                transport.request("GET", ME)
            self.record(caught.exception)
            transport.request("GET", ME)
        self.assertEqual(len(http.requests), 2)


class TransportThreadSafetyTest(TransportCase):
    """003 완료 기준 22 — 스레드 여덟이 한 `Transport`를 같이 써도 상한이 정확하다."""

    LIMIT = 12
    THREADS = 8
    EACH = 5

    def test_기준22_eight_threads_cannot_push_past_the_call_limit(self) -> None:
        transport, http = self.make(me_routes(), call_limit=self.LIMIT)
        blocked: list[BaseException] = []
        gate = threading.Barrier(self.THREADS, timeout=10)

        def worker() -> None:
            gate.wait()  # 여덟이 모인 뒤 한꺼번에 들어간다
            for _ in range(self.EACH):
                try:
                    transport.request("GET", ME)
                except CallLimitError as stop:
                    blocked.append(stop)

        threads = [threading.Thread(target=worker) for _ in range(self.THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual([t.is_alive() for t in threads], [False] * self.THREADS)

        self.assertEqual(len(http.requests), self.LIMIT, "정확히 상한만큼만 나갔다")
        self.assertEqual(transport.calls, self.LIMIT)
        self.assertEqual(transport.calls_left, 0)
        self.assertEqual(len(blocked), self.THREADS * self.EACH - self.LIMIT)

    def test_기준22_the_audit_lines_match_the_requests_and_do_not_interleave(self) -> None:
        transport, http = self.make(me_routes(), call_limit=self.LIMIT)
        gate = threading.Barrier(self.THREADS, timeout=10)

        def worker() -> None:
            gate.wait()
            for _ in range(self.EACH):
                try:
                    transport.request("GET", ME)
                except CallLimitError:
                    pass

        threads = [threading.Thread(target=worker) for _ in range(self.THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        # `audit_lines`가 줄마다 `json.loads`를 한다 — 줄이 섞였으면 여기서 깨진다.
        lines = self.audit_lines()
        self.assertEqual(len(lines), len(http.requests))
        self.assertEqual(len(lines), self.LIMIT)
        self.assertEqual({line["kind"] for line in lines}, {"api"})
        self.assertEqual(sorted(line["calls"] for line in lines), list(range(1, self.LIMIT + 1)))


class AuditRemainingTest(TransportCase):
    """003 완료 기준 27 — `api` 줄에 남은 토큰 수가 있다 (숫자 하나다. 본문이 아니다)."""

    def test_기준27_an_api_line_carries_the_remaining_header(self) -> None:
        transport, _ = self.make(
            {
                ("GET", ME): (
                    200,
                    load_fixture("members_me"),
                    {"X-RateLimit-Remaining": "17"},
                )
            }
        )
        transport.request("GET", ME)
        self.assertEqual(self.audit_lines()[0]["remaining"], 17)

    def test_기준27_a_missing_header_leaves_it_null(self) -> None:
        transport, _ = self.make(me_routes())
        transport.request("GET", ME)
        self.assertIsNone(self.audit_lines()[0]["remaining"])

    def test_기준27_a_nonsense_header_leaves_it_null(self) -> None:
        transport, _ = self.make(
            {
                ("GET", ME): (
                    200,
                    load_fixture("members_me"),
                    {"X-RateLimit-Remaining": "plenty"},
                )
            }
        )
        transport.request("GET", ME)
        self.assertIsNone(self.audit_lines()[0]["remaining"])

    def test_기준27_a_failed_call_still_records_what_the_response_said(self) -> None:
        transport, _ = self.make(
            {("GET", ME): (403, load_fixture("error_404"), {"X-RateLimit-Remaining": "3"})}
        )
        with self.assertRaises(ForbiddenError):
            transport.request("GET", ME)
        self.assertEqual(self.audit_lines()[0]["remaining"], 3)

    def test_기준27_a_blocked_request_has_no_remaining_to_report(self) -> None:
        transport, http = self.make(me_routes())
        with self.assertRaises(WriteNotAllowedError):
            transport.request("POST", "/project/v1/projects/1/posts/2/logs")
        self.assertEqual(len(http.requests), 0)
        self.assertIsNone(self.audit_lines()[0]["remaining"])


class RunParallelTest(CoreTestCase):
    """003 완료 기준 24 — `run_parallel` (spec §7-2). 네트워크도 실제 시계도 쓰지 않는다."""

    def test_the_default_worker_count_is_four(self) -> None:
        self.assertEqual(client.PARALLEL_WORKERS, 4)

    def test_기준24_the_results_keep_the_input_order(self) -> None:
        tasks = [lambda value=number: value * 2 for number in range(6)]
        self.assertEqual(client.run_parallel(tasks), [0, 2, 4, 6, 8, 10])

    def test_기준24_an_exception_comes_back_as_the_value_in_its_place(self) -> None:
        boom = ValueError("터짐")

        def fails() -> int:
            raise boom

        found = client.run_parallel([lambda: 1, fails, lambda: 3])
        self.assertEqual([found[0], found[2]], [1, 3])
        self.assertIs(found[1], boom, "예외는 던지지 않고 그 자리의 값으로 온다")

    def test_기준24_no_task_makes_no_thread(self) -> None:
        before = threading.active_count()
        self.assertEqual(client.run_parallel([]), [])
        self.assertEqual(threading.active_count(), before)

    def test_기준24_one_task_makes_no_thread(self) -> None:
        before = threading.active_count()
        self.assertEqual(client.run_parallel([lambda: "하나"]), ["하나"])
        self.assertEqual(threading.active_count(), before)

        boom = ValueError("터짐")

        def fails() -> int:
            raise boom

        self.assertEqual(client.run_parallel([fails]), [boom], "한 개짜리도 규칙이 같다")
        self.assertEqual(threading.active_count(), before)

    def test_기준24_more_tasks_than_workers_all_finish(self) -> None:
        found = client.run_parallel([lambda value=n: value for n in range(20)], workers=3)
        self.assertEqual(found, list(range(20)))

    def test_기준24_never_more_than_workers_run_at_once(self) -> None:
        # 장벽으로 잰다 — 실제 `sleep`에 기대지 않는다. workers명이 모여야 풀리므로
        # 동시에 도는 수가 더 적으면 장벽이 시간 초과로 깨지고, 더 많으면 peak이 커진다.
        workers = 3
        gate = threading.Barrier(workers, timeout=10)
        lock = threading.Lock()
        state = {"running": 0, "peak": 0}

        def task() -> str:
            with lock:
                state["running"] += 1
                state["peak"] = max(state["peak"], state["running"])
            gate.wait()
            with lock:
                state["running"] -= 1
            return "ok"

        found = client.run_parallel([task] * (workers * 2), workers=workers)
        self.assertEqual(found, ["ok"] * (workers * 2))
        self.assertEqual(state["peak"], workers)
        self.assertEqual(state["running"], 0)

    def test_기준24_a_keyboard_interrupt_is_not_swallowed(self) -> None:
        def stop() -> int:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            client.run_parallel([lambda: 1, stop, lambda: 3])

    def test_기준24_the_threads_are_all_gone_when_it_returns(self) -> None:
        before = threading.active_count()
        client.run_parallel([lambda value=n: value for n in range(12)], workers=4)
        self.assertEqual(threading.active_count(), before, "스레드를 남기지 않는다")


class AuditLineTest(TransportCase):
    def test_T14_an_api_line_matches_the_table_exactly(self) -> None:
        path = "/project/v1/projects/1000000000000000018/posts"
        transport, _ = self.make({("GET", path): (200, load_fixture("posts_list"), None)})
        transport.request(
            "GET", path, params={"member": "me"}, project_id="1000000000000000018"
        )

        lines = self.audit_lines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(set(line), AUDIT_FIELD_NAMES)
        self.assertEqual(line["kind"], "api")
        self.assertEqual(line["ts"], NOW.isoformat())
        self.assertEqual(line["tool"], "test")
        self.assertEqual(line["mode"], "read")
        self.assertEqual(line["method"], "GET")
        self.assertEqual(line["path"], path, "쿼리를 뺀 경로여야 한다")
        self.assertEqual(line["project_id"], "1000000000000000018")
        self.assertIsNone(line["post_id"])
        self.assertEqual(line["status"], 200)
        self.assertIs(line["ok"], True)
        self.assertIsNone(line["error"])
        self.assertIsInstance(line["ms"], int)
        self.assertEqual(line["attempt"], 1)
        self.assertEqual(line["calls"], 1)
        self.assertIsNone(line["comment_id"])

    def test_T14_a_failure_records_the_exception_class_name(self) -> None:
        transport, _ = self.make({("GET", ME): (404, load_fixture("error_404"), None)})
        with self.assertRaises(NotFoundError):
            transport.request("GET", ME)

        line = self.audit_lines()[0]
        self.assertEqual(line["error"], "NotFoundError")
        self.assertIs(line["ok"], False)
        self.assertEqual(line["status"], 404)
        # 응답 본문(`Post not found.`)은 어디에도 없다.
        self.assertNotIn("Post not found", json.dumps(line, ensure_ascii=False))

    def test_T14_a_network_failure_records_a_null_status(self) -> None:
        transport, _ = self.make(failing_transport())
        with self.assertRaises(NetworkError):
            transport.request("GET", ME)
        line = self.audit_lines()[0]
        self.assertIsNone(line["status"])
        self.assertEqual(line["error"], "NetworkError")
        self.assertIs(line["ok"], False)

    def test_T14_every_retry_gets_its_own_line_with_a_rising_attempt(self) -> None:
        transport, _ = self.make({("GET", ME): (500, {"header": {}}, None)})
        with self.assertRaises(ServerError):
            transport.request("GET", ME)
        lines = self.audit_lines()
        self.assertEqual([line["attempt"] for line in lines], [1, 2, 3, 4])
        self.assertEqual([line["calls"] for line in lines], [1, 2, 3, 4])

    def test_T14b_audit_tool_call_writes_the_same_shape(self) -> None:
        transport, _ = self.make(me_routes())
        transport.request("GET", ME)
        client.audit_tool_call(
            "list_my_mentions",
            mode="read",
            ok=True,
            calls=13,
            ms=4210,
            project_id="1000000000000000018",
        )

        lines = self.audit_lines(self.home / "logs" / f"audit-{datetime.now().date()}.jsonl")
        tool_line = lines[-1]
        api_line = self.audit_lines()[0]
        self.assertEqual(set(tool_line), set(api_line))
        self.assertEqual(set(tool_line), AUDIT_FIELD_NAMES)
        self.assertEqual(tool_line["kind"], "tool")
        self.assertEqual(tool_line["tool"], "list_my_mentions")
        self.assertIsNone(tool_line["method"])
        self.assertIsNone(tool_line["path"])
        self.assertIsNone(tool_line["status"])
        self.assertIsNone(tool_line["attempt"])
        self.assertEqual(tool_line["calls"], 13)
        self.assertEqual(tool_line["ms"], 4210)

    def test_T14b_the_tool_line_has_no_place_for_a_body(self) -> None:
        # 필드 목록이 고정이라 본문이 들어갈 자리가 구조적으로 없다 (§5-3).
        client.audit_tool_call(
            "list_my_mentions",
            mode="read",
            ok=True,
            audit_path=self.home / "logs" / "audit-2026-09-20.jsonl",
        )
        self.assertEqual(set(self.audit_lines()[0]), AUDIT_FIELD_NAMES)

    def test_the_tool_line_can_carry_a_parent_id_and_the_api_line_cannot(self) -> None:
        # 010 spec §4-6 — 값은 `path_id()`를 지난 ID나 우리 상수 `none`뿐이다.
        path = self.home / "logs" / "audit-2026-09-20.jsonl"
        client.audit_tool_call(
            "set_parent_post",
            mode="write",
            ok=True,
            post_id="1000000000000000026",
            parent_id="none",
            audit_path=path,
        )
        self.assertEqual(self.audit_lines()[0]["parent_id"], "none")

        transport, _ = self.make(me_routes())
        transport.request("GET", ME)
        api_line = self.audit_lines()[-1]
        self.assertEqual(api_line["kind"], "api")
        self.assertIsNone(api_line["parent_id"])

    def test_X_4_changed_keeps_only_names_from_the_vocabulary(self) -> None:
        # 012 spec §5-6, 리뷰 L3 — 호출부가 값(제목·이름)을 넣어도 파일에 닿지 못한다.
        path = self.home / "logs" / "audit-2026-09-20.jsonl"
        secret = "제목 원문-이-로그에-없어야-한다-3301"
        cases = (
            (["subject", secret, "body"], ["subject", "body"]),
            ([secret], []),
            (("assignees", "cc"), ["assignees", "cc"]),  # tuple은 list로 적힌다
            (secret, None),  # 문자열은 목록이 아니다 — 글자를 하나씩 보지 않는다
            ({"subject": secret}, None),
            (["status", 3, None, ["due"]], ["status"]),
            (None, None),
        )
        for given, written in cases:
            with self.subTest(given=given):
                client.audit_tool_call(
                    "edit_post", mode="write", ok=True, changed=given, audit_path=path  # type: ignore[arg-type]
                )
                self.assertEqual(self.audit_lines()[-1]["changed"], written)
        self.assertNotIn(secret, path.read_text(encoding="utf-8"))
        self.assertEqual(
            client.CHANGE_NAMES,
            (
                "subject",
                "body",
                "comment",
                "assignees",
                "cc",
                "due",
                "tags",
                "milestone",
                "priority",
                "status",
                "assignee_status",
            ),
        )

    def test_X_4_the_api_line_never_carries_changed(self) -> None:
        transport, _ = self.make(me_routes())
        transport.request("GET", ME)
        self.assertIsNone(self.audit_lines()[0]["changed"])

    def test_T15_no_fixture_body_text_reaches_the_log(self) -> None:
        path = "/project/v1/projects/1000000000000000018/posts/1000000000000000026/logs"
        payload = load_fixture("logs")
        secret = payload["result"][0]["body"]["content"]
        transport, _ = self.make({("GET", path): (200, payload, None)})
        transport.request("GET", path, project_id="1000000000000000018", post_id="1000000000000000026")

        blob = (self.home / "logs" / "audit-2026-09-20.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(secret, blob)
        self.assertNotIn("member-groups", blob)
        self.assertNotIn("멤버", blob)
        self.assertIn("1000000000000000026", blob, "post_id는 남는다")

    def test_T16_the_file_name_and_timestamp_use_local_time(self) -> None:
        transport, _ = self.make(me_routes())
        transport.request("GET", ME)

        expected = self.home / "logs" / "audit-2026-09-20.jsonl"
        self.assertTrue(expected.exists(), self.home_files())
        self.assertEqual(self.audit_lines()[0]["ts"], "2026-09-20T14:03:11+09:00")

    def test_the_log_file_is_0600_and_the_folder_0700(self) -> None:
        transport, _ = self.make(me_routes())
        transport.request("GET", ME)
        log = self.home / "logs" / "audit-2026-09-20.jsonl"
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)

    def test_lines_are_appended_not_replaced(self) -> None:
        transport, _ = self.make(me_routes())
        for _ in range(3):
            transport.request("GET", ME)
        self.assertEqual(len(self.audit_lines()), 3)

    def test_T17_a_failed_log_write_does_not_fail_the_api_call(self) -> None:
        config.ensure_home()
        blocker = self.home / "settings.json"
        blocker.write_text("{}\n", encoding="utf-8")
        stderr = io.StringIO()

        with mock.patch.object(sys, "stderr", stderr):
            transport, _ = self.make(me_routes(), audit_path=blocker / "audit.jsonl")
            data = transport.request("GET", ME)
            transport.request("GET", ME)

        self.assertEqual(data["result"]["id"], "1000000000000000001")
        # 경고는 프로세스당 한 번이다 (MCP stdio는 stdout만 쓴다).
        self.assertEqual(stderr.getvalue().count("\n"), 1, stderr.getvalue())
        self.assertNotIn(FAKE_TOKEN, stderr.getvalue())



def posts_result() -> list[dict]:
    return load_fixture("posts_list")["result"]


def post_by_task(task_number: str) -> dict:
    for item in posts_result():
        if item["taskNumber"] == task_number:
            return item
    raise AssertionError(f"fixture에 {task_number}가 없습니다.")


class ParsePostTest(CoreTestCase):
    """§4 자료형과 파서. 전부 `.get()`, ID는 `str()`, 개수는 `int()`."""

    def test_D5_missing_due_date_and_parent_keys_still_parse(self) -> None:
        # sample/16은 dueDate·parent 키 자체가 빠져 있다 (api-notes §2-4 실측).
        raw = post_by_task("sample/16")
        self.assertNotIn("dueDate", raw)
        self.assertNotIn("parent", raw)

        post = models.parse_post(raw)
        self.assertIsNone(post.due_date)
        self.assertIsNone(post.parent_id)
        self.assertIsNone(post.parent_number)
        self.assertIsNone(post.parent_subject)
        self.assertEqual(post.task_number, "sample/16")
        self.assertEqual(post.number, 16)

    def test_D5_an_empty_response_object_parses_without_raising(self) -> None:
        post = models.parse_post({})
        self.assertEqual(post.id, "")
        self.assertEqual(post.assignees, ())
        self.assertEqual(post.cc, ())
        self.assertEqual(post.files, ())
        self.assertIsNone(post.body)

    def test_D5_parent_is_read_when_present(self) -> None:
        post = models.parse_post(post_by_task("sample/6"))
        self.assertEqual(post.parent_id, "1000000000000000026")
        self.assertEqual(post.parent_number, 5)
        self.assertEqual(post.parent_subject, "업무 제목")

    def test_D18_due_date_flag_is_a_bool(self) -> None:
        post = models.parse_post(post_by_task("sample/5"))
        self.assertIs(post.due_date_flag, True)
        # 의미가 미확인이라 해석하지 않고 실어만 둔다 (§4).
        self.assertIsNone(models.parse_post({}).due_date_flag)
        self.assertIsNone(models.parse_post({"dueDateFlag": "true"}).due_date_flag)

    def test_ids_are_strings_and_counts_are_ints(self) -> None:
        post = models.parse_post({"id": 12345, "number": "7", "project": {"id": 99}})
        self.assertEqual(post.id, "12345")
        self.assertEqual(post.number, 7)
        self.assertEqual(post.project_id, "99")

    def test_assignees_carry_their_own_workflow(self) -> None:
        post = models.parse_post(post_by_task("sample/5"))
        self.assertEqual(len(post.assignees), 5)
        first = post.assignees[0]
        self.assertEqual(first.type, "member")
        self.assertEqual(first.member.id, "1000000000000000012")
        self.assertEqual(first.workflow_id, "1000000000000000009")
        self.assertEqual(first.workflow_name, "멤버7")

    def test_author_and_cc_come_from_users(self) -> None:
        post = models.parse_post(post_by_task("sample/5"))
        self.assertEqual(post.author.id, "1000000000000000003")
        self.assertEqual(post.author.name, "멤버2")
        self.assertIn("1000000000000000003", [m.id for m in post.cc])
        self.assertIn("1000000000000000001", [a.member.id for a in post.assignees])

    def test_detail_adds_body_and_files(self) -> None:
        post = models.parse_post(load_fixture("post_detail")["result"])
        self.assertEqual(post.body_mime, "text/x-markdown")
        self.assertIn("본문 조각 1.", post.body)
        self.assertEqual(len(post.files), 5)
        self.assertEqual(post.files[0].name, "멤버33")
        self.assertEqual(post.files[0].size, 410605)

    def test_unknown_fields_are_ignored(self) -> None:
        raw = dict(post_by_task("sample/5"))
        raw["새로생긴필드"] = {"깊이": [1, 2, 3]}
        post = models.parse_post(raw)
        self.assertEqual(post.task_number, "sample/5")
        self.assertFalse(hasattr(post, "새로생긴필드"))


class ParsePostOtherCcTest(CoreTestCase):
    """012 spec §5-5 RC-3 — 멤버로 읽히지 않은 참조를 센다. 목록을 다시 보내는 도구가 그것을 지우지 않게."""

    def test_RC_3_the_group_cc_entries_are_counted(self) -> None:
        post = models.parse_post(load_fixture("post_detail")["result"])
        self.assertEqual(post.other_cc, 2)  # 그룹 참조 둘 (000 실측 응답)
        self.assertEqual(len(post.cc), 10)

    def test_RC_3_a_members_only_post_counts_zero(self) -> None:
        raw = load_fixture("post_detail")["result"]
        raw["users"]["cc"] = [item for item in raw["users"]["cc"] if item["type"] == "member"]
        self.assertEqual(models.parse_post(raw).other_cc, 0)
        raw["users"].pop("cc")
        self.assertEqual(models.parse_post(raw).other_cc, 0)

    def test_RC_3_a_mail_user_or_a_broken_entry_counts_too(self) -> None:
        raw = load_fixture("post_detail")["result"]
        raw["users"]["cc"] = [
            {"type": "emailUser", "emailUser": {"emailAddress": "someone@example.invalid"}},
            {"type": "member", "member": {}},
            "깨진 항목",
        ]
        post = models.parse_post(raw)
        self.assertEqual((post.cc, post.other_cc), ((), 3))


class ParseTimeTest(CoreTestCase):
    def test_D7_utc_z_and_plus_nine_both_parse_as_aware(self) -> None:
        post = models.parse_post(post_by_task("sample/5"))
        comment = models.parse_comment(load_fixture("logs")["result"][0])

        self.assertIsNotNone(post.updated_at.tzinfo)
        self.assertIsNotNone(post.updated_at.utcoffset())
        self.assertIsNotNone(comment.created_at.tzinfo)
        self.assertIsNotNone(comment.created_at.utcoffset())
        # 표기가 섞여 있어도 aware끼리 비교된다 (api-notes §1-3).
        self.assertGreater(comment.created_at, post.created_at)

    def test_D7_the_same_instant_in_two_notations_compares_equal(self) -> None:
        z = models.parse_post({"updatedAt": "2026-09-19T00:21:46Z"}).updated_at
        kst = models.parse_comment({"createdAt": "2026-09-19T09:21:46+09:00"}).created_at
        self.assertEqual(z, kst)

    def test_naive_and_broken_times_become_none(self) -> None:
        self.assertIsNone(models.parse_post({"updatedAt": "2026-09-19T00:21:46"}).updated_at)
        self.assertIsNone(models.parse_post({"updatedAt": "어제"}).updated_at)
        self.assertIsNone(models.parse_post({"updatedAt": ""}).updated_at)
        self.assertIsNone(models.parse_post({"updatedAt": None}).updated_at)
        self.assertIsNone(models.parse_post({"updatedAt": 17}).updated_at)


class ParseCommentTest(CoreTestCase):
    def test_a_member_comment_keeps_the_author_id_without_a_name(self) -> None:
        comment = models.parse_comment(load_fixture("logs")["result"][1])
        self.assertEqual(comment.id, "1000000000000000054")
        self.assertEqual(comment.post_id, "1000000000000000026")
        self.assertEqual(comment.type, "comment")
        self.assertEqual(comment.subtype, "general")
        self.assertEqual(comment.author.id, "1000000000000000003")
        # 댓글 작성자는 ID만 온다 (api-notes §2-1).
        self.assertIsNone(comment.author.name)
        self.assertIsNone(comment.author_email)

    def test_an_email_author_lands_in_author_email_추정(self) -> None:
        # creator.type == "emailUser"는 fixture에 없다 (§14). 문서 구조대로 읽는다.
        comment = models.parse_comment(
            {
                "id": "9",
                "creator": {
                    "type": "emailUser",
                    "emailUser": {"emailAddress": "someone@example.com", "name": "밖의 사람"},
                },
            }
        )
        self.assertIsNone(comment.author)
        self.assertEqual(comment.author_email, "someone@example.com")

    def test_a_missing_body_becomes_an_empty_string(self) -> None:
        comment = models.parse_comment({"id": "9"})
        self.assertEqual(comment.body, "")
        self.assertIsNone(comment.body_mime)


class ParseOthersTest(CoreTestCase):
    def test_project_workflow_and_member_group_parse(self) -> None:
        project = models.parse_project(load_fixture("projects")["result"][0])
        self.assertEqual(project.id, "1000000000000000004")
        self.assertEqual(project.code, "sample")
        self.assertEqual(project.state, "active")
        self.assertEqual(project.type, "public")
        self.assertEqual(project.scope, "private")

        workflow = models.parse_workflow(load_fixture("workflows")["result"][1])
        self.assertEqual(workflow.name, "멤버7")
        # `class`는 예약어라 필드 이름이 다르다 (§4).
        self.assertEqual(workflow.workflow_class, "working")
        self.assertEqual(workflow.order, 200)

        group = models.parse_member_group(load_fixture("member_group")["result"])
        self.assertEqual(group.id, "1000000000000000017")
        self.assertEqual(len(group.members), 8)
        self.assertIn("1000000000000000001", group.member_ids)
        self.assertNotIn("없는-사람", group.member_ids)

    def test_member_reads_both_shapes(self) -> None:
        me = models.parse_member(load_fixture("members_me")["result"])
        self.assertEqual(me.id, "1000000000000000001")
        self.assertEqual(me.name, "멤버1")
        self.assertEqual(me.user_code, "sample")
        self.assertEqual(me.organization_id, "1000000000000000002")

        # 업무 응답 쪽은 organizationMemberId로 온다 (api-notes §2-1).
        inline = models.parse_member(
            {"organizationMemberId": "1000000000000000012", "name": "멤버18"}
        )
        self.assertEqual(inline.id, "1000000000000000012")
        self.assertEqual(inline.name, "멤버18")

    def test_milestone_dates_stay_raw_strings_추정(self) -> None:
        # 날짜만 오는 형태라 파싱하지 않는다 (api-notes §2-3, U-3).
        milestone = models.parse_milestone(
            {"id": "1", "name": "1분기", "status": "open", "startedAt": "2019-06-20+09:00"}
        )
        self.assertEqual(milestone.started_at, "2019-06-20+09:00")
        self.assertIsNone(milestone.ended_at)

    def test_tag_parses(self) -> None:
        tag = models.parse_tag({"id": 5, "name": "급함", "color": "#ff0000"})
        self.assertEqual(tag.id, "5")
        self.assertEqual(tag.name, "급함")
        self.assertEqual(tag.color, "#ff0000")
        self.assertIsNone(tag.group)

    def test_tag_group_name_is_read_and_null_is_none(self) -> None:
        grouped = models.parse_tag(
            {
                "id": 6,
                "name": "급함",
                "tagGroup": {"id": "7", "name": "우선순위", "mandatory": False, "selectOne": True},
            }
        )
        self.assertEqual(grouped.group, "우선순위")
        for raw in ({"tagGroup": None}, {"tagGroup": "문자열"}, {"tagGroup": {"name": " "}}):
            with self.subTest(raw=raw):
                self.assertIsNone(models.parse_tag({"id": 1, "name": "x", **raw}).group)
        self.assertEqual(
            [tag.group for tag in map(models.parse_tag, load_fixture("tags")["result"])], [None, None]
        )


class ParsePageTest(CoreTestCase):
    def test_D6_a_string_total_count_becomes_an_int(self) -> None:
        page = models.parse_page({"totalCount": "1"}, page=0, size=100)
        self.assertEqual(page.total_count, 1)
        self.assertIsInstance(page.total_count, int)
        self.assertFalse(page.has_more)

    def test_D6_a_missing_or_broken_total_count_is_zero(self) -> None:
        self.assertEqual(models.parse_page({}, page=0, size=100).total_count, 0)
        self.assertEqual(
            models.parse_page({"totalCount": "많음"}, page=0, size=100).total_count, 0
        )

    def test_has_more_follows_page_and_size(self) -> None:
        self.assertTrue(models.parse_page({"totalCount": 69}, page=0, size=10).has_more)
        self.assertFalse(models.parse_page({"totalCount": 69}, page=6, size=10).has_more)


# --- DoorayReader (§6) -------------------------------------------------------

PROJECTS = "/project/v1/projects"
PID = "1000000000000000018"
POSTS = f"{PROJECTS}/{PID}/posts"
POST_ID = "1000000000000000026"
LOGS = f"{POSTS}/{POST_ID}/logs"
MY_ID = "1000000000000000001"


def private_projects() -> dict:
    return {
        "header": {"isSuccessful": True},
        "result": [
            {
                "id": "1000000000000000099",
                "code": "personal",
                "state": "active",
                "type": "private",
                "scope": "private",
            }
        ],
        "totalCount": 1,
    }


class ReaderCase(CoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sleep = FakeSleep()
        self.enterContext(mock.patch.object(client, "_audit_swept", False))
        self.enterContext(mock.patch.object(client, "_audit_warned", False))

    def make_reader(self, routes: dict, **kwargs: object):
        http = mock_transport(routes)
        reader = client.DoorayReader(
            FAKE_TOKEN, BASE, tool="test", http_transport=http, sleep=self.sleep, **kwargs
        )
        self.addCleanup(reader.close)
        return reader, http

    def both_project_pages(self) -> dict:
        return {
            ("GET", PROJECTS): [
                (200, load_fixture("projects"), None),
                (200, private_projects(), None),
            ]
        }


class ReaderMemberTest(ReaderCase):
    def test_me_returns_the_token_owner(self) -> None:
        reader, http = self.make_reader(me_routes())
        me = reader.me()
        self.assertEqual(me.id, MY_ID)
        self.assertEqual(me.name, "멤버1")
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(reader.names[MY_ID], "멤버1")

    def test_member_asks_once_and_then_uses_the_remembered_name(self) -> None:
        reader, http = self.make_reader(
            {("GET", "/common/v1/members/1000000000000000003"): (200, load_fixture("member"), None)}
        )
        first = reader.member("1000000000000000003")
        self.assertEqual(first.name, "멤버2")
        self.assertEqual(len(http.requests), 1)

        # 이름표에 있으면 0회다 (§6-1).
        again = reader.member("1000000000000000003")
        self.assertEqual(again.name, "멤버2")
        self.assertEqual(len(http.requests), 1)

    def test_find_members_by_name_sends_the_whole_name(self) -> None:
        # name=은 부분 일치가 아니다. 이름은 전체를 받아 조회한다 (api-notes §2-1).
        reader, http = self.make_reader(
            {("GET", "/common/v1/members"): (200, {"result": [load_fixture("member")["result"]], "totalCount": 1}, None)}
        )
        found = reader.find_members_by_name("멤버2")
        self.assertEqual([m.id for m in found], ["1000000000000000003"])
        self.assertEqual(http.requests[0].url.params["name"], "멤버2")
        self.assertEqual(http.requests[0].url.params["size"], "100")


class ReaderProjectsTest(ReaderCase):
    def test_D1_projects_asks_twice_with_member_me(self) -> None:
        reader, http = self.make_reader(self.both_project_pages())
        projects = reader.projects()

        self.assertEqual(len(http.requests), 2)
        # 첫 호출은 `type`을 **보내지 않는다.** `type=public`을 명시하면 scope 기본값(private)이 걸려
        # 조직 공개(scope=public) 프로젝트가 빠진다 — 실측에서 21개 중 3개가 사라졌다 (api-notes §2-2).
        self.assertEqual(
            [r.url.params.get("type") for r in http.requests], [None, "private"]
        )
        for sent in http.requests:
            self.assertNotIn("scope", sent.url.params)
            # member=me를 빼면 남의 개인 프로젝트가 섞인다 (api-notes §2-2 실측).
            self.assertEqual(sent.url.params["member"], "me")
            self.assertEqual(sent.url.params["state"], "active")
        self.assertEqual(len(projects), 4)
        self.assertIn("1000000000000000099", [p.id for p in projects])

    def test_D1_member_and_type_are_not_arguments(self) -> None:
        reader, _ = self.make_reader(self.both_project_pages())
        with self.assertRaises(TypeError):
            reader.projects(member="everyone")
        with self.assertRaises(TypeError):
            reader.projects(type="private")

    def test_D19_a_second_call_on_the_same_reader_costs_nothing(self) -> None:
        reader, http = self.make_reader(self.both_project_pages())
        first = reader.projects()
        second = reader.projects()
        self.assertEqual(len(http.requests), 2)
        self.assertEqual([p.id for p in first], [p.id for p in second])

    def test_D20_a_new_reader_asks_again(self) -> None:
        # 낡은 목록은 새 프로젝트의 멘션을 영구히 놓치게 한다 (§6-1).
        routes = {
            ("GET", PROJECTS): [
                (200, load_fixture("projects"), None),
                (200, private_projects(), None),
                (200, load_fixture("projects"), None),
                (200, private_projects(), None),
            ]
        }
        http = mock_transport(routes)
        first = client.DoorayReader(FAKE_TOKEN, BASE, tool="t", http_transport=http, sleep=self.sleep)
        first.projects()
        first.close()
        self.assertEqual(len(http.requests), 2)

        second = client.DoorayReader(FAKE_TOKEN, BASE, tool="t", http_transport=http, sleep=self.sleep)
        self.addCleanup(second.close)
        second.projects()
        self.assertEqual(len(http.requests), 4)

    def test_seeded_projects_cost_nothing_and_are_not_reported_as_loaded(self) -> None:
        # 003 spec §2. 코어는 캐시를 모른다 — 채우는 구멍과 "직접 받았는가"만 연다.
        http = mock_transport({("GET", PROJECTS): (200, load_fixture("projects"), None)})
        fresh = client.DoorayReader(FAKE_TOKEN, BASE, tool="t", http_transport=http, sleep=self.sleep)
        self.addCleanup(fresh.close)
        self.assertIsNone(fresh.loaded_projects)
        listed = fresh.projects()
        self.assertEqual(fresh.loaded_projects, tuple(listed))

        seeded = client.DoorayReader(FAKE_TOKEN, BASE, tool="t", http_transport=http, sleep=self.sleep)
        self.addCleanup(seeded.close)
        before = len(http.requests)
        seeded.seed_projects(listed)
        self.assertEqual(seeded.projects(), listed)
        self.assertEqual(len(http.requests), before)
        self.assertIsNone(seeded.loaded_projects)  # 씨앗은 "받아 온 것"이 아니다

    def test_duplicate_projects_across_the_two_calls_are_merged(self) -> None:
        routes = {
            ("GET", PROJECTS): [
                (200, load_fixture("projects"), None),
                (200, load_fixture("projects"), None),
            ]
        }
        reader, _ = self.make_reader(routes)
        self.assertEqual(len(reader.projects()), 3)

    def test_private_projects_fixes_member_type_and_state(self) -> None:
        # 010 spec §4-2 — 쓰기가 만들 곳을 찾는 읽기 하나. 인자를 열지 않는다.
        reader, http = self.make_reader(
            {("GET", PROJECTS): (200, load_fixture("private_projects"), None)}
        )
        found = reader.private_projects()
        self.assertEqual(len(http.requests), 1)
        sent = http.requests[0]
        self.assertEqual(sent.url.params["member"], "me")
        self.assertEqual(sent.url.params["type"], "private")
        self.assertEqual(sent.url.params["state"], "active")
        self.assertEqual([p.id for p in found], ["1000000000000000099"])
        with self.assertRaises(TypeError):
            reader.private_projects(member="everyone")

    def test_private_projects_neither_reads_nor_fills_the_cache(self) -> None:
        # 쓰기는 기억해 둔 목록을 쓰지 않는다 (코어 계약 7③).
        reader, http = self.make_reader(
            {("GET", PROJECTS): (200, load_fixture("private_projects"), None)}
        )
        reader.seed_projects([models.parse_project({"id": "1000000000000000004", "code": "sample"})])
        self.assertEqual(len(reader.private_projects()), 1, "씨앗을 읽었습니다")
        self.assertEqual(len(http.requests), 1)
        # 씨앗은 그대로다 — `private_projects()`가 갈아 끼우지 않았다.
        self.assertEqual([p.id for p in reader.projects()], ["1000000000000000004"])
        self.assertEqual(len(http.requests), 1)


class ReaderProjectPartsTest(ReaderCase):
    def test_D21_workflows_asks_every_time(self) -> None:
        path = f"{PROJECTS}/{PID}/workflows"
        reader, http = self.make_reader({("GET", path): (200, load_fixture("workflows"), None)})
        first = reader.workflows(PID)
        second = reader.workflows(PID)

        self.assertEqual(len(http.requests), 2, "기억하지 않는다")
        self.assertEqual([w.name for w in first], [w.name for w in second])
        self.assertEqual(first[1].workflow_class, "working")
        # 페이지 파라미터가 없는 엔드포인트다 (api-notes §2-3).
        self.assertNotIn("page", http.requests[0].url.params)
        self.assertNotIn("size", http.requests[0].url.params)

    def test_milestones_send_no_parameters_at_all(self) -> None:
        # §2-3이 마일스톤의 페이지 파라미터를 적지 않았다. 근거 없는 파라미터를 보내지 않는다.
        path = f"{PROJECTS}/{PID}/milestones"
        reader, http = self.make_reader(
            {("GET", path): (200, {"result": [{"id": "1", "name": "1분기", "status": "open"}]}, None)}
        )
        milestones = reader.milestones(PID)
        self.assertEqual(milestones[0].name, "1분기")
        self.assertEqual(str(http.requests[0].url.query, "utf-8"), "")

    def test_tags_ask_for_a_full_page(self) -> None:
        path = f"{PROJECTS}/{PID}/tags"
        reader, http = self.make_reader(
            {("GET", path): (200, {"result": [{"id": "3", "name": "급함", "color": "#f00"}], "totalCount": 1}, None)}
        )
        tags = reader.tags(PID)
        self.assertEqual(tags[0].name, "급함")
        self.assertEqual(http.requests[0].url.params["size"], "100")

    def test_D15_a_member_group_from_another_project_is_read_on_this_project_path(self) -> None:
        # 멘션된 그룹이 다른 프로젝트 소속이어도 현재 프로젝트 경로로 조회한다 (api-notes §2-7 실측).
        gid = "1000000000000000019"
        path = f"{PROJECTS}/{PID}/member-groups/{gid}"
        payload = load_fixture("member_group_other_project")
        self.assertNotEqual(payload["result"]["project"]["id"], PID)

        reader, http = self.make_reader({("GET", path): (200, payload, None)})
        group = reader.member_group(gid, project_id=PID)

        self.assertEqual(group.id, gid)
        self.assertIn(MY_ID, group.member_ids)
        self.assertEqual(http.requests[0].url.path, path)
        # 그룹 구성원의 이름도 이름표에 들어간다 (api-notes §2-1).
        self.assertEqual(reader.names[MY_ID], "멤버1")

    def test_D16_a_member_group_is_fetched_every_time(self) -> None:
        # 오래된 그룹 구성은 그룹 멘션을 영구히 놓치게 한다 (ADR 005).
        gid = "1000000000000000017"
        path = f"{PROJECTS}/{PID}/member-groups/{gid}"
        reader, http = self.make_reader({("GET", path): (200, load_fixture("member_group"), None)})
        reader.member_group(gid, project_id=PID)
        reader.member_group(gid, project_id=PID)
        self.assertEqual(len(http.requests), 2)


class ReaderPostsTest(ReaderCase):
    def test_D2_posts_always_send_the_fixed_order(self) -> None:
        reader, http = self.make_reader({("GET", POSTS): (200, load_fixture("posts_list"), None)})
        posts, page = reader.posts(PID)

        # order를 안 주면 updatedAt 순이 아니다 (api-notes §2-4 실측).
        self.assertEqual(http.requests[0].url.params["order"], "-postUpdatedAt")
        self.assertEqual(len(posts), 5)
        self.assertEqual(page.total_count, 14)

    def test_D2_order_is_not_an_argument(self) -> None:
        reader, _ = self.make_reader({("GET", POSTS): (200, load_fixture("posts_list"), None)})
        with self.assertRaises(TypeError):
            reader.posts(PID, order="createdAt")

    def test_D3_updated_since_becomes_an_open_ended_range(self) -> None:
        reader, http = self.make_reader({("GET", POSTS): (200, load_fixture("posts_list"), None)})
        since = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
        reader.posts(PID, updated_since=since)
        self.assertEqual(http.requests[0].url.params["updatedAt"], since.isoformat() + "~")

    def test_D3_updated_until_becomes_a_closed_range(self) -> None:
        # api-notes §2-4 실측(002 등록 시험): 닫힌 범위 `A~B`는 걸리고, 앞이 열린 `~B`는 400이다.
        reader, http = self.make_reader({("GET", POSTS): (200, load_fixture("posts_list"), None)})
        until = datetime(2026, 9, 6, 12, 0, tzinfo=KST)
        reader.posts(PID, updated_until=until)
        self.assertEqual(
            http.requests[0].url.params["updatedAt"],
            "1970-01-01T00:00:00+00:00~2026-09-06T12:00:00+09:00",
        )
        since = datetime(2026, 9, 1, tzinfo=KST)
        reader.posts(PID, updated_since=since, updated_until=until)
        self.assertEqual(
            http.requests[1].url.params["updatedAt"],
            "2026-09-01T00:00:00+09:00~2026-09-06T12:00:00+09:00",
        )

    def test_D3_a_naive_until_is_refused_before_any_request(self) -> None:
        reader, http = self.make_reader({("GET", POSTS): (200, load_fixture("posts_list"), None)})
        with self.assertRaises(ValueError) as caught:
            reader.posts(PID, updated_until=datetime(2026, 9, 6, 12, 0))
        self.record(caught.exception)
        self.assertEqual(len(http.requests), 0)

    def test_D3_a_naive_since_is_refused_before_any_request(self) -> None:
        reader, http = self.make_reader({("GET", POSTS): (200, load_fixture("posts_list"), None)})
        with self.assertRaises(ValueError) as caught:
            reader.posts(PID, updated_since=datetime(2026, 9, 19, 3, 0))
        self.record(caught.exception)
        self.assertEqual(len(http.requests), 0)

    def test_the_optional_filters_are_only_sent_when_given(self) -> None:
        reader, http = self.make_reader({("GET", POSTS): (200, load_fixture("posts_list"), None)})
        reader.posts(
            PID,
            to_member_ids=["1", "2"],
            workflow_classes=["registered", "working"],
            subjects="회의",
            to_member_size=0,
            parent_post_id="9",
        )
        params = http.requests[0].url.params
        self.assertEqual(params["toMemberIds"], "1,2")
        self.assertEqual(params["postWorkflowClasses"], "registered,working")
        self.assertEqual(params["subjects"], "회의")
        self.assertEqual(params["toMemberSize"], "0")
        self.assertEqual(params["parentPostId"], "9")
        for absent in ("ccMemberIds", "fromMemberIds", "tagIds", "milestoneIds", "createdAt", "dueAt"):
            self.assertNotIn(absent, params)

    def test_max_pages_stops_the_walk_and_reports_more(self) -> None:
        reader, http = self.make_reader(
            {("GET", POSTS): [(200, load_fixture("posts_list"), None)] * 3}
        )
        posts, page = reader.posts(PID, size=2, max_pages=1)
        self.assertEqual(len(http.requests), 1)
        self.assertTrue(page.has_more)

        posts, page = reader.posts(PID, size=2, max_pages=2)
        self.assertEqual(len(http.requests), 3)
        self.assertEqual([r.url.params["page"] for r in http.requests[1:]], ["0", "1"])

    def test_D10_names_are_gathered_from_the_post_list(self) -> None:
        reader, _ = self.make_reader({("GET", POSTS): (200, load_fixture("posts_list"), None)})
        reader.posts(PID)
        # 작성자·담당자·참조자의 이름이 응답에 같이 온다 (api-notes §2-1).
        self.assertEqual(reader.names["1000000000000000012"], "멤버18")
        self.assertEqual(reader.names["1000000000000000001"], "멤버1")
        self.assertIn("1000000000000000022", reader.names)

    def test_D10_the_name_table_is_read_only(self) -> None:
        # 밖에서 채워 넣는 인자를 두지 않는다 (§16의 36).
        reader, _ = self.make_reader({("GET", POSTS): (200, load_fixture("posts_list"), None)})
        reader.posts(PID)
        with self.assertRaises(TypeError):
            reader.names["아무개"] = "끼워넣기"
        with self.assertRaises(TypeError):
            client.DoorayReader(FAKE_TOKEN, BASE, tool="t", names={"a": "b"})

    def test_D8_post_picks_the_endpoint_by_project_id(self) -> None:
        with_project = f"{POSTS}/{POST_ID}"
        without = f"/project/v1/posts/{POST_ID}"
        reader, http = self.make_reader(
            {
                ("GET", with_project): (200, load_fixture("post_detail"), None),
                ("GET", without): (200, load_fixture("post_detail"), None),
            }
        )
        first = reader.post(POST_ID, project_id=PID)
        second = reader.post(POST_ID)

        self.assertEqual(first.id, POST_ID)
        self.assertEqual(second.id, POST_ID)
        self.assertEqual([r.url.path for r in http.requests], [with_project, without])
        self.assertEqual(len(first.files), 5)

    def test_D9_find_post_id_asks_by_post_number_once(self) -> None:
        reader, http = self.make_reader({("GET", POSTS): (200, load_fixture("posts_list"), None)})
        found = reader.find_post_id(PID, 5)

        self.assertEqual(len(http.requests), 1)
        self.assertEqual(http.requests[0].url.params["postNumber"], "5")
        self.assertEqual(http.requests[0].url.params["order"], "-postUpdatedAt")
        self.assertEqual(found, "1000000000000000021")

    def test_D9_find_post_id_returns_none_when_nothing_matches(self) -> None:
        reader, _ = self.make_reader({("GET", POSTS): (200, {"result": [], "totalCount": 0}, None)})
        self.assertIsNone(reader.find_post_id(PID, 999))


class ReaderCommentsTest(ReaderCase):
    def test_comments_read_oldest_first_and_drop_system_events(self) -> None:
        payload = load_fixture("logs")
        payload["result"] = payload["result"] + [
            {"id": "1000000000000000070", "post": {"id": POST_ID}, "type": "event", "createdAt": "2026-09-19T09:00:00+09:00"}
        ]
        reader, http = self.make_reader({("GET", LOGS): (200, payload, None)})
        comments = reader.comments(POST_ID, project_id=PID)

        self.assertEqual(http.requests[0].url.params["order"], "createdAt")
        self.assertEqual(http.requests[0].url.params["size"], "100")
        # 실측에서 event는 한 건도 없었다. 필터는 두되 기본 제외다 (api-notes §2-6).
        self.assertNotIn("1000000000000000070", [c.id for c in comments])
        self.assertEqual(len(comments), 12)

    def test_comments_can_keep_the_events_when_asked(self) -> None:
        payload = load_fixture("logs")
        payload["result"] = payload["result"] + [
            {"id": "1000000000000000070", "post": {"id": POST_ID}, "type": "event", "createdAt": "2026-09-19T09:00:00+09:00"}
        ]
        reader, _ = self.make_reader({("GET", LOGS): (200, payload, None)})
        comments = reader.comments(POST_ID, project_id=PID, include_events=True)
        self.assertEqual(len(comments), 13)

    def test_RC_1_one_comment_is_one_get_on_its_own_path(self) -> None:
        # 012 spec §5-5 — api-notes 012 실측 둘째의 단건 조회 경로.
        one = f"{LOGS}/1000000000000000056"
        reader, http = self.make_reader({("GET", one): (200, load_fixture("log_single"), None)})
        found = reader.comment(POST_ID, "1000000000000000056", project_id=PID)

        self.assertEqual([(row.method, row.url.path) for row in http.requests], [("GET", one)])
        self.assertIsInstance(found, models.Comment)
        self.assertEqual(found.id, "1000000000000000056")
        self.assertEqual(found.post_id, POST_ID)
        self.assertEqual((found.type, found.subtype), ("comment", "general"))
        self.assertEqual(found.body_mime, "text/x-markdown")
        self.assertIn("dooray://", found.body)
        self.assertEqual(found.author.id if found.author else None, MY_ID)

    def test_RC_2_a_bent_comment_id_sends_nothing(self) -> None:
        reader, http = self.make_reader({})
        for bent in ("30/../../move", "30?x=1", "", "a" * 65):
            with self.subTest(bent=bent):
                with self.assertRaises(errors.InvalidIdError) as caught:
                    reader.comment(POST_ID, bent, project_id=PID)
                self.record(caught.exception)
        self.assertEqual(http.requests, [])

    def test_D4_new_comments_read_newest_first_and_stop(self) -> None:
        reader, http = self.make_reader({("GET", LOGS): (200, load_fixture("logs"), None)})
        since = datetime(2026, 9, 19, 0, 0, tzinfo=KST)
        fresh = reader.new_comments(POST_ID, project_id=PID, since=since)

        # 시각 필터가 없으니 최신부터 읽다 멈춘다 (api-notes §2-6).
        self.assertEqual(http.requests[0].url.params["order"], "-createdAt")
        self.assertEqual(len(http.requests), 1, "댓글 100개 이하면 호출 1회다")
        self.assertEqual(
            [c.id for c in fresh],
            [
                "1000000000000000056",
                "1000000000000000055",
                "1000000000000000054",
                "1000000000000000053",
            ],
            "오래된 순으로 뒤집어 돌려준다",
        )

    def test_D4_nothing_new_means_an_empty_list_after_one_call(self) -> None:
        reader, http = self.make_reader({("GET", LOGS): (200, load_fixture("logs"), None)})
        since = datetime(2026, 9, 30, 0, 0, tzinfo=KST)
        self.assertEqual(reader.new_comments(POST_ID, project_id=PID, since=since), [])
        self.assertEqual(len(http.requests), 1)

    def test_D4_the_walk_stops_at_the_page_limit_추정(self) -> None:
        # 댓글 100개 초과 업무의 페이지 넘김은 미확인이다 (api-notes §5 U-6).
        payload = load_fixture("logs")
        payload["totalCount"] = 500
        reader, http = self.make_reader({("GET", LOGS): [(200, payload, None)] * 5})
        since = datetime(2020, 1, 1, 0, 0, tzinfo=KST)
        reader.new_comments(POST_ID, project_id=PID, since=since, max_pages=2)
        self.assertEqual(len(http.requests), 2)
        self.assertEqual([r.url.params["page"] for r in http.requests], ["0", "1"])


STREAMS = "/common/v1/streams"


def feed_page(items: list[dict], cursor: str | None = "1781768002000000000") -> dict:
    """활동 피드 한 쪽. `totalCount`가 없고 `cursor`로 잇는다 (api-notes "추가 실측")."""
    page: dict = {"header": {"isSuccessful": True}, "result": items}
    if cursor is not None:
        page["cursor"] = cursor
    return page


def feed_event(
    event_id: str,
    *,
    kind: str = "comment",
    created: str | None = "2026-09-19T09:00:00+09:00",
    post_id: str = POST_ID,
    body: str = "댓글 본문",
) -> dict:
    """fixture에 없는 모양(시각을 못 읽는 항목 등)을 합성한다."""
    inner: dict = {
        "read": True,
        "id": event_id,
        "type": kind,
        "postId": post_id,
        "creator": {
            "type": "member",
            "member": {"organizationMemberId": "1000000000000000200", "name": "멤버30"},
        },
        "body": {"mimeType": "text/x-markdown", "content": body},
    }
    if created is not None:
        inner["createdAt"] = created
        inner["updatedAt"] = created
    return {"type": "event", "event": inner}


class ReaderFeedTest(ReaderCase):
    """003 spec §5-1 — 완료 기준 8. `GET /common/v1/streams` (api-notes "추가 실측")."""

    OLD = datetime(2026, 9, 1, tzinfo=KST)  # fixture의 모든 항목보다 오래됐다
    # fixture 13건 가운데 싣는 것: 바깥 `event` + 상세 `comment`인 10건.
    COMMENT_IDS = [
        "100000000000000030" + tail
        for tail in ("2", "3", "4", "5", "6", "7", "8", "9")
    ] + ["1000000000000000310", "1000000000000000312"]

    def feed(self) -> dict:
        return load_fixture("streams")

    def test_기준8_system_events_and_other_types_are_left_out(self) -> None:
        reader, http = self.make_reader(
            {("GET", STREAMS): [(200, self.feed(), None), (200, feed_page([], None), None)]}
        )
        found, reached = reader.feed_comments(since=self.OLD)

        self.assertEqual([item.id for item in found], self.COMMENT_IDS)
        self.assertEqual({item.kind for item in found}, {"comment"})
        # 시스템 이벤트 2건(…301·…311)과 `post` 1건이 빠졌다.
        self.assertEqual(len(self.feed()["result"]), 13)
        self.assertTrue(reached)
        self.assertEqual(len(http.requests), 2)

    def test_기준8_the_fields_of_one_comment_are_read(self) -> None:
        reader, _ = self.make_reader(
            {("GET", STREAMS): [(200, self.feed(), None), (200, feed_page([], None), None)]}
        )
        found, _ = reader.feed_comments(since=self.OLD)
        first = found[0]

        self.assertEqual(first.id, "1000000000000000302")
        self.assertEqual(first.post_id, "1000000000000000026")
        self.assertEqual(first.kind, "comment")
        self.assertEqual(first.created_at, datetime(2026, 9, 19, 21, 10, tzinfo=KST))
        self.assertEqual(first.updated_at, datetime(2026, 9, 19, 21, 10, tzinfo=KST))
        self.assertEqual(first.author_id, "1000000000000000200")
        self.assertEqual(first.author_name, "멤버30")
        self.assertEqual(first.body, "댓글 본문 2")
        self.assertEqual(first.body_mime, "text/x-markdown")
        self.assertTrue(first.read)
        # 작성자 이름은 추가 호출 없이 이름표에 들어간다 (003 spec §5-1).
        self.assertEqual(reader.names["1000000000000000200"], "멤버30")

    def test_기준8_the_first_page_sends_the_size_and_no_cursor(self) -> None:
        reader, http = self.make_reader({("GET", STREAMS): (200, feed_page([], None), None)})
        reader.feed_comments(since=self.OLD)
        params = http.requests[0].url.params
        self.assertEqual(params["size"], "200")
        self.assertNotIn("before", params)

    def test_기준8_the_cursor_is_sent_as_before(self) -> None:
        reader, http = self.make_reader(
            {
                ("GET", STREAMS): [
                    (200, feed_page([feed_event("1000000000000000401")], "커서1"), None),
                    (200, feed_page([feed_event("1000000000000000402")], None), None),
                ]
            }
        )
        found, reached = reader.feed_comments(since=self.OLD)

        self.assertEqual(len(http.requests), 2)
        self.assertNotIn("before", http.requests[0].url.params)
        self.assertEqual(http.requests[1].url.params["before"], "커서1")
        self.assertEqual(len(found), 2)
        self.assertTrue(reached, "cursor가 없는 쪽에서 끝났다")

    def test_기준8_an_item_older_than_since_stops_the_walk(self) -> None:
        reader, http = self.make_reader({("GET", STREAMS): (200, self.feed(), None)})
        found, reached = reader.feed_comments(since=datetime(2026, 9, 17, tzinfo=KST))

        # …309(09-16 14:10)가 기준보다 오래됐다. 그 앞까지만 싣는다.
        self.assertEqual(
            [item.id[-3:] for item in found],
            ["302", "303", "304", "305", "306", "307", "308"],
        )
        self.assertTrue(reached)
        self.assertEqual(len(http.requests), 1, "cursor가 있어도 더 묻지 않는다")

    def test_기준8_only_event_items_decide_the_stop(self) -> None:
        # fixture의 `post` 항목은 09-18 09:00으로 앞뒤 `event`보다 오래됐다 —
        # 최신순은 `event` 줄에서만 성립한다. `post`의 시각으로 멈추면 댓글을 잃는다.
        reader, _ = self.make_reader({("GET", STREAMS): (200, self.feed(), None)})
        found, reached = reader.feed_comments(since=datetime(2026, 9, 18, 12, tzinfo=KST))

        self.assertEqual([item.id[-3:] for item in found], ["302", "303", "304", "305"])
        self.assertTrue(reached)

    def test_기준8_a_page_count_smaller_than_size_is_not_the_end(self) -> None:
        # 응답 13건 < size 200이어도 마지막이 아니다 (api-notes "추가 실측").
        reader, http = self.make_reader(
            {
                ("GET", STREAMS): [
                    (200, self.feed(), None),
                    (200, feed_page([feed_event("1000000000000000401")], None), None),
                ]
            }
        )
        found, reached = reader.feed_comments(since=self.OLD)

        self.assertEqual(len(http.requests), 2)
        self.assertEqual(len(found), 11)
        self.assertTrue(reached)

    def test_기준8_an_empty_page_ends_the_walk_even_with_a_cursor(self) -> None:
        reader, http = self.make_reader({("GET", STREAMS): (200, feed_page([], "커서1"), None)})
        found, reached = reader.feed_comments(since=self.OLD)

        self.assertEqual(found, [])
        self.assertTrue(reached)
        self.assertEqual(len(http.requests), 1)

    def test_기준8_the_page_limit_leaves_since_unreached(self) -> None:
        reader, http = self.make_reader({("GET", STREAMS): (200, self.feed(), None)})
        found, reached = reader.feed_comments(since=self.OLD, max_pages=2)

        self.assertEqual(len(http.requests), 2)
        self.assertEqual(len(found), 20)
        self.assertFalse(reached, "더 오래된 멘션이 남아 있을 수 있다")

    def test_기준8_an_item_without_a_readable_time_is_kept(self) -> None:
        # 시각을 못 읽은 항목은 버리지 않는다. 멈추게 하지도 않는다 (003 spec §5-1).
        items = [
            feed_event("1000000000000000401", created=None),
            feed_event("1000000000000000402", created="2026-09-19T09:00:00+09:00"),
        ]
        reader, _ = self.make_reader({("GET", STREAMS): (200, feed_page(items, None), None)})
        found, reached = reader.feed_comments(since=datetime(2026, 9, 19, tzinfo=KST))

        self.assertEqual([item.id[-3:] for item in found], ["401", "402"])
        self.assertIsNone(found[0].created_at)
        self.assertTrue(reached)

    def test_기준8_a_naive_since_is_refused_before_any_request(self) -> None:
        reader, http = self.make_reader({})
        with self.assertRaises(ValueError):
            reader.feed_comments(since=datetime(2026, 9, 1))
        self.assertEqual(http.requests, [])

    def test_기준8_the_feed_never_asks_for_a_project(self) -> None:
        reader, http = self.make_reader(
            {("GET", STREAMS): [(200, self.feed(), None), (200, feed_page([], None), None)]}
        )
        reader.feed_comments(since=self.OLD)
        self.assertEqual({r.url.path for r in http.requests}, {STREAMS})


def feed_post(project_id: str, post_id: str = "1000000000000000050") -> dict:
    """피드의 `post` 항목. **이쪽에만 `projectId`가 있다** (api-notes "추가 실측")."""
    return {
        "type": "post",
        "post": {
            "read": True,
            "id": post_id,
            "projectId": project_id,
            "subject": "업무 제목",
            "createdAt": "2026-09-18T09:00:00+09:00",
        },
    }


class ReaderFeedActivityTest(ReaderCase):
    """003 spec §6-2 — 완료 기준 15. 비멤버 프로젝트를 찾는 단서만 거둔다. 본문은 읽지 않는다."""

    def feed(self) -> dict:
        return load_fixture("streams")

    def test_기준15_the_fixture_gives_one_project_and_three_posts(self) -> None:
        reader, http = self.make_reader(
            {("GET", STREAMS): [(200, self.feed(), None), (200, feed_page([], None), None)]}
        )
        projects, posts = reader.feed_activity()

        # `post` 항목 1건의 프로젝트(…018. fixture projects.json에 없는 비멤버다).
        self.assertEqual(projects, ["1000000000000000018"])
        # `event` 항목 12건이 가리키는 업무는 셋뿐이다. 처음 나온 순서(=최신순)를 지킨다.
        self.assertEqual(
            posts,
            [
                "1000000000000000026",
                "1000000000000000027",
                "1000000000000000028",
            ],
        )
        self.assertEqual({r.url.path for r in http.requests}, {STREAMS})

    def test_기준15_a_system_event_still_gives_its_post(self) -> None:
        # 시스템 이벤트의 업무도 비멤버 프로젝트의 단서다. 빼는 쪽이 유실이다.
        items = [feed_event("1000000000000000401", kind="event", post_id="1000000000000000077")]
        reader, _ = self.make_reader({("GET", STREAMS): (200, feed_page(items, None), None)})
        projects, posts = reader.feed_activity()
        self.assertEqual(projects, [])
        self.assertEqual(posts, ["1000000000000000077"])

    def test_기준15_the_cursor_is_sent_as_before(self) -> None:
        reader, http = self.make_reader(
            {
                ("GET", STREAMS): [
                    (200, feed_page([feed_post("1000000000000000018")], "커서1"), None),
                    (200, feed_page([feed_post("1000000000000000020")], None), None),
                ]
            }
        )
        projects, _ = reader.feed_activity()

        self.assertEqual(len(http.requests), 2)
        self.assertNotIn("before", http.requests[0].url.params)
        self.assertEqual(http.requests[1].url.params["before"], "커서1")
        self.assertEqual(http.requests[0].url.params["size"], "200")
        self.assertEqual(projects, ["1000000000000000018", "1000000000000000020"])

    def test_기준15_the_same_project_twice_is_one_entry(self) -> None:
        items = [
            feed_post("1000000000000000018", post_id="1000000000000000050"),
            feed_post("1000000000000000018", post_id="1000000000000000051"),
        ]
        reader, _ = self.make_reader({("GET", STREAMS): (200, feed_page(items, None), None)})
        projects, posts = reader.feed_activity()
        self.assertEqual(projects, ["1000000000000000018"])
        self.assertEqual(posts, [])

    def test_기준15_the_page_limit_stops_the_walk(self) -> None:
        reader, http = self.make_reader({("GET", STREAMS): (200, self.feed(), None)})
        reader.feed_activity(max_pages=2)
        self.assertEqual(len(http.requests), 2, "cursor가 있어도 상한에서 멈춘다")

    def test_기준15_an_empty_page_ends_the_walk_even_with_a_cursor(self) -> None:
        reader, http = self.make_reader({("GET", STREAMS): (200, feed_page([], "커서1"), None)})
        projects, posts = reader.feed_activity()
        self.assertEqual((projects, posts), ([], []))
        self.assertEqual(len(http.requests), 1)


class ReaderSeenProjectsTest(ReaderCase):
    """003 spec §6-2 — 단건 조회와 `seen` 씨앗. `projects()`의 뜻은 그대로다."""

    def test_project_reads_one_project_by_id(self) -> None:
        row = {
            "header": {"isSuccessful": True},
            "result": {
                "id": "1000000000000000018",
                "code": "밖의프로젝트",
                "state": "active",
                "type": "public",
                "scope": "public",
            },
        }
        reader, http = self.make_reader(
            {("GET", f"{PROJECTS}/1000000000000000018"): (200, row, None)}
        )
        found = reader.project("1000000000000000018")

        self.assertEqual(found.id, "1000000000000000018")
        self.assertEqual(found.code, "밖의프로젝트")
        self.assertEqual(found.scope, "public")
        self.assertEqual(len(http.requests), 1)

    def test_a_bent_project_id_never_reaches_the_network(self) -> None:
        reader, http = self.make_reader({})
        with self.assertRaises(errors.InvalidIdError):
            reader.project("18/../../drive/v1/drives")
        self.assertEqual(http.requests, [])

    def test_all_projects_puts_the_members_first_and_drops_duplicates(self) -> None:
        reader, http = self.make_reader(self.both_project_pages())
        member_ids = [project.id for project in reader.projects()]
        seen = (
            models.Project(id="1000000000000000018", code="밖", state=None, type=None, scope=None),
            models.Project(id=member_ids[0], code="sample", state=None, type=None, scope=None),
        )
        reader.seed_seen(seen)
        before = len(http.requests)
        both = reader.all_projects()

        self.assertEqual([p.id for p in both], member_ids + ["1000000000000000018"])
        self.assertEqual(len(http.requests), before, "씨앗은 요청을 보내지 않는다")
        self.assertEqual([p.id for p in reader.seen_projects], [p.id for p in seen])
        # `projects()`의 뜻은 바뀌지 않았다 — 멤버 목록뿐이다.
        self.assertEqual([p.id for p in reader.projects()], member_ids)

    def test_seen_is_empty_until_it_is_seeded(self) -> None:
        reader, _ = self.make_reader({})
        self.assertEqual(reader.seen_projects, ())


class KnownProjectsFileTest(CoreTestCase):
    """003 spec §6-1 — 완료 기준 17·21. `state/projects.json` 한 파일에 둘이 산다."""

    NOW = datetime(2026, 9, 20, 12, 0, tzinfo=KST)

    def rows(self, *ids: str) -> list[models.Project]:
        return [
            models.Project(id=value, code=f"p{value[-2:]}", state="active", type="public", scope="private")
            for value in ids
        ]

    def saved(self) -> dict:
        return json.loads(
            (self.home / "state" / "projects.json").read_text(encoding="utf-8")
        )

    def test_기준17_seen_survives_a_member_only_save(self) -> None:
        client.save_known_projects(
            self.rows("1000000000000000004"), seen=self.rows("1000000000000000018"), now=self.NOW
        )
        # `seen=`을 주지 않은 호출은 발견해 둔 것을 지우지 않는다.
        client.save_known_projects(self.rows("1000000000000000005"), now=self.NOW)

        self.assertEqual(
            [p.id for p in client.load_seen_projects()], ["1000000000000000018"]
        )
        self.assertEqual(
            [p.id for p in client.load_known_projects(now=self.NOW)], ["1000000000000000005"]
        )

    def test_기준17_seen_is_read_after_the_member_list_has_expired(self) -> None:
        client.save_known_projects(
            self.rows("1000000000000000004"), seen=self.rows("1000000000000000018"), now=self.NOW
        )
        later = self.NOW + timedelta(days=3)
        with mock.patch.object(client, "_local_now", lambda: later):
            self.assertIsNone(client.load_known_projects(now=later))
            self.assertEqual(
                [p.id for p in client.load_seen_projects()], ["1000000000000000018"]
            )

    def test_기준17_a_project_i_joined_leaves_seen(self) -> None:
        client.save_known_projects(
            self.rows("1000000000000000004"), seen=self.rows("1000000000000000018"), now=self.NOW
        )
        # 이제 멤버가 됐다. 같은 프로젝트가 양쪽에 있으면 순회가 두 번 돈다.
        client.save_known_projects(
            self.rows("1000000000000000004", "1000000000000000018"), now=self.NOW
        )
        self.assertEqual(client.load_seen_projects(), ())
        self.assertNotIn("seen", self.saved())

    def test_기준17_thirty_is_the_cap_and_the_later_ones_stay(self) -> None:
        many = self.rows(*[f"20000000000000000{n:02d}" for n in range(40)])
        client.save_known_projects(self.rows("1000000000000000004"), seen=many, now=self.NOW)
        kept = client.load_seen_projects()

        self.assertEqual(len(kept), client.MAX_SEEN_PROJECTS)
        self.assertEqual([p.id for p in kept], [p.id for p in many[:30]])

    def test_기준17_an_empty_seen_clears_the_key(self) -> None:
        client.save_known_projects(
            self.rows("1000000000000000004"), seen=self.rows("1000000000000000018"), now=self.NOW
        )
        client.save_known_projects(self.rows("1000000000000000004"), seen=(), now=self.NOW)
        self.assertEqual(client.load_seen_projects(), ())

    def test_기준17_a_bent_id_never_comes_back_out_of_the_file(self) -> None:
        # 파일에서 읽은 ID도 경로에 들어간다. 모양을 보는 것은 멤버 목록과 같다.
        target = self.home / "state" / "projects.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "fetched_at": self.NOW.isoformat(),
                    "projects": [{"id": "1000000000000000004", "code": "sample"}],
                    "seen": [{"id": "../x", "code": "나쁜것"}, {"id": "18", "code": "괜찮은것"}],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual([p.id for p in client.load_seen_projects()], ["18"])

    def test_기준17_a_broken_file_gives_an_empty_seen(self) -> None:
        target = self.home / "state" / "projects.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        for body in ("{", "[]", json.dumps({"fetched_at": "어제"})):
            with self.subTest(body=body[:20]):
                target.write_text(body, encoding="utf-8")
                self.assertEqual(client.load_seen_projects(), ())

    def test_기준17_save_seen_projects_keeps_the_member_list_and_its_time(self) -> None:
        client.save_known_projects(self.rows("1000000000000000004"), now=self.NOW)
        before = self.saved()["fetched_at"]
        client.save_seen_projects(self.rows("1000000000000000018"))
        after = self.saved()

        self.assertEqual(after["fetched_at"], before, "받은 시각을 고쳐 쓰지 않는다")
        self.assertEqual([row["id"] for row in after["projects"]], ["1000000000000000004"])
        self.assertEqual([p.id for p in client.load_seen_projects()], ["1000000000000000018"])

    def test_기준17_save_seen_projects_does_nothing_without_a_member_list(self) -> None:
        client.save_seen_projects(self.rows("1000000000000000018"))
        self.assertFalse((self.home / "state" / "projects.json").exists())

    def test_기준21_only_the_five_fields_and_the_time_are_stored(self) -> None:
        client.save_known_projects(
            self.rows("1000000000000000004"), seen=self.rows("1000000000000000018"), now=self.NOW
        )
        saved = self.saved()
        self.assertEqual(set(saved), {"fetched_at", "projects", "seen"})
        for row in saved["projects"] + saved["seen"]:
            with self.subTest(row=row):
                self.assertEqual(set(row), {"id", "code", "state", "type", "scope"})

    def test_a_write_failure_is_swallowed(self) -> None:
        with mock.patch.object(config, "write_atomic", side_effect=OSError("못 씀")):
            client.save_known_projects(self.rows("1000000000000000004"), now=self.NOW)
            client.save_seen_projects(self.rows("1000000000000000018"))
        self.assertFalse((self.home / "state" / "projects.json").exists())


class ReaderHousekeepingTest(ReaderCase):

    def test_D23_a_bent_id_never_reaches_the_network(self) -> None:
        reader, http = self.make_reader({})
        calls = (
            lambda: reader.post("20/../../drive/v1/drives"),
            lambda: reader.post("20", project_id="10/../.."),
            lambda: reader.member("me/../../x"),
            lambda: reader.workflows("10?x=1"),
            lambda: reader.tags("10#"),
            lambda: reader.milestones("../10"),
            lambda: reader.member_group("30/..", project_id="10"),
            lambda: reader.posts("10/posts/20/set-done"),
            lambda: reader.find_post_id("10 ", 5),
            lambda: reader.comments("20/x", project_id="10"),
            lambda: reader.new_comments("20", project_id="1/0", since=datetime(2026, 9, 1, tzinfo=KST)),
        )
        for index, call in enumerate(calls):
            with self.subTest(index=index):
                with self.assertRaises(errors.InvalidIdError):
                    call()
        self.assertEqual(http.requests, [])

    def test_D24_new_comments_needs_an_aware_since(self) -> None:
        reader, http = self.make_reader({})
        with self.assertRaises(ValueError):
            reader.new_comments("20", project_id="10", since=datetime(2026, 9, 1))
        self.assertEqual(http.requests, [])

    def test_D22_close_writes_no_files_beyond_the_audit_log(self) -> None:
        reader, _ = self.make_reader(
            {
                ("GET", ME): (200, load_fixture("members_me"), None),
                ("GET", POSTS): (200, load_fixture("posts_list"), None),
            }
        )
        reader.me()
        reader.posts(PID)
        reader.close()

        self.assertFalse((self.home / "cache").exists(), "캐시 폴더가 생겼습니다 (ADR 005)")
        made = self.home_files()
        self.assertTrue(made, "감사 로그는 남아야 한다")
        for name in made:
            self.assertTrue(name.startswith("logs/audit-"), f"뜻밖의 파일: {name}")

    def test_the_reader_exposes_its_call_budget(self) -> None:
        reader, _ = self.make_reader({("GET", ME): (200, load_fixture("members_me"), None)}, call_limit=3)
        self.assertEqual(reader.calls_left, 3)
        reader.me()
        self.assertEqual(reader.calls, 1)
        self.assertEqual(reader.calls_left, 2)

    def test_the_reader_transport_refuses_a_write(self) -> None:
        # DoorayReader를 손에 쥔 002·004가 POST를 시도해도 요청이 나가지 않는다 (§5-2).
        reader, http = self.make_reader({("GET", ME): (200, load_fixture("members_me"), None)})
        with self.assertRaises(WriteNotAllowedError):
            reader._t.request("POST", LOGS, body={"body": {"content": "몰래"}})
        self.assertEqual(len(http.requests), 0)

    def test_the_reader_works_as_a_context_manager(self) -> None:
        http = mock_transport({("GET", ME): (200, load_fixture("members_me"), None)})
        with client.DoorayReader(
            FAKE_TOKEN, BASE, tool="t", http_transport=http, sleep=self.sleep
        ) as reader:
            self.assertEqual(reader.me().id, MY_ID)
        self.assertTrue(reader._t._client.is_closed)
