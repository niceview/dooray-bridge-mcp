"""spec §10(`collect.py`) — §11-3의 X 케이스.

`collect.py`의 "활동 피드 방식" 절의 표를 줄 하나씩 증명한다.
가짜 전송 계층으로만 돈다.
"""

from __future__ import annotations

import copy
import inspect
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx2

from dooray_core import client, collect, config, models
from dooray_core.errors import AuthError, CallLimitError, ForbiddenError, ServerError
from support import FAKE_TOKEN, CoreTestCase, load_fixture, mock_transport

KST = timezone(timedelta(hours=9))
BASE = config.ALLOWED_API_BASES[0]

PROJECTS = "/project/v1/projects"
PID = "1000000000000000018"
POST_ID = "1000000000000000026"
MY_ID = "1000000000000000001"
GROUP_ID = "1000000000000000019"
TENANT = "1000000000000000052"

LATER = datetime(2026, 9, 19, tzinfo=KST)


def logs_path(project_id: str, post_id: str) -> str:
    return f"{PROJECTS}/{project_id}/posts/{post_id}/logs"


def group_path(project_id: str, group_id: str) -> str:
    return f"{PROJECTS}/{project_id}/member-groups/{group_id}"


def reply(result: object, total: int | None = None) -> dict:
    rows = result if isinstance(result, list) else [result]
    return {
        "header": {"isSuccessful": True},
        "result": result,
        "totalCount": len(rows) if total is None else total,
    }


def project_row(project_id: str, code: str = "sample") -> dict:
    return {
        "id": project_id,
        "code": code,
        "state": "active",
        "type": "public",
        "scope": "private",
    }


def post_row(project_id: str, post_id: str, number: int = 5, code: str = "sample") -> dict:
    return {
        "id": post_id,
        "number": number,
        "taskNumber": f"{code}/{number}",
        "subject": "업무 제목",
        "project": {"id": project_id, "code": code},
        "createdAt": "2026-09-01T00:00:00Z",
        "updatedAt": "2026-09-19T06:49:32Z",
        "closed": False,
        "workflowClass": "working",
        "workflow": {"id": "1000000000000000009", "name": "진행"},
        "tags": [],
        "users": {
            "from": {
                "type": "member",
                "member": {"organizationMemberId": "1000000000000000003", "name": "멤버2"},
            },
            "to": [],
            "cc": [],
        },
    }


def comment_row(
    comment_id: str,
    body: str,
    *,
    author: str = "1000000000000000003",
    created: str = "2026-09-19T09:00:00+09:00",
    kind: str = "comment",
    post_id: str = POST_ID,
) -> dict:
    return {
        "id": comment_id,
        "post": {"id": post_id},
        "type": kind,
        "subtype": "general",
        "createdAt": created,
        "creator": {"type": "member", "member": {"organizationMemberId": author}},
        "body": {"mimeType": "text/x-markdown", "content": body},
    }


def mention_text(member_id: str = MY_ID) -> str:
    return f'본문 [@멤버1](dooray://{TENANT}/members/{member_id} "member")'


def group_text(group_id: str = GROUP_ID) -> str:
    return f"본문 [@그룹1](dooray://{TENANT}/member-groups/{group_id})"


def gated_transport(
    routes: dict, gate: threading.Barrier, needle: str
) -> httpx2.MockTransport:
    """`needle`이 든 경로의 요청을 장벽에 세운다 (003 완료 기준 25).

    순차로 돌면 장벽이 시간 초과로 깨지고 그 예외가 테스트를 실패시킨다 — 실제 `sleep`이나
    시계에 기대지 않고 "동시에 나갔다"를 재는 방법이다.
    """
    inner = mock_transport(routes)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if needle in request.url.path:
            gate.wait()
        return inner.handler(request)

    transport = httpx2.MockTransport(handler)
    transport.requests = inner.requests  # type: ignore[attr-defined]
    return transport


def boom_transport(routes: dict, broken: set[str]) -> httpx2.MockTransport:
    """정해진 경로에서 연결이 끊기는 가짜 전송 계층."""
    inner = mock_transport(routes)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path in broken:
            inner.requests.append(request)  # type: ignore[attr-defined]
            raise httpx2.ConnectError("연결 실패", request=request)
        return inner.handler(request)

    transport = httpx2.MockTransport(handler)
    transport.requests = inner.requests  # type: ignore[attr-defined]
    return transport


class CollectCase(CoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sleep = lambda seconds: None
        self.enterContext(mock.patch.object(client, "_audit_swept", False))
        self.enterContext(mock.patch.object(client, "_audit_warned", False))

    def make_reader(self, routes, **kwargs):
        http = routes if isinstance(routes, httpx2.MockTransport) else mock_transport(routes)
        reader = client.DoorayReader(
            FAKE_TOKEN, BASE, tool="test", http_transport=http, sleep=self.sleep, **kwargs
        )
        self.addCleanup(reader.close)
        return reader, http

    def paths(self, http) -> list[str]:
        return [request.url.path for request in http.requests]


# --- 활동 피드 방식 (003 spec §5-3) ------------------------------------------

STREAMS = "/common/v1/streams"
POST_PATH = "/project/v1/posts"
# `streams.json`이 가리키는 업무 셋. 026·028에만 내 멘션이 있다.
FEED_POSTS = {
    "1000000000000000026": ("1000000000000000018", "sample", 5),
    "1000000000000000027": ("1000000000000000019", "plan", 6),
    "1000000000000000028": ("1000000000000000020", "other", 7),
}
FEED_SINCE = datetime(2026, 9, 14, tzinfo=KST)  # fixture 13건보다 모두 오래됐다


def feed_page(items: list[dict], cursor: str | None = "커서1") -> dict:
    page: dict = {"header": {"isSuccessful": True}, "result": items}
    if cursor is not None:
        page["cursor"] = cursor
    return page


def feed_event(
    event_id: str,
    body: str,
    *,
    author: str = "1000000000000000203",
    created: str = "2026-09-18T18:10:00+09:00",
    post_id: str = "1000000000000000028",
    mime: str = "text/x-markdown",
) -> dict:
    """fixture에 없는 모양(그룹 멘션 여러 개 등)을 합성한다."""
    return {
        "type": "event",
        "event": {
            "read": True,
            "id": event_id,
            "type": "comment",
            "postId": post_id,
            "creator": {
                "type": "member",
                "member": {"organizationMemberId": author, "name": "멤버33"},
            },
            "body": {"mimeType": mime, "content": body},
            "createdAt": created,
            "updatedAt": created,
        },
    }


class FeedCase(CollectCase):
    """`collect_my_mentions_from_feed`의 공통 장치. 프로젝트 목록 경로를 아예 열지 않는다."""

    def feed_routes(self, pages: list | None = None, *, group: object | None = None) -> dict:
        table: dict = {
            ("GET", STREAMS): pages
            or [(200, load_fixture("streams"), None), (200, feed_page([], None), None)]
        }
        for post_id, (project_id, code, number) in FEED_POSTS.items():
            table[("GET", f"{POST_PATH}/{post_id}")] = (
                200,
                reply(post_row(project_id, post_id, number=number, code=code)),
                None,
            )
        group_project = FEED_POSTS["1000000000000000028"][0]
        table[("GET", group_path(group_project, GROUP_ID))] = group or (
            200,
            load_fixture("member_group_other_project"),
            None,
        )
        return table

    def feed_scan(self, reader, **kwargs) -> collect.FeedScan:
        kwargs.setdefault("my_id", MY_ID)
        kwargs.setdefault("since", FEED_SINCE)
        return collect.collect_my_mentions_from_feed(reader, **kwargs)

    def tails(self, scan: collect.FeedScan) -> list[str]:
        return [hit.comment_id[-3:] for hit in scan.hits]


class FeedWholePathTest(FeedCase):
    """완료 기준 10 — `streams.json` 13건을 손으로 센 결과와 맞춘다.

    hit 넷: …305(멤버 멘션) …308(그룹 멘션) …309(멤버 멘션) …312(html 멘션).
    hit이 아닌 아홉: 시스템 이벤트 2(…301 …311) · `post` 항목 1 · 내가 쓴 댓글 2(…306 …307) ·
    멘션 없는 댓글 3(…302 …303 …304) · 남만 멘션한 댓글 1(…310).
    """

    def test_기준10_the_hits_are_the_four_comments_that_called_me(self) -> None:
        reader, _ = self.make_reader(self.feed_routes())
        scan = self.feed_scan(reader)

        # 오래된 순이다: 312(09-15) 309(09-16) 308(09-17) 305(09-18).
        self.assertEqual(self.tails(scan), ["312", "309", "308", "305"])
        self.assertTrue(scan.complete)
        self.assertTrue(scan.reached_since)
        self.assertFalse(scan.unresolved_groups)
        self.assertEqual(scan.unread_posts, 0)
        self.assertIsNone(scan.stopped_reason)

    def test_기준10_nothing_else_in_the_fixture_becomes_a_hit(self) -> None:
        reader, _ = self.make_reader(self.feed_routes())
        scan = self.feed_scan(reader)
        for tail in ("301", "302", "303", "304", "306", "307", "310", "311"):
            with self.subTest(comment=tail):
                self.assertNotIn(tail, self.tails(scan))

    def test_기준10_a_post_is_looked_up_once_and_only_when_needed(self) -> None:
        reader, http = self.make_reader(self.feed_routes())
        scan = self.feed_scan(reader)
        asked = [r.url.path for r in http.requests if r.url.path.startswith(POST_PATH)]

        # hit이 걸린 업무 둘뿐이다. …027(멘션 없는 댓글만 있는 업무)은 묻지 않는다.
        self.assertEqual(
            sorted(asked),
            [f"{POST_PATH}/1000000000000000026", f"{POST_PATH}/1000000000000000028"],
        )
        # 2(피드) + 2(업무) + 1(그룹). 이름 조회도 프로젝트 목록도 없다.
        self.assertEqual(scan.calls_used, 5)
        self.assertEqual(
            [r.url.path for r in http.requests if r.url.path.startswith(PROJECTS)],
            [group_path(FEED_POSTS["1000000000000000028"][0], GROUP_ID)],
        )

    def test_기준10_a_hit_carries_the_task_info_from_the_post(self) -> None:
        reader, _ = self.make_reader(self.feed_routes())
        scan = self.feed_scan(reader)
        hit = next(h for h in scan.hits if h.comment_id.endswith("309"))

        self.assertEqual(hit.post_id, "1000000000000000026")
        self.assertEqual(hit.project_id, "1000000000000000018")
        self.assertEqual(hit.project_code, "sample")
        self.assertEqual(hit.task_number, "sample/5")
        self.assertEqual(hit.post_subject, "업무 제목")
        self.assertEqual(hit.post_number, 5)
        self.assertEqual(hit.comment_created_at, datetime(2026, 9, 16, 14, 10, tzinfo=KST))
        self.assertEqual(hit.author_id, "1000000000000000200")
        self.assertEqual(hit.author_name, "멤버30", "피드 응답의 이름이다. 추가 호출 0회")
        self.assertIsNone(hit.via_group_id)

    def test_기준10_the_html_hit_keeps_its_mime_and_raw_body(self) -> None:
        reader, _ = self.make_reader(self.feed_routes())
        scan = self.feed_scan(reader)
        hit = next(h for h in scan.hits if h.comment_id.endswith("312"))

        self.assertEqual(hit.body_mime, "text/html")
        self.assertIn("data-dooray-href", hit.body, "본문은 원문 그대로다")
        self.assertEqual(hit.project_code, "other")

    def test_기준10_the_group_hit_records_the_group_it_came_through(self) -> None:
        reader, _ = self.make_reader(self.feed_routes())
        scan = self.feed_scan(reader)
        hit = next(h for h in scan.hits if h.comment_id.endswith("308"))
        self.assertEqual(hit.via_group_id, GROUP_ID)

    def test_기준10_no_member_lookup_is_needed(self) -> None:
        reader, http = self.make_reader(self.feed_routes())
        scan = self.feed_scan(reader)
        self.assertEqual([r for r in self.paths(http) if "/members/" in r], [])
        self.assertEqual(dict(scan.member_names)["1000000000000000200"], "멤버30")

    def test_the_scan_time_is_taken_before_the_first_call(self) -> None:
        fixed = datetime(2026, 9, 20, tzinfo=KST)
        reader, _ = self.make_reader(self.feed_routes())
        scan = self.feed_scan(reader, now=fixed)
        self.assertEqual(scan.scanned_at, fixed)


class FeedParallelTest(FeedCase):
    """003 완료 기준 25 — 업무 단건을 한꺼번에 미리 읽는다 (spec §7-3의 2).

    hit·경고·호출 수는 순차 때와 같아야 한다. 달라지는 것은 요청이 나가는 순서와 시간뿐이다.
    """

    def test_기준25_the_post_lookups_go_out_at_the_same_time(self) -> None:
        # 멘션이 걸린 서로 다른 업무는 둘(…026 …028)이다. 순차면 장벽이 깨진다.
        gate = threading.Barrier(2, timeout=10)
        reader, http = self.make_reader(
            gated_transport(self.feed_routes(), gate, f"{POST_PATH}/")
        )
        scan = self.feed_scan(reader)

        self.assertEqual(self.tails(scan), ["312", "309", "308", "305"])
        self.assertTrue(scan.complete)
        self.assertEqual(scan.calls_used, 5, "호출 수는 순차 때와 같다")

    def test_기준25_the_feed_pages_still_come_before_the_batch(self) -> None:
        # 묶음 **밖**의 순서는 그대로다: 피드가 먼저, 그다음이 업무 묶음, 그룹 조회는 순차로 남는다.
        reader, http = self.make_reader(self.feed_routes())
        self.feed_scan(reader)

        paths = self.paths(http)
        self.assertEqual(paths[:2], [STREAMS, STREAMS])
        self.assertEqual(
            sorted(paths[2:4]),
            [f"{POST_PATH}/1000000000000000026", f"{POST_PATH}/1000000000000000028"],
        )
        self.assertEqual(
            paths[4:], [group_path(FEED_POSTS["1000000000000000028"][0], GROUP_ID)]
        )

    def test_기준25_a_comment_that_needs_no_post_is_not_prefetched(self) -> None:
        # 멤버 멘션도 그룹 멘션도 없는 댓글에는 업무 조회를 쓰지 않는다 — 미리 읽기도 마찬가지다.
        others = feed_event("1000000000000000401", mention_text("1000000000000000012"))
        reader, http = self.make_reader(
            self.feed_routes(pages=[(200, feed_page([others], None), None)])
        )
        scan = self.feed_scan(reader)

        self.assertEqual(scan.hits, ())
        self.assertEqual(self.paths(http), [STREAMS])

    def test_기준25_the_prefetch_leaves_room_for_the_group_lookups(self) -> None:
        # 미리 읽기가 그룹 조회의 몫까지 써 버리면 순차 때와 결과가 달라진다.
        # 상한 5: 피드 2 + 업무 …028 1 + 그룹 1 뒤에 여유가 모자라 …026을 못 읽는다.
        reader, _ = self.make_reader(self.feed_routes(), call_limit=5)
        scan = self.feed_scan(reader)

        self.assertEqual(scan.stopped_reason, collect.CALL_LIMIT_REASON)
        self.assertEqual(
            [hit.comment_id[-3:] for hit in scan.hits if not hit.task_number], ["309"]
        )


class FeedStormTest(FeedCase):
    """리뷰 H1·M1·M2 — 오류 경로의 **요청 수**를 못 박는다. 행복 경로만 세면 증폭이 안 보인다."""

    def storm(self, status: int):
        items = [
            feed_event(f"10000000000000005{n:02d}", mention_text(MY_ID), post_id=f"10000000000000006{n:02d}")
            for n in range(10)
        ]
        routes = self.feed_routes(pages=[(200, feed_page(items, None), None)])
        for n in range(10):
            routes[("GET", f"{POST_PATH}/10000000000000006{n:02d}")] = (
                status,
                load_fixture("error_404"),
                None,
            )
        return self.make_reader(routes)

    def test_a_rate_limited_batch_does_not_pour_the_rest_out(self) -> None:
        # 업무 10건이 전부 429. 묶음의 첫 실패 뒤에 **아직 시작하지 않은** 조회는 나가지 않는다.
        # 상한: 피드 1 + 동시에 돌던 워커 4 × (1 + 재시도 3) = 17. 막기 전에는 41회였다(순차는 5회).
        reader, http = self.storm(429)
        scan = self.feed_scan(reader)
        self.assertLessEqual(len(http.requests), 1 + client.PARALLEL_WORKERS * 4)
        self.assertEqual(scan.stopped_reason, "RateLimitError")
        self.assertFalse(scan.complete)

    def test_a_server_error_is_reported_as_itself_not_as_the_call_limit(self) -> None:
        # 리뷰 M1. 미리 읽기가 예산을 다 태우면 "두레이가 아팠다"가 "내 상한이 작다"로 바뀐다.
        reader, http = self.storm(500)
        scan = self.feed_scan(reader)
        self.assertEqual(scan.stopped_reason, "ServerError")
        self.assertLessEqual(len(http.requests), 1 + client.PARALLEL_WORKERS * 4)
        self.assertFalse(scan.complete)


class FeedIncompleteTest(FeedCase):
    """완료 기준 11 — 판정하지 못한 것이 결과에 드러난다."""

    def test_기준11_an_unreadable_post_does_not_throw_the_mention_away(self) -> None:
        routes = self.feed_routes()
        routes[("GET", f"{POST_PATH}/1000000000000000026")] = (403, {"header": {}}, None)
        reader, _ = self.make_reader(routes)
        scan = self.feed_scan(reader)

        hit = next(h for h in scan.hits if h.comment_id.endswith("309"))
        self.assertEqual(hit.task_number, "")
        self.assertEqual(hit.post_subject, "")
        self.assertEqual(hit.project_id, "")
        self.assertIsNone(hit.project_code)
        self.assertEqual(hit.post_id, "1000000000000000026", "링크를 만들 postId는 남는다")
        self.assertEqual(scan.unread_posts, 1)
        self.assertFalse(scan.complete)
        self.assertIsNone(scan.stopped_reason, "오류가 아니다. 그 업무만 못 읽었다")
        self.assertEqual(self.tails(scan), ["312", "309", "308", "305"])

    def test_M3_running_out_on_a_group_mention_does_not_drop_the_later_member_mentions(self) -> None:
        # 003 리뷰 M3. 상한 3: 피드 2회 뒤 여유 1 < 2. 맨 앞 …305(멤버)는 업무를 못 채우고,
        # …308(그룹)은 업무 조회를 못 해 **판정 불가**, 그 뒤 …309·…312(멤버)는 호출 없이 판정된다.
        reader, _ = self.make_reader(self.feed_routes(), call_limit=3)
        scan = self.feed_scan(reader)
        self.assertEqual(self.tails(scan), ["312", "309", "305"])
        self.assertEqual(scan.stopped_reason, collect.CALL_LIMIT_REASON)
        self.assertTrue(scan.unresolved_groups)
        self.assertFalse(scan.complete)

    def test_M4_a_post_that_answers_400_does_not_sink_the_whole_scan(self) -> None:
        # 003 리뷰 M4. 피드는 여러 프로젝트의 업무를 섞어 준다 — 업무 하나의 4xx가 멘션 전부를 지우면 안 된다.
        routes = self.feed_routes()
        routes[("GET", f"{POST_PATH}/1000000000000000026")] = (400, load_fixture("error_400"), None)
        reader, _ = self.make_reader(routes)
        scan = self.feed_scan(reader)
        self.assertEqual(self.tails(scan), ["312", "309", "308", "305"])
        self.assertGreaterEqual(scan.unread_posts, 1)
        self.assertFalse(scan.complete)
        self.assertIsNone(scan.stopped_reason)

    def test_M4_an_auth_error_on_a_post_still_rises(self) -> None:
        routes = self.feed_routes()
        routes[("GET", f"{POST_PATH}/1000000000000000026")] = (401, load_fixture("error_400"), None)
        reader, _ = self.make_reader(routes)
        with self.assertRaises(AuthError) as caught:
            self.feed_scan(reader)
        self.record(caught.exception)

    def test_L7_a_comment_that_arrives_twice_is_one_hit(self) -> None:
        # 쪽 경계에서 커서가 겹치면 같은 댓글이 두 번 온다.
        page = load_fixture("streams")
        reader, _ = self.make_reader(
            self.feed_routes([(200, page, None), (200, page, None), (200, feed_page([], None), None)])
        )
        scan = self.feed_scan(reader)
        self.assertEqual(self.tails(scan), ["312", "309", "308", "305"])

    def test_기준11_an_unreadable_post_is_asked_only_once(self) -> None:
        routes = self.feed_routes()
        routes[("GET", f"{POST_PATH}/1000000000000000028")] = (404, {"header": {}}, None)
        reader, http = self.make_reader(routes)
        scan = self.feed_scan(reader)

        asked = [r for r in self.paths(http) if r == f"{POST_PATH}/1000000000000000028"]
        self.assertEqual(len(asked), 1, "실패도 기억한다 — 같은 업무를 다시 묻지 않는다")
        self.assertEqual(scan.unread_posts, 1)
        # 업무를 못 읽으면 프로젝트를 몰라 그룹을 판정할 수 없다. 그 사실이 드러난다.
        self.assertTrue(scan.unresolved_groups)
        self.assertNotIn("308", self.tails(scan))
        self.assertEqual(self.tails(scan), ["312", "309", "305"])
        self.assertFalse(scan.complete)

    def test_기준11_a_group_we_cannot_read_leaves_the_scan_incomplete(self) -> None:
        reader, _ = self.make_reader(self.feed_routes(group=(403, {"header": {}}, None)))
        scan = self.feed_scan(reader)

        self.assertTrue(scan.unresolved_groups)
        self.assertFalse(scan.complete)
        self.assertNotIn("308", self.tails(scan))
        self.assertEqual(self.tails(scan), ["312", "309", "305"], "나머지는 그대로 온다")

    def test_기준11_a_flood_of_group_mentions_stops_at_the_comment_cap(self) -> None:
        many = " ".join(
            group_text(f"100000000000000090{number}") for number in range(5)
        )
        pages = [(200, feed_page([feed_event("1000000000000000401", many)], None), None)]
        routes = self.feed_routes(pages=pages)
        for number in range(5):
            routes[("GET", group_path(FEED_POSTS["1000000000000000028"][0], f"100000000000000090{number}"))] = (
                200,
                load_fixture("member_group_other_project"),
                None,
            )
        reader, http = self.make_reader(routes)
        scan = self.feed_scan(reader)

        asked = [r for r in self.paths(http) if "member-groups" in r]
        self.assertEqual(len(asked), collect.MAX_GROUP_LOOKUPS_PER_COMMENT)
        self.assertTrue(scan.unresolved_groups)
        self.assertFalse(scan.complete)

    def test_기준11_the_call_limit_keeps_what_was_already_found(self) -> None:
        reader, _ = self.make_reader(self.feed_routes(), call_limit=5)
        scan = self.feed_scan(reader)

        # 2(피드) + 1(업무 …028) + 1(그룹) 뒤에 여유가 모자란다 → …309의 업무 조회를 못 한다.
        # **그래도 …309·…312는 이미 내 멘션으로 확정된 댓글이다.** 업무 제목을 못 채운다고 멘션까지
        # 버리지 않는다(메인 세션 검토) — 판정에 호출이 필요 없는 멤버 멘션은 끝까지 센다.
        self.assertEqual(scan.stopped_reason, collect.CALL_LIMIT_REASON)
        self.assertEqual(self.tails(scan), ["312", "309", "308", "305"])
        # …305·…308·…312는 같은 업무(…028)라 이미 읽혀 있다. 못 채운 것은 업무 …026의 …309 하나다.
        unfilled = [hit.comment_id[-3:] for hit in scan.hits if not hit.task_number]
        self.assertEqual(unfilled, ["309"])
        self.assertFalse(scan.complete)
        self.assertTrue(scan.reached_since)

    def test_기준11_the_page_limit_leaves_since_unreached(self) -> None:
        pages = [(200, load_fixture("streams"), None)]
        reader, http = self.make_reader(self.feed_routes(pages=pages))
        scan = self.feed_scan(reader, max_pages=1)

        self.assertFalse(scan.reached_since)
        self.assertFalse(scan.complete)
        self.assertEqual(self.tails(scan), ["312", "309", "308", "305"], "찾은 것은 돌려준다")
        self.assertEqual(len([r for r in self.paths(http) if r == STREAMS]), 1)

    def test_기준11_a_network_failure_is_a_partial_result(self) -> None:
        routes = self.feed_routes()
        reader, _ = self.make_reader(boom_transport(routes, {f"{POST_PATH}/1000000000000000026"}))
        scan = self.feed_scan(reader)

        self.assertEqual(scan.stopped_reason, "NetworkError")
        self.assertFalse(scan.complete)
        self.assertEqual(self.tails(scan), ["308", "305"])

    def test_기준11_a_server_error_is_reported_by_its_class_name(self) -> None:
        routes = self.feed_routes()
        routes[("GET", f"{POST_PATH}/1000000000000000028")] = (503, {"header": {}}, None)
        reader, _ = self.make_reader(routes)
        scan = self.feed_scan(reader)

        self.assertEqual(scan.stopped_reason, "ServerError")
        self.assertFalse(scan.complete)

    def test_an_auth_error_is_not_caught(self) -> None:
        reader, _ = self.make_reader({("GET", STREAMS): (401, {"header": {}}, None)})
        with self.assertRaises(AuthError):
            self.feed_scan(reader)

    def test_a_bent_post_id_never_reaches_the_network(self) -> None:
        # postId는 API가 준 값이지만 경로에 넣기 전에 본다 (`client.path_id`).
        bent = feed_event("1000000000000000401", mention_text(), post_id="26/set-done?x=")
        reader, http = self.make_reader(self.feed_routes(pages=[(200, feed_page([bent], None), None)]))
        scan = self.feed_scan(reader)

        self.assertEqual([r for r in self.paths(http) if r != STREAMS], [])
        self.assertEqual(self.tails(scan), ["401"], "멘션은 남는다")
        self.assertEqual(scan.hits[0].task_number, "")
        self.assertEqual(scan.unread_posts, 1)
        self.assertFalse(scan.complete)

    def test_the_limit_raised_by_the_transport_is_caught_too(self) -> None:
        # 그룹 조회 앞에는 여유 계산(`_need_room`)이 없다. 상한 3: 피드 1 + 업무 1 + 그룹 1 뒤에
        # 둘째 그룹 조회를 **전송 계층이** 막는다. 그래도 부분 결과로 만든다 — 예외로 날리지 않는다.
        group_project = FEED_POSTS["1000000000000000028"][0]
        items = [
            feed_event("1000000000000000401", group_text("2000000000000000001")),
            feed_event("1000000000000000402", group_text("2000000000000000002")),
        ]
        routes = self.feed_routes(pages=[(200, feed_page(items, None), None)])
        for group_id in ("2000000000000000001", "2000000000000000002"):
            routes[("GET", group_path(group_project, group_id))] = (
                200,
                load_fixture("member_group_other_project"),
                None,
            )
        reader, http = self.make_reader(routes, call_limit=3)
        scan = self.feed_scan(reader)

        self.assertEqual(len(http.requests), 3, "상한에 닿은 뒤로는 요청이 없다")
        self.assertEqual(scan.stopped_reason, collect.CALL_LIMIT_REASON)
        self.assertEqual(self.tails(scan), ["401"], "이미 찾은 것은 남는다")
        self.assertFalse(scan.complete)


class FeedSkipRulesTest(FeedCase):
    def test_my_own_comment_is_skipped_even_when_it_mentions_me(self) -> None:
        mine = feed_event("1000000000000000401", mention_text(), author=MY_ID)
        reader, http = self.make_reader(self.feed_routes(pages=[(200, feed_page([mine], None), None)]))
        scan = self.feed_scan(reader)

        self.assertEqual(scan.hits, ())
        self.assertEqual([r for r in self.paths(http) if r != STREAMS], [])

    def test_a_quoted_mention_does_not_become_a_hit_추정(self) -> None:
        quoted = feed_event("1000000000000000401", f"> {mention_text()}")
        reader, _ = self.make_reader(self.feed_routes(pages=[(200, feed_page([quoted], None), None)]))
        self.assertEqual(self.feed_scan(reader).hits, ())

    def test_a_comment_that_mentions_only_others_costs_no_call(self) -> None:
        others = feed_event("1000000000000000401", mention_text("1000000000000000012"))
        reader, http = self.make_reader(self.feed_routes(pages=[(200, feed_page([others], None), None)]))
        scan = self.feed_scan(reader)

        self.assertEqual(scan.hits, ())
        self.assertEqual([r for r in self.paths(http) if r != STREAMS], [])
        self.assertTrue(scan.complete)

    def test_a_group_i_am_not_in_is_not_a_hit(self) -> None:
        said = feed_event("1000000000000000401", group_text())
        # 내가 없는 그룹. fixture 둘 다 나를 담고 있어 여기서 합성한다.
        without_me = reply({"id": GROUP_ID, "members": [{"id": "1000000000000000012"}]})
        routes = self.feed_routes(
            pages=[(200, feed_page([said], None), None)],
            group=(200, without_me, None),
        )
        reader, _ = self.make_reader(routes)
        scan = self.feed_scan(reader)

        self.assertEqual(scan.hits, ())
        self.assertTrue(scan.complete, "판정을 했다 — 내가 그 그룹에 없을 뿐이다")

    def test_a_comment_without_a_time_is_kept_with_the_scan_time(self) -> None:
        item = feed_event("1000000000000000401", mention_text())
        del item["event"]["createdAt"]
        fixed = datetime(2026, 9, 20, tzinfo=KST)
        reader, _ = self.make_reader(self.feed_routes(pages=[(200, feed_page([item], None), None)]))
        scan = self.feed_scan(reader, now=fixed)

        self.assertEqual(self.tails(scan), ["401"])
        self.assertEqual(scan.hits[0].comment_created_at, fixed)


class FeedGroupTest(FeedCase):
    """그룹 멘션의 조회 규칙 (001 spec §11-3의 X10·X14·X15). 프로젝트 방식에서 옮겨 왔다 (009 spec §5-5).

    댓글은 전부 업무 …028에 둔다 — 업무 조회가 1회로 끝나 그룹 조회만 센다.
    """

    GROUP_PROJECT = FEED_POSTS["1000000000000000028"][0]

    def group_ok(self) -> tuple:
        return (200, load_fixture("member_group_other_project"), None)

    def routes_for(self, items: list[dict]) -> dict:
        return self.feed_routes(pages=[(200, feed_page(items, None), None)])

    def group_lookups(self, http) -> list[str]:
        return [p for p in self.paths(http) if "member-groups" in p]

    def test_X10_the_same_group_is_fetched_once_per_scan(self) -> None:
        items = [
            feed_event("1000000000000000401", group_text()),
            feed_event("1000000000000000402", group_text()),
        ]
        reader, http = self.make_reader(self.routes_for(items))
        scan = self.feed_scan(reader)

        self.assertEqual(len(self.group_lookups(http)), 1, "스캔 안 dict가 중복을 막는다")
        self.assertEqual(len(scan.hits), 2)

    def test_X14_a_comment_that_already_mentions_me_costs_no_group_lookup(self) -> None:
        # 멤버 멘션으로 이미 내 것이 확정되면 그룹을 조회하지 않는다 (리뷰 H1).
        items = [feed_event("1000000000000000401", mention_text() + " " + group_text())]
        reader, http = self.make_reader(self.routes_for(items))
        scan = self.feed_scan(reader)

        self.assertEqual(len(scan.hits), 1)
        self.assertIsNone(scan.hits[0].via_group_id)
        self.assertEqual(self.group_lookups(http), [])

    def test_X15_the_scan_wide_cap_holds_across_comments(self) -> None:
        # 댓글마다 그룹 하나라 댓글당 상한(3)에는 걸리지 않는다. 스캔당 상한(10)이 막는다 (리뷰 H1).
        items = [
            feed_event(str(1000000000000000100 + n), group_text(str(2000000000000000000 + n)))
            for n in range(40)
        ]
        routes = self.routes_for(items)
        for n in range(40):
            routes[("GET", group_path(self.GROUP_PROJECT, str(2000000000000000000 + n)))] = (
                self.group_ok()
            )
        reader, http = self.make_reader(routes, call_limit=200)
        scan = self.feed_scan(reader)

        self.assertEqual(len(self.group_lookups(http)), collect.MAX_GROUP_LOOKUPS_PER_SCAN)
        self.assertTrue(scan.unresolved_groups, "다 보지 못했음을 드러낸다")
        self.assertFalse(scan.complete)

    def test_a_bent_group_id_never_reaches_the_network(self) -> None:
        # 그룹 ID는 **사용자가 쓴 본문**에서 온다. `member_group()`의 `path_id()`가 막는다 (§5-1b).
        items = [
            feed_event("1000000000000000401", group_text("..%2F..%2Fx")),
            feed_event("1000000000000000402", mention_text()),
        ]
        reader, http = self.make_reader(self.routes_for(items))
        scan = self.feed_scan(reader)

        self.assertEqual(self.group_lookups(http), [])
        self.assertEqual([hit.comment_id for hit in scan.hits], ["1000000000000000402"])
        # 조작된 그룹 ID도 "판정하지 못한 그룹 멘션"이다. 조용히 넘기지 않는다 (리뷰 H3).
        self.assertTrue(scan.unresolved_groups)
        self.assertFalse(scan.complete)


class FeedContractTest(FeedCase):
    def test_an_empty_feed_is_a_complete_scan(self) -> None:
        reader, _ = self.make_reader(self.feed_routes(pages=[(200, feed_page([], None), None)]))
        scan = self.feed_scan(reader)

        self.assertEqual(scan.hits, ())
        self.assertTrue(scan.complete)
        self.assertEqual(scan.calls_used, 1)

    def test_the_since_is_passed_to_the_feed_as_given(self) -> None:
        reader, _ = self.make_reader(self.feed_routes())
        seen: list[object] = []
        real = reader.feed_comments

        def spy(**kwargs):
            seen.append((kwargs["since"], kwargs["max_pages"]))
            return real(**kwargs)

        with mock.patch.object(reader, "feed_comments", spy):
            self.feed_scan(reader, since=LATER, max_pages=2)
        self.assertEqual(seen, [(LATER, 2)])


# --- 비멤버 프로젝트 발견 (003 spec §6-2) ------------------------------------

MEMBER_PID = "1000000000000000019"  # FEED_POSTS의 …027이 여기 있다
OUTSIDE_A = "1000000000000000018"  # 피드의 `post` 항목이 가리키는 프로젝트
OUTSIDE_B = "1000000000000000020"  # …028의 프로젝트
PREVIOUS_PID = "1000000000000000021"  # 지난번에 발견해 둔 것


def project_path(project_id: str) -> str:
    return f"{PROJECTS}/{project_id}"


def feed_post(project_id: str, post_id: str = "1000000000000000050") -> dict:
    """피드의 `post` 항목. `projectId`는 여기에만 있다 (api-notes "추가 실측")."""
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


def previous(*ids: str) -> tuple[models.Project, ...]:
    return tuple(
        models.Project(id=value, code=f"옛{value[-2:]}", state=None, type=None, scope=None)
        for value in ids
    )


class DiscoverSeenProjectsTest(CollectCase):
    """완료 기준 16 — `streams.json`을 손으로 센 결과와 맞춘다.

    피드: `post` 1건 → 프로젝트 …018 / `event` 12건 → 업무 셋(…026 …027 …028).
    멤버는 …019 하나라고 둔다. 그러면 발견되는 것은 …018과 …020이다.
    """

    def routes(self, *, pages: list | None = None, extra: dict | None = None) -> dict:
        table: dict = {
            ("GET", STREAMS): pages
            or [(200, load_fixture("streams"), None), (200, feed_page([], None), None)]
        }
        for post_id, (project_id, code, number) in FEED_POSTS.items():
            table[("GET", f"{POST_PATH}/{post_id}")] = (
                200,
                reply(post_row(project_id, post_id, number=number, code=code)),
                None,
            )
        for project_id in (OUTSIDE_A, OUTSIDE_B, PREVIOUS_PID):
            table[("GET", project_path(project_id))] = (
                200,
                reply(project_row(project_id, code=f"밖{project_id[-2:]}")),
                None,
            )
        table.update(extra or {})
        return table

    def discover(self, reader, **kwargs) -> tuple[models.Project, ...]:
        kwargs.setdefault("member_ids", {MEMBER_PID})
        kwargs.setdefault("previous", ())
        return collect.discover_seen_projects(reader, **kwargs)

    def test_기준16_the_feed_finds_the_projects_i_am_not_a_member_of(self) -> None:
        reader, http = self.make_reader(self.routes())
        found = self.discover(reader)

        self.assertEqual([p.id for p in found], [OUTSIDE_A, OUTSIDE_B])
        # …018은 프로젝트 단건 조회로 이름이 왔다. …020은 업무 응답의 이름이다.
        self.assertEqual(found[0].code, "밖18")
        self.assertEqual(found[1].code, "other")
        # 2(피드) + 1(프로젝트) + 3(업무). 프로젝트 목록(`?member=me`)은 부르지 않는다.
        self.assertEqual(len(http.requests), 6)
        self.assertEqual([r.url.path for r in http.requests].count(PROJECTS), 0)

    def test_기준16_a_member_project_never_becomes_a_seen_one(self) -> None:
        reader, http = self.make_reader(self.routes())
        found = self.discover(reader, member_ids={MEMBER_PID, OUTSIDE_A, OUTSIDE_B})

        self.assertEqual(found, ())
        # 멤버 프로젝트는 단건 조회도 하지 않는다. 남은 것은 피드 2 + 업무 3이다.
        self.assertEqual(len(http.requests), 5)
        self.assertNotIn(project_path(OUTSIDE_A), [r.url.path for r in http.requests])

    def test_기준16_a_project_found_twice_is_one_entry(self) -> None:
        # `post` 항목의 …018은 업무 …026의 프로젝트이기도 하다. 두 번 조회하지 않는다.
        reader, http = self.make_reader(self.routes())
        found = self.discover(reader)
        asked = [r.url.path for r in http.requests]
        self.assertEqual(asked.count(project_path(OUTSIDE_A)), 1)
        self.assertEqual([p.id for p in found].count(OUTSIDE_A), 1)

    def test_기준16_a_previous_project_is_kept_without_being_asked_about(self) -> None:
        # 운영자 결정: **발견은 더하기만 한다.** 한 번 등록된 프로젝트는 계속 조회하고, 빠지는 것은
        # 실제 조회가 403·404를 줬을 때다(`tools._across_projects`). 여기서 확인 호출을 쓰지 않는다.
        for status in (200, 403, 404):
            with self.subTest(status=status):
                reader, http = self.make_reader(
                    self.routes(
                        extra={
                            ("GET", project_path(PREVIOUS_PID)): (
                                status,
                                load_fixture("error_404"),
                                None,
                            )
                        }
                    )
                )
                found = self.discover(reader, previous=previous(PREVIOUS_PID))
                self.assertEqual([p.id for p in found], [OUTSIDE_A, OUTSIDE_B, PREVIOUS_PID])
                self.assertEqual(found[-1].code, "옛21", "있던 값 그대로다")
                self.assertNotIn(project_path(PREVIOUS_PID), [r.url.path for r in http.requests])

    def test_기준16_a_previous_project_that_became_mine_is_dropped(self) -> None:
        reader, http = self.make_reader(self.routes())
        found = self.discover(
            reader, member_ids={MEMBER_PID, PREVIOUS_PID}, previous=previous(PREVIOUS_PID)
        )
        self.assertNotIn(PREVIOUS_PID, [p.id for p in found])
        self.assertNotIn(project_path(PREVIOUS_PID), [r.url.path for r in http.requests])

    def test_기준16_the_call_limit_keeps_what_we_have_and_the_previous(self) -> None:
        # 피드 2회로 여유가 떨어진다. 발견은 덤이지 도구의 성패가 아니다.
        reader, http = self.make_reader(self.routes(), call_limit=3)
        found = self.discover(reader, previous=previous(PREVIOUS_PID))

        self.assertEqual([p.id for p in found], [PREVIOUS_PID])
        self.assertEqual(len(http.requests), 2)

    def test_기준16_a_server_error_keeps_what_we_have_and_the_previous(self) -> None:
        reader, _ = self.make_reader(
            self.routes(
                extra={
                    ("GET", f"{POST_PATH}/1000000000000000026"): (
                        500,
                        load_fixture("error_404"),
                        None,
                    )
                }
            )
        )
        found = self.discover(reader, previous=previous(PREVIOUS_PID))

        # …018은 이미 찾았고, 업무 조회에서 멈췄다. previous는 확인하지 못한 채 남는다.
        self.assertEqual([p.id for p in found], [OUTSIDE_A, PREVIOUS_PID])

    def test_기준16_a_network_failure_keeps_the_previous(self) -> None:
        reader, _ = self.make_reader(
            boom_transport(self.routes(), {STREAMS}),
        )
        found = self.discover(reader, previous=previous(PREVIOUS_PID))
        self.assertEqual([p.id for p in found], [PREVIOUS_PID])

    def test_기준16_an_auth_error_is_not_caught(self) -> None:
        reader, _ = self.make_reader(
            self.routes(
                extra={
                    ("GET", project_path(OUTSIDE_A)): (401, load_fixture("error_404"), None)
                }
            )
        )
        with self.assertRaises(AuthError):
            self.discover(reader)

    def test_기준16_a_bent_id_from_the_feed_is_skipped_not_fetched(self) -> None:
        items = [feed_post("../drive/v1/drives", post_id="1000000000000000050")]
        reader, http = self.make_reader(self.routes(pages=[(200, feed_page(items, None), None)]))
        found = self.discover(reader)

        self.assertEqual(found, ())
        self.assertEqual([r.url.path for r in http.requests], [STREAMS])

    def test_기준16_neither_cap_is_passed(self) -> None:
        # 피드가 주는 수는 우리가 정하지 않는다. 조회 수는 우리가 정한다 (≤ 10 + ≤ 15).
        items = [feed_post(f"30000000000000000{n:02d}", post_id=f"31000000000000000{n:02d}") for n in range(20)]
        items += [
            feed_event(f"4000000000000000{n:03d}", "본문", post_id=f"50000000000000000{n:02d}")
            for n in range(20)
        ]
        routes = self.routes(pages=[(200, feed_page(items, None), None)])
        for number in range(20):
            routes[("GET", project_path(f"30000000000000000{number:02d}"))] = (
                200,
                reply(project_row(f"30000000000000000{number:02d}", code=f"밖{number}")),
                None,
            )
            routes[("GET", f"{POST_PATH}/50000000000000000{number:02d}")] = (
                200,
                reply(post_row(f"60000000000000000{number:02d}", f"50000000000000000{number:02d}")),
                None,
            )
        reader, http = self.make_reader(routes, call_limit=100)
        found = self.discover(reader)

        asked = [r.url.path for r in http.requests]
        self.assertEqual(len([p for p in asked if p.startswith(f"{POST_PATH}/")]), 15)
        self.assertEqual(
            len([p for p in asked if p.startswith(f"{PROJECTS}/")]), 10
        )
        self.assertEqual(len(http.requests), 1 + 10 + 15)
        self.assertEqual(len(found), 10 + 15)

    def test_기준25_the_post_lookups_of_the_discovery_go_out_at_the_same_time(self) -> None:
        # 003 spec §7-3의 3. 피드가 가리키는 업무 셋이 한 묶음이다. 순차면 장벽이 깨진다.
        gate = threading.Barrier(3, timeout=10)
        reader, http = self.make_reader(
            gated_transport(self.routes(), gate, f"{POST_PATH}/")
        )
        found = self.discover(reader)

        self.assertEqual([p.id for p in found], [OUTSIDE_A, OUTSIDE_B])
        self.assertEqual(len(http.requests), 6, "호출 수는 순차 때와 같다")

    def test_기준25_the_feed_comes_first_then_the_two_batches(self) -> None:
        # 묶음 **밖**의 순서는 그대로다: 피드 → 프로젝트 단건 묶음 → 업무 단건 묶음.
        reader, http = self.make_reader(self.routes())
        self.discover(reader)

        paths = [request.url.path for request in http.requests]
        self.assertEqual(paths[:2], [STREAMS, STREAMS])
        self.assertEqual(paths[2], project_path(OUTSIDE_A))
        self.assertEqual(
            sorted(paths[3:]),
            sorted(f"{POST_PATH}/{post_id}" for post_id in FEED_POSTS),
        )

    def test_기준25_a_tight_budget_sends_only_what_it_can_afford(self) -> None:
        # 상한 4: 피드 2 + 프로젝트 단건 1 뒤에 여유가 1뿐이라 업무 묶음을 시작하지 못한다.
        reader, http = self.make_reader(self.routes(), call_limit=4)
        found = self.discover(reader, previous=previous(PREVIOUS_PID))

        self.assertEqual([p.id for p in found], [OUTSIDE_A, PREVIOUS_PID])
        self.assertEqual(len(http.requests), 3)

    def test_the_default_caps_are_the_documented_ones(self) -> None:
        signature = inspect.signature(collect.discover_seen_projects)
        self.assertEqual(signature.parameters["max_post_lookups"].default, 15)
        self.assertEqual(signature.parameters["max_project_lookups"].default, 10)


# --- 007 — 멘션할 수 있는 사람 (spec §4-2, §8-2의 T 계열) ---------------------

MEMBERS = "/common/v1/members"


def detail_post() -> models.Post:
    return models.parse_post(load_fixture("post_detail")["result"])


def member_reply(member_id: str, name: str) -> dict:
    return reply({"organizationMemberId": member_id, "name": name})


class ThreadPeopleTest(CollectCase):
    """참여자는 업무 응답·댓글 응답의 구조 필드에서만 온다 (완료 기준 8·18)."""

    def test_T1_the_people_are_author_assignees_cc_and_commenters(self) -> None:
        routes = {("GET", logs_path(PID, POST_ID)): (200, load_fixture("logs"), None)}
        reader, http = self.make_reader(routes)
        post = detail_post()
        found = collect.thread_people(reader, post, max_pages=3, max_lookups=5)

        ids = [person.id for person in found.people]
        self.assertEqual(len(ids), len(set(ids)))  # ID 중복 없음
        wanted = {
            "1000000000000000003",  # 작성자(참조에도 있다)
            # 멤버 담당 다섯
            "1000000000000000012", "1000000000000000013", "1000000000000000014",
            "1000000000000000001", "1000000000000000011",
        }
        wanted |= {member.id for member in post.cc}  # 멤버 참조
        self.assertEqual(len(post.cc), 10)
        self.assertEqual(set(ids), wanted)
        # 그룹 참조의 구성원(멤버17)은 이 업무에 나타난 사람이 아니다.
        self.assertNotIn("1000000000000000016", ids)
        self.assertNotIn("멤버17", [person.name for person in found.people])
        # 본문 속 링크에만 나오는 ID(파일·다른 멤버)도 들어가지 않는다.
        self.assertTrue(all(len(one) == 19 for one in ids))
        self.assertEqual(found.tenant, TENANT)
        self.assertEqual(found.unnamed, 0)
        self.assertFalse(found.comments_cut)
        # 댓글 작성자는 전부 업무 응답의 이름표에 있었다 — 단건 조회 0회.
        self.assertEqual(self.paths(http), [logs_path(PID, POST_ID)])

    def test_T2_name_lookups_stop_at_the_limit(self) -> None:
        strangers = [f"20000000000000000{n:02d}" for n in range(7)]
        logs = reply(
            [comment_row(f"30000000000000000{n:02d}", "본문", author=one) for n, one in enumerate(strangers)],
            total=7,
        )
        routes = {("GET", logs_path(PID, POST_ID)): (200, logs, None)}
        for number, one in enumerate(strangers):
            routes[("GET", f"{MEMBERS}/{one}")] = (200, member_reply(one, f"낯선이{number}"), None)
        reader, http = self.make_reader(routes)
        found = collect.thread_people(reader, detail_post(), max_pages=3, max_lookups=5)

        looked = [path for path in self.paths(http) if path.startswith(MEMBERS)]
        self.assertEqual(len(looked), 5)
        self.assertEqual(found.unnamed, 2)
        named = [person for person in found.people if person.name.startswith("낯선이")]
        self.assertEqual(len(named), 5)
        # 응답의 앞(0번)이 최신이다. 조회 5회는 **최근** 작성자에게 쓰인다 — 나를 부른 사람이 빠지지 않게 (007 리뷰 M2).
        self.assertEqual({person.name for person in named}, {f"낯선이{n}" for n in range(5)})

    def test_T2_a_forbidden_name_is_skipped_and_counted(self) -> None:
        logs = reply([comment_row("3000000000000000001", "본문", author="2000000000000000001")])
        routes = {
            ("GET", logs_path(PID, POST_ID)): (200, logs, None),
            ("GET", f"{MEMBERS}/2000000000000000001"): (403, {"header": {}}, None),
        }
        reader, _ = self.make_reader(routes)
        found = collect.thread_people(reader, detail_post(), max_pages=3, max_lookups=5)
        self.assertEqual(found.unnamed, 1)

    def test_T3_a_failed_comment_read_is_raised(self) -> None:
        from dooray_core.errors import ForbiddenError

        routes = {("GET", logs_path(PID, POST_ID)): (403, {"header": {}}, None)}
        reader, _ = self.make_reader(routes)
        with self.assertRaises(ForbiddenError):
            collect.thread_people(reader, detail_post(), max_pages=3, max_lookups=5)

    def test_T4_one_comment_page_costs_one_call(self) -> None:
        routes = {
            ("GET", logs_path(PID, POST_ID)): (200, load_fixture("logs"), None),
            ("GET", f"{POST_PATH}/{POST_ID}"): (200, load_fixture("post_detail"), None),
        }
        reader, http = self.make_reader(routes)
        post = reader.post(POST_ID)
        self.assertEqual(reader.calls, 1)
        collect.thread_people(reader, post, max_pages=3, max_lookups=5)
        self.assertEqual(reader.calls, 2)

    def test_T_an_email_commenter_and_an_odd_id_are_not_people(self) -> None:
        email = comment_row("3000000000000000001", "본문")
        email["creator"] = {"type": "emailUser", "emailUser": {"emailAddress": "a@b.c"}}
        odd = comment_row("3000000000000000002", "본문", author="1/../x")
        logs = reply([email, odd], total=2)
        routes = {("GET", logs_path(PID, POST_ID)): (200, logs, None)}
        reader, http = self.make_reader(routes)
        base = detail_post()
        found = collect.thread_people(reader, base, max_pages=3, max_lookups=5)
        self.assertNotIn("1/../x", [person.id for person in found.people])
        self.assertEqual(found.unnamed, 1)
        self.assertEqual(self.paths(http), [logs_path(PID, POST_ID)])  # 이상한 ID로 묻지 않는다

    def test_T_a_full_comment_page_is_reported_as_cut(self) -> None:
        rows = [comment_row(f"3{n:018d}", "본문") for n in range(100)]
        routes = {("GET", logs_path(PID, POST_ID)): (200, reply(rows, total=500), None)}
        reader, _ = self.make_reader(routes)
        found = collect.thread_people(reader, detail_post(), max_pages=1, max_lookups=5)
        self.assertTrue(found.comments_cut)


# --- 014 — 링크 없는 업무의 테넌트는 활동 피드 한 쪽에서 (spec §4·§5, TF-7~15) -------

OTHER_TENANT = "1000000000000000099"  # `TENANT`와 다른 값이기만 하면 된다


def plain_post(content: str = "링크 없는 본문") -> models.Post:
    """`post_detail`의 본문만 바꾼 업무. 참여자는 그대로다."""
    data = load_fixture("post_detail")
    data["result"]["body"]["content"] = content
    return models.parse_post(data["result"])


def plain_logs_reply() -> dict:
    """`logs.json`의 본문을 전부 링크 없는 글로. 작성자·종류는 그대로다."""
    data = load_fixture("logs")
    for row in data["result"]:
        row["body"]["content"] = "링크 없는 댓글"
    return data


def tenant_link(tenant: str = TENANT) -> str:
    return f"[x](dooray://{tenant}/members/1)"


class FeedTenantTest(CollectCase):
    """`collect.feed_tenant` — 피드 한 쪽, 사람이 쓴 댓글 본문만 (TF-7~10)."""

    def test_TF_7_one_get_of_size_200(self) -> None:
        # 목록이 아니다 — 두 번 부르면 같은 응답(`cursor` 있음)이 다시 와서 요청이 는다.
        reader, http = self.make_reader({("GET", STREAMS): (200, load_fixture("streams"), None)})
        self.assertEqual(collect.feed_tenant(reader), TENANT)
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(self.paths(http), [STREAMS])
        self.assertEqual(http.requests[0].url.params["size"], "200")
        self.assertNotIn("before", http.requests[0].url.params)

    def test_TF_8_another_value_anywhere_stops(self) -> None:
        for author in ("1000000000000000203", MY_ID):
            with self.subTest(author=author):
                items = load_fixture("streams")["result"] + [
                    feed_event("3000000000000000001", tenant_link(OTHER_TENANT), author=author)
                ]
                reader, http = self.make_reader({("GET", STREAMS): (200, feed_page(items, None), None)})
                self.assertIsNone(collect.feed_tenant(reader))
                self.assertEqual(len(http.requests), 1)

    def test_TF_9_only_human_comments_count(self) -> None:
        system = feed_event("3000000000000000002", tenant_link())
        system["event"]["type"] = "event"
        post = copy.deepcopy(load_fixture("streams")["result"][2])
        post["post"]["body"]["content"] = tenant_link()
        items = [
            feed_event("3000000000000000001", "링크 없는 댓글"),
            system,
            post,
            feed_event("3000000000000000003", "링크 없는 댓글"),
        ]
        reader, http = self.make_reader({("GET", STREAMS): (200, feed_page(items, None), None)})
        self.assertIsNone(collect.feed_tenant(reader))
        self.assertEqual(len(http.requests), 1)

    def test_TF_10_the_text_is_cut_at_the_limit(self) -> None:
        body = "가" * collect.THREAD_TEXT_LIMIT + " " + tenant_link()
        items = [feed_event("3000000000000000001", body)]
        reader, _ = self.make_reader({("GET", STREAMS): (200, feed_page(items, None), None)})
        self.assertIsNone(collect.feed_tenant(reader))


class ThreadPeopleFeedTest(CollectCase):
    """`thread_people` — 업무의 글에 `dooray://`가 없을 때만 피드를 읽는다 (TF-11~15)."""

    def run_people(self, routes: dict, post: models.Post):
        reader, http = self.make_reader(routes)
        return collect.thread_people(reader, post, max_pages=3, max_lookups=5), http

    def test_TF_11_a_thread_without_a_link_reads_the_feed_once(self) -> None:
        found, http = self.run_people(
            {
                ("GET", logs_path(PID, POST_ID)): (200, plain_logs_reply(), None),
                ("GET", STREAMS): (200, load_fixture("streams"), None),
            },
            plain_post(),
        )
        self.assertEqual(self.paths(http), [logs_path(PID, POST_ID), STREAMS])
        self.assertEqual(found.tenant, TENANT)
        self.assertTrue(found.feed_read)
        # 대조: 링크 있는 같은 업무(T1의 입력) — 참여자는 피드와 상관없이 같다.
        linked, _ = self.run_people(
            {("GET", logs_path(PID, POST_ID)): (200, load_fixture("logs"), None)}, detail_post()
        )
        self.assertEqual(set(found.people), set(linked.people))
        self.assertFalse(linked.feed_read)

    def test_TF_12_a_thread_with_a_link_never_reads_the_feed(self) -> None:
        # 피드 경로를 열어 둔다 — 불렀다면 장치가 아니라 아래 단언이 실패한다.
        found, http = self.run_people(
            {
                ("GET", logs_path(PID, POST_ID)): (200, load_fixture("logs"), None),
                ("GET", STREAMS): (200, load_fixture("streams"), None),
            },
            detail_post(),
        )
        self.assertNotIn(STREAMS, self.paths(http))
        self.assertFalse(found.feed_read)
        self.assertEqual(found.tenant, TENANT)

    def test_TF_13_two_values_in_the_thread_stop_without_the_feed(self) -> None:
        found, http = self.run_people(
            {
                ("GET", logs_path(PID, POST_ID)): (200, plain_logs_reply(), None),
                ("GET", STREAMS): (200, load_fixture("streams"), None),
            },
            plain_post(f"{tenant_link()} {tenant_link(OTHER_TENANT)}"),
        )
        self.assertNotIn(STREAMS, self.paths(http))
        self.assertIsNone(found.tenant)
        self.assertFalse(found.feed_read)

    def test_TF_14_a_failed_feed_is_raised(self) -> None:
        for status, error in ((403, ForbiddenError), (500, ServerError)):
            with self.subTest(status=status):
                reader, _ = self.make_reader(
                    {
                        ("GET", logs_path(PID, POST_ID)): (200, plain_logs_reply(), None),
                        ("GET", STREAMS): (status, {"header": {}}, None),
                    }
                )
                with self.assertRaises(error):
                    collect.thread_people(reader, plain_post(), max_pages=3, max_lookups=5)

    def test_TF_15_the_feed_adds_no_one(self) -> None:
        stranger = "2000000000000000001"
        logs = reply([comment_row("3000000000000000001", "링크 없는 댓글", author=stranger)])
        feed = feed_page([feed_event("3000000000000000009", tenant_link(), author=stranger)], None)
        found, http = self.run_people(
            {
                ("GET", logs_path(PID, POST_ID)): (200, logs, None),
                ("GET", f"{MEMBERS}/{stranger}"): (404, {"header": {}}, None),
                ("GET", STREAMS): (200, feed, None),
            },
            plain_post(),
        )
        # 피드에는 그 사람의 이름("멤버33")이 있다. 참여자는 피드 전에 정해졌다.
        self.assertEqual(found.unnamed, 1)
        self.assertNotIn(stranger, [person.id for person in found.people])
        self.assertEqual(found.tenant, TENANT)  # 피드를 읽었다
        self.assertEqual(self.paths(http), [logs_path(PID, POST_ID), f"{MEMBERS}/{stranger}", STREAMS])

    # --- 014 보충(리뷰 L1·M1) — "링크가 없다"가 확인된 때만 피드로 간다 ---------------

    def open_feed(self, logs: dict) -> dict:
        """피드 경로를 열어 둔다 — 불렀다면 장치가 아니라 단언이 실패한다."""
        return {
            ("GET", logs_path(PID, POST_ID)): (200, logs, None),
            ("GET", STREAMS): (200, load_fixture("streams"), None),
        }

    def test_TF_28_a_link_after_the_body_limit_keeps_the_feed_closed(self) -> None:
        body = "가" * collect.THREAD_TEXT_LIMIT + " " + tenant_link()
        found, http = self.run_people(self.open_feed(plain_logs_reply()), plain_post(body))
        self.assertNotIn(STREAMS, self.paths(http))
        self.assertIsNone(found.tenant)  # 값은 자른 글에서 — 자른 글에는 링크가 없다
        self.assertFalse(found.feed_read)

    def test_TF_29_a_link_after_the_limit_of_a_comment_keeps_the_feed_closed(self) -> None:
        logs = plain_logs_reply()
        logs["result"][1]["body"]["content"] = "가" * collect.THREAD_TEXT_LIMIT + " " + tenant_link()
        found, http = self.run_people(self.open_feed(logs), plain_post())
        self.assertNotIn(STREAMS, self.paths(http))
        self.assertIsNone(found.tenant)
        self.assertFalse(found.feed_read)

    def test_TF_30_comments_not_read_to_the_end_keep_the_feed_closed(self) -> None:
        # T_a_full과 같은 장치 — 한 쪽이 가득, `max_pages=1`. 읽지 못한 댓글에 링크가 없다고 말할 수 없다.
        rows = [comment_row(f"3{n:018d}", "링크 없는 댓글") for n in range(100)]
        reader, http = self.make_reader(self.open_feed(reply(rows, total=500)))
        found = collect.thread_people(reader, plain_post(), max_pages=1, max_lookups=5)
        self.assertNotIn(STREAMS, self.paths(http))
        self.assertTrue(found.comments_cut)
        self.assertIsNone(found.tenant)
        self.assertFalse(found.feed_read)

    def test_TF_33_the_call_limit_on_the_feed_is_raised(self) -> None:
        # 댓글 1회로 상한이 찬다 — 피드는 보내지 않고 `CallLimitError`가 올라간다(잡아서 None으로 두지 않는다).
        reader, http = self.make_reader(self.open_feed(plain_logs_reply()), call_limit=1)
        with self.assertRaises(CallLimitError):
            collect.thread_people(reader, plain_post(), max_pages=3, max_lookups=5)
        self.assertEqual(self.paths(http), [logs_path(PID, POST_ID)])


# --- 023 — 프로젝트 멤버의 멘션 범위 (spec §2-3, §7-1 CO-1) ---------------------


class ProjectPeopleTest(CollectCase):
    """`project_people` — 명단(이름 없음)의 이름을 채운다. 피드·댓글·`me`를 읽지 않는다."""

    KNOWN = "1000000000000000801"

    def member_routes(self, ids: list[str]) -> dict:
        return {
            ("GET", f"{MEMBERS}/{one}"): (200, member_reply(one, f"멤버{one[-2:]}"), None) for one in ids
        }

    def named_reader(self, ids: list[str]):
        """`KNOWN`의 이름을 이름표에 넣어 둔 Reader. 넣는 데 쓴 GET 1회는 뒤에서 뺀다."""
        reader, http = self.make_reader(self.member_routes([self.KNOWN, *ids]))
        reader.member(self.KNOWN)
        return reader, http, len(http.requests)

    def test_CO_1_a_known_name_asks_nothing_and_others_are_looked_up(self) -> None:
        others = ["1000000000000000802", "1000000000000000803"]
        reader, http, before = self.named_reader(others)
        members = [
            models.ProjectMember(id=self.KNOWN, role="admin"),
            *(models.ProjectMember(id=one, role="member") for one in others),
            models.ProjectMember(id=self.KNOWN, role="admin"),  # 중복은 한 번
            models.ProjectMember(id="10/../x", role="member"),  # 모양이 틀린 ID
        ]
        found = collect.project_people(reader, members, tenant=TENANT, cut=False, max_lookups=20)
        looked = self.paths(http)[before:]
        self.assertEqual(looked, [f"{MEMBERS}/{one}" for one in others])
        self.assertEqual([person.id for person in found.people], [self.KNOWN, *others])
        self.assertEqual(found.tenant, TENANT)
        self.assertFalse(found.feed_read)
        self.assertFalse(found.comments_cut)
        self.assertEqual(found.unnamed, 1)  # 모양이 틀린 ID 하나

    def test_CO_1_over_the_limit_asks_nobody(self) -> None:
        strangers = [f"10000000000000008{n:02d}" for n in range(2, 23)]  # 이름표에 없는 21명
        reader, http, before = self.named_reader(strangers)
        members = [models.ProjectMember(id=one, role="member") for one in [self.KNOWN, *strangers]]
        found = collect.project_people(reader, members, tenant=TENANT, cut=False, max_lookups=20)
        self.assertEqual(self.paths(http)[before:], [])  # 이름 GET 0
        self.assertEqual([person.id for person in found.people], [self.KNOWN])
        self.assertEqual(found.unnamed, 21)

        # 상한과 같으면 모두 조회한다.
        reader, http, before = self.named_reader(strangers[:20])
        members = [models.ProjectMember(id=one, role="member") for one in strangers[:20]]
        found = collect.project_people(reader, members, tenant=TENANT, cut=False, max_lookups=20)
        self.assertEqual(len(self.paths(http)[before:]), 20)
        self.assertEqual(found.unnamed, 0)

    def test_CO_1_a_cut_roster_asks_nobody(self) -> None:
        others = ["1000000000000000802"]
        reader, http, before = self.named_reader(others)
        members = [models.ProjectMember(id=one, role="member") for one in [self.KNOWN, *others]]
        found = collect.project_people(reader, members, tenant=None, cut=True, max_lookups=20)
        self.assertEqual(self.paths(http)[before:], [])  # 이름 GET 0
        self.assertTrue(found.comments_cut)
        self.assertEqual(found.unnamed, 1)
        self.assertEqual([person.id for person in found.people], [self.KNOWN])
        self.assertIsNone(found.tenant)

    def test_CO_1_a_failed_lookup_is_counted(self) -> None:
        reader, http = self.make_reader(
            {("GET", f"{MEMBERS}/1000000000000000802"): (404, {"header": {"isSuccessful": False}}, None)}
        )
        members = [models.ProjectMember(id="1000000000000000802", role="member")]
        found = collect.project_people(reader, members, tenant=TENANT, cut=False, max_lookups=20)
        self.assertEqual(found.people, ())
        self.assertEqual(found.unnamed, 1)
        self.assertEqual(len(http.requests), 1)


# --- 007 — 내가 쓴 댓글 (spec §6-2, §8-2의 F 계열) ------------------------------


class MyCommentsTest(FeedCase):
    def mine(self, reader, **kwargs) -> collect.MyCommentScan:
        kwargs.setdefault("my_id", MY_ID)
        kwargs.setdefault("since", FEED_SINCE)
        kwargs.setdefault("max_pages", 3)
        kwargs.setdefault("limit", 10)
        return collect.collect_my_comments_from_feed(reader, **kwargs)

    def test_F1_only_the_token_owner_comments_and_no_system_event(self) -> None:
        reader, http = self.make_reader(self.feed_routes())
        scan = self.mine(reader)
        self.assertEqual([row.comment_id[-3:] for row in scan.comments], ["307", "306"])
        self.assertEqual(scan.total, 2)
        self.assertTrue(scan.reached_since)
        self.assertIsNone(scan.stopped_reason)
        self.assertEqual(scan.unread_posts, 0)
        self.assertEqual([row.task_number for row in scan.comments], ["plan/6", "sample/5"])
        # 업무 조회는 고른 댓글의 업무 둘뿐이다.
        posts = sorted(path for path in self.paths(http) if path.startswith(POST_PATH))
        self.assertEqual(
            posts, [f"{POST_PATH}/1000000000000000026", f"{POST_PATH}/1000000000000000027"]
        )

    def test_F2_the_newest_limit_and_the_total_before_the_cut(self) -> None:
        items = [
            feed_event(
                f"10000000000000005{n:02d}",
                f"내 댓글 {n}",
                author=MY_ID,
                created=f"2026-09-19T{n:02d}:00:00+09:00",
                post_id=post_id,
            )
            for n, post_id in reversed(
                list(enumerate(["1000000000000000026"] * 3 + ["1000000000000000027", "1000000000000000028"]))
            )
        ]
        reader, http = self.make_reader(
            self.feed_routes(pages=[(200, feed_page(items, None), None)])
        )
        scan = self.mine(reader, limit=2)
        self.assertEqual([row.body for row in scan.comments], ["내 댓글 3", "내 댓글 4"])
        self.assertEqual(scan.total, 5)
        posts = sorted(path for path in self.paths(http) if path.startswith(POST_PATH))
        self.assertEqual(
            posts, [f"{POST_PATH}/1000000000000000027", f"{POST_PATH}/1000000000000000028"]
        )

    def test_F3_an_unreadable_post_is_counted_and_left_unnamed(self) -> None:
        routes = self.feed_routes()
        routes[("GET", f"{POST_PATH}/1000000000000000026")] = (404, load_fixture("error_404"), None)
        reader, _ = self.make_reader(routes)
        scan = self.mine(reader)
        self.assertEqual(scan.unread_posts, 1)
        by_tail = {row.comment_id[-3:]: row for row in scan.comments}
        self.assertIsNone(by_tail["306"].task_number)
        self.assertEqual(by_tail["307"].task_number, "plan/6")
        self.assertIsNone(scan.stopped_reason)

    def test_F3_the_call_limit_is_a_stop_reason(self) -> None:
        page = feed_page(load_fixture("streams")["result"], None)
        reader, _ = self.make_reader(
            self.feed_routes(pages=[(200, page, None)]), call_limit=3
        )
        scan = self.mine(reader)
        self.assertEqual(scan.stopped_reason, collect.CALL_LIMIT_REASON)
        self.assertEqual(len(scan.comments), 2)  # 이미 모은 댓글은 버리지 않는다

    def test_F3_a_server_error_on_a_post_is_a_stop_reason(self) -> None:
        routes = self.feed_routes()
        routes[("GET", f"{POST_PATH}/1000000000000000026")] = (503, {"header": {}}, None)
        reader, _ = self.make_reader(routes)
        scan = self.mine(reader)
        self.assertEqual(scan.stopped_reason, "ServerError")
        self.assertEqual(len(scan.comments), 2)

    def test_F3_an_auth_error_is_not_caught(self) -> None:
        reader, _ = self.make_reader(self.feed_routes(pages=[(401, {"header": {}}, None)]))
        with self.assertRaises(AuthError):
            self.mine(reader)

    def test_F4_a_comment_repeated_across_pages_is_one(self) -> None:
        one = feed_event("1000000000000000501", "내 댓글", author=MY_ID, post_id="1000000000000000026")
        pages = [
            (200, feed_page([one], "커서1"), None),
            (200, feed_page([one], None), None),
        ]
        reader, _ = self.make_reader(self.feed_routes(pages=pages))
        scan = self.mine(reader)
        self.assertEqual(scan.total, 1)
        self.assertEqual(len(scan.comments), 1)

    def test_F_the_feed_page_limit_is_reported(self) -> None:
        one = feed_event("1000000000000000501", "내 댓글", author=MY_ID, post_id="1000000000000000026")
        reader, _ = self.make_reader(
            self.feed_routes(pages=[(200, feed_page([one], "커서1"), None)])
        )
        scan = self.mine(reader, max_pages=1)
        self.assertFalse(scan.reached_since)


if __name__ == "__main__":
    unittest.main()
