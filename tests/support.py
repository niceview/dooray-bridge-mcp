"""테스트 공통 장치. 네트워크·키체인·실제 홈 폴더에 닿지 않게 막는다 (spec §11-1·§11-2)."""

from __future__ import annotations

import fnmatch
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

import httpx2

from dooray_core import config

# 저장소에서 유일한 문자열이다. 유출 검사가 이 값을 찾는다 (spec §11-2).
FAKE_TOKEN = "dooray-test-7f3a9c2b1d4e"

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# 코어가 홈 폴더에 남겨도 되는 파일 (완료 기준 74, ADR 005 결정 4).
# `state/projects.json`: 기억해 둔 프로젝트 목록(ID·이름뿐, 24시간. ADR 005 개정).
ALLOWED_HOME_FILES = ("settings.json", "logs/audit-*.jsonl", "state/projects.json")


def load_fixture(name: str) -> Any:
    """`tests/fixtures/`의 JSON 응답을 읽는다. 확장자는 생략할 수 있다."""
    if not name.endswith(".json"):
        name += ".json"
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# --- 테넌트 주소 검사 (005 리뷰 M5) -------------------------------------------------
# 문서·번들에 실제 회사의 두레이 주소가 없는지 본다. B7ⓔ(`test_bundle`)와 D11(`test_docs`)이 같은 판정을 쓴다.
# 모양으로 잡는다: 대소문자 무시, 스킴이 있든 없든(`http`도), 뒤에 포트·경로·문장 끝 점이 붙어도.
_DOORAY_HOST = re.compile(
    r"(?i)(?<![\w.-])(?:[a-z][a-z0-9+.-]*://)?([\w.-]*\.(?:gov-)?dooray\.(?:com|co\.kr))(?![\w-])"
)
# `dooray://` 뒤의 칸(테넌트). 공백이나 `/` 앞까지.
_DOORAY_SCHEME = re.compile(r"(?i)dooray://([^\s/]*)")
# 실제 값의 모양 — 영문·숫자·점·하이픈·밑줄뿐. 이 밖의 글자(`{테넌트}`, `…`, 정규식 `[^/\s)]`)가 있으면 자리표시다.
_PLAIN_AUTHORITY = re.compile(r"[a-z0-9._-]+", re.ASCII)


def tenant_addresses(text: str) -> list[str]:
    """글에 든 두레이 테넌트 주소(자리표시가 아닌 것)의 목록. 비어 있어야 통과다.

    허용: API 호스트 넷(`config.ALLOWED_API_BASES`), 접미만 적은 것(`config.ALLOWED_WEB_SUFFIXES`),
    자리표시 — 접미 앞부분에 ASCII 영문·숫자가 하나도 없는 것(`회사이름.dooray.com`).
    `dooray://X`는 X가 실제 값 모양(영문·숫자·점·하이픈·밑줄뿐)이면 걸린다.
    """
    api_hosts = {base.split("://", 1)[1] for base in config.ALLOWED_API_BASES}
    found: list[str] = []
    for match in _DOORAY_HOST.finditer(text):
        host = match.group(1).lower().rstrip(".")
        if host in api_hosts or host in config.ALLOWED_WEB_SUFFIXES:
            continue
        suffix = next(one for one in config.ALLOWED_WEB_SUFFIXES if host.endswith(one))
        tenant = host[: -len(suffix)]
        if re.search(r"[a-z0-9]", tenant, re.ASCII) is None:
            continue  # 자리표시
        found.append(match.group(0))
    for match in _DOORAY_SCHEME.finditer(text):
        authority = match.group(1).rstrip(")\"'.,;:>]`").lower()
        if _PLAIN_AUTHORITY.fullmatch(authority):
            found.append(match.group(0))
    return found


Route = tuple[str, str]
Reply = tuple[int, Any, Mapping[str, str] | None]


def mock_transport(routes: Mapping[Route, Reply | list[Reply]]) -> httpx2.MockTransport:
    """`(method, path) -> (status, json, headers)` 표로 가짜 전송 계층을 만든다.

    값이 **list면 호출마다 다음 응답**을 주고, 목록이 떨어지면 마지막 것을 반복한다.
    재시도 테스트(T3~T6)와 같은 경로를 두 번 부르는 테스트(D1)가 이 형태를 쓴다.
    list가 아니면 예전처럼 같은 응답을 계속 준다.

    돌려주는 객체의 `requests` 목록에 보낸 요청이 순서대로 쌓인다.
    표에 없는 경로가 오면 즉시 실패시킨다 — 조용히 통과하면 테스트가 거짓으로 녹색이 된다.

    **잠금 하나가 장부 전체를 지킨다** (003 spec §7). 003부터 도구가 서로 독립인 호출을
    스레드로 나눠 보내므로 이 handler가 동시에 불린다. `used`의 "읽고 더하기"가 갈라지면
    같은 경로의 두 스레드가 같은 응답을 받아 테스트가 흔들린다.
    """
    sent: list[httpx2.Request] = []
    used: dict[Route, int] = {}
    ledger = threading.Lock()

    def handler(request: httpx2.Request) -> httpx2.Response:
        key = (request.method.upper(), request.url.path)
        with ledger:
            sent.append(request)
            if key not in routes:
                raise AssertionError(f"예상하지 못한 요청입니다: {key[0]} {key[1]}")
            value = routes[key]
            if isinstance(value, list):
                if not value:
                    raise AssertionError(f"응답 목록이 비어 있습니다: {key[0]} {key[1]}")
                turn = used.get(key, 0)
                used[key] = turn + 1
                status, payload, headers = value[min(turn, len(value) - 1)]
            else:
                status, payload, headers = value
        return httpx2.Response(status, json=payload, headers=dict(headers or {}))

    transport = httpx2.MockTransport(handler)
    transport.requests = sent  # type: ignore[attr-defined]
    return transport


class _RecordCollector(logging.Handler):
    """루트 로거에 붙어 레코드를 **모으기만** 한다 (002 spec §7-1).

    SDK가 예상 밖 예외를 `logger.exception(...)`으로 찍고 `ToolError`의 문장도 로그로 간다
    (mcp-notes §5). 파일과 반환값만 보는 검사로는 그 둘이 잡히지 않는다.
    아무것도 내보내지 않으므로 기존 핸들러의 출력을 바꾸지 않는다.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _Forbidden:
    """호출되면 실패하는 대역. 무엇이 실수로 새어 나갔는지 이름으로 알린다."""

    def __init__(self, what: str) -> None:
        self._what = what
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args)
        raise AssertionError(f"테스트가 {self._what}에 닿으려고 했습니다.")


class CoreTestCase(unittest.TestCase):
    """임시 홈·가짜 토큰·차단된 바깥 세계. 끝날 때 토큰 유출과 남은 파일을 검사한다."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dooray-bridge-test-")
        self.home = Path(self._tmp.name).resolve()
        self.recorded: list[str] = []

        self._env = mock.patch.dict(
            os.environ,
            {"DOORAY_BRIDGE_HOME": str(self.home), "DOORAY_API_TOKEN": FAKE_TOKEN},
        )
        self._env.start()
        for name in [k for k in os.environ if k.startswith("DOORAY_")]:
            if name not in ("DOORAY_BRIDGE_HOME", "DOORAY_API_TOKEN"):
                del os.environ[name]

        # 가로챈 두 채널. stdout은 stdio MCP의 프로토콜 채널이라 **비어 있어야** 한다 (mcp-notes §6).
        self.stderr = io.StringIO()
        self.stdout = io.StringIO()

        self.security = _Forbidden("실제 키체인")
        self._patches = [
            mock.patch.object(Path, "home", _Forbidden("실제 홈 폴더")),
            mock.patch.object(config, "_run_security", self.security),
            mock.patch.object(
                httpx2.HTTPTransport, "handle_request", _Forbidden("진짜 네트워크")
            ),
            mock.patch.object(sys, "stderr", self.stderr),
            mock.patch.object(sys, "stdout", self.stdout),
        ]
        for patch in self._patches:
            patch.start()

        # 루트 로거의 기존 핸들러는 건드리지 않는다 — 하나를 더 붙였다가 tearDown에서 뗀다.
        self._log_handler = _RecordCollector()
        logging.getLogger().addHandler(self._log_handler)

    def tearDown(self) -> None:
        try:
            self._check_home_files()
            self._check_token_not_leaked()
            self._check_stderr_has_no_token()
            self._check_log_records_have_no_token()
            self._check_stdout_is_empty()
            self.assertEqual(
                self.security.calls, [], "기본 키체인 실행기가 호출됐습니다."
            )
        finally:
            # 되돌리는 순서는 붙인 순서의 반대다. 되돌린 **뒤에** unittest가 실패 메시지를 쓰므로
            # 위 검사가 실패해도 보고가 가려지지 않는다.
            logging.getLogger().removeHandler(self._log_handler)
            for patch in reversed(self._patches):
                patch.stop()
            self._env.stop()
            self._tmp.cleanup()

    def record(self, value: Any) -> Any:
        """도구 반환값·예외를 유출 검사 대상으로 모아 둔다. 값을 그대로 돌려준다."""
        self.recorded.append(str(value))
        self.recorded.append(repr(value))
        return value

    def home_files(self) -> list[str]:
        """임시 홈에 생긴 파일의 상대 경로. 폴더는 세지 않는다."""
        return sorted(
            str(path.relative_to(self.home))
            for path in self.home.rglob("*")
            if path.is_file()
        )

    def _check_home_files(self) -> None:
        for name in self.home_files():
            allowed = any(fnmatch.fnmatch(name, p) for p in ALLOWED_HOME_FILES)
            self.assertTrue(allowed, f"홈 폴더에 허용되지 않은 파일이 생겼습니다: {name}")

    def _check_token_not_leaked(self) -> None:
        for path in self.home.rglob("*"):
            if path.is_file():
                blob = path.read_bytes().decode("utf-8", errors="ignore")
                self.assertNotIn(
                    FAKE_TOKEN, blob, f"토큰이 파일에 남았습니다: {path.name}"
                )
        for text in self.recorded:
            self.assertNotIn(FAKE_TOKEN, text, "토큰이 반환값이나 예외 문장에 남았습니다.")

    def _check_stderr_has_no_token(self) -> None:
        self.assertNotIn(FAKE_TOKEN, self.stderr.getvalue(), "토큰이 stderr에 남았습니다.")

    def _check_log_records_have_no_token(self) -> None:
        # `Formatter().format(record)`는 `exc_info`의 트레이스백까지 문자열로 만든다.
        # 표준 트레이스백은 지역 변수를 찍지 않지만(mcp-notes §5) 그것은 가정이다 — 검사로 바꾼다.
        formatter = logging.Formatter()
        for record in self._log_handler.records:
            for text in (record.getMessage(), repr(record.args), formatter.format(record)):
                self.assertNotIn(FAKE_TOKEN, text, "토큰이 로그 레코드에 남았습니다.")

    def _check_stdout_is_empty(self) -> None:
        written = self.stdout.getvalue()
        self.assertEqual(written, "", f"stdout에 출력이 있었습니다: {written[:200]!r}")
