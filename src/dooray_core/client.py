"""전송 계층, 감사 로그, 프로젝트 목록 기억, `DoorayReader` (spec §5·§6).

이 모듈은 **GET만** 보낸다. 쓰기는 다른 모듈에 있고, 이 파일은 그 모듈을 모른다 (§7-2).
자료형과 응답 파서는 `models.py`에 있다 — 의존은 `client → models` 한쪽이다.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import httpx2

from . import config
from .errors import (
    INVALID_ID_MESSAGE,
    NETWORK_MESSAGE,
    WRITE_NOT_ALLOWED_MESSAGE,
    WRITE_PATH_NOT_ALLOWED_MESSAGE,
    ApiError,
    AuthError,
    DoorayError,
    CallLimitError,
    ForbiddenError,
    InvalidIdError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RedirectError,
    ServerError,
    WriteNotAllowedError,
    api_message,
    not_json_message,
)
from .models import (
    Comment,
    FeedComment,
    Member,
    MemberGroup,
    Milestone,
    Page,
    Post,
    Project,
    ProjectMember,
    Tag,
    Workflow,
    parse_comment,
    parse_feed_comment,
    parse_member,
    parse_member_group,
    parse_milestone,
    parse_page,
    parse_post,
    parse_project,
    parse_project_member,
    parse_tag,
    parse_workflow,
    _as_dict,
    _as_list,
    _feed_item,
    _id,
    _text,
    _time,
)

# --- 경로 안전성 -------------------------------------------------------------
#
# 경로는 **우리가** 조립한다. ID 자리에 `/`·`?`·`#`·`..`이 끼어들면 요청이 엉뚱한 API로 간다.
# 002에서 ID는 AI가 채우는 도구 인자이고, 두레이 댓글에 심은 지시문이 AI를 속일 수 있다.
# `post_id="20/set-done?x="`는 검증이 없으면 댓글 등록 대신 업무 완료 처리로 POST를 보낸다.

_PATH_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")
_SAFE_PATH = re.compile(r"(?:/[0-9A-Za-z_-]+)+")
# 경로의 ID 한 칸. `/`가 없다 — 한 칸이 두 칸을 먹어 다른 행으로 넘어가지 못한다 (012 spec §5-3).
_ID = r"[0-9A-Za-z_-]+"
# 무언가를 **만드는** 두 행의 경로. 감사 로그가 만든 항목의 ID를 적을 때도 이것으로 가린다 (012 spec §5-6).
_COMMENT_CREATE = re.compile(rf"/project/v1/projects/{_ID}/posts/{_ID}/logs")
_POST_CREATE = re.compile(rf"/project/v1/projects/{_ID}/posts")
# 쓰기가 허용된 전송 계층이 보낼 수 있는 (동사, 경로, 본문 키) 열 행. 이 표가 전부다 (012 spec §5-1).
# 정규식은 전부 **완전 일치**다 — 접두 일치면 생성 경로에 한 칸을 더한 경로가 통과한다.
# 동사도 행에 있다 — 같은 경로라도 다른 동사는 다른 요청이다(업무 생성과 업무 수정).
_WRITE_PATHS: tuple[tuple[str, re.Pattern[str], frozenset[str]], ...] = (
    # 댓글 등록 (001)
    ("POST", _COMMENT_CREATE, frozenset({"body"})),
    # 업무 생성 (api-notes 010 실측 ①)
    (
        "POST",
        _POST_CREATE,
        frozenset({"parentPostId", "subject", "body", "dueDate", "dueDateFlag", "tagIds"}),
    ),
    # 상위 업무 지정·해제 (api-notes 010 실측 ②③)
    (
        "POST",
        re.compile(rf"/project/v1/projects/{_ID}/posts/{_ID}/set-parent-post"),
        frozenset({"parentPostId"}),
    ),
    # 업무 수정 — 맨 위 키는 보낸 것만 바뀐다. `users` 안쪽은 통째로 바뀐다 (api-notes 012 실측·실측 둘째).
    # `version`·`estimatedDays`·`parentPostId`는 열지 않는다 (012 spec §5-1).
    (
        "PUT",
        re.compile(rf"/project/v1/projects/{_ID}/posts/{_ID}"),
        frozenset(
            {"subject", "body", "users", "dueDate", "dueDateFlag", "milestoneId", "tagIds", "priority"}
        ),
    ),
    # 댓글 수정 (api-notes 012 실측). 파일(`attachFileIds`)은 열지 않는다
    ("PUT", re.compile(rf"/project/v1/projects/{_ID}/posts/{_ID}/logs/{_ID}"), frozenset({"body"})),
    # 업무 전체의 상태 (api-notes 012 실측)
    (
        "POST",
        re.compile(rf"/project/v1/projects/{_ID}/posts/{_ID}/set-workflow"),
        frozenset({"workflowId"}),
    ),
    # 담당자 한 사람의 상태 — `me` 또는 멤버 ID (api-notes 012 실측 둘째·셋째)
    ("PUT", re.compile(rf"/project/v1/projects/{_ID}/posts/{_ID}/to/{_ID}"), frozenset({"workflowId"})),
    # 완료 처리 — 본문이 없다 (api-notes 012 실측). 빈 키 집합 = 본문 None만 받는다
    ("POST", re.compile(rf"/project/v1/projects/{_ID}/posts/{_ID}/set-done"), frozenset()),
    # 다른 프로젝트로 옮기기 (api-notes 022 실측). 하위 업무는 언제나 같이 간다
    (
        "POST",
        re.compile(rf"/project/v1/projects/{_ID}/posts/{_ID}/move"),
        frozenset({"targetProjectId", "includeSubPosts"}),
    ),
    # 프로젝트 설명 (api-notes 023 확인). 이름(`code`)·카테고리는 열지 않는다 — 본문 키가 하나다
    ("PUT", re.compile(rf"/project/v1/projects/{_ID}"), frozenset({"description"})),
)


def path_id(value: object) -> str:
    """경로의 한 칸에 넣어도 되는 ID인지 확인하고 문자열로 돌려준다.

    받은 값을 문장에 되풀이하지 않는다 — 사용자 작성 텍스트일 수 있다.
    """
    text = "" if value is None else str(value)
    if _PATH_ID.fullmatch(text) is None:
        raise InvalidIdError(INVALID_ID_MESSAGE)
    return text


def _check_write(verb: str, path: str, body: Mapping[str, object] | None) -> None:
    """요청을 보내기 전의 마지막 관문. 어기면 요청이 나가지 않는다 (012 spec §5-2).

    규칙: 경로 모양 → GET은 여기까지 → 위 표에서 동사가 같고 경로가 완전 일치하는 행을 찾는다.
    없으면 거절. 그 행의 키 집합이 비었으면 본문이 없어야 하고, 아니면 본문은 비지 않은
    Mapping이고 **최상위 키**가 그 집합 안이어야 한다.
    키 집합만 본다 — 값은 건드리지 않는다. 본문을 로그에 적는 코드와 같은 함수에 두지 않는다.
    동사는 표에만 적는다 — 이 함수가 따로 허락하는 동사가 없다.
    """
    if _SAFE_PATH.fullmatch(path) is None:
        raise InvalidIdError(INVALID_ID_MESSAGE)
    if verb == "GET":
        return
    for allowed_verb, pattern, allowed in _WRITE_PATHS:
        if allowed_verb != verb or pattern.fullmatch(path) is None:
            continue
        if not allowed:
            if body is not None:
                raise WriteNotAllowedError(WRITE_PATH_NOT_ALLOWED_MESSAGE)
            return
        # 키 검사를 지나는 리스트 본문·빈 본문이 없게 (012 리뷰 L7)
        if not isinstance(body, Mapping) or not body or set(body) - allowed:
            raise WriteNotAllowedError(WRITE_PATH_NOT_ALLOWED_MESSAGE)
        return
    raise WriteNotAllowedError(WRITE_PATH_NOT_ALLOWED_MESSAGE)


# --- 감사 로그 (§5-3) --------------------------------------------------------

AUDIT_RETENTION_DAYS = 90

# 한 줄의 필드. **이 목록이 전부다.** 본문·이름·쿼리·헤더·토큰은 들어갈 자리가 없다 (제약 7).
AUDIT_FIELDS: tuple[str, ...] = (
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
    # 응답의 `X-RateLimit-Remaining`. **숫자 하나다** — 본문이 아니다 (제약 7, 003 spec §7-1).
    # 실측에서 한도에 얼마나 가까이 갔는지 보는 유일한 길이다.
    "remaining",
    "comment_id",
    # 상위 업무로 지정한 업무 ID, 떼기면 문자열 `none` (010 spec §4-6). **도구 줄에만** 값이 들어간다 —
    # api 줄에서는 언제나 `null`이다. 전송 계층은 본문을 보지 않는다.
    "parent_id",
    # 고치는 도구가 바꾼 것의 **이름** 목록 (012 spec §5-6). 어휘는 `CHANGE_NAMES`뿐이다 — 값은 적지 않는다.
    # `comment_id`에 겹쳐 넣지 않는다(뜻이 다른 값을 한 칸에 두지 않는다). 도구 줄에만, api 줄은 `null`.
    "changed",
)

# `changed` 칸에 들어갈 수 있는 이름 전부 (012 spec §5-6). 이 밖의 값은 줄을 쓸 때 버린다.
CHANGE_NAMES: tuple[str, ...] = (
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
)

_AUDIT_NAME = re.compile(r"audit-(\d{4}-\d{2}-\d{2})\.jsonl")

# 프로세스당 한 번만 하는 일 둘 (§5-3, 검토 38). 전역인 것이 곧 "프로세스당 한 번"이다.
_audit_swept = False
_audit_warned = False


_AUDIT_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,40}")
_KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def _label(value: object, fallback: str) -> str:
    text = value if isinstance(value, str) else ""
    return text if _AUDIT_LABEL.fullmatch(text) else fallback


def _change_names(value: object) -> list[str] | None:
    """list·tuple이면 `CHANGE_NAMES` 안의 이름만 list로, 그 밖의 타입이면 `None` (012 리뷰 L3)."""
    if not isinstance(value, (list, tuple)):
        return None
    return [name for name in value if isinstance(name, str) and name in CHANGE_NAMES]


def _local_now() -> datetime:
    """오프셋이 붙은 로컬 시각. 쓰는 사람 둘 다 KST에서 로그를 읽는다 (§5-3)."""
    return datetime.now().astimezone()


def _audit_file(moment: datetime) -> Path:
    return config.paths().logs_dir / f"audit-{moment.date().isoformat()}.jsonl"


def _sweep_old_audit_files(folder: Path, moment: datetime) -> None:
    """90일 지난 감사 로그를 지운다. **이 도구가 하는 유일한 삭제다** (§5-3).

    대상을 좁게 잡는다: `logs/` 직속 파일, 이름이 `audit-YYYY-MM-DD.jsonl`에 정확히 맞고,
    날짜를 읽을 수 있고, 그 날짜가 (오늘 − 90일)보다 이른 것. 심볼릭 링크는 따라가지 않고,
    하위 폴더는 들여다보지 않는다. 실패는 조용히 넘어간다.
    """
    global _audit_swept
    if _audit_swept:
        return
    _audit_swept = True

    cutoff = moment.date() - timedelta(days=AUDIT_RETENTION_DAYS)
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        matched = _AUDIT_NAME.fullmatch(entry.name)
        if matched is None:
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        try:
            stamp = datetime.strptime(matched.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue  # 날짜를 못 읽으면 건드리지 않는다
        if stamp >= cutoff:
            continue
        try:
            entry.unlink()
        except OSError:
            continue


def _write_audit_line(
    fields: Mapping[str, object],
    *,
    audit_path: Path | None = None,
    now: datetime | None = None,
) -> None:
    """한 줄 덧붙이기. `write_atomic`을 쓰면 동시 실행에서 줄을 잃는다 (§5-3).

    `AUDIT_FIELDS`로 줄을 다시 짜므로 표에 없는 값은 애초에 파일에 닿지 못한다.
    기록에 실패해도 API 호출을 실패시키지 않는다.
    """
    global _audit_warned
    moment = now or _local_now()
    try:
        if audit_path is None:
            # 홈 폴더를 처음 만드는 주체가 감사 로그일 수 있다. `mkdir(parents=True)`는 부모를
            # 기본 권한으로 만들므로, 홈과 하위 폴더를 0o700으로 만드는 일은 `ensure_home`에 맡긴다.
            path = config.ensure_home().logs_dir / _audit_file(moment).name
        else:
            path = audit_path
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if audit_path is None:
            # 삭제는 **기본 로그 폴더에서만** 한다. 주입된 경로의 폴더는 건드리지 않는다.
            _sweep_old_audit_files(path.parent, moment)
        line = {name: fields.get(name) for name in AUDIT_FIELDS}
        # `tool`과 `error`는 호출부(`mcp_server`)가 주는 문자열이다. 평범한 라벨이 아니면 버린다 —
        # `tool=f"get_thread:{제목}"`이나 `error=str(exc)` 한 줄로 본문이 로그에 들어오지 못하게 한다.
        line["tool"] = _label(line.get("tool"), "unknown")
        if line.get("error") is not None:
            line["error"] = _label(line.get("error"), "Error")
        # `changed`도 호출부가 준다. 어휘 안의 이름만 남긴다 — 제목·이름을 넣어도 파일에 닿지 못한다 (012 spec §5-6).
        line["changed"] = _change_names(line.get("changed"))
        # 만들 때부터 0o600이다. 만든 뒤에 chmod하면 그 사이 잠깐 다른 권한으로 보인다.
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with open(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        if not _audit_warned:
            _audit_warned = True
            print("감사 로그를 기록하지 못했습니다.", file=sys.stderr)


def audit_tool_call(
    tool: str,
    *,
    mode: str,
    ok: bool,
    error: str | None = None,
    project_id: str | None = None,
    post_id: str | None = None,
    calls: int = 0,
    ms: int = 0,
    comment_id: str | None = None,
    parent_id: str | None = None,
    changed: Sequence[str] | None = None,
    audit_path: Path | None = None,
    now: datetime | None = None,
) -> None:
    """도구 호출 한 건을 한 줄로 적는다 (§5-3).

    줄을 만드는 함수는 코어가 주고, **부르는 일은 `mcp_server`가 한다.** 코어는 부르지 않는다.
    본문을 받는 인자가 없다 — 필드 목록이 고정이라 본문이 들어갈 자리가 구조적으로 없다.
    """
    _write_audit_line(
        {
            "kind": "tool",
            "ts": (now or _local_now()).isoformat(),
            "tool": tool,
            "mode": mode,
            "method": None,
            "path": None,
            "project_id": project_id,
            "post_id": post_id,
            "status": None,
            "ok": ok,
            "error": error,
            "ms": ms,
            "attempt": None,
            "calls": calls,
            "remaining": None,  # 도구 한 건에는 응답 헤더가 없다
            "comment_id": comment_id,
            "parent_id": parent_id,
            "changed": changed,
        },
        audit_path=audit_path,
        now=now,
    )


# --- 전송 계층 (§5-1) --------------------------------------------------------

# 요청 **시작** 간의 최소 간격. 실측 한도가 초당 5회(api-notes §1-4)다. 무조건 쉬지 않는다 —
# 응답이 이보다 오래 걸렸으면(중앙값 0.29초, 003 실측) 기다릴 것이 없다.
MIN_INTERVAL = 0.2
DEFAULT_DELAY = MIN_INTERVAL
# 우리 쪽 토큰 버킷의 용량 (003 spec §7-1). 실측 버스트 20의 절반이다 — 이 한도는 토큰당이라
# MCP 서버·봇·동시에 도는 다른 도구 호출이 같이 쓴다. 넘치는 쪽은 `_extra_rest`와 429 백오프가 받는다.
BURST = 10
RATE_LIMIT_FLOOR = 5  # 남은 토큰이 이보다 적으면 더 쉰다 (api-notes §1-4)
DEFAULT_REPLENISH_RATE = 5.0  # 실측: 버스트 20, 초당 5
MAX_EXTRA_REST = 2.0
BASE_BACKOFF = 0.5  # 0.5 → 1 → 2초
MAX_RETRY_AFTER = 30


def _header_int(value: object) -> int | None:
    """헤더 값이 숫자가 아니면 무시한다 (api-notes §1-4)."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _exception_for(status: int) -> ApiError:
    """HTTP 상태 하나를 예외로 바꾼다. 문장은 §2-1 표의 고정 문자열뿐이다."""
    message = api_message(status)
    if status == 401:
        return AuthError(message, status)
    if status == 403:
        return ForbiddenError(message, status)
    if status == 404:
        return NotFoundError(message, status)
    if status == 429:
        return RateLimitError(message, status)
    if 300 <= status < 400:
        return RedirectError(message, status)
    if status >= 500:
        return ServerError(message, status)
    return ApiError(message, status)


def _backoff_seconds(response: httpx2.Response, attempt: int) -> float:
    """`Retry-After`가 있으면 따르고(상한 30초), 없으면 지수 백오프 (§5-1, U-5)."""
    seconds = _header_int(response.headers.get("Retry-After"))
    if seconds is not None and seconds >= 0:
        return float(min(seconds, MAX_RETRY_AFTER))
    return BASE_BACKOFF * (2 ** (attempt - 1))


def _again(failure: DoorayError) -> DoorayError:
    """같은 종류의 오류를 **새 객체로** 만든다. 묶음을 세운 오류를 그대로 다시 던지면
    여러 스레드가 예외 객체 하나(와 그 트레이스백)를 나눠 갖는다."""
    if isinstance(failure, ApiError):
        return type(failure)(failure.message, failure.status)
    return type(failure)(failure.message)


class Pacer:
    """속도 제한의 상태 하나 (003 spec §7-1). **`Transport` 여럿이 나눠 쓸 수 있다.**

    두레이의 한도는 토큰당이다. `Transport`마다 버킷을 따로 두면, 모델이 도구 둘을 동시에 부를 때
    버스트가 곱으로 나간다 — 실측에서 `whoami`와 `list_projects`가 같은 초에 21회를 보내 429를 받았다.
    그래서 MCP 서버는 프로세스에 하나를 두고 모든 Reader·Writer에 넘긴다(`mcp_server.runtime`).
    넘기지 않으면 `Transport`가 자기 것을 만든다(테스트, 단발 스크립트).

    세 가지를 한 잠금 안에서 든다:
    - **토큰 버킷**: 용량 `burst`, `delay`초에 하나 보충, 가득 찬 채로 시작. `delay=0`이면 꺼진다.
    - **멈춤(hold)**: 응답이 "남은 토큰이 적다"고 했거나 429·5xx 백오프가 필요하면 그 시각까지
      **모두가** 기다린다. 스레드마다 같은 시간을 동시에 자는 것으로는 처리량이 줄지 않는다(리뷰 H3).
      멈춤이 끝난 뒤에는 버스트 없이 `delay` 간격으로 나간다.
    - 잠금을 쥔 채로 자지 않는다. 기다릴 시간을 돌려주고, 자는 것은 호출부다.
    """

    def __init__(self, delay: float = DEFAULT_DELAY, burst: int = BURST) -> None:
        self._delay = delay
        self._burst = max(1, burst)
        self._lock = threading.Lock()
        self._next_token: float | None = None  # 다음 토큰이 준비되는 시각
        self._hold_until: float | None = None

    def reserve(self, now: float) -> float:
        """토큰 하나를 가져가고, 나가도 되는 때까지 기다릴 시간을 돌려준다."""
        with self._lock:
            wait = 0.0
            if self._delay > 0:
                ready = now if self._next_token is None else max(self._next_token, now)
                self._next_token = ready + self._delay
                wait = ready - (self._burst - 1) * self._delay - now
            if self._hold_until is not None:
                wait = max(wait, self._hold_until - now)
            # 부동소수점 덧셈이 남기는 나노초 이하는 0이다. 잔여값으로 재우지 않는다.
            return wait if wait > 1e-9 else 0.0

    def hold(self, now: float, seconds: float) -> None:
        """지금부터 `seconds`초 동안 아무도 내보내지 않는다. 더 긴 멈춤이 이미 있으면 그대로 둔다."""
        if seconds <= 0:
            return
        with self._lock:
            until = now + seconds
            if self._hold_until is None or until > self._hold_until:
                self._hold_until = until
            if self._delay > 0:
                # 멈춤 뒤에는 버스트를 쓰지 않는다 — 쌓아 둔 여유를 비운다.
                drained = self._hold_until + (self._burst - 1) * self._delay
                if self._next_token is None or drained > self._next_token:
                    self._next_token = drained

    def note_remaining(self, now: float, remaining: int | None, replenish: int | None) -> None:
        """응답의 남은 토큰 수가 바닥에 가까우면 멈춘다 (api-notes §1-4)."""
        if remaining is None or remaining >= RATE_LIMIT_FLOOR:
            return
        rate = float(replenish or 0) or DEFAULT_REPLENISH_RATE
        self.hold(now, min(MAX_EXTRA_REST, (RATE_LIMIT_FLOOR - remaining) / rate))


class Transport:
    """토큰 부착, 지연, 백오프, 호출 상한, 감사 로그 (§5-1).

    `@dataclass`로 만들지 않는다 — 자동 생성된 `__repr__`이 필드를 찍는다 (§2-2의 5).
    받은 토큰은 곧바로 `httpx2.Client`로 들어가고 `self`에 남지 않는다 (§2-2의 4).

    **스레드에 안전하다** (003 spec §7-1). 잠금 하나가 지키는 것은 넷이다:
    호출 상한 검사와 `calls` 증가(검사와 증가가 갈라지면 여덟 스레드가 한꺼번에 통과한다),
    토큰 버킷, `_remaining`·`_replenish`, 감사 로그 한 줄 쓰기.
    **잠금을 쥔 채로 자지 않고, 잠금을 쥔 채로 요청을 보내지 않는다.**
    """

    def __init__(
        self,
        token: str,
        api_base: str,
        *,
        tool: str,
        allow_write: bool = False,
        call_limit: int = 50,
        delay: float = DEFAULT_DELAY,
        max_retries: int = 3,
        timeout: float = 15.0,
        http_transport: httpx2.BaseTransport | None = None,
        audit_path: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _local_now,
        clock: Callable[[], float] = time.monotonic,
        pacer: Pacer | None = None,
    ) -> None:
        base = config.check_api_base(api_base)  # 허용 밖이면 요청을 한 번도 보내지 않는다
        self._tool = tool
        self._allow_write = allow_write
        self._delay = delay
        self._max_retries = max_retries
        self._audit_path = audit_path
        self._sleep = sleep
        self._now = now
        self._clock = clock
        # 잠금은 재진입하지 않는다 — 잠금 안에서 부르는 것은 잠금을 다시 잡지 않는다.
        self._lock = threading.Lock()
        # 속도 제한은 `Pacer`가 든다. 받지 않았으면 이 전송 계층만의 것을 만든다.
        self._pacer = pacer if pacer is not None else Pacer(delay)
        # 병렬 묶음 안에서 429·5xx·네트워크 오류가 나면 **아직 시작하지 않은** 호출을 보내지 않는다 (리뷰 H1).
        self._batch_depth = 0
        self._halted: DoorayError | None = None
        self.calls = 0
        self.call_limit = call_limit
        self._client = httpx2.Client(
            base_url=base,
            headers={
                "Authorization": f"dooray-api {token}",
                "Accept": "application/json",
            },
            follow_redirects=False,  # 토큰이 다른 호스트로 가는 경로를 없앤다
            trust_env=False,  # 프록시 환경변수가 토큰을 제3의 호스트로 보내지 못하게 한다
            timeout=httpx2.Timeout(timeout, connect=5.0),
            transport=http_transport,
        )

    @property
    def calls_left(self) -> int:
        return max(0, self.call_limit - self.calls)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
        project_id: str | None = None,
        post_id: str | None = None,
    ) -> dict:
        """요청 하나. 병렬 묶음 안에서 '멈춤' 오류로 끝나면 묶음의 나머지를 세운다 (리뷰 H1)."""
        try:
            return self._request(
                method, path, params=params, body=body, project_id=project_id, post_id=post_id
            )
        except (NetworkError, ServerError, RateLimitError) as failure:
            self._halt(failure)
            raise

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
        project_id: str | None = None,
        post_id: str | None = None,
    ) -> dict:
        """요청 한 번. 성공·실패 모두 감사 로그 한 줄을 남긴다 (§5-1)."""
        verb = method.upper()
        try:
            if verb != "GET" and not self._allow_write:
                raise WriteNotAllowedError(WRITE_NOT_ALLOWED_MESSAGE)
            _check_write(verb, path, body)
        except (WriteNotAllowedError, InvalidIdError) as blocked:
            # 요청은 나가지 않았다. 그래도 **시도가 있었다는 사실**은 남긴다 — "AI가 댓글 등록이 아닌
            # 쓰기를 시도했다"는 로그에서 가장 보고 싶은 사건이다. 경로는 사용자 작성 텍스트일 수 있어 적지 않는다.
            self._audit(
                verb if verb in _KNOWN_METHODS else "OTHER",
                None,
                None,
                None,
                status=None,
                ok=False,
                error=type(blocked).__name__,
                ms=0,
                attempt=0,
                calls=self.calls,
            )
            raise

        query = {key: str(value) for key, value in (params or {}).items()}
        attempt = 0
        while True:
            attempt += 1
            # 호출 한 번을 예약한다(상한 검사 + `calls` 증가 + 버킷). 잔다면 **잠금 밖에서** 잔다.
            ordinal = self._reserve()
            wait = self._pacer.reserve(self._clock())
            if wait > 0:
                self._sleep(wait)
            started = time.monotonic()
            response = None
            try:
                response = self._client.request(
                    verb, path, params=query or None, json=body
                )
            except httpx2.HTTPError:
                pass
            if response is None:
                # except 블록 **밖에서** 던진다. `raise ... from None`은 `__context__`를 지우지 않고,
                # 거기 남는 httpx2 예외는 요청 객체를 — 헤더 원본 바이트의 토큰까지 — 들고 있다.
                self._audit(
                    verb,
                    path,
                    project_id,
                    post_id,
                    status=None,
                    ok=False,
                    error="NetworkError",
                    ms=_elapsed_ms(started),
                    attempt=attempt,
                    calls=ordinal,
                )
                raise NetworkError(NETWORK_MESSAGE)

            status = response.status_code
            elapsed = _elapsed_ms(started)
            # 이 응답이 말한 남은 토큰 수. 다른 스레드의 값이 섞이지 않게 **그대로 들고 다닌다.**
            remaining = self._note_limits(response)

            if 200 <= status < 300:
                data: object = None
                try:
                    data = response.json()
                except ValueError:
                    data = None
                if not isinstance(data, dict):
                    self._audit(
                        verb,
                        path,
                        project_id,
                        post_id,
                        status=status,
                        ok=False,
                        error="ApiError",
                        ms=elapsed,
                        attempt=attempt,
                        calls=ordinal,
                        remaining=remaining,
                    )
                    raise ApiError(not_json_message(status), status)
                self._audit(
                    verb,
                    path,
                    project_id,
                    post_id,
                    status=status,
                    ok=True,
                    error=None,
                    ms=elapsed,
                    attempt=attempt,
                    calls=ordinal,
                    remaining=remaining,
                    comment_id=_created_id(verb, path, data),
                )
                return data

            failure = _exception_for(status)
            retryable = status == 429 or status >= 500
            self._audit(
                verb,
                path,
                project_id,
                post_id,
                status=status,
                ok=False,
                error=type(failure).__name__,
                ms=elapsed,
                attempt=attempt,
                calls=ordinal,
                remaining=remaining,
            )
            # 쓰기는 멱등하지 않다. GET만 재시도한다 (§5-1).
            if verb == "GET" and retryable and attempt <= self._max_retries:
                # 백오프는 **모두가** 기다린다 — 워커마다 따로 자면 나머지가 그 사이에 계속 보낸다.
                self._pacer.hold(self._clock(), _backoff_seconds(response, attempt))
                continue
            raise failure

    def close(self) -> None:
        """전송 계층만 닫는다. 파일을 쓰지 않는다 (§6)."""
        self._client.close()

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- 안쪽 ---------------------------------------------------------------

    def _reserve(self) -> int:
        """호출 한 번을 예약하고 **이 요청이 몇 번째인지**를 돌려준다 (003 spec §7-1).

        상한 검사와 `calls` 증가가 한 덩어리다 — 갈라지면 스레드 여덟이 함께 통과해 상한을 넘긴다.
        몇 번째인지도 여기서 정한다 — 감사 로그에 적을 때 `self.calls`를 다시 읽으면 그 사이
        다른 스레드가 올린 값이 찍힌다. 속도 제한은 `Pacer`의 일이다(잠금이 따로다. 겹쳐 잡지 않는다).
        """
        with self._lock:
            if self._halted is not None and self._batch_depth > 0:
                # 같은 묶음의 다른 호출이 429·5xx·네트워크 오류로 끝났다. 아직 시작하지 않은 것은
                # 보내지 않는다 — 두레이가 "천천히"라고 말한 순간에 나머지를 쏟아붓지 않는다 (리뷰 H1).
                raise _again(self._halted)
            if self.calls >= self.call_limit:
                raise CallLimitError(self.call_limit, self.calls)
            self.calls += 1
            return self.calls

    def _note_limits(self, response: httpx2.Response) -> int | None:
        """응답 헤더의 남은 토큰 수를 `Pacer`에 알리고 그 값을 돌려준다."""
        remaining = _header_int(response.headers.get("X-RateLimit-Remaining"))
        replenish = _header_int(response.headers.get("X-RateLimit-Replenish-Rate"))
        self._pacer.note_remaining(self._clock(), remaining, replenish)
        return remaining

    @contextmanager
    def batch(self) -> Iterator[None]:
        """병렬 묶음 하나. 안에서 난 첫 '멈춤' 오류가 아직 시작하지 않은 호출을 막는다 (리뷰 H1).

        묶음이 끝나면 풀린다 — 순차 호출의 뜻은 바뀌지 않는다.
        """
        with self._lock:
            self._batch_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._batch_depth -= 1
                if self._batch_depth <= 0:
                    self._batch_depth = 0
                    self._halted = None

    def _halt(self, failure: DoorayError) -> None:
        with self._lock:
            if self._batch_depth > 0 and self._halted is None:
                self._halted = failure

    def _audit(
        self,
        method: str,
        path: str | None,
        project_id: str | None,
        post_id: str | None,
        *,
        status: int | None,
        ok: bool,
        error: str | None,
        ms: int,
        attempt: int,
        calls: int,
        remaining: int | None = None,
        comment_id: str | None = None,
    ) -> None:
        moment = self._now()
        # 한 줄 쓰기를 잠금으로 감싼다 — 스레드 여덟이 같은 파일에 쓰는 줄이 서로 섞이지 않게 한다.
        # `_write_audit_line`은 자지도 요청을 보내지도 않고 실패를 스스로 삼킨다.
        with self._lock:
            _write_audit_line(
                {
                    "kind": "api",
                    "ts": moment.isoformat(),
                    "tool": self._tool,
                    "mode": "read" if method == "GET" else "write",
                    "method": method,
                    "path": path,  # 쿼리를 뺀 경로다
                    "project_id": project_id,
                    "post_id": post_id,
                    "status": status,
                    "ok": ok,
                    "error": error,
                    "ms": ms,
                    "attempt": attempt,
                    "calls": calls,
                    "remaining": remaining,
                    "comment_id": comment_id,
                    "changed": None,  # 전송 계층은 본문을 보지 않는다 — 무엇이 바뀌었는지 모른다
                },
                audit_path=self._audit_path,
                now=moment,
            )


# --- 동시에 돌리는 도우미 (003 spec §7-2) ------------------------------------

PARALLEL_WORKERS = 4


def run_parallel[T](
    tasks: Sequence[Callable[[], T]], *, workers: int = PARALLEL_WORKERS
) -> list[T | BaseException]:
    """서로 독립인 호출 묶음을 동시에 보낸다 (003 spec §7-2).

    **결과는 입력 순서 그대로**고, 예외는 던지지 않고 **그 자리의 값으로** 온다 —
    호출부가 항목마다 순차 때와 같은 규칙으로 처리한다. 작업이 1개 이하면 스레드를 만들지 않는다.
    `KeyboardInterrupt` 등 `Exception`이 아닌 것은 그대로 올린다.
    돌아올 때 스레드는 전부 거둬져 있다 — 도구는 여전히 동기 함수다 (002 완료 기준 22).
    """
    jobs = list(tasks)
    if len(jobs) <= 1:
        return [_ran(job) for job in jobs]
    pool = ThreadPoolExecutor(
        max_workers=max(1, min(workers, len(jobs))), thread_name_prefix="dooray"
    )
    try:
        futures = [pool.submit(job) for job in jobs]
        return [_gathered(future) for future in futures]
    finally:
        # 정상 종료면 전부 끝나 있다. Ctrl-C로 나가는 길이면 **아직 시작하지 않은 것은 버린다** —
        # 안 그러면 남은 작업(요청 타임아웃 15초 × 재시도)을 전부 기다린다 (리뷰 낮음).
        pool.shutdown(wait=True, cancel_futures=True)


def _ran[T](job: Callable[[], T]) -> T | BaseException:
    try:
        return job()
    except Exception as failure:  # noqa: BLE001 — 값으로 돌려주는 것이 이 함수의 일이다
        return failure


def _gathered[T](future: Future[T]) -> T | BaseException:
    try:
        return future.result()
    except Exception as failure:  # noqa: BLE001 — 같은 규칙. 그 밖의 BaseException은 올라간다
        return failure


def _created_id(verb: str, path: str, data: dict) -> str | None:
    """쓰기가 성공했을 때 만들어진 항목의 ID를 감사 로그에 적는다 (§5-3, 012 spec §5-6).

    **만드는 두 요청에서만** 적는다 — 댓글 등록이면 댓글 ID, 업무 생성이면 새 업무 ID.
    고치기·상태·상위 지정은 응답에 `result.id`가 있어도 `None`이다(무엇의 ID인지 뜻이 달라진다).
    GET은 같은 경로(업무 목록·댓글 목록)를 쓰므로 먼저 뺀다. GET이 아닌 요청이 이 두 경로에 닿았다면
    `_check_write`가 이미 표의 동사로 걸렀다.
    """
    if verb == "GET":
        return None
    if _COMMENT_CREATE.fullmatch(path) is None and _POST_CREATE.fullmatch(path) is None:
        return None
    return _text(_as_dict(data.get("result")).get("id"))


# --- DoorayReader (§6) -------------------------------------------------------

POST_ORDER = "-postUpdatedAt"  # 안 주면 updatedAt 순이 아니다 (api-notes §2-4 실측)
COMMENT_ORDER_OLDEST = "createdAt"
COMMENT_ORDER_NEWEST = "-createdAt"
PAGE_SIZE = 100  # api-notes §1-5의 최댓값

# 활동 피드 (api-notes "추가 실측"). 최근 2주만, 언제나 최신순이고 `cursor`로 잇는다.
STREAMS_PATH = "/common/v1/streams"
FEED_SIZE = 200  # 문서상 최댓값
FEED_EVENT = "event"  # 바깥 항목의 type
FEED_POST = "post"  # 바깥 항목의 type. 이쪽에만 `projectId`가 있다
FEED_COMMENT = "comment"  # 상세 type. 사람이 쓴 댓글이다


def _joined(values: Sequence[object]) -> str | None:
    return ",".join(str(value) for value in values) or None


def _open_range(moment: datetime) -> str:
    """aware datetime을 `{ISO}~` 범위로 만든다 (api-notes §2-4)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("기준 시각은 시간대가 붙은 datetime이어야 합니다.")
    return moment.isoformat() + "~"


# 앞이 열린 범위(`~{ISO}`)는 400이다. 닫힌 범위의 앞을 이 시각으로 채운다 (api-notes §2-4 실측).
_RANGE_FLOOR = "1970-01-01T00:00:00+00:00"


def _updated_range(since: datetime | None, until: datetime | None) -> str | None:
    """`updatedAt` 필터. `until`이 있으면 닫힌 범위 `{앞}~{뒤}`다 (api-notes §2-4 실측)."""
    if until is None:
        return None if since is None else _open_range(since)
    if until.tzinfo is None or until.utcoffset() is None:
        raise ValueError("기준 시각은 시간대가 붙은 datetime이어야 합니다.")
    start = _RANGE_FLOOR if since is None else _open_range(since)[:-1]
    return f"{start}~{until.isoformat()}"


# --- 기억해 둔 프로젝트 목록 (003 spec §2, ADR 005 개정) ----------------------
#
# 프로젝트 목록은 드물게 바뀐다. 호출마다 2회씩 다시 받지 않고 `state/projects.json`에 24시간 둔다.
# **프로젝트의 ID·이름·종류뿐이다** — 업무·댓글·멤버·그룹은 여전히 어디에도 남기지 않는다.
# Reader는 이것을 모른다. 읽고 쓰는 것은 호출부(MCP 서버, 봇)이고 Reader에는 씨앗으로 넣는다.

PROJECTS_FILE = "projects.json"
PROJECTS_MAX_AGE = timedelta(hours=24)
_PROJECT_FIELDS = ("id", "code", "state", "type", "scope")

# 활동이 보인(멤버가 아닌) 프로젝트는 **만료가 없다** (003 spec §6-1). 피드는 2주만 주므로
# 24시간마다 버리면 2주 넘게 조용한 프로젝트의 내 업무를 다시 놓친다. 대신 개수에 상한을 둔다.
MAX_SEEN_PROJECTS = 30


def _project_row(project: Project) -> dict:
    return {name: getattr(project, name) for name in _PROJECT_FIELDS}


def _projects_file() -> dict:
    """`state/projects.json`을 그대로 읽는다. 없거나 깨졌으면 빈 dict."""
    try:
        raw = json.loads((config.paths().state_dir / PROJECTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return _as_dict(raw)


def _seen_rows(rows: Sequence[object], members: frozenset[str]) -> list[dict]:
    """`seen` 목록을 다듬는다: 경로에 못 넣을 ID·멤버 프로젝트·중복을 빼고 상한까지만.

    멤버가 된 프로젝트는 여기서 빠진다 — 같은 프로젝트가 양쪽에 있으면 순회가 두 번 돈다.
    """
    kept: list[dict] = []
    taken: set[str] = set()
    for item in rows:
        project = parse_project(item) if not isinstance(item, Project) else item
        if not _PATH_ID.fullmatch(project.id) or project.id in members or project.id in taken:
            continue
        taken.add(project.id)
        kept.append(_project_row(project))
    # 상한을 넘으면 **나중에 발견한 것**부터 남긴다 (003 spec §6-1). 목록은 그 순서로 온다.
    return kept[:MAX_SEEN_PROJECTS]


def _write_projects_file(body: dict) -> None:
    try:
        config.ensure_home()
        config.write_atomic(
            config.paths().state_dir / PROJECTS_FILE, json.dumps(body, ensure_ascii=False)
        )
    except (OSError, TypeError, ValueError):
        # 적지 못해도 도구는 이미 성공했다 — 성공 감사 로그를 남긴 뒤에 실패로 바뀌면 안 된다 (003 리뷰 L10).
        return


def load_known_projects(*, now: datetime | None = None) -> tuple[Project, ...] | None:
    """24시간 안에 받아 둔 **멤버** 목록. 없거나 낡았거나 읽을 수 없으면 `None`."""
    moment = now or _local_now()
    saved = _projects_file()
    fetched = _time(saved.get("fetched_at"))
    # 미래의 시각(시계가 바뀌었다)도 버린다 — 안 그러면 그만큼 더 오래 산다.
    if fetched is None or not timedelta(0) <= moment - fetched < PROJECTS_MAX_AGE:
        return None
    found = tuple(parse_project(item) for item in _as_list(saved.get("projects")))
    found = tuple(project for project in found if _PATH_ID.fullmatch(project.id))
    return found or None


def load_seen_projects() -> tuple[Project, ...]:
    """활동이 보인 프로젝트. **만료를 보지 않는다** (003 spec §6-1).

    한 번 발견한 것을 24시간 뒤에 버리면, 2주 넘게 조용한 비멤버 프로젝트의 내 업무를
    다시 놓친다 — 빼는 쪽이 유실이다. 깨진 항목과 경로에 못 넣을 ID만 버린다.
    """
    rows = _seen_rows(_as_list(_projects_file().get("seen")), frozenset())
    return tuple(parse_project(row) for row in rows)


def save_known_projects(
    projects: Sequence[Project],
    *,
    seen: Sequence[Project] | None = None,
    now: datetime | None = None,
) -> None:
    """직접 받아 온 멤버 목록을 적어 둔다. 실패해도 조회 결과를 잃지 않는다 — 다음에 다시 받을 뿐이다.

    `seen=None`이면 파일에 있던 `seen`을 **그대로 둔다** — 멤버 목록만 갈아 끼우는 호출이
    발견해 둔 프로젝트를 지우지 않게 한다 (003 spec §6-1).
    """
    moment = now or _local_now()
    current = _projects_file()
    rows = _as_list(current.get("seen")) if seen is None else list(seen)
    kept = _seen_rows(rows, frozenset(project.id for project in projects))
    body: dict = {
        "fetched_at": moment.isoformat(),
        "projects": [_project_row(project) for project in projects],
    }
    if kept:
        body["seen"] = kept
    _write_projects_file(body)


def save_seen_projects(seen: Sequence[Project]) -> None:
    """`seen`만 갈아 끼운다. 멤버 목록과 받은 시각은 건드리지 않는다 (003 spec §6-3의 "덤으로").

    프로젝트 목록을 부르지 않은 도구(`list_my_mentions`)가 쓴다 — 받아 온 적이 없으니
    받은 시각을 고쳐 쓸 자격도 없다. 멤버 목록이 적혀 있지 않으면 아무것도 하지 않는다.
    """
    current = _projects_file()
    members = [parse_project(item) for item in _as_list(current.get("projects"))]
    if not isinstance(current.get("fetched_at"), str) or not members:
        return
    kept = _seen_rows(list(seen), frozenset(project.id for project in members))
    body = {
        "fetched_at": current["fetched_at"],
        "projects": [_project_row(project) for project in members],
    }
    if kept:
        body["seen"] = kept
    _write_projects_file(body)


class DoorayReader:
    """읽기 전용 조회 (§6). 도구 호출 1회마다 새로 만든다.

    `Transport`를 **가진다**(합성). 상속하지 않는다 (§7-2).
    기억은 이 객체가 사는 동안 메모리에만 있다 — 파일에 쓰지 않는다 (ADR 005).
    """

    def __init__(
        self,
        token: str,
        api_base: str,
        *,
        tool: str,
        call_limit: int = 50,
        http_transport: httpx2.BaseTransport | None = None,
        audit_path: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        pacer: Pacer | None = None,
    ) -> None:
        self._t = Transport(
            token,
            api_base,
            tool=tool,
            pacer=pacer,
            allow_write=False,
            call_limit=call_limit,
            http_transport=http_transport,
            audit_path=audit_path,
            sleep=sleep,
            clock=clock,
        )
        self._names: dict[str, str] = {}
        self._projects: tuple[Project, ...] | None = None
        self._projects_seeded = False
        # 멤버가 아닌데 내 업무가 보인 프로젝트. 호출부가 씨앗으로 넣는다 (003 spec §6-2).
        self._seen: tuple[Project, ...] = ()

    @property
    def calls(self) -> int:
        return self._t.calls

    @property
    def calls_left(self) -> int:
        return self._t.calls_left

    @property
    def names(self) -> Mapping[str, str]:
        """응답에서 거둔 멤버 ID→이름 표. 읽기 전용이고 밖에서 채워 넣을 수 없다 (§6)."""
        return MappingProxyType(self._names)

    # --- 멤버 (§2-1) --------------------------------------------------------

    def me(self) -> Member:
        data = self._t.request("GET", "/common/v1/members/me")
        member = parse_member(data.get("result"))
        self._remember(member)
        return member

    def member(self, member_id: str) -> Member:
        """이름표에 이미 있으면 호출하지 않는다 (§6-1)."""
        key = path_id(member_id)
        remembered = self._names.get(key)
        if remembered is not None:
            return Member(id=key, name=remembered)
        data = self._t.request("GET", f"/common/v1/members/{key}")
        member = parse_member(data.get("result"))
        self._remember(member)
        return member

    def find_members_by_name(self, name: str) -> list[Member]:
        """`name=`은 부분 일치가 아니다. 전체 이름을 받아 조회한다 (api-notes §2-1)."""
        data = self._t.request(
            "GET", "/common/v1/members", params={"name": name, "size": PAGE_SIZE}
        )
        members = [parse_member(item) for item in _as_list(data.get("result"))]
        for member in members:
            self._remember(member)
        return members

    # --- 프로젝트 (§2-2·§2-3) -----------------------------------------------

    def projects(self) -> list[Project]:
        """`member=me` 고정. 한 번은 `type` 없이, 한 번은 `type=private`로 (api-notes §2-2 실측).

        **첫 호출에 `type=public`을 보내지 않는다.** 명시하면 `scope`의 기본값(private)이 걸려
        조직 공개(scope=public) 프로젝트가 빠진다 — 실측에서 21개 중 3개가 사라졌다.
        `type`을 생략하면 두 공개 범위가 모두 온다. 개인 프로젝트는 `type=private`로 따로 받는다.
        `member=me`와 `type`을 인자로 열지 않는다 — 빼면 남의 개인 프로젝트가 섞이고,
        쉼표 조합(`private,public`)은 400이다.
        같은 Reader의 두 번째 호출은 0회지만, **새 Reader는 언제나 다시 받는다** (§6-1).
        """
        if self._projects is not None:
            return list(self._projects)

        found: list[Project] = []
        seen: set[str] = set()
        for kind in (None, "private"):
            params: dict[str, object] = {"member": "me", "state": "active"}
            if kind is not None:
                params["type"] = kind
            items, _ = self._paged("/project/v1/projects", params, max_pages=5)
            for item in items:
                project = parse_project(item)
                if project.id and project.id not in seen:
                    seen.add(project.id)
                    found.append(project)
        self._projects = tuple(found)
        return list(found)

    def private_projects(self) -> list[Project]:
        """`type=private&member=me`. 내 개인 프로젝트만 온다 (api-notes §2-2 실측: 1건).

        **인자를 열지 않는다** — `member=me`를 빼면 남의 개인 프로젝트 35건이 온다.
        `projects()`의 캐시를 **읽지도 쓰지도 않는다**: 쓰기는 기억해 둔 목록을 쓰지 않는다 (코어 계약 7③).
        """
        items, _ = self._paged(
            "/project/v1/projects",
            {"member": "me", "type": "private", "state": "active"},
            max_pages=2,
        )
        return [parse_project(item) for item in items]

    def seed_projects(self, projects: Sequence[Project]) -> None:
        """프로젝트 목록을 미리 채운다 — 이후 `projects()`는 요청을 보내지 않는다 (003 spec §2).

        코어는 캐시를 **모른다.** 채우는 구멍만 연다. 누가 얼마나 오래 기억할지는 호출부의 일이고,
        **쓰기는 쓰지 않는다** — 낡은 목록은 동명 프로젝트 판정을 낡게 한다 (코어 계약 7).
        """
        self._projects = tuple(projects)
        self._projects_seeded = True

    def refresh_projects(self) -> list[Project]:
        """씨앗을 버리고 다시 받는다. 찾던 프로젝트가 기억해 둔 목록에 없을 때 호출부가 부른다."""
        self._projects = None
        self._projects_seeded = False
        return self.projects()

    @property
    def loaded_projects(self) -> tuple[Project, ...] | None:
        """이 Reader가 **직접 받아 온** 목록. 아직 안 받았거나 씨앗으로 채워졌으면 `None`."""
        return None if self._projects_seeded else self._projects

    def project(self, project_id: str) -> Project:
        """프로젝트 한 건. **멤버가 아니어도 200이다**(조직 공개 범위. api-notes 맨 끝 실측).

        발견한 프로젝트의 이름을 얻고, 기억해 둔 것이 아직 읽히는지 확인하는 데 쓴다.
        """
        data = self._t.request(
            "GET",
            f"/project/v1/projects/{path_id(project_id)}",
            project_id=_id(project_id),
        )
        return parse_project(data.get("result"))

    def project_description(self, project_id: str) -> str | None:
        """프로젝트 설명 (api-notes 023 확인). `GET …/projects/{id}` 1회. 키가 없거나 문자열이 아니면 None — 모른다."""
        data = self._t.request(
            "GET",
            f"/project/v1/projects/{path_id(project_id)}",
            project_id=_id(project_id),
        )
        description = _as_dict(data.get("result")).get("description")
        return description if isinstance(description, str) else None

    def project_members(self, project_id: str, *, max_pages: int) -> tuple[list[ProjectMember], bool]:
        """프로젝트 멤버의 ID와 역할 (api-notes §2-3). 이름은 없다. `_paged`(size 100). 둘째 값은 "더 있다"."""
        items, info = self._paged(
            f"/project/v1/projects/{path_id(project_id)}/members",
            {},
            max_pages=max_pages,
            project_id=_id(project_id),
        )
        # `totalCount`가 없으면(셀 수 없으면) `_paged`는 첫 쪽에서 멈춘다. 그 쪽이 꽉 찼으면 더 있는지 모른다 — 모를 때 막는다.
        more = info.has_more or (info.total_count < len(items) and len(items) >= PAGE_SIZE)
        return [parse_project_member(item) for item in items], more

    def seed_seen(self, projects: Sequence[Project]) -> None:
        """활동이 보인 프로젝트를 미리 채운다 (003 spec §6-2). 코어는 파일을 모른다."""
        self._seen = tuple(projects)

    @property
    def seen_projects(self) -> tuple[Project, ...]:
        return self._seen

    def all_projects(self) -> list[Project]:
        """멤버 프로젝트 + 활동이 보인 프로젝트. 멤버가 먼저고 ID로 중복을 없앤다.

        `projects()`의 뜻은 바꾸지 않는다 — 그쪽은 여전히 `member=me`가 준 것뿐이다.
        """
        found = self.projects()
        known = {project.id for project in found}
        return found + [
            project for project in self._seen if project.id and project.id not in known
        ]

    def workflows(self, project_id: str) -> list[Workflow]:
        """페이지 파라미터가 없는 엔드포인트다 (api-notes §2-3). 부를 때마다 1회."""
        data = self._t.request(
            "GET", f"/project/v1/projects/{path_id(project_id)}/workflows", project_id=_id(project_id)
        )
        return [parse_workflow(item) for item in _as_list(data.get("result"))]

    def tags(self, project_id: str) -> list[Tag]:
        items, _ = self._paged(
            f"/project/v1/projects/{path_id(project_id)}/tags", {}, max_pages=5, project_id=_id(project_id)
        )
        return [parse_tag(item) for item in items]

    def milestones(self, project_id: str) -> list[Milestone]:
        """근거 없는 파라미터를 보내지 않는다 (api-notes §2-3, U-3)."""
        data = self._t.request(
            "GET", f"/project/v1/projects/{path_id(project_id)}/milestones", project_id=_id(project_id)
        )
        return [parse_milestone(item) for item in _as_list(data.get("result"))]

    def member_group(self, group_id: str, *, project_id: str) -> MemberGroup:
        """멘션에서 얻은 그룹 ID로 바로 단건 조회한다 (api-notes §2-7 실측).

        멘션된 그룹이 다른 프로젝트 소속이어도 현재 프로젝트 경로로 200이 온다.
        오래된 구성은 그룹 멘션을 영구히 놓치게 하므로 **부를 때마다 조회한다** (ADR 005).
        """
        data = self._t.request(
            "GET",
            f"/project/v1/projects/{path_id(project_id)}/member-groups/{path_id(group_id)}",
            project_id=_id(project_id),
        )
        group = parse_member_group(data.get("result"))
        for member in group.members:
            self._remember(member)
        return group

    # --- 업무 (§2-4·§2-5) ---------------------------------------------------

    def posts(
        self,
        project_id: str,
        *,
        updated_since: datetime | None = None,
        updated_until: datetime | None = None,
        created_range: str | None = None,
        due_range: str | None = None,
        to_member_ids: Sequence[str] = (),
        from_member_ids: Sequence[str] = (),
        cc_member_ids: Sequence[str] = (),
        to_member_size: int | None = None,
        workflow_classes: Sequence[str] = (),
        workflow_ids: Sequence[str] = (),
        tag_ids: Sequence[str] = (),
        milestone_ids: Sequence[str] = (),
        parent_post_id: str | None = None,
        subjects: str | None = None,
        page: int = 0,
        size: int = PAGE_SIZE,
        max_pages: int = 1,
    ) -> tuple[list[Post], Page]:
        """`order`는 인자가 아니다. `-postUpdatedAt`으로 고정한다 (§6-1)."""
        params: dict[str, object] = {"order": POST_ORDER}
        optional: dict[str, object | None] = {
            "updatedAt": _updated_range(updated_since, updated_until),
            "createdAt": created_range,
            "dueAt": due_range,
            "toMemberIds": _joined(to_member_ids),
            "fromMemberIds": _joined(from_member_ids),
            "ccMemberIds": _joined(cc_member_ids),
            "toMemberSize": None if to_member_size is None else int(to_member_size),
            "postWorkflowClasses": _joined(workflow_classes),
            "postWorkflowIds": _joined(workflow_ids),
            "tagIds": _joined(tag_ids),
            "milestoneIds": _joined(milestone_ids),
            "parentPostId": parent_post_id,
            "subjects": subjects,
        }
        params.update({key: value for key, value in optional.items() if value is not None})

        items, info = self._paged(
            f"/project/v1/projects/{path_id(project_id)}/posts",
            params,
            page=page,
            size=size,
            max_pages=max_pages,
            project_id=_id(project_id),
        )
        posts = [parse_post(item) for item in items]
        for post in posts:
            self._remember_post(post)
        return posts, info

    def find_post_id(self, project_id: str, number: int) -> str | None:
        """업무 번호 → postId (api-notes §2-4 실측). 1회."""
        data = self._t.request(
            "GET",
            f"/project/v1/projects/{path_id(project_id)}/posts",
            params={"postNumber": number, "order": POST_ORDER},
            project_id=_id(project_id),
        )
        items = _as_list(data.get("result"))
        if not items:
            return None
        return _text(_as_dict(items[0]).get("id"))

    def post(self, post_id: str, *, project_id: str | None = None) -> Post:
        """`project_id`가 없으면 프로젝트 없는 상세 엔드포인트를 쓴다 (api-notes §2-5)."""
        if project_id:
            path = f"/project/v1/projects/{path_id(project_id)}/posts/{path_id(post_id)}"
        else:
            path = f"/project/v1/posts/{path_id(post_id)}"
        data = self._t.request(
            "GET", path, project_id=_id(project_id) or None, post_id=_id(post_id)
        )
        post = parse_post(data.get("result"))
        self._remember_post(post)
        return post

    # --- 댓글 (§2-6) --------------------------------------------------------

    def comments(
        self,
        post_id: str,
        *,
        project_id: str,
        max_pages: int = 5,
        include_events: bool = False,
    ) -> list[Comment]:
        """오래된 순으로 받는다. 시스템 로그는 기본 제외다 (api-notes §2-6 실측)."""
        items, _ = self._paged(
            f"/project/v1/projects/{path_id(project_id)}/posts/{path_id(post_id)}/logs",
            {"order": COMMENT_ORDER_OLDEST},
            max_pages=max_pages,
            project_id=_id(project_id),
            post_id=_id(post_id),
        )
        comments = [parse_comment(item) for item in items]
        if include_events:
            return comments
        return [comment for comment in comments if comment.type == "comment"]

    def comment(self, post_id: str, comment_id: str, *, project_id: str) -> Comment:
        """댓글 하나 (api-notes 012 실측 둘째: 단건 조회). 1회.

        `files`는 읽지 않는다 — 목록과 같은 파서를 쓴다. 시스템 기록·메일 댓글을 거르지 않는다 —
        `type`·`subtype`을 보고 판단하는 것은 호출부다.
        """
        data = self._t.request(
            "GET",
            f"/project/v1/projects/{path_id(project_id)}/posts/{path_id(post_id)}/logs/{path_id(comment_id)}",
            project_id=_id(project_id),
            post_id=_id(post_id),
        )
        return parse_comment(data.get("result"))

    def new_comments(
        self,
        post_id: str,
        *,
        project_id: str,
        since: datetime,
        max_pages: int = 3,
    ) -> list[Comment]:
        """최신부터 읽다가 기준 시각보다 오래된 것을 만나면 그 자리에서 멈춘다 (§6-1).

        엔드포인트에 시각 필터가 없어서 이렇게 한다 (api-notes §2-6).
        시각을 못 읽은 항목은 판단할 수 없으므로 멈추지 않고 그대로 싣는다.
        결과는 오래된 순으로 뒤집어 돌려준다.
        """
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("기준 시각은 시간대가 붙은 datetime이어야 합니다.")
        path = f"/project/v1/projects/{path_id(project_id)}/posts/{path_id(post_id)}/logs"
        fresh: list[Comment] = []
        page = 0
        while True:
            data = self._t.request(
                "GET",
                path,
                params={"order": COMMENT_ORDER_NEWEST, "page": page, "size": PAGE_SIZE},
                project_id=_id(project_id),
                post_id=_id(post_id),
            )
            reached_old = False
            for item in _as_list(data.get("result")):
                comment = parse_comment(item)
                if comment.created_at is not None and comment.created_at < since:
                    reached_old = True
                    break
                fresh.append(comment)
            info = parse_page(data, page=page, size=PAGE_SIZE)
            if reached_old or not info.has_more or page + 1 >= max_pages:
                break
            page += 1
        fresh.reverse()
        return fresh

    # --- 활동 피드 (003 spec §5-1, api-notes "추가 실측") --------------------

    def feed_comments(
        self, *, since: datetime, max_pages: int = 3, size: int = FEED_SIZE
    ) -> tuple[list[FeedComment], bool]:
        """내 활동 피드에서 **댓글만** 모은다. 프로젝트 목록도 대상 설정도 필요 없다.

        돌려주는 두 번째 값은 **`since`까지 닿았는가**다. 빈 쪽·`cursor` 없음·기준보다 오래된
        항목 가운데 하나를 만나면 참, `max_pages`에 막혔으면 거짓이다 — 거짓이면 더 오래된
        멘션이 남아 있다는 뜻이라 호출부가 드러내야 한다.
        **응답 건수가 `size`보다 적어도 마지막 쪽이 아니다** (api-notes "추가 실측").

        멈춤 판정은 바깥 `type`이 `event`인 항목으로만 한다. `post` 항목의 `createdAt`은
        업무를 만든 시각이라 이 줄의 최신순을 따르지 않는다(fixture에서 중간에 끼어 있다).
        시각을 못 읽은 항목은 싣되 멈추게 하지 않는다 — 멘션을 잃는 쪽이 더 나쁘다.
        """
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("기준 시각은 시간대가 붙은 datetime이어야 합니다.")
        found: list[FeedComment] = []
        before: str | None = None
        for _ in range(max(1, max_pages)):
            params: dict[str, object] = {"size": size}
            if before is not None:
                params["before"] = before
            data = self._t.request("GET", STREAMS_PATH, params=params)
            items = _as_list(data.get("result"))
            cursor = _text(data.get("cursor"))
            reached = False
            for item in items:
                kind, inner = _feed_item(item)
                if kind != FEED_EVENT:
                    continue  # post·mail·schedule·page 등은 이 도구의 대상이 아니다
                moment = _time(inner.get("createdAt"))
                if moment is not None and moment < since:
                    # 여기서부터는 기준 시각보다 오래됐다 — 다음 쪽은 부르지 않는다. 다만 이 쪽은 끝까지
                    # 본다: 실측에서 1초짜리 순서 역전이 있었다(178건 중 1건. api-notes). 멈추면 그 뒤에
                    # 끼어 있는 기준 안의 댓글을 잃는다.
                    reached = True
                    continue
                comment = parse_feed_comment(inner)
                if comment.kind != FEED_COMMENT:
                    continue  # 시스템 이벤트 (api-notes "추가 실측")
                if comment.author_id:
                    self._remember(Member(id=comment.author_id, name=comment.author_name))
                found.append(comment)
            if reached or not items or cursor is None:
                return found, True
            before = cursor
        return found, False

    def feed_activity(self, *, max_pages: int = 3) -> tuple[list[str], list[str]]:
        """피드에서 (`post`의 `projectId`, `event`의 `postId`)를 최신순·중복 없이 거둔다.

        `since`가 없다 — 피드가 주는 2주 전부가 대상이다 (003 spec §6-2). 본문은 읽지 않는다.
        `event` 항목은 시스템 이벤트도 싣는다: 그 업무도 비멤버 프로젝트의 단서이고,
        빼는 쪽이 유실이다.
        """
        project_ids: list[str] = []
        post_ids: list[str] = []
        seen_projects: set[str] = set()
        seen_posts: set[str] = set()
        before: str | None = None
        for _ in range(max(1, max_pages)):
            params: dict[str, object] = {"size": FEED_SIZE}
            if before is not None:
                params["before"] = before
            data = self._t.request("GET", STREAMS_PATH, params=params)
            items = _as_list(data.get("result"))
            cursor = _text(data.get("cursor"))
            for item in items:
                kind, inner = _feed_item(item)
                if kind == FEED_POST:
                    found = _id(inner.get("projectId"))
                    if found and found not in seen_projects:
                        seen_projects.add(found)
                        project_ids.append(found)
                elif kind == FEED_EVENT:
                    found = _id(inner.get("postId"))
                    if found and found not in seen_posts:
                        seen_posts.add(found)
                        post_ids.append(found)
            if not items or cursor is None:
                break
            before = cursor
        return project_ids, post_ids

    # --- 살림 ---------------------------------------------------------------

    def batch(self):
        """병렬 묶음 하나를 연다 (`Transport.batch`). `run_parallel`은 언제나 이 안에서 부른다."""
        return self._t.batch()

    def close(self) -> None:
        """전송 계층만 닫는다. 파일을 쓰지 않는다 (§6)."""
        self._t.close()

    def __enter__(self) -> DoorayReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- 안쪽 ---------------------------------------------------------------

    def _paged(
        self,
        path: str,
        params: Mapping[str, object],
        *,
        page: int = 0,
        size: int = PAGE_SIZE,
        max_pages: int = 1,
        project_id: str | None = None,
        post_id: str | None = None,
    ) -> tuple[list[dict], Page]:
        items: list[dict] = []
        current = page
        walked = 0
        info = Page(total_count=0, page=current, has_more=False)
        while True:
            query = dict(params)
            query["page"] = current
            query["size"] = size
            data = self._t.request(
                "GET", path, params=query, project_id=project_id, post_id=post_id
            )
            items.extend(_as_list(data.get("result")))
            info = parse_page(data, page=current, size=size)
            walked += 1
            if not info.has_more or walked >= max_pages:
                break
            current += 1
        return items, info

    def _remember(self, member: Member | None) -> None:
        if member is not None and member.id and member.name:
            self._names[member.id] = member.name

    def _remember_post(self, post: Post) -> None:
        """`users.*.member.name`을 거둔다 (api-notes §2-1). 댓글 작성자 이름이 여기서 채워진다."""
        self._remember(post.author)
        for assignee in post.assignees:
            self._remember(assignee.member)
        for member in post.cc:
            self._remember(member)
