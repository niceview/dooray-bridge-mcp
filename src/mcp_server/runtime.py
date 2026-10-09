"""상한 · Reader 만들기 · 도구 단위 감사 로그 · ToolError 변환 · 인자 검증 (spec §3).

도구가 되풀이하는 것을 여기 모은다. `tool_call` 하나가 감사 로그 한 줄과 예외 변환을 맡는다.
Reader를 만드는 자리는 `_open_reader` 하나다 (§3-6) — 테스트는 그 한 곳만 갈아 끼운다.

여기서 밖으로 나가는 것은 두레이 GET 요청과 감사 로그 파일뿐이다 (§1의 표).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from mcp.server.mcpserver.exceptions import ToolError

from dooray_core import config, render
from dooray_core.client import (
    DoorayReader,
    Pacer,
    audit_tool_call,
    load_known_projects,
    load_seen_projects,
    save_known_projects,
    save_seen_projects,
)
from dooray_core.models import Project
from dooray_core.errors import DoorayError

# --- 상한 (§3-1) -------------------------------------------------------------
#
# 전부 모듈 상수다. 인자로 열지 않는다 (§14의 35).

TOOL_CALL_LIMIT = 50  # 제약 8. 코어 기본값과 같지만 값을 소스에 둔다
OUTPUT_LIMIT = 8000  # 도구 반환 한 건의 글자 수
BODY_LIMIT = 2000  # 본문·댓글 한 건 (코어 render의 기본값)
LIST_SIZE_DEFAULT = 20
LIST_SIZE_MAX = 50
PAGE_MAX = 50  # 50페이지 × 50건이면 다른 도구를 써야 한다
MAX_PROJECTS_SCANNED = 30  # 프로젝트를 안 받은 조회가 도는 최대 프로젝트 수
MAX_PROJECT_FILTER = 50  # settings.projects의 항목 수 상한 (§3-7)
MAX_NAME_LOOKUPS = 5  # 댓글 작성자 이름 단건 조회
MAX_COMMENT_PAGES = 3  # 댓글 300건
MAX_THREADS_IN_DIGEST = 15
MAX_WORKLOAD_PAGES = 10  # 1,000건
MAX_LIST_ROWS = 10  # workload_summary가 뽑아 보여 주는 업무 줄 수
CONTENT_LIMIT = 4000  # post_comment의 본문과 create_my_post의 body. 두레이의 실제 상한은 미확인이다 (T-12)
STYLE_LIMIT = 500  # set_reply_style
SUBJECT_LIMIT = 300  # create_my_post의 한 줄 제목. 두레이의 실제 상한은 미확인이다 (010 spec §2-5)
MAX_TAGS = 10  # 사용자 텍스트에서 나온 값으로 루프를 돌 때는 개수 상한을 둔다 (001 리뷰 H1)
MAX_PEOPLE = 10  # set_post_people의 사람 목록 인자 하나. 이름마다 조회가 하나 나간다 (012 spec §2-8)

# 인자 문자열의 길이 상한 (§3-2의 표).
PROJECT_ARG_LIMIT = 200
NAME_ARG_LIMIT = 100
SINCE_ARG_LIMIT = 40  # ISO 8601 한 개. create_my_post의 due도 같은 잣대다
POST_ARG_LIMIT = 200
TAGS_ARG_LIMIT = 200  # 쉼표로 이은 태그 이름 전체. 쪼갠 뒤 `MAX_TAGS`개까지다. 사람 목록도 같은 잣대다
CHOICE_ARG_LIMIT = 20  # 정해진 값 가운데 하나를 고르는 인자(set_post_fields의 priority)
# `settings.projects` 항목 하나의 길이. 설정 파일도 사람이 편집하는 텍스트다 (§14의 49).
PROJECT_VALUE_LIMIT = 200
# 경고 줄에 값을 몇 개까지 적는가 (§3-7).
WARN_VALUES_SHOWN = 3

# 쓰기 도구가 등록됐는가. `build_server()`가 등록 시점에 한 번 정하고 `whoami`가 읽는다 (§6-1).
# 모듈 전역인 것이 곧 "프로세스당 한 번"이라는 뜻이다 (§14의 48).
WRITE_TOOLS_REGISTERED = False

# 부를 때마다 사람이 허락해야 실행된다 — 클라이언트의 모드·허용 규칙과 무관하게 (ADR 009).
# 값은 JSON 불리언 true여야 한다. 다른 값은 클라이언트가 무시한다.
# 두레이 쓰기 도구 아홉과 `set_reply_style`의 등록에 건다. 끄는 스위치를 두지 않는다 (005 spec §3-4).
ASK_EVERY_CALL = {"anthropic/requiresUserInteraction": True}


# --- 인자 검증 문장 (§3-5) ---------------------------------------------------
#
# ToolError에 들어가는 문장은 `DoorayError.message`와 **여기 적힌 고정 문장**뿐이다.
# 받은 값을 문장에 되풀이하지 않는다 — 사용자 작성 텍스트일 수 있다.

INT_RANGE_MESSAGE = "{name}은(는) {low}에서 {high} 사이의 정수여야 합니다."
TEXT_TOO_LONG_MESSAGE = "{name}이(가) 너무 깁니다. {limit}자 이하로 줄여 주세요."
TEXT_REQUIRED_MESSAGE = "{name}이(가) 비어 있습니다. 내용을 채워 주세요."
TEXT_BLANK_MESSAGE = "{name}이(가) 공백뿐입니다. 값을 주거나 인자를 빼 주세요."
SINCE_FORMAT_MESSAGE = (
    "기간 형식이 올바르지 않습니다. today, thisweek, prev-7d 같은 형태이거나 "
    "시간대가 붙은 ISO 8601 시각(2026-09-13T00:00:00+09:00)이어야 합니다."
)
SINCE_NAIVE_MESSAGE = (
    "기간에 시간대가 없습니다. 2026-09-13T00:00:00+09:00처럼 시간대를 붙여 주세요."
)
SINCE_TOO_LONG_MESSAGE = "기간이 너무 깁니다. {days}일 이내로 좁혀 주세요."

# --- `settings.projects` 경고 문장 (§3-7) ------------------------------------
#
# `⚠ `는 `view.warn`이 붙인다. 여기서는 문장만 만든다.

PROJECTS_NOT_FOUND_MESSAGE = (
    "settings.json의 projects에 적힌 {values}를 내 프로젝트 목록에서 찾지 못했습니다. "
    "이름(code) 또는 프로젝트 ID로 적어 주세요."
)
PROJECTS_ALL_MISSING_MESSAGE = (
    "settings.json의 projects가 가리키는 프로젝트를 하나도 찾지 못했습니다. "
    "설정을 무시하고 내 프로젝트 전체를 봤습니다."
)
PROJECTS_TOO_MANY_MESSAGE = (
    "settings.json의 projects 항목이 {limit}개를 넘어 앞의 {limit}개만 사용했습니다."
)
PROJECTS_VALUE_TOO_LONG_MESSAGE = (
    "settings.json의 projects에 너무 긴 항목이 있어 무시했습니다(한 항목은 {limit}자까지)."
)


# --- Reader (§3-6) -----------------------------------------------------------

# 속도 제한은 **프로세스에 하나**다 (003 spec §7-1). 두레이의 한도는 토큰당인데 Reader는 도구 호출마다
# 새로 만든다 — 버킷을 Reader마다 두면 모델이 도구 둘을 동시에 부를 때 버스트가 곱으로 나간다.
# 실측: `whoami`와 `list_projects`가 같은 초에 21회를 보내 429를 받았다.
# 모듈 전역인 것이 곧 그 뜻이다. 상태는 "언제 내보내도 되는가"뿐이라 테스트 사이로 새도 결과를 바꾸지 않는다
# (테스트는 `_open_reader`를 갈아 끼우므로 이것을 쓰지 않는다).
PACER = Pacer()


def _open_reader(tool: str) -> DoorayReader:
    """Reader를 만드는 **유일한 자리**다 (§3-6, 코어 계약 7).

    도구 호출 1회마다 새로 만든다. 오래 산 Reader는 프로젝트 목록이 낡아
    새 프로젝트의 멘션을 영구히 잃는다. 동기 도구가 여러 워커 스레드에서
    동시에 도는 것(mcp-notes §4)도 이 규칙이 함께 해결한다.

    테스트는 이 함수 하나를 갈아 끼운다. 제품 코드에 테스트용 인자를 남기지 않는다.
    """
    settings = config.load_settings()
    return DoorayReader(
        config.read_token(),
        settings.api_base,
        tool=tool,
        call_limit=TOOL_CALL_LIMIT,
        pacer=PACER,
    )


# --- 기억해 둔 프로젝트 목록 (003 spec §2, ADR 005 개정) ----------------------
#
# 코어가 `state/projects.json`에 24시간 둔다 — 봇도 같은 파일을 쓴다. 도구마다 프로젝트 목록 2회를
# 다시 받던 것을 없앤다. 낡음의 비용: 새로 들어간 프로젝트가 최대 24시간 늦게 보인다. 그래서
#   - 찾는 프로젝트가 기억해 둔 목록에 없으면 **한 번 새로 받아** 다시 찾는다(`tools._projects_matching`),
#   - `whoami`·`list_projects`는 언제나 새로 받아 기억을 갈아 끼운다("목록 새로 받아 줘"의 길),
#   - 쓰기(`post_comment`)는 기억을 읽지 않는다.


def _cached_projects() -> tuple[Project, ...] | None:
    return load_known_projects(now=_now())


def _cached_seen() -> tuple[Project, ...]:
    """멤버가 아닌데 내 업무가 보인 프로젝트 (003 spec §6). **만료가 없다.**

    피드는 2주만 준다 — 24시간마다 버리면 조용한 비멤버 프로젝트의 내 업무를 다시 놓친다.
    """
    return load_seen_projects()


# --- 도구 단위 감사 로그 (§3-4) ----------------------------------------------


class ToolCall:
    """도구 호출 한 건. 감사 로그 한 줄에 들어갈 값을 모은다 (§3-4).

    `project_id`·`post_id`·`comment_id`·`parent_id`·`changed`는 도구 본문 **도중에** 정해지므로 밖에서 채운다.
    """

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.project_id: str | None = None
        self.post_id: str | None = None
        self.comment_id: str | None = None
        # 상위로 지정한 업무 ID, 떼기면 문자열 `none` (010 spec §4-6). 도구 줄에만 들어간다.
        self.parent_id: str | None = None
        # 고치는 도구가 바꾼 것의 이름(`client.CHANGE_NAMES` 안). 쓰기가 성공한 바로 뒤에 채운다 (012 spec §5-6).
        self.changed: tuple[str, ...] | None = None
        # 두레이가 쓰기를 거절해(400) 도구가 **문장을 반환**했을 때의 예외 이름. 도구 줄의 `ok`가
        # 거짓이 되게 한다 — 안 그러면 거절이 성공으로 센다 (010 리뷰 M1).
        self.refused: str | None = None
        # 이 호출이 새로 정한 `seen`. `None`이면 파일에 있던 것을 건드리지 않는다 (003 spec §6-3).
        self.discovered: tuple[Project, ...] | None = None
        # 조회가 403·404를 준 `seen` 프로젝트. 성공한 호출의 끝에서 기억에서 뺀다 (003 spec §6-1).
        self.dropped_seen: set[str] = set()
        self._counted: list[object] = []
        self._opened: list[DoorayReader] = []

    def reader(self, *, cached: bool = True) -> DoorayReader:
        """만들고, 셈에 등록하고, 빠져나갈 때 닫는다.

        `cached=True`면 살아 있는 프로젝트 목록을 씨앗으로 넣는다 — Reader는 여전히 새것이다.
        진단(`whoami`)·목록 자체가 답인 도구·멘션 판정·쓰기는 `cached=False`로 부른다 (003 spec §2).
        활동이 보인 프로젝트는 **언제나** 씨앗으로 넣는다 — 만료가 없으니 새로 받을 것도 없다.
        """
        opened = _open_reader(self.tool)
        if cached:
            known = _cached_projects()
            if known is not None:
                opened.seed_projects(known)
        opened.seed_seen(_cached_seen())
        self._opened.append(opened)
        self._counted.append(opened)
        return opened

    def count(self, counted: object) -> None:
        """`.calls`를 가진 것을 셈에 더한다. 쓰기 쪽이 쓴다 (C5)."""
        self._counted.append(counted)

    @property
    def calls(self) -> int:
        total = 0
        for item in self._counted:
            used = getattr(item, "calls", 0)
            if isinstance(used, int):
                total += used
        return total

    def _remember(self) -> None:
        """성공한 호출에서 **직접 받아 온** 목록만 기억한다. 씨앗으로 돈 호출은 만료를 늘리지 않는다.

        `discovered`가 있으면 `seen`도 함께 갈아 끼운다. 프로젝트 목록을 부르지 않은 도구
        (`list_my_mentions`의 덤)는 `seen`만 적는다 — 받은 시각은 그대로다 (003 spec §6-3).
        """
        seen = self.discovered
        if self.dropped_seen:
            kept = seen if seen is not None else load_seen_projects()
            seen = tuple(one for one in kept if one.id not in self.dropped_seen)
        wrote = False
        for opened in self._opened:
            loaded = opened.loaded_projects
            if loaded is not None:
                save_known_projects(loaded, seen=seen, now=_now())
                wrote = True
        if not wrote and seen is not None:
            save_seen_projects(seen)

    def _close(self) -> None:
        for opened in self._opened:
            try:
                opened.close()
            except Exception:
                # 닫기에 실패해도 도구의 결과나 감사 로그를 잃지 않는다.
                continue


@contextmanager
def tool_call(tool: str, *, mode: str = "read") -> Iterator[ToolCall]:
    """성공·실패 모두 감사 로그를 **정확히 한 줄** 남긴다 (§3-4).

    `DoorayError`는 `ToolError(exc.message)`가 되고(§3-5), 그 밖의 예외는 그대로 다시 던진다 —
    SDK가 메시지를 가린다(mcp-notes §5). `tool`과 `error`에는 리터럴 상수와 예외 클래스 이름만 들어간다.
    """
    call = ToolCall(tool)
    started = time.monotonic()
    message: str | None = None
    try:
        yield call
    except DoorayError as failure:
        _audit(call, mode, ok=False, error=type(failure).__name__, started=started)
        message = failure.message
        # 사슬을 여기서 끊는다. `@contextmanager`는 `__exit__`가 예외를 처리하는 **도중에** 이 제너레이터를
        # 재개하므로, 아래의 raise가 `except` 밖에 있어도 새 예외의 `__context__`에 이 예외가 붙는다
        # (002 리뷰 M1에서 실측). 붙는 것 자체는 막을 수 없으니 붙는 쪽을 비운다 — 이 예외가 들고 있던
        # 원인 예외(요청 객체를 쥔 것일 수 있다)와 트레이스백을 떼어 낸다. 문장은 고정 상수라 남아도 된다.
        failure.__cause__ = None
        failure.__context__ = None
        failure.__suppress_context__ = True
        failure.__traceback__ = None
    except BaseException as failure:
        _audit(call, mode, ok=False, error=type(failure).__name__, started=started)
        raise
    else:
        _audit(call, mode, ok=call.refused is None, error=call.refused, started=started)
        call._remember()
    finally:
        call._close()
    if message is not None:
        raise ToolError(message)


def _audit(
    call: ToolCall, mode: str, *, ok: bool, error: str | None, started: float
) -> None:
    audit_tool_call(
        call.tool,
        mode=mode,
        ok=ok,
        error=error,
        project_id=call.project_id,
        post_id=call.post_id,
        calls=call.calls,
        ms=int((time.monotonic() - started) * 1000),
        comment_id=call.comment_id,
        parent_id=call.parent_id,
        changed=call.changed,
    )


# --- 인자 범위 검사 (§3-2) ---------------------------------------------------


def check_int(value: object, *, name: str, low: int, high: int) -> int:
    """정수 인자 하나. 범위 밖이면 요청이 한 번도 나가지 않는다 (완료 기준 33)."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolError(INT_RANGE_MESSAGE.format(name=name, low=low, high=high))
    return value


def check_text(value: object, *, name: str, limit: int) -> str | None:
    """선택 문자열 인자. 비면 `None`, 상한을 넘으면 `ToolError`."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(TEXT_REQUIRED_MESSAGE.format(name=name))
    if len(value) > limit:
        raise ToolError(TEXT_TOO_LONG_MESSAGE.format(name=name, limit=limit))
    if value and not value.strip():
        # 공백뿐인 값은 "안 줌"이 아니다. `project="  "`를 조용히 전체 순회로 바꾸면
        # 한 프로젝트를 보려던 호출이 30개 순회가 된다 (002 리뷰 L5). 빈 문자열은 "안 줌"이다.
        raise ToolError(TEXT_BLANK_MESSAGE.format(name=name))
    return value.strip() or None


def check_required_text(value: object, *, name: str, limit: int) -> str:
    """필수 문자열 인자. 빈 값·공백뿐이면 `ToolError`(같은 문장 — 필수 값에는 "안 줌"이 없다)."""
    if isinstance(value, str) and not value.strip():
        raise ToolError(TEXT_REQUIRED_MESSAGE.format(name=name))
    text = check_text(value, name=name, limit=limit)
    if text is None:
        raise ToolError(TEXT_REQUIRED_MESSAGE.format(name=name))
    return text


def check_edit_text(
    value: object, *, name: str, limit: int, allow_empty: bool, allow_blank: bool = False
) -> str:
    """고치는 도구의 `old`·`new` (012 spec §2-8). **자르지 않는다** — 앞뒤 공백도 찾을 글의 일부다.

    빈 문자열은 `allow_empty`일 때만 받는다. 비지 않았는데 공백뿐인 값은 `allow_blank`일 때만 받는다 —
    `old`의 공백뿐인 값은 한 곳을 가리키지 못하고, `new`의 공백은 "공백으로 바꾸기"라 받는다.
    """
    if not isinstance(value, str):
        raise ToolError(TEXT_REQUIRED_MESSAGE.format(name=name))
    if len(value) > limit:
        raise ToolError(TEXT_TOO_LONG_MESSAGE.format(name=name, limit=limit))
    if value == "":
        if not allow_empty:
            raise ToolError(TEXT_REQUIRED_MESSAGE.format(name=name))
        return value
    if not value.strip() and not allow_blank:
        raise ToolError(TEXT_BLANK_MESSAGE.format(name=name))
    return value


# --- 기간 문자열 (§3-3) ------------------------------------------------------

TODAY = "today"
THISWEEK = "thisweek"
_PREV_DAYS = re.compile(r"prev-(\d{1,4})d")


def _now() -> datetime:
    """시간대가 붙은 로컬 시각. 테스트는 이 함수를 갈아 끼운다."""
    return datetime.now().astimezone()


def parse_since(value: str, *, max_days: int) -> datetime:
    """`today`·`thisweek`·`prev-{N}d`·ISO 8601을 aware datetime으로 바꾼다 (§3-3).

    코어의 `posts(updated_since=…)`가 aware datetime만 받으므로 **002가 파싱한다**.
    naive·상한 초과·형식 오류는 `ToolError`이고 API 호출이 한 번도 나가지 않는다.
    시계 오차 여유를 빼지 않는다 — 기준 시각을 기억하는 곳이 없다 (ADR 007).
    """
    text = check_required_text(value, name="기간", limit=SINCE_ARG_LIMIT)
    lowered = text.casefold()
    if lowered == TODAY:
        return _midnight(_now())
    if lowered == THISWEEK:
        # api-notes §2-4가 주 시작을 월요일로 적었다.
        start = _midnight(_now())
        return start - timedelta(days=start.weekday())
    matched = _PREV_DAYS.fullmatch(lowered)
    if matched is not None:
        days = int(matched.group(1))
        if days > max_days:
            raise ToolError(SINCE_TOO_LONG_MESSAGE.format(days=max_days))
        return _now() - timedelta(days=days)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ToolError(SINCE_FORMAT_MESSAGE) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolError(SINCE_NAIVE_MESSAGE)
    if _now() - parsed > timedelta(days=max_days):
        # 상한은 입력 형식과 무관하다. `prev-{N}d`만 막으면 ISO 시각으로 비켜 간다 (002 리뷰 L2).
        raise ToolError(SINCE_TOO_LONG_MESSAGE.format(days=max_days))
    return parsed


def _midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


# --- 대상 프로젝트 설정 (§3-7) -----------------------------------------------


@dataclass(frozen=True)
class ProjectChoice:
    """`settings.projects`를 내 프로젝트 목록과 대조한 결과.

    `values`는 **검증한 원래 문자열**이다 — `whoami`의 "대상 프로젝트" 줄이 그것을 그대로 보여 준다.
    대조와 조회에는 해석된 `Project`만 쓰고, 경로에는 `Project.id`만 간다 (완료 기준 78).
    """

    configured: bool  # 설정에 값이 있었는가
    projects: tuple[Project, ...]  # 해석된 프로젝트. 설정이 없거나 전부 못 찾으면 비어 있다
    values: tuple[str, ...]  # 코어에 넘길 원래 문자열
    warnings: tuple[str, ...]
    # 목록에서 못 찾은 값이 있었다 — 기억해 둔 목록이 낡았을 수 있다는 **유일한** 신호다.
    # 너무 긴 항목·항목 수 초과 같은 경고는 다시 받아도 사라지지 않는다 (003 리뷰 M2).
    missing: bool = False


def choose_projects(
    configured: Sequence[str], projects: Sequence[Project]
) -> ProjectChoice:
    """설정 값을 프로젝트 목록과 대조한다. 이름(code) 또는 ID, 대소문자 무시 (§3-7).

    **조용히 0개가 되지 않게 한다.** 못 찾은 값은 경고로 드러내고, 전부 못 찾으면
    설정을 무시하고 전체로 간다 — 빈 필터로 돌면 "0건"이 정상 결과처럼 보인다 (§14의 40).
    """
    warnings: list[str] = []
    kept = [str(value).strip() for value in configured if str(value).strip()]
    if not kept:
        return ProjectChoice(configured=False, projects=(), values=(), warnings=())

    short = [value for value in kept if len(value) <= PROJECT_VALUE_LIMIT]
    if len(short) != len(kept):
        warnings.append(
            PROJECTS_VALUE_TOO_LONG_MESSAGE.format(limit=PROJECT_VALUE_LIMIT)
        )
    if len(short) > MAX_PROJECT_FILTER:
        # 설정 파일에서 온 값이라도 상한 없이 루프를 돌리지 않는다 (§14의 42).
        warnings.append(PROJECTS_TOO_MANY_MESSAGE.format(limit=MAX_PROJECT_FILTER))
        short = short[:MAX_PROJECT_FILTER]

    found: list[Project] = []
    matched: set[str] = set()
    wanted = {value.casefold(): value for value in short}
    for project in projects:
        for key in (project.id.casefold(), (project.code or "").casefold()):
            if key and key in wanted:
                found.append(project)
                matched.add(wanted[key])
                break

    missing = [value for value in short if value not in matched]
    if missing and found:
        warnings.append(PROJECTS_NOT_FOUND_MESSAGE.format(values=_quoted(missing)))
    if not found:
        warnings.append(PROJECTS_ALL_MISSING_MESSAGE)
        return ProjectChoice(
            configured=True, projects=(), values=(), warnings=tuple(warnings), missing=True
        )
    return ProjectChoice(
        configured=True,
        projects=tuple(found),
        values=tuple(short),
        warnings=tuple(warnings),
        missing=bool(missing),
    )


def _quoted(values: Sequence[str]) -> str:
    """경고 줄에 되풀이되는 값. 설정 파일의 원문이라 `inline_safe`를 거친다 (§3-7)."""
    shown = ", ".join(
        f'"{render.inline_safe(value, limit=PROJECT_VALUE_LIMIT)}"'
        for value in values[:WARN_VALUES_SHOWN]
    )
    if len(values) > WARN_VALUES_SHOWN:
        return f"{shown} 외 {len(values) - WARN_VALUES_SHOWN}개"
    return shown


def home_is_default() -> bool:
    """홈 폴더가 기본 자리인가. 반환값에 절대 경로를 찍지 않으려고 본다 (§6-9)."""
    return not os.environ.get(config.HOME_ENV, "").strip()
