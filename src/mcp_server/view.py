"""출력 조립 (spec §5). 순수 함수다 — 파일도 네트워크도 건드리지 않는다.

규칙 하나(§4): **우리가 만든 구조는 블록 밖에, 사용자가 쓴 본문은 블록 안에.**
블록 안에 우리 구조가 하나도 없으므로 위조할 대상이 없다 (코어 계약 3).
블록 밖에 놓이는 사용자 문자열은 예외 없이 `render.inline_safe`를 거친다.

코어의 `post_markdown`·`comment_markdown`·`thread_markdown`은 부르지 않는다 (§5-1) —
그 셋은 우리 구조와 사용자 본문을 한 덩어리로 만든다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import tzinfo

from dooray_core import render
from dooray_core.models import Comment, Post
from dooray_core.collect import MentionHit
from dooray_core.render import Names

WARN_MARK = "⚠ "
NO_BODY = "(본문 없음)"
# 머리 줄에 적는 참조 수. 나머지는 " 외 N명"이다 (007 spec §6-6).
CC_SHOWN = 5


# --- 업무 (§5) ---------------------------------------------------------------


def task_head(
    post: Post,
    *,
    names: Names,
    web_base: str | None,
    tz: tzinfo | None = None,
    tag_names: Mapping[str, str] | None = None,
) -> list[str]:
    """업무 하나의 머리 줄들. 전부 블록 밖이라 사용자 문자열은 `inline_safe`를 거친다.

    `tag_names`는 태그 ID → 이름. 없거나 모르는 태그는 이름 없이 센다 (012 spec §3-1).
    """
    lines = [f"## {render.inline_safe(post.task_number)} · {render.inline_safe(post.subject)}"]

    facts: list[str] = []
    if post.workflow_name:
        facts.append(f"상태: {render.inline_safe(post.workflow_name)}")
    if post.assignees:
        people = [
            render.display_name(a.member.id if a.member else None, names)
            for a in post.assignees
        ]
        facts.append("담당: " + ", ".join(people))
    if post.cc:
        # 멘션될 수 있는 사람(작성자·담당·참조·댓글 작성자)이 모델에게 다 보이게 한다 (007 spec §6-6).
        # `post.cc`는 멤버 참조뿐이다 — 그룹 참조는 파서가 거두지 않는다.
        cc = [render.display_name(member.id, names) for member in post.cc]
        rest = len(cc) - CC_SHOWN
        facts.append(
            "참조: " + ", ".join(cc[:CC_SHOWN]) + (f" 외 {rest}명" if rest > 0 else "")
        )
    if post.due_date is not None:
        # 마감은 날짜만 적는다. 형식 문자열은 코어에 한 번만 있다.
        facts.append("마감 " + post.due_date.astimezone(tz).strftime(render.DATE_FORMAT))
    if facts:
        lines.append("- " + " · ".join(facts))

    made = [f"작성: {render.display_name(post.author.id if post.author else None, names)}"]
    if post.created_at is not None:
        made.append(render.format_time(post.created_at, tz))
    if post.files:
        shown = ", ".join(
            render.inline_safe(ref.name or f"파일(…{ref.id[-4:]})") for ref in post.files[:2]
        )
        rest = "" if len(post.files) <= 2 else f" 외 {len(post.files) - 2}건"
        made.append(f"첨부 {len(post.files)}건: {shown}{rest}")
    lines.append("- " + " · ".join(made))

    # 012 spec §3-1 — 고치기 전의 "지금 값"이 보이게 한다. 자리는 작성 줄 **뒤**다(앞 줄들은 번호로 읽힌다).
    sorts = _sorting_line(post, tag_names)
    if sorts is not None:
        lines.append(sorts)
    statuses = _assignee_status_line(post, names)
    if statuses is not None:
        lines.append(statuses)

    parent = parent_ref(post)
    if parent is not None:
        # 정리하기(010)에서 모델이 상위를 알아야 한다 — 등록 시험 E에서 "109의 상위를 모른다"고 했다.
        label, subject = parent
        lines.append(f"- 상위 업무: [{label}] {subject}".rstrip())

    url = render.post_url(web_base, post.id)
    if url is not None:
        lines.append(f"- 링크: {url}")
    return lines


def _sorting_line(post: Post, tag_names: Mapping[str, str] | None) -> str | None:
    """우선순위(`none` 제외) · 마일스톤 · 태그. 값이 있는 것만, 셋 다 없으면 줄이 없다.

    우선순위는 API 값 그대로 적는다 — `set_post_fields`가 받는 글자와 같다.
    """
    parts: list[str] = []
    if post.priority and post.priority != "none":
        parts.append(f"우선순위 {render.inline_safe(post.priority)}")
    if post.milestone_name:
        parts.append(f"마일스톤 {render.inline_safe(post.milestone_name)}")
    if post.tag_ids:
        known = [tag_names[one] for one in post.tag_ids if tag_names and one in tag_names]
        unknown = len(post.tag_ids) - len(known)
        if not known:
            parts.append(f"태그 {unknown}개")
        else:
            shown = ", ".join(render.inline_safe(name) for name in known)
            parts.append(f"태그 {shown}" + (f" 외 {unknown}개" if unknown else ""))
    return "- " + " · ".join(parts) if parts else None


def _assignee_status_line(post: Post, names: Names) -> str | None:
    """담당자별 상태. 담당의 상태 이름이 하나라도 있을 때만 (012 spec §3-1)."""
    if not any(one.workflow_name for one in post.assignees):
        return None
    shown = [
        render.display_name(one.member.id if one.member else None, names)
        + " "
        + (render.inline_safe(one.workflow_name) if one.workflow_name else "상태 모름")
        for one in post.assignees
    ]
    return "- 담당자별 상태: " + " · ".join(shown)


def parent_ref(post: Post) -> tuple[str, str] | None:
    """상위 업무 — (라벨, 제목). 없으면 `None`. 둘 다 블록 밖에 놓이므로 `inline_safe`를 거친다.

    업무 번호는 `code/number`다 (api-notes §2-5). 번호를 못 읽으면 ID로 적는다.
    """
    if not post.parent_id:
        return None
    code = post.task_number.partition("/")[0] or post.project_code or ""
    if code and post.parent_number is not None:
        label = f"{code}/{post.parent_number}"
    else:
        label = post.parent_id
    return render.inline_safe(label), render.inline_safe(post.parent_subject or "")


def post_row(
    post: Post, *, names: Names, web_base: str | None, tz: tzinfo | None = None
) -> str:
    """목록 한 줄. 코어의 `post_line`이 제목·상태를 무해화한다 (C3)."""
    return render.post_line(post, names=names, web_base=web_base, tz=tz)


def body_block(post: Post, *, web_base: str | None, body_limit: int) -> str:
    """`### 본문` + 신뢰 경계 블록. 본문은 사용자가 쓴다."""
    body = render.clean_body(post.body or "", post.body_mime, web_base=web_base).strip()
    if not body:
        return "### 본문\n" + NO_BODY
    # 머리말은 도구가 반환값 맨 위에 한 번 적는다. 라벨은 짧게 — 업무 번호는 바로 위 머리 줄에 있다.
    wrapped = render.untrusted_block("본문", render.truncate(body, body_limit), notice=False)
    return "### 본문\n" + wrapped


def comment_entry(
    comment: Comment,
    *,
    order: int,
    total: int,
    task_number: str,
    names: Names,
    web_base: str | None,
    body_limit: int,
    tz: tzinfo | None = None,
) -> str:
    """댓글 한 건. 머리 한 줄은 블록 밖, 본문은 블록 안이다."""
    label = render.inline_safe(task_number)
    who = (
        render.inline_safe(comment.author_email)
        if comment.author_email
        else render.display_name(comment.author.id if comment.author else None, names)
    )
    head = f"### 댓글 {order}/{total} · {who}"
    when = render.format_time(comment.created_at, tz)
    if when:
        head += f" · {when}"
    head += f" · 댓글 {render.inline_safe(comment.id)}"
    body = render.clean_body(comment.body, comment.body_mime, web_base=web_base).strip()
    # 라벨은 순번만 — 댓글 ID는 바로 위 머리 줄에 있다. 되풀이하면 댓글마다 40자가 든다.
    wrapped = render.untrusted_block(
        f"댓글 {order}", render.truncate(body, body_limit) if body else "", notice=False
    )
    return head + "\n" + wrapped


def mention_entry(
    hit: MentionHit,
    *,
    order: int,
    names: Names,
    web_base: str | None,
    body_limit: int,
    tz: tzinfo | None = None,
) -> str:
    """멘션 하나. `MentionHit.body`는 원문이라 `clean_body`를 거친다 (코어 계약 4)."""
    label = render.inline_safe(hit.task_number)
    who = hit.author_name or render.display_name(hit.author_id, names)
    head = (
        f"### {order}. [{label}] {render.inline_safe(hit.post_subject)} · "
        f"{render.inline_safe(who)} · {render.format_time(hit.comment_created_at, tz)}"
    )
    facts = [f"댓글 {render.inline_safe(hit.comment_id)}"]
    if hit.via_group_id:
        facts.append(f"그룹 멘션(그룹 {render.inline_safe(hit.via_group_id)})")
    url = render.post_url(web_base, hit.post_id)
    if url is not None:
        facts.append(url)
    body = render.clean_body(hit.body, hit.body_mime, web_base=web_base).strip()
    wrapped = render.untrusted_block(
        f"멘션 {order}", render.truncate(body, body_limit) if body else "", notice=False
    )
    return head + "\n- " + " · ".join(facts) + "\n" + wrapped


# --- 표·경고·길이 (§5) -------------------------------------------------------


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """마크다운 표. **모든 칸을 `inline_safe`에 통과시킨다** — 호출부가 잊을 자리를 없앤다.

    `inline_safe`가 `|`를 공백으로 바꾸므로 사용자 문자열이 칸 경계를 넘지 못한다.
    """
    head = [render.inline_safe(str(cell)) for cell in headers]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for row in rows:
        cells = [render.inline_safe(str(cell)) for cell in row]
        cells = (cells + [""] * len(head))[: len(head)]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def warn(lines: Sequence[str]) -> list[str]:
    """경고 줄. 빈 문장은 버린다 — `⚠ `만 남은 줄은 아무것도 알려 주지 않는다."""
    return [WARN_MARK + line for line in lines if line]


def fit(
    head: Sequence[str],
    items: Sequence[str],
    *,
    limit: int,
    drop_first: bool,
    omitted: Callable[[int], str],
) -> str:
    """`fit_counted`의 문자열 부분. 생략 건수가 필요 없는 호출부가 쓴다."""
    return fit_counted(head, items, limit=limit, drop_first=drop_first, omitted=omitted)[0]


def fit_counted(
    head: Sequence[str],
    items: Sequence[str],
    *,
    limit: int,
    drop_first: bool,
    omitted: Callable[[int], str],
) -> tuple[str, int]:
    """길이 상한. 넘치면 멈추고 몇 건을 생략했는지 적는다 (완료 기준 41). 생략 건수도 돌려준다.

    머리 줄이 "N건 중 M건 표시"를 적어야 하는 호출부(`get_thread`)가 그 수를 **값으로** 받는다 —
    생략 줄 콜백의 호출 횟수에 기대면 이 함수의 내부가 바뀔 때 조용히 틀린다(묶음 3 검토).

    `drop_first=True`면 앞(오래된 것)부터 버리고 생략 줄을 머리 바로 뒤에 놓는다 —
    지시서 §4 3단계("초과하면 오래된 댓글부터 생략")가 그 뜻이다.
    머리 길이는 셀 때와 적을 때가 달라질 수 있어 가장 긴 생략 줄로 자리를 잡아 둔다.
    """
    lines = list(head)
    used = sum(len(line) + 1 for line in lines)
    reserved = len(omitted(len(items))) + 2

    order = list(reversed(items)) if drop_first else list(items)
    kept: list[str] = []
    for block in order:
        if used + reserved + len(block) + 2 > limit:
            break
        kept.append(block)
        used += len(block) + 2
    if drop_first:
        kept.reverse()

    dropped = len(items) - len(kept)
    if dropped:
        note = omitted(dropped)
        if drop_first:
            lines.append("")
            lines.append(note)
            lines.extend(_spaced(kept))
        else:
            lines.extend(_spaced(kept))
            lines.append("")
            lines.append(note)
    else:
        lines.extend(_spaced(kept))
    return "\n".join(lines).strip("\n"), dropped


def _spaced(blocks: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for block in blocks:
        lines.append("")
        lines.append(block)
    return lines


def page_hint(page: int, size: int, total: int) -> str:
    """다음 페이지가 있으면 한 줄, 없으면 빈 문자열 (완료 기준 42)."""
    if size <= 0 or (page + 1) * size >= total:
        return ""
    return f"다음 페이지는 page={page + 1}"
