"""자료형과 응답 파서 (001 spec §4).

순수하다 — 요청도 파일도 모르고, 이 패키지의 다른 모듈을 import하지 않는다. `client`가 이것을 쓴다(한쪽 방향).
자료형은 응답 원본 dict를 들고 있지 않다 — 들고 있으면 렌더링이 편법을 쓴다 (§4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


# --- 파서 도우미 -------------------------------------------------------------
#
# 모르는 필드는 무시하고, 없는 값은 키가 빠지므로 전부 `.get()`으로 읽는다 (api-notes §1-3, §2-4).


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _id(value: object) -> str:
    """ID는 언제나 문자열로 감싼다 (api-notes §1-3)."""
    return "" if value is None else str(value)


def _text(value: object) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _count(value: object, default: int = 0) -> int:
    """개수는 문자열로 올 수 있다. `int()`로 감싼다 (api-notes §1-3)."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _time(value: object) -> datetime | None:
    """aware datetime만 돌려준다. naive거나 파싱에 실패하면 `None` (§4)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


# --- 자료형 (§4) -------------------------------------------------------------


@dataclass(frozen=True)
class Member:
    id: str
    name: str | None
    user_code: str | None = None
    email: str | None = None
    organization_id: str | None = None


@dataclass(frozen=True)
class Project:
    id: str
    code: str
    state: str | None
    type: str | None
    scope: str | None


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    workflow_class: str | None
    order: int | None


@dataclass(frozen=True)
class MemberGroup:
    id: str
    members: tuple[Member, ...]

    @property
    def member_ids(self) -> frozenset[str]:
        return frozenset(member.id for member in self.members)


@dataclass(frozen=True)
class FileRef:
    id: str
    name: str | None
    size: int | None


@dataclass(frozen=True)
class Assignee:
    type: str
    member: Member | None
    workflow_id: str | None
    workflow_name: str | None


@dataclass(frozen=True)
class Post:
    id: str
    number: int
    task_number: str
    subject: str
    project_id: str
    project_code: str | None
    created_at: datetime | None
    updated_at: datetime | None
    due_date: datetime | None
    due_date_flag: bool | None
    closed: bool
    priority: str | None
    workflow_id: str | None
    workflow_name: str | None
    workflow_class: str | None
    milestone_name: str | None
    tag_ids: tuple[str, ...]
    author: Member | None
    assignees: tuple[Assignee, ...]
    cc: tuple[Member, ...]
    # 참조 가운데 멤버로 읽히지 않은 항목(그룹·메일 사용자)의 수. `cc`에는 없다 — 목록을 통째로
    # 다시 보내는 쪽이 이것을 모르면 그 항목을 지운다 (012 spec §5-5·§6-2).
    other_cc: int
    parent_id: str | None
    parent_number: int | None
    parent_subject: str | None
    body_mime: str | None
    body: str | None
    files: tuple[FileRef, ...]


@dataclass(frozen=True)
class Comment:
    id: str
    post_id: str
    type: str
    subtype: str | None
    created_at: datetime | None
    author: Member | None
    author_email: str | None
    body_mime: str | None
    body: str


@dataclass(frozen=True)
class FeedComment:
    """활동 피드(`/common/v1/streams`)의 댓글 한 건 (003 spec §5-1).

    `Comment`와 따로 둔다 — 응답 모양이 다르고(`projectId`가 없다) `updatedAt`·`read`가 더 온다.
    프로젝트는 `post_id`로 단건 조회해야 안다 (api-notes "추가 실측").
    """

    id: str
    post_id: str
    kind: str  # `event.type`. `comment`가 사람이 쓴 댓글이고 `event`가 시스템 이벤트다
    created_at: datetime | None
    updated_at: datetime | None
    author_id: str | None
    author_name: str | None
    body: str
    body_mime: str | None
    read: bool | None


@dataclass(frozen=True)
class Tag:
    id: str
    name: str
    color: str | None
    group: str | None = None  # 태그 그룹 이름. 그룹이 없으면 None (api-notes: `tagGroup{id,name,…}|null`)


@dataclass(frozen=True)
class Milestone:
    id: str
    name: str
    status: str | None
    started_at: str | None
    ended_at: str | None


@dataclass(frozen=True)
class Page:
    total_count: int
    page: int
    has_more: bool


# --- 파서 (§4) ---------------------------------------------------------------


def parse_member(data: object) -> Member:
    """`/members/me`와 업무 응답 안의 멤버를 같은 자료형으로 읽는다 (api-notes §2-1)."""
    raw = _as_dict(data)
    organization = _as_dict(raw.get("defaultOrganization"))
    return Member(
        id=_id(raw.get("organizationMemberId") or raw.get("id")),
        name=_text(raw.get("name")),
        user_code=_text(raw.get("userCode")),
        email=_text(raw.get("externalEmailAddress")),
        organization_id=_text(organization.get("id")),
    )


def _member_or_none(data: object) -> Member | None:
    raw = _as_dict(data)
    if not (raw.get("organizationMemberId") or raw.get("id")):
        return None
    return parse_member(raw)


def parse_project(data: object) -> Project:
    raw = _as_dict(data)
    return Project(
        id=_id(raw.get("id")),
        code=_id(raw.get("code")),
        state=_text(raw.get("state")),
        type=_text(raw.get("type")),
        scope=_text(raw.get("scope")),
    )


def parse_workflow(data: object) -> Workflow:
    raw = _as_dict(data)
    order = raw.get("order")
    return Workflow(
        id=_id(raw.get("id")),
        name=_id(raw.get("name")),
        workflow_class=_text(raw.get("class")),
        order=_count(order) if order is not None else None,
    )


def parse_member_group(data: object) -> MemberGroup:
    raw = _as_dict(data)
    members = tuple(
        member
        for member in (_member_or_none(item) for item in _as_list(raw.get("members")))
        if member is not None
    )
    return MemberGroup(id=_id(raw.get("id")), members=members)


def parse_file(data: object) -> FileRef:
    raw = _as_dict(data)
    size = raw.get("size")
    return FileRef(
        id=_id(raw.get("id")),
        name=_text(raw.get("name")),
        size=_count(size) if size is not None else None,
    )


def parse_assignee(data: object) -> Assignee:
    """담당자마다 상태를 따로 가진다 (api-notes §2-4)."""
    raw = _as_dict(data)
    workflow = _as_dict(raw.get("workflow"))
    return Assignee(
        type=_id(raw.get("type")),
        member=_member_or_none(raw.get("member")),
        workflow_id=_text(workflow.get("id")),
        workflow_name=_text(workflow.get("name")),
    )


def parse_post(data: object) -> Post:
    raw = _as_dict(data)
    project = _as_dict(raw.get("project"))
    workflow = _as_dict(raw.get("workflow"))
    milestone = _as_dict(raw.get("milestone"))
    users = _as_dict(raw.get("users"))
    parent = _as_dict(raw.get("parent"))
    body = _as_dict(raw.get("body"))
    parent_number = parent.get("number")
    content = body.get("content")
    cc_items = [_member_or_none(_as_dict(item).get("member")) for item in _as_list(users.get("cc"))]
    return Post(
        id=_id(raw.get("id")),
        number=_count(raw.get("number")),
        task_number=_id(raw.get("taskNumber")),
        subject=_id(raw.get("subject")),
        project_id=_id(project.get("id")),
        project_code=_text(project.get("code")),
        created_at=_time(raw.get("createdAt")),
        updated_at=_time(raw.get("updatedAt")),
        due_date=_time(raw.get("dueDate")),
        due_date_flag=_flag(raw.get("dueDateFlag")),
        closed=bool(raw.get("closed")),
        priority=_text(raw.get("priority")),
        workflow_id=_text(workflow.get("id")),
        workflow_name=_text(workflow.get("name")),
        workflow_class=_text(raw.get("workflowClass")),
        milestone_name=_text(milestone.get("name")),
        tag_ids=tuple(_id(_as_dict(tag).get("id")) for tag in _as_list(raw.get("tags"))),
        author=_member_or_none(_as_dict(users.get("from")).get("member")),
        assignees=tuple(parse_assignee(item) for item in _as_list(users.get("to"))),
        cc=tuple(member for member in cc_items if member is not None),
        other_cc=sum(1 for member in cc_items if member is None),
        parent_id=_text(parent.get("id")),
        parent_number=_count(parent_number) if parent_number is not None else None,
        parent_subject=_text(parent.get("subject")),
        body_mime=_text(body.get("mimeType")),
        body=content if isinstance(content, str) else None,
        files=tuple(parse_file(item) for item in _as_list(raw.get("files"))),
    )


def parse_comment(data: object) -> Comment:
    """`creator.type`으로 작성자를 가른다 (api-notes §2-6)."""
    raw = _as_dict(data)
    creator = _as_dict(raw.get("creator"))
    body = _as_dict(raw.get("body"))
    content = body.get("content")
    kind = creator.get("type")
    return Comment(
        id=_id(raw.get("id")),
        post_id=_id(_as_dict(raw.get("post")).get("id")),
        type=_id(raw.get("type")),
        subtype=_text(raw.get("subtype")),
        created_at=_time(raw.get("createdAt")),
        author=_member_or_none(creator.get("member")) if kind == "member" else None,
        author_email=(
            _text(_as_dict(creator.get("emailUser")).get("emailAddress"))
            if kind == "emailUser"
            else None
        ),
        body_mime=_text(body.get("mimeType")),
        body=content if isinstance(content, str) else "",
    )


def parse_feed_comment(data: object) -> FeedComment:
    """피드 항목의 **안쪽**(`{"type": "event", "event": {…}}`의 `event`)을 읽는다.

    작성자 이름이 응답에 같이 온다 — 이름을 얻으려고 멤버를 따로 묻지 않는다 (003 spec §5-1).
    """
    raw = _as_dict(data)
    creator = _as_dict(raw.get("creator"))
    member = (
        _member_or_none(creator.get("member")) if creator.get("type") == "member" else None
    )
    body = _as_dict(raw.get("body"))
    content = body.get("content")
    return FeedComment(
        id=_id(raw.get("id")),
        post_id=_id(raw.get("postId")),
        kind=_id(raw.get("type")),
        created_at=_time(raw.get("createdAt")),
        updated_at=_time(raw.get("updatedAt")),
        author_id=member.id if member is not None else None,
        author_name=member.name if member is not None else None,
        body=content if isinstance(content, str) else "",
        body_mime=_text(body.get("mimeType")),
        read=_flag(raw.get("read")),
    )


def _feed_item(data: object) -> tuple[str, dict]:
    """피드 항목은 `{"type": T, T: {…}}` 모양이다 (api-notes "추가 실측")."""
    raw = _as_dict(data)
    kind = _id(raw.get("type"))
    return kind, _as_dict(raw.get(kind))


def parse_tag(data: object) -> Tag:
    raw = _as_dict(data)
    return Tag(
        id=_id(raw.get("id")),
        name=_id(raw.get("name")),
        color=_text(raw.get("color")),
        group=_text(_as_dict(raw.get("tagGroup")).get("name")),
    )


def parse_milestone(data: object) -> Milestone:
    """날짜에 시각이 없어 파서가 실패할 수 있다. 문자열 그대로 둔다 (api-notes §2-3, U-3)."""
    raw = _as_dict(data)
    return Milestone(
        id=_id(raw.get("id")),
        name=_id(raw.get("name")),
        status=_text(raw.get("status")),
        started_at=_text(raw.get("startedAt")),
        ended_at=_text(raw.get("endedAt")),
    )


def parse_page(data: object, *, page: int, size: int) -> Page:
    """`totalCount`로 다음 페이지가 있는지 센다 (api-notes §1-5)."""
    total = _count(_as_dict(data).get("totalCount"))
    return Page(total_count=total, page=page, has_more=(page + 1) * size < total)
