"""멘션 수집 (spec §10). `list_my_mentions`가 쓴다.

읽기 전용이다. 쓰기 모듈을 import하지 않는다.
기억은 이 함수가 도는 동안만 있다 — 그룹 구성은 스캔 안 dict로만 들고 있고 파일에 쓰지 않는다 (ADR 005).

중복 제거를 맡을 곳은 없다 — 같은 댓글이 다시 나올 수 있다(코어 계약 10, ADR 007).
상한·오류·판정 불가의 갈래는 아래 '활동 피드 방식' 절의 표에 있다 (003 spec §5-3).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial

from .client import PAGE_SIZE, DoorayReader, run_parallel
from .models import FeedComment, Member, Post, Project, ProjectMember
from .errors import (
    ApiError,
    AuthError,
    CallLimitError,
    ForbiddenError,
    InvalidIdError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ServerError,
)
from .mention import Mention, Person, find_mentions, is_mentioned, scan_tenant, tenant_of

# 업무·프로젝트 조회를 시작하기 전에 남겨 두는 호출 여유 (§10-1의 5).
MIN_CALLS_LEFT = 2

# 스캔 전체를 멈추는 오류. 부분 결과를 돌려준다.
STOP_ERRORS = (NetworkError, ServerError, RateLimitError)
CALL_LIMIT_REASON = "call_limit"

# 그룹 ID는 **사용자가 쓴 본문**에서 온다. 상한이 없으면 가짜 그룹 링크를 잔뜩 넣은 댓글 하나로
# 호출 예산을 다 태울 수 있다. 상한은 인자로 열지 않는다 (리뷰 H1).
MAX_GROUP_LOOKUPS_PER_COMMENT = 3
MAX_GROUP_LOOKUPS_PER_SCAN = 10


@dataclass
class _Groups:
    """한 번의 스캔 동안만 사는 그룹 구성 기억. 파일에 쓰지 않는다 (ADR 005)."""

    members: dict[str, frozenset[str]] = field(default_factory=dict)
    lookups: int = 0
    unresolved: bool = False  # 판정하지 못한 그룹 멘션이 있었다 → FeedScan.unresolved_groups로 드러낸다
    out_of_calls: bool = False  # 피드 방식: 업무 정보를 채우다 호출 여유가 떨어졌다


@dataclass(frozen=True)
class MentionHit:
    project_id: str
    project_code: str | None
    post_id: str
    post_number: int
    task_number: str
    post_subject: str
    comment_id: str
    comment_created_at: datetime
    author_id: str | None
    author_name: str | None
    body: str  # 원문. 정리는 render가 한다
    body_mime: str | None
    via_group_id: str | None  # 그룹 멘션으로 걸렸으면 그 그룹 ID


# --- 활동 피드 방식 (003 spec §5-3) ------------------------------------------
#
# 프로젝트를 돌지 않는다. `GET /common/v1/streams` 1~3회로 내 계정 전체의 댓글을 받고,
# 멘션이 걸린 업무만 단건 조회한다. 기존 방식이 구조적으로 볼 수 없던 것 —
# **내가 멤버가 아닌 프로젝트의 업무** — 까지 온다 (api-notes "추가 실측").
#
# | 상황 | 결과 |
# |---|---|
# | 피드가 `since`까지 닿았다 | `reached_since=True` |
# | `max_pages`에 막혔다 | `reached_since=False` (더 오래된 멘션이 남아 있다) |
# | 업무 단건 조회가 403·404·잘못된 ID | hit을 **버리지 않고** 업무 정보를 비워 둔다. `unread_posts` 증가 |
# | 그룹 조회 실패·상한, 또는 그 댓글의 업무를 못 읽어 프로젝트를 모름 | `unresolved_groups=True` |
# | 호출 여유 부족·`NetworkError`·`ServerError`·`RateLimitError` | 부분 결과 + `stopped_reason` |
# | `AuthError`·`ConfigError` | 잡지 않는다. 예외가 호출부로 올라간다 |
#
# `complete`는 위 넷이 모두 깨끗할 때만 참이다 — 거짓이면 호출부가 "다 못 봤다"를 드러낸다.


@dataclass(frozen=True)
class FeedScan:
    hits: tuple[MentionHit, ...]  # 오래된 순
    scanned_at: datetime  # 스캔을 시작한 시각
    complete: bool
    stopped_reason: str | None
    calls_used: int
    reached_since: bool
    unresolved_groups: bool
    unread_posts: int  # 정보를 읽지 못한 **서로 다른** 업무 수
    member_names: tuple[tuple[str, str], ...]


def collect_my_mentions_from_feed(
    reader: DoorayReader,
    *,
    my_id: str,
    since: datetime,
    max_pages: int = 3,
    now: datetime | None = None,
) -> FeedScan:
    """활동 피드에서 나를 멘션한 **댓글**을 모은다 (003 spec §5-3).

    받은 `since`를 그대로 쓴다 — 시계 오차 여유는 호출부가 뺀다 (§10-2).
    호출 수: 피드 1~3 + 서로 다른 업무 수 + 그룹 조회(≤10).
    """
    scanned_at = now or datetime.now(timezone.utc)
    hits: list[MentionHit] = []
    groups = _Groups()
    posts: dict[str, Post | None] = {}  # 업무당 한 번. `None`은 읽지 못한 업무다
    reached = False
    stopped: str | None = None

    try:
        comments, reached = reader.feed_comments(since=since, max_pages=max_pages)
        # 먼저 **호출 없이** 댓글을 나눈다: 멤버 멘션 확정 / 그룹 멘션 후보 / 해당 없음 (003 spec §7-3의 2).
        prepared = [(comment, _feed_mentions(comment, my_id=my_id)) for comment in comments]
        ready = _prefetch_posts(reader, prepared, my_id=my_id)
        for comment, found in prepared:
            hit = _feed_hit(
                reader,
                comment,
                found=found,
                my_id=my_id,
                groups=groups,
                posts=posts,
                ready=ready,
                scanned_at=scanned_at,
            )
            if hit is not None:
                hits.append(hit)
    except CallLimitError:
        stopped = CALL_LIMIT_REASON
    except STOP_ERRORS as failure:
        # 기준 시각이 전진하지 않으므로 다음 스캔이 이어받는다. 오류 때문에 멘션을 잃지 않는다.
        stopped = type(failure).__name__

    if groups.out_of_calls and stopped is None:
        stopped = CALL_LIMIT_REASON
    # 쪽 경계에서 커서가 겹치면 같은 댓글이 두 번 온다 (003 리뷰 L7).
    hits = list({hit.comment_id: hit for hit in hits}.values())
    hits.sort(key=lambda hit: (hit.comment_created_at, hit.comment_id))
    unread = sum(1 for post in posts.values() if post is None)
    return FeedScan(
        hits=tuple(hits),
        scanned_at=scanned_at,
        complete=(
            reached and not groups.unresolved and unread == 0 and stopped is None
        ),
        stopped_reason=stopped,
        calls_used=reader.calls,
        reached_since=reached,
        unresolved_groups=groups.unresolved,
        unread_posts=unread,
        member_names=tuple(sorted(reader.names.items())),
    )


def _feed_mentions(comment: FeedComment, *, my_id: str) -> list[Mention]:
    """이 댓글이 들고 있는 멘션. 볼 필요가 없는 댓글이면 빈 목록이다. **호출을 쓰지 않는다.**

    본문 한 건에 정규식을 **한 번만** 돌리려고 여기서 거둬 `_feed_hit`에 넘긴다
    (본문은 사용자가 쓴 텍스트다 — 두 번 훑을 이유가 없다).
    """
    if comment.kind != "comment":
        return []  # 시스템 이벤트 (api-notes "추가 실측")
    if comment.author_id and comment.author_id == my_id:
        return []  # 내가 쓴 댓글은 제외한다
    return find_mentions(comment.body, mime_type=comment.body_mime)


def _has_group(found: Sequence[Mention]) -> bool:
    return any(mention.kind == "group" and not mention.quoted for mention in found)


def _feed_hit(
    reader: DoorayReader,
    comment: FeedComment,
    *,
    found: list[Mention],
    my_id: str,
    groups: _Groups,
    posts: dict[str, Post | None],
    ready: Mapping[str, Post | BaseException],
    scanned_at: datetime,
) -> MentionHit | None:
    if not found:
        return None
    post: Post | None = None
    # 멤버 멘션으로 이미 내 것이 확정되면 업무도 그룹도 묻지 않는다 — 대부분이 여기서 끝난다.
    if not is_mentioned(found, my_id=my_id):
        if not _has_group(found):
            return None  # 남만 부른 댓글이다. 업무 조회를 쓰지 않는다
        # 그룹 판정에는 프로젝트가 필요하고, 피드는 프로젝트를 주지 않는다.
        try:
            post = _feed_post(reader, comment.post_id, posts, ready=ready)
        except CallLimitError:
            # 이 댓글은 판정하지 못했다. **그래도 루프를 끊지 않는다** — 뒤에 남은 멤버 멘션은
            # 호출 없이 판정된다 (003 리뷰 M3).
            groups.out_of_calls = True
            groups.unresolved = True
            return None
        if post is None:
            # 업무를 못 읽었으니 이 그룹 멘션은 **판정하지 못했다.** 조용히 넘기지 않는다.
            groups.unresolved = True
            return None
        _resolve_groups(reader, found, post.project_id, groups)
        if not is_mentioned(found, my_id=my_id, group_members=groups.members):
            return None

    if post is None:
        try:
            post = _feed_post(reader, comment.post_id, posts, ready=ready)
        except CallLimitError:
            # **이미 내 멘션으로 확정된 댓글이다.** 업무 제목을 못 채운다고 멘션까지 버리지 않는다 —
            # 뒤에 남은 멤버 멘션들도 호출 없이 판정되므로 스캔을 계속한다 (메인 세션 검토).
            groups.out_of_calls = True
    return MentionHit(
        # 업무를 못 읽어도 hit을 버리지 않는다. 멘션을 잃는 쪽이 더 나쁘다 (003 spec §5-3의 4).
        project_id=post.project_id if post is not None else "",
        project_code=post.project_code if post is not None else None,
        post_id=comment.post_id,
        post_number=post.number if post is not None else 0,
        task_number=post.task_number if post is not None else "",
        post_subject=post.subject if post is not None else "",
        comment_id=comment.id,
        # 시각을 못 읽은 댓글은 버리지 않는다.
        comment_created_at=comment.created_at or scanned_at,
        author_id=comment.author_id,
        author_name=comment.author_name or reader.names.get(comment.author_id or ""),
        body=comment.body,
        body_mime=comment.body_mime,
        via_group_id=_via_group(found, my_id, groups.members),
    )


def _feed_post(
    reader: DoorayReader,
    post_id: str,
    posts: dict[str, Post | None],
    *,
    ready: Mapping[str, Post | BaseException],
) -> Post | None:
    """업무 단건 조회. **업무당 한 번**이고 실패도 기억한다 (003 spec §5-3의 3).

    프로젝트를 거치지 않는 엔드포인트다 (api-notes §2-5) — 내가 멤버가 아닌 프로젝트의 업무도 읽힌다.
    조회 전에 호출 여유를 본다. 모자라면 `CallLimitError`로 스캔이 부분 결과가 된다.
    미리 읽어 둔 것이 있으면 그것을 **이 자리에서** 푼다 — 예외도 순차 때 났을 자리에서 난다.
    """
    if post_id in posts:
        return posts[post_id]
    waiting = ready.get(post_id)
    if waiting is None:
        _need_room(reader)
    try:
        post = reader.post(post_id) if waiting is None else _unwrapped(waiting)
    except (ForbiddenError, NotFoundError, InvalidIdError):
        posts[post_id] = None  # 권한이 없거나 지워졌거나 경로에 넣을 수 없는 ID다
        return None
    except (AuthError, *STOP_ERRORS):
        raise  # 인증 오류는 호출부로, 네트워크·5xx·429는 스캔을 멈춘다 (부분 결과 + 사유)
    except ApiError:
        # 그 밖의 4xx. 피드는 여러 프로젝트의 업무를 섞어 준다 — 업무 하나가 400이라고
        # 멘션 전부를 오류 문장으로 바꾸지 않는다 (003 리뷰 M4).
        posts[post_id] = None
        return None
    posts[post_id] = post
    return post


# --- 한꺼번에 미리 읽기 (003 spec §7-3) --------------------------------------
#
# 서로 독립인 단건 조회를 묶어 동시에 보낸다. **결과와 순서는 순차 때와 같아야 한다** —
# 그래서 결과도 예외도 그 자리에 담아 두고, 푸는 일은 순차 로직이 제 차례에 한다.
# 보내는 수는 호출 여유 안으로 자른다. 남은 것은 순차 로직이 지금의 규칙대로 처리한다.


def _room(reader: DoorayReader) -> int:
    """이 Reader가 지금부터 보낼 수 있는 요청 수. 순차로 돌 때와 같은 셈이다.

    요청 하나를 시작하려면 여유가 `MIN_CALLS_LEFT` 이상이어야 하므로 `여유 − MIN + 1`개다.
    """
    return max(0, reader.calls_left - MIN_CALLS_LEFT + 1)


def _prefetch[T](
    reader: DoorayReader, ids: Sequence[str], fetch: Callable[[str], T], *, room: int
) -> dict[str, T | BaseException]:
    """앞에서부터 `room`개를 한꺼번에 보낸다. 하나 이하면 순차와 다를 것이 없어 보내지 않는다."""
    chosen = list(ids)[: max(0, room)]
    if len(chosen) <= 1:
        return {}
    # 묶음 안에서 429·5xx·네트워크 오류가 나면 아직 시작하지 않은 조회는 나가지 않는다 (리뷰 H1).
    with reader.batch():
        outcomes = run_parallel([partial(fetch, one) for one in chosen])
    return dict(zip(chosen, outcomes))


def _unwrapped[T](outcome: T | BaseException) -> T:
    """미리 읽어 둔 결과를 푼다. 예외였으면 **여기서** 던진다 — 순차 때 났을 자리다."""
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


def _prefetch_posts(
    reader: DoorayReader,
    prepared: Sequence[tuple[FeedComment, list[Mention]]],
    *,
    my_id: str,
) -> dict[str, Post | BaseException]:
    """멘션이 걸린 **서로 다른 업무**를 피드 순서대로 미리 읽는다 (003 spec §7-3의 2).

    그룹 조회가 쓸 몫은 덜어 둔다 — 미리 읽기가 여유를 앞에서 다 쓰면 순차 때와 결과가 달라진다.
    """
    wanted: list[str] = []
    taken: set[str] = set()
    for comment, found in prepared:
        if not found or comment.post_id in taken:
            continue
        if not (is_mentioned(found, my_id=my_id) or _has_group(found)):
            continue  # 남만 부른 댓글이다. 업무 조회를 쓰지 않는다
        taken.add(comment.post_id)
        wanted.append(comment.post_id)
    return _prefetch(
        reader, wanted, reader.post, room=_room(reader) - _group_room(prepared, my_id=my_id)
    )


def _group_room(
    prepared: Sequence[tuple[FeedComment, list[Mention]]], *, my_id: str
) -> int:
    """이 스캔이 쓸 수 있는 그룹 조회 수의 **상한**. 미리 읽기에서 이만큼을 덜어 둔다.

    순차로 돌면 업무 조회와 그룹 조회가 번갈아 나간다. 덜어 두지 않으면 여유가 빠듯할 때
    미리 읽기가 그룹의 몫까지 써서 순차 때와 다른 답이 나온다 (003 spec §7-3, 완료 기준 25).
    상한을 넉넉히 잡아도 손해는 미리 읽기를 조금 덜 하는 것뿐이다 — 남은 것은 순차가 받는다.
    """
    asked: set[str] = set()
    for _, found in prepared:
        if not found or is_mentioned(found, my_id=my_id):
            continue  # 멤버 멘션으로 확정된 댓글은 그룹을 묻지 않는다
        here = 0
        for mention in found:
            if mention.kind != "group" or mention.quoted or mention.target_id in asked:
                continue
            if here >= MAX_GROUP_LOOKUPS_PER_COMMENT:
                break
            asked.add(mention.target_id)
            here += 1
            if len(asked) >= MAX_GROUP_LOOKUPS_PER_SCAN:
                return MAX_GROUP_LOOKUPS_PER_SCAN
    return len(asked)


# --- 멤버가 아닌데 내 업무가 있는 프로젝트 (003 spec §6-2) --------------------
#
# `member=me` 프로젝트 목록에서 출발하는 순회는 이런 업무를 **조용히 놓친다**(api-notes 맨 끝 실측:
# 피드에 뜬 내 업무 22건 중 4건, 댓글이 달린 업무의 프로젝트 13개 중 3개가 비멤버였다).
# 피드가 그 프로젝트를 찾아내는 단서다 — 최근 2주만 보이므로 한 번 찾은 것은 호출부가 쌓아 둔다.
#
# 발견은 **덤이지 도구의 성패가 아니다**: 상한·네트워크·5xx는 그때까지 모은 것에 `previous`를
# 붙여 돌려준다. `previous`를 빼는 쪽이 유실이다 (§6-2의 4·5).

# 조회 수 상한. 피드가 주는 수는 우리가 정하지 않지만 조회 수는 우리가 정한다.
MAX_POST_LOOKUPS = 15
MAX_PROJECT_LOOKUPS = 10


def discover_seen_projects(
    reader: DoorayReader,
    *,
    member_ids: Sequence[str] | set[str],
    previous: Sequence[Project] = (),
    max_post_lookups: int = MAX_POST_LOOKUPS,
    max_project_lookups: int = MAX_PROJECT_LOOKUPS,
) -> tuple[Project, ...]:
    """활동 피드로 **멤버가 아닌데 내 업무가 보이는** 프로젝트를 찾는다 (003 spec §6-2).

    호출 수: 피드 1~3 + 프로젝트 ≤ 10 + 업무 ≤ 15 = **≤ 28**. `previous`는 그대로 돌려준다(멤버가 된 것만 뺀다).
    돌려주는 순서는 **나중에 발견한 것이 먼저**다 — 호출부의 상한이 그 순서로 자른다.
    """
    members = {str(value) for value in member_ids}
    # `previous`는 파일에서 온다. 멤버가 된 것은 여기서 빠진다 (중복 금지, §6-1).
    kept: dict[str, Project] = {
        project.id: project
        for project in previous
        if project.id and project.id not in members
    }
    found: dict[str, Project] = {}
    posts: dict[str, Post | None] = {}

    try:
        project_ids, post_ids = reader.feed_activity()

        # 물어볼 프로젝트를 먼저 추린 뒤 한꺼번에 보낸다 (003 spec §7-3의 3). 피드가 주는
        # `projectId`는 이미 중복이 없어, 순차로 돌며 걸러 내던 것과 같은 목록이다.
        asking = [
            project_id
            for project_id in project_ids
            if project_id not in members and project_id not in kept
        ][:max_project_lookups]
        ready_projects = _prefetch(reader, asking, reader.project, room=_room(reader))
        for project_id in asking:
            row = _seen_project(reader, project_id, ready=ready_projects)
            if row is not None:
                found[project_id] = row

        chosen = post_ids[:max_post_lookups]
        ready_posts = _prefetch(reader, chosen, reader.post, room=_room(reader))
        for post_id in chosen:
            post = _feed_post(reader, post_id, posts, ready=ready_posts)
            if post is None or not post.project_id:
                continue  # 못 읽은 업무는 단서가 되지 못한다. 그뿐이다
            if post.project_id in members or post.project_id in kept:
                continue
            if post.project_id not in found:
                # 단건 응답에 프로젝트 이름이 있다 — 프로젝트를 따로 묻지 않는다 (§6-2의 3).
                found[post.project_id] = Project(
                    id=post.project_id,
                    code=post.project_code or "",
                    state=None,
                    type=None,
                    scope=None,
                )

        # `previous`는 확인하지 않는다 — **발견은 더하기만 한다** (운영자 결정, §6-1).
        # 한 번 등록된 프로젝트는 계속 조회하고, 빠지는 것은 실제 조회가 403·404를 줬을 때다
        # (`mcp_server.tools._across_projects`). 여기서 하루 열 번씩 확인 호출을 쓰지 않는다.
    except (CallLimitError, *STOP_ERRORS):
        # 발견은 덤이다. 여기까지 모은 것과 `previous`를 그대로 돌려준다 (§6-2의 5).
        pass

    return tuple(found.values()) + tuple(kept.values())


def _seen_project(
    reader: DoorayReader,
    project_id: str,
    *,
    ready: Mapping[str, Project | BaseException],
) -> Project | None:
    """프로젝트 단건 조회. 읽을 수 없으면 `None`이고 그 항목만 건너뛴다 (§6-2의 5).

    미리 읽어 둔 것이 있으면 그것을 이 자리에서 푼다 — 예외도 순차 때 났을 자리에서 난다.
    """
    waiting = ready.get(project_id)
    if waiting is None:
        _need_room(reader)
    try:
        return reader.project(project_id) if waiting is None else _unwrapped(waiting)
    except (ForbiddenError, NotFoundError, InvalidIdError):
        return None
    except (AuthError, CallLimitError, *STOP_ERRORS):
        raise  # 인증 오류는 호출부로, 나머지는 발견을 끝낸다
    except ApiError:
        return None  # 그 밖의 4xx. 프로젝트 하나 때문에 발견 전체를 버리지 않는다


# --- 멘션할 수 있는 사람 (007 spec §4-2) --------------------------------------
#
# 댓글의 `@이름`을 멘션 링크로 바꿀 때 쓰는 이름표. **ID와 이름은 전부 두레이 응답의 구조 필드에서** 온다
# — 본문 속 멘션 링크의 ID는 쓴 사람이 정하는 값이라 쓰지 않는다. 본문에서 오는 값은 테넌트 하나뿐이다.

# `new_comments`가 멈추지 않게 하는 기준 시각. `mcp_server.tools.EPOCH`와 같은 값이다 —
# 코어는 위를 모르므로 자기 상수를 둔다. 최신부터 읽어 상한에서 빠지는 쪽이 오래된 댓글이 된다.
THREAD_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# 테넌트를 찾기 전에 본문 한 건을 자르는 길이. `render.RAW_BODY_LIMIT`과 같은 값이다(코어 `collect`는
# `render`를 import하지 않는다 — P8). 두레이 본문 크기에는 상한이 없다.
THREAD_TEXT_LIMIT = 40_000
FEED_TENANT_PAGES = 1  # 한 쪽. 014 실측: 한 쪽에 사람이 쓴 댓글 142건, 링크 181개, 값 1개
# 멘션 링크의 주소 자리에 들어가도 되는 ID 모양 (`client.path_id`와 같은 규칙).
_PERSON_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")
# 이름을 단건으로 채우다 못 한 것. 그 사람만 건너뛴다 (`tools._fill_names`와 같은 규칙).
_NAME_MISSES = (ForbiddenError, NotFoundError, InvalidIdError)


@dataclass(frozen=True)
class ThreadPeople:
    people: tuple[Person, ...]  # 멘션해도 되는 사람. ID 중복 없음
    # 정확히 하나일 때만 값. 업무의 글(본문과 댓글 전부)에 `dooray://`가 하나도 없다고 **확인된** 때만
    # 활동 피드 한 쪽에서 — 댓글을 다 읽지 못했으면 가지 않는다
    tenant: str | None
    feed_read: bool  # 테넌트를 찾으러 활동 피드를 읽었다 (014)
    unnamed: int  # 이름을 얻지 못해(또는 ID 모양이 맞지 않아) people에서 빠진 사람 수
    comments_cut: bool  # 댓글이 max_pages 쪽을 넘었다


def feed_tenant(reader: DoorayReader) -> str | None:
    """활동 피드 한 쪽에서, 사람이 쓴 댓글 본문의 테넌트. 정확히 하나이고 모양이 맞을 때만 값이다.

    호출: 피드 GET 1회. 예외를 잡지 않는다.
    """
    comments, _ = reader.feed_comments(since=THREAD_EPOCH, max_pages=FEED_TENANT_PAGES)
    return tenant_of(comment.body[:THREAD_TEXT_LIMIT] for comment in comments)


def thread_people(
    reader: DoorayReader, post: Post, *, max_pages: int, max_lookups: int
) -> ThreadPeople:
    """업무 하나의 참여자 — 작성자·멤버 담당·멤버 참조·댓글 작성자 (007 spec §4-2).

    넣지 않는 것: 이메일 사용자(멤버 ID가 없다), 그룹 담당·그룹 참조의 구성원(그 사람이 이 업무에
    나타난 것이 아니다), 본문·댓글 속 링크의 ID. 호출: 댓글 1~max_pages + 이름 0~max_lookups + 피드 0~1.
    댓글·피드 조회의 `DoorayError`는 잡지 않는다 — 참여자를 모르면 호출부가 등록하지 않는다 (§4-1).
    업무의 글(본문과 댓글 전부)에 `dooray://`가 하나도 없다고 **확인된** 때만 테넌트를 활동 피드 한 쪽에서
    얻는다(014) — 자르기 전의 글로 보고, 댓글을 다 읽지 못했으면(`comments_cut`) 가지 않는다.
    """
    wanted: dict[str, str | None] = {}  # ID → 응답에 같이 온 이름. 나온 순서를 지킨다
    for member in (
        post.author,
        *(one.member for one in post.assignees if one.type == "member"),
        *post.cc,
    ):
        if member is not None and member.id:
            wanted.setdefault(member.id, member.name or None)

    comments = reader.new_comments(
        post.id, project_id=post.project_id, since=THREAD_EPOCH, max_pages=max_pages
    )
    # **최신부터** 넣는다 — 이름 조회 상한(`max_lookups`)이 나를 부른 최근 작성자에게 먼저 쓰이게.
    # `new_comments`는 오래된 순으로 돌려준다 (007 리뷰 M2).
    for comment in reversed(comments):
        if comment.type == "comment" and comment.author is not None and comment.author.id:
            wanted.setdefault(comment.author.id, None)

    people: list[Person] = []
    unnamed = 0
    lookups = 0
    for member_id, name in wanted.items():
        if _PERSON_ID.fullmatch(member_id) is None:
            unnamed += 1  # 링크 주소 자리에 들어갈 값이다. 조용히 빼지 않고 센다
            continue
        name = name or reader.names.get(member_id)
        if not name and lookups < max_lookups:
            lookups += 1
            name = _member_name(reader, member_id)
        if not name:
            unnamed += 1
            continue
        people.append(Person(id=member_id, name=name))

    cut = len(comments) >= max_pages * PAGE_SIZE
    texts = [(post.body or "")[:THREAD_TEXT_LIMIT]]
    texts.extend(comment.body[:THREAD_TEXT_LIMIT] for comment in comments)
    scan = scan_tenant(texts)  # 값은 자른 글에서 — 정규식의 시간 상한
    tenant, feed_read = scan.value, False
    whole = [post.body or "", *(comment.body for comment in comments)]  # 자르기 전의 글
    if not scan.seen and not cut and not _has_scheme(whole):
        # 그 업무의 글(본문과 댓글 전부)에 두레이 링크가 하나도 없다고 확인됐다 — 테넌트는 조직에
        # 하나다 (api-notes 007·014). 댓글을 다 읽지 못했으면 확인할 수 없으므로 가지 않는다.
        tenant, feed_read = feed_tenant(reader), True
    return ThreadPeople(
        people=tuple(people),
        tenant=tenant,
        feed_read=feed_read,
        unnamed=unnamed,
        comments_cut=cut,
    )


def project_people(
    reader: DoorayReader,
    members: Sequence[ProjectMember],
    *,
    tenant: str | None,
    cut: bool,
    max_lookups: int,
) -> ThreadPeople:
    """프로젝트 멤버 전원 — 설명의 멘션 범위 (023 spec §2-3). 이름은 이름표에 없으면 단건으로 채운다.

    `feed_read`는 언제나 거짓이다 — 이 함수는 피드를 읽지 않고, `tenant`는 호출부가 `collect.feed_tenant`로
    얻어 넘긴 값이다. 피드를 읽었는지로 문장을 고르는 `_mention_lines`(`NO_TENANT_FEED`)는 이 결과를 받지
    않는다 — 설명의 문장은 `DESC_NO_TENANT` 하나다.
    `comments_cut`은 '프로젝트 멤버 명단을 다 읽지 못했다'로 쓴다.
    이름표에 없는 멤버가 `max_lookups`를 넘거나 명단이 잘렸으면 조회하지 않고 그 수를 `unnamed`에 센다 —
    어차피 멘션하지 않는다(설계 리뷰 2 낮음 4).
    호출: 이름 0~max_lookups. 피드·댓글을 읽지 않는다. `me`를 부르지 않는다.
    """
    wanted: list[str] = []  # 나온 순서, ID 중복 없음
    unnamed = 0
    for member in members:
        if member.id in wanted:
            continue
        if _PERSON_ID.fullmatch(member.id) is None:
            unnamed += 1  # 링크 주소 자리에 들어갈 값이다. 조용히 빼지 않고 센다
            continue
        wanted.append(member.id)

    people: list[Person] = []
    missing: list[str] = []
    for member_id in wanted:
        name = reader.names.get(member_id)
        if name:
            people.append(Person(id=member_id, name=name))
        else:
            missing.append(member_id)

    if cut or len(missing) > max_lookups:
        unnamed += len(missing)
        missing = []
    for member_id in missing:
        name = _member_name(reader, member_id)
        if not name:
            unnamed += 1
            continue
        people.append(Person(id=member_id, name=name))

    return ThreadPeople(
        people=tuple(people),
        tenant=tenant,
        feed_read=False,
        unnamed=unnamed,
        comments_cut=cut,
    )


def _has_scheme(texts: Sequence[str]) -> bool:
    """자르기 전의 글 어딘가에 글자 `dooray://`가 있는가. `in` 검사라 길이에 선형이다 (014 보충)."""
    return any("dooray://" in text for text in texts)


def _member_name(reader: DoorayReader, member_id: str) -> str | None:
    try:
        member: Member = reader.member(member_id)
    except _NAME_MISSES:
        return None
    return member.name or None


# --- 내가 쓴 댓글 (007 spec §6-2) ---------------------------------------------
#
# 말투를 뽑는 표본이다. 활동 피드를 그대로 쓴다 — 피드가 이미 시스템 이벤트를 뺀다.
# 업무 조회는 **고른 댓글의 업무만** 한다. 판정을 못 했으면(피드 끝까지 못 닿음, 멈춤, 못 읽은 업무)
# 결과에 드러내고 도구가 ⚠ 줄을 낸다.


@dataclass(frozen=True)
class MyComment:
    comment_id: str
    post_id: str
    created_at: datetime | None
    body: str  # 원문. 호출부가 clean_body·own_lines를 거친다
    body_mime: str | None
    task_number: str | None  # 업무를 못 읽었으면 None
    post_subject: str | None


@dataclass(frozen=True)
class MyCommentScan:
    comments: tuple[MyComment, ...]  # 최신이 뒤. 최대 limit건
    total: int  # 기간 안의 내 댓글 수(limit에 잘리기 전)
    reached_since: bool
    stopped_reason: str | None
    unread_posts: int


def collect_my_comments_from_feed(
    reader: DoorayReader, *, my_id: str, since: datetime, max_pages: int, limit: int
) -> MyCommentScan:
    """활동 피드에서 **토큰 주인이 쓴** 댓글을 최신 `limit`건 모은다.

    호출: 피드 1~`max_pages` + 고른 댓글의 서로 다른 업무 ≤ `limit`.
    `AuthError`·`ConfigError`는 잡지 않는다. 상한·네트워크·5xx·429는 부분 결과 + 사유다.
    """
    stopped: str | None = None
    reached = False
    found: list[FeedComment] = []
    try:
        found, reached = reader.feed_comments(since=since, max_pages=max_pages)
    except CallLimitError:
        stopped = CALL_LIMIT_REASON
    except STOP_ERRORS as failure:
        stopped = type(failure).__name__

    # 쪽 경계에서 커서가 겹치면 같은 댓글이 두 번 온다 (003 리뷰 L7).
    mine = list(
        {
            comment.id: comment
            for comment in found
            if comment.author_id and comment.author_id == my_id
        }.values()
    )
    mine.sort(key=lambda comment: (comment.created_at or since, comment.id))
    chosen = mine[-limit:] if limit > 0 else []

    posts: dict[str, Post | None] = {}
    if stopped is None:
        wanted = list(dict.fromkeys(comment.post_id for comment in reversed(chosen)))
        try:
            ready = _prefetch(reader, wanted, reader.post, room=_room(reader))
            for post_id in wanted:
                _feed_post(reader, post_id, posts, ready=ready)
        except CallLimitError:
            stopped = CALL_LIMIT_REASON
        except STOP_ERRORS as failure:
            stopped = type(failure).__name__

    rows = []
    for comment in chosen:
        post = posts.get(comment.post_id)
        rows.append(
            MyComment(
                comment_id=comment.id,
                post_id=comment.post_id,
                created_at=comment.created_at,
                body=comment.body,
                body_mime=comment.body_mime,
                task_number=post.task_number if post is not None else None,
                post_subject=post.subject if post is not None else None,
            )
        )
    return MyCommentScan(
        comments=tuple(rows),
        total=len(mine),
        reached_since=reached,
        stopped_reason=stopped,
        unread_posts=sum(1 for post in posts.values() if post is None),
    )


# --- 그룹 멘션 판정과 호출 여유 (피드 경로가 쓴다) ---------------------------


def _via_group(
    found: list[Mention], my_id: str, groups: Mapping[str, frozenset[str]]
) -> str | None:
    """멤버 멘션으로 걸렸으면 `None`, 그룹 멘션으로 걸렸으면 그 그룹 ID."""
    for mention in found:
        if mention.quoted:
            continue
        if mention.kind == "member" and mention.target_id == my_id:
            return None
    for mention in found:
        if mention.quoted or mention.kind != "group":
            continue
        if my_id in groups.get(mention.target_id, frozenset()):
            return mention.target_id
    return None


def _resolve_groups(
    reader: DoorayReader, found: list[Mention], project_id: str, groups: _Groups
) -> None:
    """이 댓글의 그룹 멘션을 상한 안에서 조회한다. 못 본 것이 있으면 `unresolved`를 세운다."""
    asked_here = 0
    for mention in found:
        if mention.kind != "group" or mention.quoted or mention.target_id in groups.members:
            continue
        if (
            asked_here >= MAX_GROUP_LOOKUPS_PER_COMMENT
            or groups.lookups >= MAX_GROUP_LOOKUPS_PER_SCAN
        ):
            groups.unresolved = True
            continue
        asked_here += 1
        groups.lookups += 1
        members = _group_members(reader, mention.target_id, project_id)
        if members is None:
            groups.unresolved = True
            members = frozenset()
        groups.members[mention.target_id] = members


def _group_members(
    reader: DoorayReader, group_id: str, project_id: str
) -> frozenset[str] | None:
    """멘션에서 얻은 그룹 ID로 바로 단건 조회한다 (api-notes §2-7 실측).

    **그 ID는 사용자가 쓴 본문에서 온다.** 경로에 넣어도 되는 모양인지는 `member_group()` 안의
    `path_id()`가 본다 — 아니면 요청이 나가기 전에 `InvalidIdError`다.
    조회에 실패한 그룹은 이 스캔 동안만 빈 집합으로 둔다. 멘션 하나 때문에 스캔 전체가 죽지 않는다.
    """
    try:
        return reader.member_group(group_id, project_id=project_id).member_ids
    except (ForbiddenError, NotFoundError, InvalidIdError):
        return None  # 판정할 수 없다. 호출부가 빈 집합으로 두되 그 사실을 드러낸다


def _need_room(reader: DoorayReader) -> None:
    if reader.calls_left < MIN_CALLS_LEFT:
        raise CallLimitError(reader.calls + reader.calls_left, reader.calls)
