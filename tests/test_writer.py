"""spec §7(`writer.py`) — §11-3의 W 케이스와 완료 기준 25·26·57.

실제 POST를 보내지 않는다. `httpx2.MockTransport`로만 돈다 (§7-1, 제약 9).
"""

from __future__ import annotations

import json
from unittest import mock

from dooray_core import client, config, writer
from dooray_core.errors import CallLimitError, InvalidIdError, ServerError
from support import FAKE_TOKEN, CoreTestCase, load_fixture, mock_transport

BASE = config.ALLOWED_API_BASES[0]
PID = "1000000000000000018"
POST_ID = "1000000000000000026"
LOGS = f"/project/v1/projects/{PID}/posts/{POST_ID}/logs"
CREATE = f"/project/v1/projects/{PID}/posts"
SET_PARENT = f"/project/v1/projects/{PID}/posts/{POST_ID}/set-parent-post"
UPDATE = f"/project/v1/projects/{PID}/posts/{POST_ID}"
COMMENT_ID = "1000000000000000056"
EDIT_COMMENT = f"/project/v1/projects/{PID}/posts/{POST_ID}/logs/{COMMENT_ID}"
SET_WORKFLOW = f"/project/v1/projects/{PID}/posts/{POST_ID}/set-workflow"
SET_DONE = f"/project/v1/projects/{PID}/posts/{POST_ID}/set-done"
MOVE = f"/project/v1/projects/{PID}/posts/{POST_ID}/move"  # 022
TARGET_PID = "1000000000000000190"  # 022 — 옮길 곳
WORKFLOW_ID = "1000000000000000201"
MEMBER_ID = "1000000000000000009"
NEW_COMMENT_ID = "1000000000000000099"
NEW_POST_ID = "1000000000000000101"
PARENT_ID = "1000000000000000102"
AUDIT = "audit-2026-09-20.jsonl"


class FakeSleep:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def posted() -> dict:
    return {
        "header": {"isSuccessful": True, "resultCode": 0, "resultMessage": ""},
        "result": {"id": NEW_COMMENT_ID},
    }


class WriterCase(CoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sleep = FakeSleep()
        self.enterContext(mock.patch.object(client, "_audit_swept", False))
        self.enterContext(mock.patch.object(client, "_audit_warned", False))
        self.audit_path = self.home / "logs" / AUDIT

    def make_writer(self, routes: dict, **kwargs: object):
        http = mock_transport(routes)
        made = writer.DoorayWriter(
            FAKE_TOKEN,
            BASE,
            tool="post_comment",
            http_transport=http,
            sleep=self.sleep,
            audit_path=self.audit_path,
            **kwargs,
        )
        self.addCleanup(made.close)
        return made, http

    def audit_lines(self) -> list[dict]:
        text = self.audit_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


class PostCommentTest(WriterCase):
    def test_W1_the_request_shape_and_the_returned_id(self) -> None:
        made, http = self.make_writer({("POST", LOGS): (200, posted(), None)})
        result = made.post_comment(
            project_id=PID, post_id=POST_ID, content="확인했습니다. 내일까지 회신드리겠습니다."
        )

        self.assertEqual(len(http.requests), 1)
        sent = http.requests[0]
        self.assertEqual(sent.method, "POST")
        self.assertEqual(sent.url.path, LOGS)
        # Content-Type이 없으면 415다 (api-notes §3).
        self.assertEqual(sent.headers["Content-Type"], "application/json")
        body = json.loads(sent.content)
        self.assertEqual(
            body,
            {
                "body": {
                    "content": "확인했습니다. 내일까지 회신드리겠습니다.",
                    "mimeType": "text/x-markdown",
                }
            },
        )
        # attachFileIds를 보내지 않는다 (§7).
        self.assertNotIn("attachFileIds", body)

        self.assertIsInstance(result, writer.PostedComment)
        self.assertEqual(result.id, NEW_COMMENT_ID)
        self.assertEqual(result.post_id, POST_ID)
        self.assertEqual(result.project_id, PID)

    def test_W1_one_call_is_enough(self) -> None:
        made, http = self.make_writer({("POST", LOGS): (200, posted(), None)})
        made.post_comment(project_id=PID, post_id=POST_ID, content="네")
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(made._t.calls, 1)

    def test_W4_the_mime_type_is_always_markdown(self) -> None:
        # 인자로 열지 않는다 — 인젝션 표면을 넓히지 않는다 (§7).
        made, http = self.make_writer({("POST", LOGS): [(200, posted(), None)] * 2})
        made.post_comment(project_id=PID, post_id=POST_ID, content="<b>굵게</b>")
        made.post_comment(project_id=PID, post_id=POST_ID, content="# 제목")
        for sent in http.requests:
            self.assertEqual(json.loads(sent.content)["body"]["mimeType"], "text/x-markdown")

        with self.assertRaises(TypeError):
            made.post_comment(
                project_id=PID, post_id=POST_ID, content="x", mime_type="text/html"
            )

    def test_W3_blank_content_is_refused_before_any_request(self) -> None:
        made, http = self.make_writer({("POST", LOGS): (200, posted(), None)})
        for blank in ("", "   ", "\n\t ", " " * 0):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(ValueError) as caught:
                    made.post_comment(project_id=PID, post_id=POST_ID, content=blank)
                self.record(caught.exception)
        self.assertEqual(len(http.requests), 0, "호출 0회")
        self.assertEqual(made._t.calls, 0)

    def test_the_surrounding_whitespace_of_real_content_is_kept(self) -> None:
        made, http = self.make_writer({("POST", LOGS): (200, posted(), None)})
        made.post_comment(project_id=PID, post_id=POST_ID, content="  안녕하세요\n")
        self.assertEqual(json.loads(http.requests[0].content)["body"]["content"], "  안녕하세요\n")

    def test_a_server_error_is_not_retried(self) -> None:
        # 댓글 등록은 멱등하지 않다 (§5-1).
        made, http = self.make_writer({("POST", LOGS): (503, {"header": {}}, None)})
        with self.assertRaises(ServerError) as caught:
            made.post_comment(project_id=PID, post_id=POST_ID, content="네")
        self.record(caught.exception)
        self.assertEqual(len(http.requests), 1)

    def test_the_default_call_budget_is_five(self) -> None:
        made, _ = self.make_writer({("POST", LOGS): [(200, posted(), None)] * 6})
        self.assertEqual(made._t.call_limit, 5)
        for _ in range(5):
            made.post_comment(project_id=PID, post_id=POST_ID, content="네")
        with self.assertRaises(CallLimitError):
            made.post_comment(project_id=PID, post_id=POST_ID, content="네")


    def test_W5_a_bent_id_never_becomes_another_write(self) -> None:
        # `post_id="20/set-done?x="`가 검증 없이 경로에 들어가면 업무 완료 처리 API로 POST가 나간다.
        # 제약 3("쓰기는 셋뿐")을 지키는 테스트다 (묶음 2 검토에서 실제로 재현했다).
        writer_, http = self.make_writer({})
        for project_id, post_id in (
            ("10", "20/set-done?x="),
            ("10", "20/../../../../common/v1/incoming-hooks#"),
            ("10/posts/20/set-done?", "20"),
            ("10", ""),
            ("10", ".."),
        ):
            with self.subTest(project_id=project_id, post_id=post_id):
                with self.assertRaises(InvalidIdError):
                    writer_.post_comment(project_id=project_id, post_id=post_id, content="내용")
        self.assertEqual(http.requests, [])


class CreatePostTest(WriterCase):
    """010 spec §9-2의 W1-c~W1-f."""

    def make(self, routes: dict | None = None):
        return self.make_writer(
            {("POST", CREATE): (200, load_fixture("post_created"), None), **(routes or {})}
        )

    def test_W1_c_the_request_shape(self) -> None:
        made, http = self.make()
        result = made.create_post(
            project_id=PID,
            subject="다음 주 발표 자료 정리",
            content="배경\n\n할 일",
            parent_post_id=PARENT_ID,
            due_date="2026-09-30T18:00:00+09:00",
            tag_ids=["1000000000000000110"],
        )

        self.assertEqual(len(http.requests), 1)
        sent = http.requests[0]
        self.assertEqual(sent.method, "POST")
        self.assertEqual(sent.url.path, CREATE)
        self.assertEqual(sent.headers["Content-Type"], "application/json")
        body = json.loads(sent.content)
        self.assertEqual(
            body,
            {
                "parentPostId": PARENT_ID,
                "subject": "다음 주 발표 자료 정리",
                "body": {"mimeType": "text/x-markdown", "content": "배경\n\n할 일"},
                "dueDate": "2026-09-30T18:00:00+09:00",
                "dueDateFlag": True,
                "tagIds": ["1000000000000000110"],
            },
        )
        # 담당·참조와 그 밖의 필드는 인자조차 없다 (ADR 008 결정 3).
        for absent in ("users", "milestoneId", "priority", "attachFileIds"):
            with self.subTest(key=absent):
                self.assertNotIn(absent, body)
        self.assertIsInstance(result, writer.CreatedPost)
        self.assertEqual(result.id, NEW_POST_ID)
        self.assertEqual(result.project_id, PID)

    def test_W1_d_the_optional_keys_are_absent_when_not_given(self) -> None:
        made, http = self.make()
        made.create_post(project_id=PID, subject="제목", content="본문")
        body = json.loads(http.requests[0].content)
        self.assertEqual(
            body, {"subject": "제목", "body": {"mimeType": "text/x-markdown", "content": "본문"}}
        )
        # `null`로도 보내지 않는다 — 200을 받은 실측 본문에 그 키들이 없었다.
        for absent in ("parentPostId", "dueDate", "dueDateFlag", "tagIds"):
            with self.subTest(key=absent):
                self.assertNotIn(absent, body)

    def test_W1_e_the_flag_only_travels_with_a_due_date(self) -> None:
        made, http = self.make()
        made.create_post(project_id=PID, subject="제목", content="본문")
        self.assertNotIn("dueDateFlag", json.loads(http.requests[0].content))
        made.create_post(
            project_id=PID, subject="제목", content="본문", due_date="2026-09-30T18:00:00+09:00"
        )
        second = json.loads(http.requests[1].content)
        self.assertIs(second["dueDateFlag"], True)
        self.assertEqual(second["dueDate"], "2026-09-30T18:00:00+09:00")

    def test_W1_e_an_empty_tag_list_sends_no_key(self) -> None:
        made, http = self.make()
        made.create_post(project_id=PID, subject="제목", content="본문", tag_ids=[])
        self.assertNotIn("tagIds", json.loads(http.requests[0].content))

    def test_W1_f_the_milestone_in_the_answer_is_not_read(self) -> None:
        made, _ = self.make()
        result = made.create_post(project_id=PID, subject="제목", content="본문")
        self.assertEqual({name for name in vars(result)}, {"id", "project_id"})

    def test_W1_f_a_missing_id_leaves_the_field_empty(self) -> None:
        made, _ = self.make_writer({("POST", CREATE): (200, {"result": {}}, None)})
        self.assertEqual(made.create_post(project_id=PID, subject="제목", content="본문").id, "")

    def test_a_blank_subject_or_body_is_refused_before_any_request(self) -> None:
        made, http = self.make()
        for subject, content in (("", "본문"), ("   ", "본문"), ("제목", ""), ("제목", "\n\t ")):
            with self.subTest(subject=repr(subject), content=repr(content)):
                with self.assertRaises(ValueError) as caught:
                    made.create_post(project_id=PID, subject=subject, content=content)
                self.record(caught.exception)
        self.assertEqual(http.requests, [])
        self.assertEqual(made.calls, 0)

    def test_the_mime_type_is_not_an_argument(self) -> None:
        made, _ = self.make()
        with self.assertRaises(TypeError):
            made.create_post(
                project_id=PID, subject="제목", content="본문", mime_type="text/html"
            )

    def test_W6_b_a_creation_counts_one_call(self) -> None:
        made, http = self.make()
        made.create_post(project_id=PID, subject="제목", content="본문")
        self.assertEqual(made.calls, 1)
        self.assertEqual(made.calls, len(http.requests))

    def test_W6_b_a_failed_creation_counts_one_too(self) -> None:
        made, http = self.make_writer({("POST", CREATE): (400, load_fixture("error_400"), None)})
        with self.assertRaises(Exception) as caught:
            made.create_post(project_id=PID, subject="제목", content="본문")
        self.record(caught.exception)
        self.assertEqual(made.calls, 1)
        self.assertEqual(len(http.requests), 1)


class SetParentPostTest(WriterCase):
    """010 spec §9-2의 W1-g."""

    def make(self):
        return self.make_writer({("POST", SET_PARENT): (200, load_fixture("parent_set"), None)})

    def test_W1_g_the_body_is_one_key(self) -> None:
        made, http = self.make()
        result = made.set_parent_post(
            project_id=PID, post_id=POST_ID, parent_post_id=PARENT_ID
        )
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(http.requests[0].url.path, SET_PARENT)
        self.assertEqual(json.loads(http.requests[0].content), {"parentPostId": PARENT_ID})
        self.assertIsInstance(result, writer.MovedPost)
        self.assertEqual(result.post_id, POST_ID)
        self.assertEqual(result.project_id, PID)
        self.assertEqual(result.parent_post_id, PARENT_ID)

    def test_W1_g_detaching_keeps_the_key_and_sends_null(self) -> None:
        made, http = self.make()
        result = made.set_parent_post(project_id=PID, post_id=POST_ID, parent_post_id=None)
        body = json.loads(http.requests[0].content)
        self.assertEqual(body, {"parentPostId": None})
        self.assertIn("parentPostId", body)
        self.assertIsNone(result.parent_post_id)

    def test_an_answer_without_the_shape_falls_back_to_what_we_sent(self) -> None:
        made, _ = self.make_writer({("POST", SET_PARENT): (200, {"result": {}}, None)})
        result = made.set_parent_post(
            project_id=PID, post_id=POST_ID, parent_post_id=PARENT_ID
        )
        self.assertEqual((result.post_id, result.project_id), (POST_ID, PID))

    def test_W6_b_a_move_counts_one_call(self) -> None:
        made, http = self.make()
        made.set_parent_post(project_id=PID, post_id=POST_ID, parent_post_id=PARENT_ID)
        self.assertEqual(made.calls, 1)
        self.assertEqual(made.calls, len(http.requests))


class MovePostWriterTest(WriterCase):
    """022 spec §7-1의 WR-1 — 업무를 다른 프로젝트로 옮기는 요청 하나."""

    def make(self):
        moved = {"result": {"post": {"id": POST_ID}, "project": {"id": "1000000000000000999"}}}
        return self.make_writer({("POST", MOVE): (200, moved, None)})

    def test_WR_1_the_request_is_one_post_with_two_fixed_keys(self) -> None:
        made, http = self.make()
        result = made.move_post(project_id=PID, post_id=POST_ID, target_project_id=TARGET_PID)
        self.assertIsNone(result)  # 응답을 읽지 않는다
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(made.calls, 1)
        self.assertEqual((http.requests[0].method, http.requests[0].url.path), ("POST", MOVE))
        body = json.loads(http.requests[0].content)
        self.assertEqual(set(body), {"targetProjectId", "includeSubPosts"})
        self.assertEqual(body["targetProjectId"], TARGET_PID)
        self.assertIs(body["includeSubPosts"], True)

    def test_WR_1_a_bent_id_in_any_place_sends_nothing(self) -> None:
        made, http = self.make()
        for bent in ("30/../../x", "?", "1" * 65):
            for place in ("project_id", "post_id", "target_project_id"):
                ids = {"project_id": PID, "post_id": POST_ID, "target_project_id": TARGET_PID}
                ids[place] = bent
                with self.subTest(bent=bent, place=place):
                    with self.assertRaises(InvalidIdError):
                        made.move_post(**ids)
        self.assertEqual(http.requests, [])
        self.assertEqual(made.calls, 0)

    def test_WR_1_the_same_project_is_refused_before_sending(self) -> None:
        made, http = self.make()
        with self.assertRaises(ValueError):
            made.move_post(project_id=PID, post_id=POST_ID, target_project_id=PID)
        self.assertEqual(http.requests, [])

    def test_WR_1_there_is_no_argument_to_choose_the_subposts(self) -> None:
        import inspect

        names = list(inspect.signature(writer.DoorayWriter.move_post).parameters)
        self.assertEqual(names, ["self", "project_id", "post_id", "target_project_id"])
        self.assertEqual([name for name in names if name.startswith("include")], [])


class ProjectDescriptionWriterTest(WriterCase):
    """023 spec §7-1의 WR-1 — 프로젝트 설명을 바꾸는 요청 하나."""

    PATH = f"/project/v1/projects/{PID}"

    def make(self):
        return self.make_writer({("PUT", self.PATH): (200, {"result": None}, None)})

    def test_WR_1_the_request_is_one_put_with_only_the_description(self) -> None:
        made, http = self.make()
        result = made.update_project_description(project_id=PID, description="제목\n\n![a.png](/files/1)")
        self.assertIsNone(result)  # 응답을 읽지 않는다
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(made.calls, 1)
        self.assertEqual((http.requests[0].method, http.requests[0].url.path), ("PUT", self.PATH))
        self.assertEqual(json.loads(http.requests[0].content), {"description": "제목\n\n![a.png](/files/1)"})

    def test_WR_1_an_empty_description_sends_nothing(self) -> None:
        made, http = self.make()
        for empty in ("", "   ", "\n\t", None):
            with self.subTest(empty=empty):
                with self.assertRaises(ValueError):
                    made.update_project_description(project_id=PID, description=empty)  # type: ignore[arg-type]
        self.assertEqual(http.requests, [])
        self.assertEqual(made.calls, 0)

    def test_WR_1_a_bent_id_sends_nothing(self) -> None:
        made, http = self.make()
        for bent in ("30/../../x", "10/posts/20", "?", "", "1" * 65):
            with self.subTest(bent=bent):
                with self.assertRaises(InvalidIdError):
                    made.update_project_description(project_id=bent, description="설명")
        self.assertEqual(http.requests, [])
        self.assertEqual(made.calls, 0)

    def test_WR_1_the_signature_has_no_other_field(self) -> None:
        import inspect

        names = list(inspect.signature(writer.DoorayWriter.update_project_description).parameters)
        self.assertEqual(names, ["self", "project_id", "description"])


class WriteIdSafetyTest(WriterCase):
    """W5-b — 굽은 ID는 다른 쓰기가 되지 않는다. 요청이 한 번도 나가지 않는다."""

    BENT = (
        "110/set-done?x=",
        "110/../../../../common/v1/incoming-hooks#",
        "10/posts/20/set-done?",
        "",
        "..",
    )

    def test_W5_b_a_bent_id_never_becomes_another_write(self) -> None:
        made, http = self.make_writer({})
        for bent in self.BENT:
            with self.subTest(bent=bent):
                with self.assertRaises(InvalidIdError) as caught:
                    made.create_post(project_id=bent, subject="제목", content="본문")
                self.record(caught.exception)
                with self.assertRaises(InvalidIdError):
                    made.create_post(project_id=PID, subject="제목", content="본문", parent_post_id=bent)
                # 본문에 든 태그 ID도 같은 관문을 지난다.
                with self.assertRaises(InvalidIdError):
                    made.create_post(project_id=PID, subject="제목", content="본문", tag_ids=[bent])
                with self.assertRaises(InvalidIdError):
                    made.set_parent_post(project_id=PID, post_id=bent, parent_post_id=PARENT_ID)
                with self.assertRaises(InvalidIdError):
                    made.set_parent_post(project_id=PID, post_id=POST_ID, parent_post_id=bent)
        self.assertEqual(http.requests, [])
        self.assertEqual(made.calls, 0)


class WriterAuditTest(WriterCase):
    def test_W2_a_successful_write_records_the_mode_and_the_comment_id(self) -> None:
        made, _ = self.make_writer({("POST", LOGS): (200, posted(), None)})
        made.post_comment(project_id=PID, post_id=POST_ID, content="확인했습니다.")

        lines = self.audit_lines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["kind"], "api")
        self.assertEqual(line["mode"], "write")
        self.assertEqual(line["method"], "POST")
        self.assertEqual(line["path"], LOGS)
        self.assertEqual(line["project_id"], PID)
        self.assertEqual(line["post_id"], POST_ID)
        self.assertEqual(line["comment_id"], NEW_COMMENT_ID)
        self.assertIs(line["ok"], True)
        self.assertEqual(line["tool"], "post_comment")

    def test_W2_the_comment_body_never_reaches_the_log(self) -> None:
        secret = "이-문장은-로그에-없어야-한다-4417"
        made, _ = self.make_writer({("POST", LOGS): (200, posted(), None)})
        made.post_comment(project_id=PID, post_id=POST_ID, content=secret)
        self.assertNotIn(secret, self.audit_path.read_text(encoding="utf-8"))

    def test_a_failed_write_records_no_comment_id(self) -> None:
        made, _ = self.make_writer({("POST", LOGS): (403, load_fixture("error_404"), None)})
        with self.assertRaises(Exception) as caught:
            made.post_comment(project_id=PID, post_id=POST_ID, content="네")
        self.record(caught.exception)
        line = self.audit_lines()[0]
        self.assertIsNone(line["comment_id"])
        self.assertEqual(line["mode"], "write")
        self.assertEqual(line["error"], "ForbiddenError")

    def test_W2_c_the_creation_line_carries_the_new_post_id_and_no_text(self) -> None:
        secret_subject = "이-제목은-로그에-없어야-한다-8821"
        secret_body = "이-본문은-로그에-없어야-한다-8822"
        made, _ = self.make_writer(
            {("POST", CREATE): (200, load_fixture("post_created"), None)}
        )
        made.create_post(
            project_id=PID,
            subject=secret_subject,
            content=secret_body,
            tag_ids=["1000000000000000110"],
        )

        lines = self.audit_lines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["kind"], "api")
        self.assertEqual(line["mode"], "write")
        self.assertEqual(line["method"], "POST")
        self.assertEqual(line["path"], CREATE)
        self.assertEqual(line["project_id"], PID)
        # `_created_id`가 만들어진 항목의 ID를 적는다 — 생성이면 새 업무 ID다.
        self.assertEqual(line["comment_id"], NEW_POST_ID)
        # 전송 계층은 본문을 보지 않는다 — api 줄의 `parent_id`는 언제나 null이다.
        self.assertIsNone(line["parent_id"])
        blob = self.audit_path.read_text(encoding="utf-8")
        for secret in (secret_subject, secret_body, "긴급"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_W2_d_the_move_line_has_a_post_id_and_no_comment_id(self) -> None:
        made, _ = self.make_writer(
            {("POST", SET_PARENT): (200, load_fixture("parent_set"), None)}
        )
        made.set_parent_post(project_id=PID, post_id=POST_ID, parent_post_id=PARENT_ID)
        line = self.audit_lines()[0]
        self.assertEqual(line["path"], SET_PARENT)
        self.assertEqual(line["post_id"], POST_ID)
        self.assertEqual(line["project_id"], PID)
        # 응답에 최상위 `result.id`가 없다 (api-notes 010).
        self.assertIsNone(line["comment_id"])
        self.assertIsNone(line["parent_id"])

    def test_the_audit_field_list_holds_parent_id(self) -> None:
        self.assertIn("parent_id", client.AUDIT_FIELDS)


def write_null() -> dict:
    """고치기·상태 요청의 응답 — `result: null` [문서·실측 둘째·셋째]."""
    return {"header": {"isSuccessful": True, "resultCode": 0, "resultMessage": ""}, "result": None}


def to_path(member: str) -> str:
    return f"/project/v1/projects/{PID}/posts/{POST_ID}/to/{member}"


class UpdatePostTest(WriterCase):
    """012 spec §10-1의 WE-1~WE-4 — 업무 수정은 **준 키만** 보낸다."""

    def make(self):
        return self.make_writer({("PUT", UPDATE): (200, write_null(), None)})

    def sent_body(self, changes: writer.PostChanges) -> dict:
        made, http = self.make()
        self.assertIsNone(made.update_post(project_id=PID, post_id=POST_ID, changes=changes))
        self.assertEqual(len(http.requests), 1)
        sent = http.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("PUT", UPDATE))
        self.assertEqual(sent.headers["Content-Type"], "application/json")
        return json.loads(sent.content)

    def test_WE_1_each_field_alone_sends_only_its_keys(self) -> None:
        cases = (
            (writer.PostChanges(subject="새 제목"), {"subject": "새 제목"}),
            (
                writer.PostChanges(body=("text/x-markdown", "새 본문")),
                {"body": {"mimeType": "text/x-markdown", "content": "새 본문"}},
            ),
            (
                writer.PostChanges(milestone_id="1000000000000000301"),
                {"milestoneId": "1000000000000000301"},
            ),
            (writer.PostChanges(clear_milestone=True), {"milestoneId": None}),
            (
                writer.PostChanges(tag_ids=("1000000000000000110", "1000000000000000111")),
                {"tagIds": ["1000000000000000110", "1000000000000000111"]},
            ),
            (writer.PostChanges(tag_ids=()), {"tagIds": []}),  # 태그를 모두 뗀다
            (writer.PostChanges(priority="high"), {"priority": "high"}),
            (writer.PostChanges(priority="none"), {"priority": "none"}),
        )
        for changes, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.sent_body(changes), expected)

    def test_WE_1_two_fields_mixed_send_both_and_nothing_else(self) -> None:
        body = self.sent_body(
            writer.PostChanges(subject="새 제목", body=("text/html", "<p>새 본문</p>"))
        )
        self.assertEqual(
            body,
            {"subject": "새 제목", "body": {"mimeType": "text/html", "content": "<p>새 본문</p>"}},
        )
        # 안 준 키는 `null`로도 없다 — 맨 위 키는 보낸 것만 바뀐다 (012 실측).
        for absent in (
            "users",
            "dueDate",
            "dueDateFlag",
            "milestoneId",
            "tagIds",
            "priority",
            "version",
            "parentPostId",
            "estimatedDays",
        ):
            with self.subTest(key=absent):
                self.assertNotIn(absent, body)

    def test_WE_2_due_travels_with_its_flag_and_clearing_is_one_null_key(self) -> None:
        self.assertEqual(
            self.sent_body(writer.PostChanges(due="2026-09-30T18:00:00+09:00")),
            {"dueDate": "2026-09-30T18:00:00+09:00", "dueDateFlag": True},
        )
        self.assertEqual(self.sent_body(writer.PostChanges(clear_due=True)), {"dueDate": None})

    def test_WE_3_people_go_out_as_both_lists_of_members(self) -> None:
        body = self.sent_body(
            writer.PostChanges(
                assignee_ids=(MEMBER_ID, "1000000000000000019"), cc_ids=("1000000000000000027",)
            )
        )
        self.assertEqual(set(body), {"users"})
        self.assertEqual(
            body["users"],
            {
                "to": [
                    {"type": "member", "member": {"organizationMemberId": MEMBER_ID}},
                    {"type": "member", "member": {"organizationMemberId": "1000000000000000019"}},
                ],
                "cc": [{"type": "member", "member": {"organizationMemberId": "1000000000000000027"}}],
            },
        )

    def test_WE_3_empty_people_lists_are_still_both_sent(self) -> None:
        # 담당 0명을 받는다 (실측 둘째). 비어 있어도 두 목록을 함께 보낸다.
        self.assertEqual(
            self.sent_body(writer.PostChanges(assignee_ids=(), cc_ids=())),
            {"users": {"to": [], "cc": []}},
        )

    def test_WE_3_one_side_alone_or_the_same_person_on_both_sends_nothing(self) -> None:
        made, http = self.make()
        for changes in (
            writer.PostChanges(assignee_ids=(MEMBER_ID,)),
            writer.PostChanges(cc_ids=(MEMBER_ID,)),
            writer.PostChanges(assignee_ids=(), subject="제목"),
            writer.PostChanges(assignee_ids=(MEMBER_ID,), cc_ids=(MEMBER_ID,)),
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError) as caught:
                    made.update_post(project_id=PID, post_id=POST_ID, changes=changes)
                self.record(caught.exception)
        self.assertEqual(http.requests, [])
        self.assertEqual(made.calls, 0)

    def test_WE_4_bad_changes_are_refused_before_any_request(self) -> None:
        made, http = self.make()
        refused = (
            writer.PostChanges(),
            writer.PostChanges(subject="   "),
            writer.PostChanges(body=("text/plain", "본문")),
            writer.PostChanges(body=("text/x-markdown", " \n\t")),
            writer.PostChanges(due="2026-09-30T18:00:00+09:00", clear_due=True),
            writer.PostChanges(milestone_id="1000000000000000301", clear_milestone=True),
            writer.PostChanges(priority="hightest"),  # 문서의 오타 — 보내지 않는다
            writer.PostChanges(priority="HIGH"),
        )
        for changes in refused:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError) as caught:
                    made.update_post(project_id=PID, post_id=POST_ID, changes=changes)
                self.record(caught.exception)
        self.assertEqual(http.requests, [])

    def test_WE_4_bent_ids_in_the_body_or_the_path_send_nothing(self) -> None:
        made, http = self.make()
        bent = "20/set-done?x="
        for kwargs in (
            {"changes": writer.PostChanges(assignee_ids=(bent,), cc_ids=())},
            {"changes": writer.PostChanges(assignee_ids=(), cc_ids=(bent,))},
            {"changes": writer.PostChanges(tag_ids=(bent,))},
            {"changes": writer.PostChanges(milestone_id=bent)},
            {"changes": writer.PostChanges(subject="제목"), "post_id": bent},
            {"changes": writer.PostChanges(subject="제목"), "project_id": bent},
        ):
            call = {"project_id": PID, "post_id": POST_ID, **kwargs}
            with self.subTest(call=repr(call)):
                with self.assertRaises(InvalidIdError) as caught:
                    made.update_post(**call)
                self.record(caught.exception)
        self.assertEqual(http.requests, [])


class UpdateCommentTest(WriterCase):
    """WE-5 — 댓글 수정 본문은 `body` 한 키이고 형식은 원래 값 그대로다."""

    def test_WE_5_the_body_is_one_key_with_the_given_mime(self) -> None:
        made, http = self.make_writer({("PUT", EDIT_COMMENT): [(200, write_null(), None)] * 2})
        for mime, content in (("text/x-markdown", "고친 댓글"), ("text/html", "<p>고친 댓글</p>")):
            self.assertIsNone(
                made.update_comment(
                    project_id=PID, post_id=POST_ID, comment_id=COMMENT_ID, mime=mime, content=content
                )
            )
        self.assertEqual(
            [(row.method, row.url.path) for row in http.requests], [("PUT", EDIT_COMMENT)] * 2
        )
        self.assertEqual(
            [json.loads(row.content) for row in http.requests],
            [
                {"body": {"mimeType": "text/x-markdown", "content": "고친 댓글"}},
                {"body": {"mimeType": "text/html", "content": "<p>고친 댓글</p>"}},
            ],
        )

    def test_WE_5_an_unknown_mime_blank_content_or_bent_id_sends_nothing(self) -> None:
        made, http = self.make_writer({})
        for kwargs, error in (
            ({"mime": "text/plain", "content": "네"}, ValueError),
            ({"mime": None, "content": "네"}, ValueError),
            ({"mime": "text/x-markdown", "content": "  \n"}, ValueError),
            ({"mime": "text/x-markdown", "content": "네", "comment_id": "30/../../move"}, InvalidIdError),
            ({"mime": "text/x-markdown", "content": "네", "post_id": "20/set-done?x="}, InvalidIdError),
        ):
            call = {"project_id": PID, "post_id": POST_ID, "comment_id": COMMENT_ID, **kwargs}
            with self.subTest(call=repr(call)):
                with self.assertRaises(error) as caught:
                    made.update_comment(**call)  # type: ignore[arg-type]
                self.record(caught.exception)
        self.assertEqual(http.requests, [])


class StatusWriteTest(WriterCase):
    """WE-6 — 상태 셋. 완료 처리는 본문이 없다."""

    def test_WE_6_set_workflow_posts_one_key(self) -> None:
        made, http = self.make_writer({("POST", SET_WORKFLOW): (200, write_null(), None)})
        self.assertIsNone(made.set_workflow(project_id=PID, post_id=POST_ID, workflow_id=WORKFLOW_ID))
        sent = http.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("POST", SET_WORKFLOW))
        self.assertEqual(json.loads(sent.content), {"workflowId": WORKFLOW_ID})

    def test_WE_6_set_assignee_workflow_puts_to_me_or_a_member_id(self) -> None:
        made, http = self.make_writer(
            {
                ("PUT", to_path("me")): (200, write_null(), None),
                ("PUT", to_path(MEMBER_ID)): (200, write_null(), None),
            }
        )
        made.set_assignee_workflow(project_id=PID, post_id=POST_ID, member="me", workflow_id=WORKFLOW_ID)
        made.set_assignee_workflow(
            project_id=PID, post_id=POST_ID, member=MEMBER_ID, workflow_id=WORKFLOW_ID
        )
        self.assertEqual(
            [(row.method, row.url.path) for row in http.requests],
            [("PUT", to_path("me")), ("PUT", to_path(MEMBER_ID))],
        )
        for row in http.requests:
            self.assertEqual(json.loads(row.content), {"workflowId": WORKFLOW_ID})

    def test_WE_6_set_done_posts_without_a_body(self) -> None:
        made, http = self.make_writer({("POST", SET_DONE): (200, write_null(), None)})
        self.assertIsNone(made.set_done(project_id=PID, post_id=POST_ID))
        sent = http.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("POST", SET_DONE))
        self.assertEqual(sent.content, b"")

    def test_WE_6_bent_ids_send_nothing(self) -> None:
        made, http = self.make_writer({})
        bent = "40/../../move"
        calls = (
            lambda: made.set_assignee_workflow(
                project_id=PID, post_id=POST_ID, member=bent, workflow_id=WORKFLOW_ID
            ),
            lambda: made.set_assignee_workflow(
                project_id=PID, post_id=POST_ID, member="me", workflow_id=bent
            ),
            lambda: made.set_workflow(project_id=PID, post_id=POST_ID, workflow_id=bent),
            lambda: made.set_workflow(project_id=PID, post_id=bent, workflow_id=WORKFLOW_ID),
            lambda: made.set_done(project_id=PID, post_id="20/set-done?x="),
            lambda: made.set_done(project_id=bent, post_id=POST_ID),
        )
        for index, call in enumerate(calls):
            with self.subTest(index=index):
                with self.assertRaises(InvalidIdError) as caught:
                    call()
                self.record(caught.exception)
        self.assertEqual(http.requests, [])


class EditAuditTest(WriterCase):
    """WE-7 — 고치기의 api 줄. 본문·제목이 없고 `comment_id`·`changed`가 null이다."""

    def test_WE_7_the_put_line_has_no_text_and_no_ids_it_did_not_make(self) -> None:
        secret_subject = "이-제목은-로그에-없어야-한다-9911"
        secret_body = "이-본문은-로그에-없어야-한다-9912"
        made, _ = self.make_writer(
            {
                ("PUT", UPDATE): (200, {"result": {"id": "1000000000000000777"}}, None),
                ("PUT", EDIT_COMMENT): (200, write_null(), None),
            }
        )
        made.update_post(
            project_id=PID,
            post_id=POST_ID,
            changes=writer.PostChanges(subject=secret_subject, body=("text/x-markdown", secret_body)),
        )
        made.update_comment(
            project_id=PID, post_id=POST_ID, comment_id=COMMENT_ID, mime="text/x-markdown", content=secret_body
        )
        lines = self.audit_lines()
        self.assertEqual([(line["method"], line["path"]) for line in lines], [("PUT", UPDATE), ("PUT", EDIT_COMMENT)])
        for line in lines:
            with self.subTest(path=line["path"]):
                self.assertEqual(line["kind"], "api")
                self.assertEqual(line["mode"], "write")
                self.assertEqual((line["project_id"], line["post_id"]), (PID, POST_ID))
                # 응답에 `result.id`가 있어도 만든 항목이 아니다 (리뷰 L4).
                self.assertIsNone(line["comment_id"])
                self.assertIsNone(line["changed"])
                self.assertIs(line["ok"], True)
        blob = self.audit_path.read_text(encoding="utf-8")
        for secret in (secret_subject, secret_body):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)


class WriterCallCountTest(WriterCase):
    """002 C5 (spec §0, 완료 기준 68) — 도구 단위 감사 로그의 `calls`가 쓰기까지 세려면 필요하다."""

    def test_W6_a_successful_write_counts_one(self) -> None:
        made, http = self.make_writer({("POST", LOGS): (200, posted(), None)})
        self.assertEqual(made.calls, 0)
        made.post_comment(project_id=PID, post_id=POST_ID, content="네")
        self.assertEqual(made.calls, 1)
        self.assertEqual(made.calls, len(http.requests))

    def test_W6_a_failed_write_counts_one_too(self) -> None:
        made, http = self.make_writer({("POST", LOGS): (503, {"header": {}}, None)})
        with self.assertRaises(ServerError) as caught:
            made.post_comment(project_id=PID, post_id=POST_ID, content="네")
        self.record(caught.exception)
        self.assertEqual(made.calls, 1)
        self.assertEqual(made.calls, len(http.requests))

    def test_W6_a_refusal_before_the_request_counts_nothing(self) -> None:
        made, http = self.make_writer({})
        with self.assertRaises(ValueError) as blank:
            made.post_comment(project_id=PID, post_id=POST_ID, content="   ")
        self.record(blank.exception)
        with self.assertRaises(InvalidIdError) as bent:
            made.post_comment(project_id=PID, post_id="20/set-done?x=", content="내용")
        self.record(bent.exception)
        self.assertEqual(made.calls, 0)
        self.assertEqual(http.requests, [])

    def test_W6_the_count_is_read_only(self) -> None:
        # `DoorayReader.calls`와 대칭이다 — 밖에서 셈을 고쳐 쓸 수 없다.
        made, _ = self.make_writer({("POST", LOGS): (200, posted(), None)})
        with self.assertRaises(AttributeError):
            made.calls = 99  # type: ignore[misc]


class WriterShapeTest(WriterCase):
    def test_criterion_26_only_the_ten_writes_and_close(self) -> None:
        # 002 C5가 읽기 전용 프로퍼티 `calls`를 더했다. 010이 쓰기 메서드를 둘 더해 셋,
        # 012가 고치기 다섯을 더해 여덟이다 (012 spec §10-2 #27). 022가 옮기기를 더해 아홉,
        # 023이 프로젝트 설명을 더해 열이다.
        made, _ = self.make_writer({("POST", LOGS): (200, posted(), None)})
        public = {name for name in dir(made) if not name.startswith("_")}
        writes = {
            "post_comment",
            "create_post",
            "set_parent_post",
            "update_post",
            "update_comment",
            "set_workflow",
            "set_assignee_workflow",
            "set_done",
            "move_post",
            "update_project_description",
        }
        self.assertEqual(len(writes), 10)
        self.assertEqual(public, writes | {"close", "calls"})
        methods = {name for name in public if callable(getattr(made, name))}
        self.assertEqual(methods, writes | {"close"})
        self.assertIsInstance(writer.DoorayWriter.calls, property)

    def test_the_writer_does_not_inherit_from_the_reader(self) -> None:
        # 서로 상속하지 않고 공통 조상도 없다. 둘 다 Transport를 가진다 (§7-2).
        self.assertNotIsInstance(
            self.make_writer({("POST", LOGS): (200, posted(), None)})[0],
            client.DoorayReader,
        )
        self.assertEqual(writer.DoorayWriter.__mro__[1:], (object,))
        self.assertIsInstance(
            self.make_writer({("POST", LOGS): (200, posted(), None)})[0]._t, client.Transport
        )

    def test_T2_no_token_in_the_writer_repr_or_its_client_headers(self) -> None:
        made, _ = self.make_writer({("POST", LOGS): (200, posted(), None)})
        made.post_comment(project_id=PID, post_id=POST_ID, content="네")

        self.record(made)
        self.assertNotIn(FAKE_TOKEN, repr(made))
        self.assertNotIn(FAKE_TOKEN, str(made))
        self.assertNotIn(FAKE_TOKEN, repr(vars(made)))
        self.assertNotIn(FAKE_TOKEN, repr(made._t._client.headers))
        self.assertNotIn(FAKE_TOKEN, str(made._t._client.headers))

    def test_the_posted_comment_carries_no_web_link(self) -> None:
        # 댓글 단위 링크 형식이 미확인이라 코어가 형식을 못 박지 않는다 (§7, U-4).
        made, _ = self.make_writer({("POST", LOGS): (200, posted(), None)})
        result = made.post_comment(project_id=PID, post_id=POST_ID, content="네")
        self.assertEqual(
            {f for f in vars(result)}, {"id", "post_id", "project_id"}
        )
