"""두레이로 나가는 쓰기 여덟 (010 spec §4, 012 spec §5-4).

이 모듈을 import하는 곳은 `mcp_server`의 쓰기 도구 등록부 **하나뿐이다** (제약 4, ADR 007).
읽기 경로는 이 파일을 import하지 않고, 코드 검색 한 번으로 그것이 증명된다 (§7-2).
의존 방향은 한쪽이다 — 이 파일이 `client`를 쓰고, `client`는 이 파일을 모른다.

쓰기는 **여덟 요청뿐이다**: 업무 댓글 등록, 업무 만들기(내 개인 프로젝트나 지정한 프로젝트 — ADR 008·018), 상위 업무 지정·해제 (ADR 008),
업무 수정, 댓글 수정, 업무 전체의 상태, 담당자 한 사람의 상태, 완료 처리 (ADR 012).
댓글·업무 삭제, `move`, 파일, 멤버·태그·마일스톤·프로젝트 목록 자체의 변경,
훅 등록은 구현하지 않는다 (제약 3).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx2

from .client import Transport, path_id
from .client import Pacer  # 속도 제한을 Reader와 나눠 쓴다 (003 spec §7-1)

# 댓글 본문과 업무 본문에 같은 값을 쓴다. 인자로 열지 않는다 (010 spec §4-3).
COMMENT_MIME = "text/x-markdown"

# 고치기가 받는 본문 형식 (api-notes §2-5). 원래 글의 형식을 그대로 돌려보낸다 — 그 밖의 값은 보내지 않는다.
MIME_TYPES = frozenset({"text/x-markdown", "text/html"})
# 업무의 우선순위 [문서]. 문서의 `hightest`는 오타로 본다 (api-notes 012).
PRIORITIES = ("highest", "high", "normal", "low", "lowest", "none")


@dataclass(frozen=True)
class PostChanges:
    """업무 수정에 실을 것. None·False면 그 키를 보내지 않는다 — 맨 위 키는 보낸 것만 바뀐다 (012 실측).

    담당(`assignee_ids`)과 참조(`cc_ids`)는 **함께만** 준다. `users` 안쪽은 통째로 바뀌어,
    한쪽만 보내면 다른 쪽이 지워진다 (api-notes 012 실측 둘째).
    """

    subject: str | None = None
    body: tuple[str, str] | None = None  # (mimeType, content)
    assignee_ids: tuple[str, ...] | None = None  # users.to — 멤버 ID
    cc_ids: tuple[str, ...] | None = None  # users.cc — 멤버 ID
    due: str | None = None  # ISO 시각
    clear_due: bool = False  # {"dueDate": null}
    milestone_id: str | None = None
    clear_milestone: bool = False  # {"milestoneId": null}
    tag_ids: tuple[str, ...] | None = None  # () = 태그를 모두 뗀다
    priority: str | None = None


def _members(ids: tuple[str, ...]) -> list[dict]:
    """`users.to`·`users.cc`의 항목. 우리는 멤버 모양만 만든다 (012 spec §6-2)."""
    return [
        {"type": "member", "member": {"organizationMemberId": path_id(one)}} for one in ids
    ]


def _check_content(mime: str, content: str) -> None:
    if mime not in MIME_TYPES:
        raise ValueError("본문 형식이 정해진 둘(마크다운, HTML)이 아닙니다.")
    if not content.strip():
        raise ValueError("본문이 비어 있습니다.")


def _post_payload(changes: PostChanges) -> dict[str, object]:
    """`PostChanges` → 업무 수정 본문. **준 것만** 담는다. 요청을 보내기 전에 검사한다 (012 spec §5-4).

    본문에 실리는 ID(`users`·`milestoneId`·`tagIds`)도 경로의 ID처럼 `path_id()`를 거친다.
    """
    payload: dict[str, object] = {}
    if changes.subject is not None:
        if not changes.subject.strip():
            raise ValueError("업무 제목이 비어 있습니다.")
        payload["subject"] = changes.subject
    if changes.body is not None:
        mime, content = changes.body
        _check_content(mime, content)
        payload["body"] = {"mimeType": mime, "content": content}
    if (changes.assignee_ids is None) != (changes.cc_ids is None):
        raise ValueError("담당과 참조는 함께 보냅니다. 한쪽만 보내면 다른 쪽이 지워집니다.")
    if changes.assignee_ids is not None and changes.cc_ids is not None:
        to = _members(changes.assignee_ids)
        cc = _members(changes.cc_ids)
        # 도구가 먼저 뺀다 — 여기는 둘째 관문이다. 함께 보내면 담당만 남는다 (실측 둘째).
        if set(changes.assignee_ids) & set(changes.cc_ids):
            raise ValueError("같은 사람이 담당과 참조에 함께 있습니다.")
        payload["users"] = {"to": to, "cc": cc}
    if changes.due is not None and changes.clear_due:
        raise ValueError("마감을 정하면서 없앨 수는 없습니다.")
    if changes.due is not None:
        payload["dueDate"] = changes.due
        payload["dueDateFlag"] = True  # 문서 권장값. `dueDate`와 **함께만** 나간다
    elif changes.clear_due:
        payload["dueDate"] = None
    if changes.milestone_id is not None and changes.clear_milestone:
        raise ValueError("마일스톤을 정하면서 뗄 수는 없습니다.")
    if changes.milestone_id is not None:
        payload["milestoneId"] = path_id(changes.milestone_id)
    elif changes.clear_milestone:
        payload["milestoneId"] = None
    if changes.tag_ids is not None:
        payload["tagIds"] = [path_id(one) for one in changes.tag_ids]
    if changes.priority is not None:
        if changes.priority not in PRIORITIES:
            raise ValueError("우선순위가 정해진 값이 아닙니다.")
        payload["priority"] = changes.priority
    if not payload:
        raise ValueError("바꿀 것이 없습니다.")
    return payload


@dataclass(frozen=True)
class PostedComment:
    id: str
    post_id: str
    project_id: str


@dataclass(frozen=True)
class CreatedPost:
    """생성 응답에는 `id`(와 `milestone`)만 온다 — 업무 번호는 호출부가 단건 조회로 얻는다."""

    id: str
    project_id: str


@dataclass(frozen=True)
class MovedPost:
    post_id: str
    project_id: str
    parent_post_id: str | None  # None이면 뗐다


class DoorayWriter:
    """두레이로 나가는 유일한 쓰기 경로 (§7).

    `@dataclass`로 만들지 않는다 — 자동 생성된 `__repr__`이 필드를 찍는다 (§2-2의 5).
    `DoorayReader`를 상속하지 않고 공통 조상도 없다. 둘 다 `Transport`를 하나씩 가진다 (§7-2).
    """

    def __init__(
        self,
        token: str,
        api_base: str,
        *,
        tool: str,
        call_limit: int = 5,
        http_transport: httpx2.BaseTransport | None = None,
        audit_path: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        pacer: Pacer | None = None,
    ) -> None:
        # 쓰기 허용이 켜지는 곳은 저장소에서 이 한 줄뿐이다 (§5-2, 완료 기준 13).
        self._t = Transport(
            token,
            api_base,
            tool=tool,
            allow_write=True,
            call_limit=call_limit,
            http_transport=http_transport,
            audit_path=audit_path,
            sleep=sleep,
            clock=clock,
            pacer=pacer,
        )

    @property
    def calls(self) -> int:
        """보낸 요청 수. `DoorayReader.calls`와 대칭이고 읽기 전용이다.

        002의 도구 단위 감사 로그가 읽기와 쓰기 호출을 합쳐 세는 데 쓴다 (002 spec §0의 C5).
        """
        return self._t.calls

    def post_comment(self, *, project_id: str, post_id: str, content: str) -> PostedComment:
        """업무에 댓글을 하나 등록한다 (api-notes §3). 1회 호출.

        `mimeType`을 인자로 열지 않는다 — `text/x-markdown` 고정이다.
        내용이 비어 있으면 요청을 보내지 않는다.
        등록된 댓글 ID는 전송 계층이 감사 로그의 `comment_id`에 적는다 (제약 7).
        """
        if not content.strip():
            raise ValueError("등록할 댓글 내용이 비어 있습니다.")

        data = self._t.request(
            "POST",
            f"/project/v1/projects/{path_id(project_id)}/posts/{path_id(post_id)}/logs",
            body={"body": {"content": content, "mimeType": COMMENT_MIME}},
            project_id=str(project_id),
            post_id=str(post_id),
        )
        result = data.get("result")
        created = result.get("id") if isinstance(result, dict) else None
        return PostedComment(
            id="" if created is None else str(created),
            post_id=str(post_id),
            project_id=str(project_id),
        )

    def create_post(
        self,
        *,
        project_id: str,
        subject: str,
        content: str,
        parent_post_id: str | None = None,
        due_date: str | None = None,
        tag_ids: Sequence[str] = (),
    ) -> CreatedPost:
        """업무 하나를 만든다 (api-notes 010 실측 ①). 1회 호출.

        본문에는 **우리가 보내는 키만** 담는다. 값이 없는 선택 키는 `null`로도 넣지 않는다 —
        실측에서 200을 받은 본문에 그 키들이 없었다.
        `users`(담당·참조)·`milestoneId`·`priority`·`attachFileIds`는 **인자조차 두지 않는다** —
        남에게 알림이 가는 길과 바꿀 수 있는 필드를 늘리지 않는다 (ADR 008 결정 3).
        본문에 실리는 `parentPostId`·`tagIds`도 경로의 ID와 같이 `path_id()`를 거친다 —
        몸통에 들었다고 검사를 건너뛰면 `path_id()`의 뜻이 반쪽이 된다.
        `dueDate`·`dueDateFlag`·`tagIds`의 조합은 문서에서만 왔다 [실측 필요] — 틀리면 400이고
        업무는 만들어지지 않는다.
        """
        if not subject.strip():
            raise ValueError("업무 제목이 비어 있습니다.")
        if not content.strip():
            raise ValueError("업무 본문이 비어 있습니다.")

        payload: dict[str, object] = {}
        if parent_post_id is not None:
            payload["parentPostId"] = path_id(parent_post_id)
        payload["subject"] = subject
        payload["body"] = {"mimeType": COMMENT_MIME, "content": content}
        if due_date is not None:
            payload["dueDate"] = due_date
            payload["dueDateFlag"] = True  # 문서 권장값. `dueDate`와 **함께만** 나간다
        if tag_ids:
            payload["tagIds"] = [path_id(one) for one in tag_ids]

        data = self._t.request(
            "POST",
            f"/project/v1/projects/{path_id(project_id)}/posts",
            body=payload,
            project_id=str(project_id),
        )
        result = data.get("result")
        # `milestone`도 같이 오지만 읽지 않는다 — 쓰지 않을 값이다.
        created = result.get("id") if isinstance(result, dict) else None
        return CreatedPost(
            id="" if created is None else str(created), project_id=str(project_id)
        )

    def set_parent_post(
        self, *, project_id: str, post_id: str, parent_post_id: str | None
    ) -> MovedPost:
        """업무 하나의 상위 업무를 지정하거나 뗀다 (api-notes 010 실측 ②③). 1회 호출.

        전용 엔드포인트라 **바뀌는 것은 상위 업무 하나뿐이다** (ADR 008 7′).
        본문의 키는 언제나 하나이고, 떼기는 그 값이 `None`이다(실측 ③: 200).
        """
        target = None if parent_post_id is None else path_id(parent_post_id)
        data = self._t.request(
            "POST",
            f"/project/v1/projects/{path_id(project_id)}/posts/{path_id(post_id)}/set-parent-post",
            body={"parentPostId": target},
            project_id=str(project_id),
            post_id=str(post_id),
        )
        result = data.get("result")
        moved = result if isinstance(result, dict) else {}
        post = moved.get("post")
        project = moved.get("project")
        # 응답이 모양을 안 지키면 **우리가 보낸 값**을 담는다. 감사 로그는 전송 계층이 이미 적었다.
        answered_post = post.get("id") if isinstance(post, dict) else None
        answered_project = project.get("id") if isinstance(project, dict) else None
        return MovedPost(
            post_id=str(answered_post) if answered_post else str(post_id),
            project_id=str(answered_project) if answered_project else str(project_id),
            parent_post_id=target,
        )

    # --- 고치기 (012 spec §5-4) -------------------------------------------------
    # 다섯 모두 1회 호출이고 응답을 읽지 않는다 — 응답의 `result`는 `null`이다 [문서·실측 둘째·셋째].
    # 성공은 예외가 없는 것이다. 감사 로그의 api 줄은 전송 계층이 적는다(만든 항목이 없어 `comment_id`는 null).

    def update_post(self, *, project_id: str, post_id: str, changes: PostChanges) -> None:
        """업무 하나를 고친다. `changes`에 준 키만 보낸다 (api-notes 012 실측)."""
        payload = _post_payload(changes)
        self._t.request(
            "PUT",
            f"/project/v1/projects/{path_id(project_id)}/posts/{path_id(post_id)}",
            body=payload,
            project_id=str(project_id),
            post_id=str(post_id),
        )

    def update_comment(
        self, *, project_id: str, post_id: str, comment_id: str, mime: str, content: str
    ) -> None:
        """댓글 하나의 본문을 고친다. `mimeType`은 필수다 — 원래 글의 형식을 그대로 받는다 (api-notes 012 실측)."""
        _check_content(mime, content)
        self._t.request(
            "PUT",
            f"/project/v1/projects/{path_id(project_id)}/posts/{path_id(post_id)}/logs/{path_id(comment_id)}",
            body={"body": {"mimeType": mime, "content": content}},
            project_id=str(project_id),
            post_id=str(post_id),
        )

    def set_workflow(self, *, project_id: str, post_id: str, workflow_id: str) -> None:
        """업무 전체의 상태를 바꾼다. 담당자별 상태도 같이 바뀐다(담당 한 명일 때 실측 둘째)."""
        self._t.request(
            "POST",
            f"/project/v1/projects/{path_id(project_id)}/posts/{path_id(post_id)}/set-workflow",
            body={"workflowId": path_id(workflow_id)},
            project_id=str(project_id),
            post_id=str(post_id),
        )

    def set_assignee_workflow(
        self, *, project_id: str, post_id: str, member: str, workflow_id: str
    ) -> None:
        """담당자 한 사람의 상태를 바꾼다. `member`는 `me`나 멤버 ID다 — `path_id()`를 지난다.

        담당이 아닌 사람이면 두레이가 200을 주고 아무것도 바꾸지 않는다(실측 둘째) — 확인은 호출부가 먼저 한다.
        """
        self._t.request(
            "PUT",
            f"/project/v1/projects/{path_id(project_id)}/posts/{path_id(post_id)}/to/{path_id(member)}",
            body={"workflowId": path_id(workflow_id)},
            project_id=str(project_id),
            post_id=str(post_id),
        )

    def set_done(self, *, project_id: str, post_id: str) -> None:
        """업무를 완료 처리한다. **본문이 없다** — 전송 계층이 본문 있는 요청을 막는다 (api-notes 012)."""
        self._t.request(
            "POST",
            f"/project/v1/projects/{path_id(project_id)}/posts/{path_id(post_id)}/set-done",
            body=None,
            project_id=str(project_id),
            post_id=str(post_id),
        )

    def close(self) -> None:
        self._t.close()
