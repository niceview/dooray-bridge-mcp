"""spec §9(`mention.py`) — §11-3의 M 케이스.

순수 함수라 네트워크·홈 폴더가 필요 없다. `CoreTestCase`를 쓰지 않는다.
`_추정`이 붙은 이름은 **관찰한 적 없는 동작**이다 (api-notes §5 U-1·U-2).
"""

from __future__ import annotations

import re
import time
import unittest

from dooray_core import mention
from dooray_core.mention import (
    Mention,
    Person,
    TenantScan,
    find_mentions,
    has_dooray_address,
    has_mention_marks,
    link_mentions,
    mentions_me,
    scan_tenant,
    tenant_of,
)
from support import load_fixture

MY_ID = "1000000000000000001"  # 토큰 주인 (api-notes §2-7)
OTHER_ID = "1000000000000000012"
GROUP_ID = "1000000000000000019"
TENANT = "1000000000000000052"


def comments() -> dict[str, dict]:
    """`logs.json`의 12건을 ID 끝 4자리로 찾을 수 있게 만든다."""
    return {item["id"][-4:]: item for item in load_fixture("logs")["result"]}


def body(tail: str) -> str:
    return comments()[tail]["body"]["content"]


def group_table(members: list[str]) -> dict[str, frozenset[str]]:
    return {GROUP_ID: frozenset(members)}


class FindMentionsTest(unittest.TestCase):
    def test_M4_a_body_with_many_mentions_keeps_every_one_in_order(self) -> None:
        found = find_mentions(body("0054"))
        self.assertEqual(len(found), 12)
        self.assertEqual([m.kind for m in found], ["member"] * 12)
        self.assertEqual(found[0].display, "멤버15")
        self.assertEqual(found[0].target_id, "1000000000000000011")
        # 같은 사람이 두 번 나오면 두 건이다. 판정은 ID로만 한다.
        self.assertEqual(sum(1 for m in found if m.target_id == MY_ID), 2)
        self.assertFalse(any(m.quoted for m in found))

    def test_M11_a_body_without_mentions_yields_an_empty_list(self) -> None:
        self.assertEqual(find_mentions(body("0055")), [])
        self.assertEqual(find_mentions(body("0057")), [])

    def test_a_file_or_task_link_is_not_a_mention(self) -> None:
        # `dooray://…/files/` `…/tasks/`는 첨부·업무 링크다 (api-notes §2-5).
        self.assertEqual(find_mentions(body("0056")), [])
        self.assertEqual(find_mentions(body("0064")), [])

    def test_M3_a_name_written_as_plain_text_is_not_a_mention(self) -> None:
        # 이름이 텍스트로 언급된 것은 멘션이 아니다 (완료 기준 33).
        text = "@멤버1 님 확인 부탁드립니다. 멤버1 님께도 전달했습니다."
        self.assertEqual(find_mentions(text), [])
        self.assertFalse(mentions_me(text, my_id=MY_ID))

    def test_M8_every_observed_title_parses_and_none_of_them_decides(self) -> None:
        # 관찰된 title: member·me·admin·guest·없음 (api-notes §2-7).
        titles = {
            "0054": ("member", "me"),
            "0061": ("admin", "me"),
            "0062": ("guest", "member"),
        }
        for tail in titles:
            with self.subTest(comment=tail):
                self.assertTrue(find_mentions(body(tail)))
        # 그룹 멘션에는 title이 아예 없다.
        group = find_mentions(body("0053"))
        self.assertEqual([(m.kind, m.target_id) for m in group], [("group", GROUP_ID)])
        self.assertEqual(group[0].display, "그룹1")

    def test_M8_the_same_link_with_any_title_gives_the_same_mention(self) -> None:
        shapes = [
            f'[@멤버1](dooray://{TENANT}/members/{MY_ID} "member")',
            f'[@멤버1](dooray://{TENANT}/members/{MY_ID} "me")',
            f'[@멤버1](dooray://{TENANT}/members/{MY_ID} "admin")',
            f'[@멤버1](dooray://{TENANT}/members/{MY_ID} "guest")',
            f'[@멤버1](dooray://{TENANT}/members/{MY_ID} "처음 보는 값")',
            f'[@멤버1](dooray://{TENANT}/members/{MY_ID})',
        ]
        for text in shapes:
            with self.subTest(text=text):
                self.assertEqual(
                    find_mentions(text),
                    [Mention(kind="member", target_id=MY_ID, display="멤버1", quoted=False)],
                )
                self.assertTrue(mentions_me(text, my_id=MY_ID))

    def test_M10_the_tenant_segment_is_thrown_away(self) -> None:
        # 첫 세그먼트는 테넌트 ID다. 판정에도 표시에도 쓰지 않는다 (api-notes §2-7).
        first = find_mentions(f'[@멤버1](dooray://{TENANT}/members/{MY_ID} "me")')
        second = find_mentions('[@멤버1](dooray://9999999999/members/%s "me")' % MY_ID)
        self.assertEqual(first, second)

    def test_the_display_name_keeps_its_text_and_drops_one_leading_at(self) -> None:
        found = find_mentions(f"[이름 없는 표기](dooray://{TENANT}/members/{MY_ID})")
        self.assertEqual(found[0].display, "이름 없는 표기")

    def test_a_mention_never_spans_two_lines(self) -> None:
        # 정규식에 `.`을 쓰지 않아 줄을 넘지 않는다 (§9-2).
        text = f"[@멤버1](dooray://{TENANT}/members/\n{MY_ID})"
        self.assertEqual(find_mentions(text), [])

    def test_a_group_id_with_a_path_segment_is_not_captured_at_all(self) -> None:
        # ID를 `[^/\\s")]+`로 받으므로 `/` 앞에서 끊기고, 그러면 닫는 괄호가 오지 않아
        # 멘션 자체가 잡히지 않는다. 조작된 경로가 그룹 ID 자리로 흘러들 길이 없다 (§5-1b).
        text = f"[@그룹1](dooray://{TENANT}/member-groups/30/../../x)"
        self.assertEqual(find_mentions(text), [])

    def test_a_group_id_with_escaped_punctuation_is_captured_verbatim(self) -> None:
        # `%`·`.`은 문자 집합에 걸리지 않아 그대로 잡힌다. 거르는 일은 `client.path_id()`가 한다.
        text = f"[@그룹1](dooray://{TENANT}/member-groups/..%2F..%2Fx)"
        self.assertEqual([m.target_id for m in find_mentions(text)], ["..%2F..%2Fx"])


class QuotedMentionTest(unittest.TestCase):
    def test_M5_a_mention_inside_a_blockquote_is_not_mine_추정(self) -> None:
        # 인용문 안의 멘션은 실제로 관찰하지 못했다 (api-notes §5 U-2). markdown 규칙으로 구현했다.
        text = f'> 지난 논의: [@멤버1](dooray://{TENANT}/members/{MY_ID} "me") 참고\n확인했습니다.'
        found = find_mentions(text)
        self.assertEqual([m.quoted for m in found], [True])
        self.assertFalse(mentions_me(text, my_id=MY_ID))
        # 인용을 포함하라고 하면 참이 된다.
        self.assertTrue(mentions_me(text, my_id=MY_ID, include_quoted=True))

    def test_M5_three_leading_spaces_still_count_as_a_quote_추정(self) -> None:
        text = f'   > [@멤버1](dooray://{TENANT}/members/{MY_ID} "me")'
        self.assertTrue(find_mentions(text)[0].quoted)

    def test_M5_four_leading_spaces_are_not_a_quote_추정(self) -> None:
        text = f'    > [@멤버1](dooray://{TENANT}/members/{MY_ID} "me")'
        self.assertFalse(find_mentions(text)[0].quoted)

    def test_M6_a_mention_inside_a_fenced_block_is_not_mine(self) -> None:
        text = (
            "```\n"
            f'[@멤버1](dooray://{TENANT}/members/{MY_ID} "me")\n'
            "```\n"
            "위 예시를 참고해 주세요."
        )
        self.assertEqual([m.quoted for m in find_mentions(text)], [True])
        self.assertFalse(mentions_me(text, my_id=MY_ID))

    def test_M6_a_tilde_fence_closes_only_with_tildes(self) -> None:
        text = (
            "~~~\n"
            "```\n"
            f'[@멤버1](dooray://{TENANT}/members/{MY_ID} "me")\n'
            "~~~\n"
            f'[@멤버1](dooray://{TENANT}/members/{MY_ID} "me")\n'
        )
        self.assertEqual([m.quoted for m in find_mentions(text)], [True, False])

    def test_M6_a_mention_inside_inline_code_is_not_mine(self) -> None:
        text = f'이렇게 씁니다: `[@멤버1](dooray://{TENANT}/members/{MY_ID} "me")` 끝.'
        self.assertEqual([m.quoted for m in find_mentions(text)], [True])
        self.assertFalse(mentions_me(text, my_id=MY_ID))

    def test_M6_an_unpaired_backtick_does_not_hide_a_mention(self) -> None:
        text = f'백틱 하나 ` 뒤의 [@멤버1](dooray://{TENANT}/members/{MY_ID} "me")'
        self.assertEqual([m.quoted for m in find_mentions(text)], [False])
        self.assertTrue(mentions_me(text, my_id=MY_ID))

    def test_M6_a_backtick_pair_on_the_next_line_does_not_reach_back(self) -> None:
        text = f'`열기\n[@멤버1](dooray://{TENANT}/members/{MY_ID} "me")\n닫기`'
        self.assertEqual([m.quoted for m in find_mentions(text)], [False])


class HtmlMentionTest(unittest.TestCase):
    """html 본문은 관찰하지 못했다 (api-notes §5 U-1). 전부 추측이다."""

    def test_M7_a_mention_in_an_html_body_is_read_추정(self) -> None:
        html = (
            "<p>본문 조각 1. "
            f'<a href="dooray://{TENANT}/members/{MY_ID}">@멤버1</a>'
            " 확인 부탁드립니다.</p>"
        )
        found = find_mentions(html, mime_type="text/html")
        self.assertEqual(
            found, [Mention(kind="member", target_id=MY_ID, display="멤버1", quoted=False)]
        )
        self.assertTrue(mentions_me(html, my_id=MY_ID, mime_type="text/html"))

    def test_M7_a_group_mention_in_html_is_read_추정(self) -> None:
        html = f'<a href="dooray://{TENANT}/member-groups/{GROUP_ID}">@그룹1</a>'
        found = find_mentions(html, mime_type="text/html")
        self.assertEqual([(m.kind, m.target_id) for m in found], [("group", GROUP_ID)])

    def test_M7_a_mention_inside_a_blockquote_element_is_quoted_추정(self) -> None:
        html = (
            "<blockquote><p>"
            f'<a href="dooray://{TENANT}/members/{MY_ID}">@멤버1</a>'
            "</p></blockquote>"
        )
        self.assertEqual([m.quoted for m in find_mentions(html, mime_type="text/html")], [True])
        self.assertFalse(mentions_me(html, my_id=MY_ID, mime_type="text/html"))

    def test_M7_an_ordinary_link_in_html_is_not_a_mention_추정(self) -> None:
        html = f'<a href="dooray://{TENANT}/files/1000000000000000035">보고서</a>'
        self.assertEqual(find_mentions(html, mime_type="text/html"), [])

    def test_M7_markdown_rules_do_not_run_on_html_추정(self) -> None:
        # html에서는 줄 머리 `>`가 인용문이 아니다. `<blockquote>`만 본다.
        html = f'&gt; <a href="dooray://{TENANT}/members/{MY_ID}">@멤버1</a>'
        self.assertEqual([m.quoted for m in find_mentions(html, mime_type="text/html")], [False])


class ObservedHtmlMentionTest(unittest.TestCase):
    """실측한 html 멘션 모양 (003 spec §5-2, 완료 기준 9).

    `streams.json`의 마지막 항목이 실제 응답이다. 주소가 `href`가 아니라
    **`data-dooray-href`**에 있어 `href`만 읽던 코어는 이 멘션을 한 건도 보지 못했다.
    """

    def observed(self) -> tuple[str, str]:
        event = load_fixture("streams")["result"][-1]["event"]
        return event["body"]["content"], event["body"]["mimeType"]

    def test_H1_the_verdict_does_not_hang_on_the_exact_mime_type(self) -> None:
        # 003 리뷰 H1. `[]`는 "멘션 없음"과 구분되지 않는다 — `mimeType`이 `text/html; charset=…`이거나
        # 비었거나, 마크다운 본문에 html 앵커가 끼어 와도 멘션을 조용히 놓치면 안 된다.
        content, _ = self.observed()
        for mime in ("text/html", "TEXT/HTML; charset=utf-8", None, "", "text/x-markdown"):
            with self.subTest(mime=mime):
                self.assertTrue(mention.mentions_me(content, my_id=MY_ID, mime_type=mime))
        mixed = '[@멤버2](dooray://1000000000000000052/members/1000000000000000003 "member") ' + content
        found = mention.find_mentions(mixed, mime_type="text/x-markdown")
        self.assertEqual(
            sorted(m.target_id for m in found), ["1000000000000000001", "1000000000000000003"]
        )

    def test_기준9_the_fixture_really_is_an_html_comment_with_a_data_href(self) -> None:
        content, mime = self.observed()
        self.assertEqual(mime, "text/html")
        self.assertIn("data-dooray-href=", content)
        self.assertNotIn(" href=", content)

    def test_기준9_a_data_dooray_href_mention_is_mine(self) -> None:
        content, mime = self.observed()
        self.assertEqual(
            find_mentions(content, mime_type=mime),
            [Mention(kind="member", target_id=MY_ID, display="멤버1", quoted=False)],
        )
        self.assertTrue(mentions_me(content, my_id=MY_ID, mime_type=mime))
        self.assertFalse(mentions_me(content, my_id=OTHER_ID, mime_type=mime))

    def test_기준9_a_group_mention_in_the_data_attribute_is_read_too(self) -> None:
        html = (
            f'<a class="mention-member" data-dooray-href="dooray://{TENANT}/member-groups/{GROUP_ID}"'
            ' data-id="x" title="member">@그룹1</a>'
        )
        found = find_mentions(html, mime_type="text/html")
        self.assertEqual([(m.kind, m.target_id) for m in found], [("group", GROUP_ID)])

    def test_기준9_a_quoted_data_href_mention_is_still_excluded(self) -> None:
        # 기존 규칙 그대로다: `<blockquote>` 안이면 나를 부른 것이 아니다.
        html = (
            "<blockquote><div>앞"
            f'<a data-dooray-href="dooray://{TENANT}/members/{MY_ID}" title="member">@멤버1</a>'
            "뒤</div></blockquote>"
        )
        self.assertEqual([m.quoted for m in find_mentions(html, mime_type="text/html")], [True])
        self.assertFalse(mentions_me(html, my_id=MY_ID, mime_type="text/html"))

    def test_기준9_the_data_attribute_is_read_before_href(self) -> None:
        html = (
            f'<a href="dooray://{TENANT}/members/{OTHER_ID}"'
            f' data-dooray-href="dooray://{TENANT}/members/{MY_ID}">@멤버1</a>'
        )
        found = find_mentions(html, mime_type="text/html")
        self.assertEqual([m.target_id for m in found], [MY_ID])

    def test_기준9_href_still_works_when_there_is_no_data_attribute_추정(self) -> None:
        # `href`만 있는 모양은 여전히 관찰하지 못했다 (api-notes §5 U-1). 규칙은 그대로 둔다.
        html = f'<a href="dooray://{TENANT}/members/{MY_ID}">@멤버1</a>'
        self.assertTrue(mentions_me(html, my_id=MY_ID, mime_type="text/html"))

    def test_기준9_a_data_href_to_a_file_is_not_a_mention(self) -> None:
        html = f'<a data-dooray-href="dooray://{TENANT}/files/1000000000000000035">보고서</a>'
        self.assertEqual(find_mentions(html, mime_type="text/html"), [])


class MentionsMeTest(unittest.TestCase):
    def test_M1_a_comment_that_mentions_me_is_true(self) -> None:
        self.assertTrue(mentions_me(body("0063"), my_id=MY_ID))

    def test_M2_a_comment_that_mentions_only_other_people_is_false(self) -> None:
        self.assertFalse(mentions_me(body("0062"), my_id=MY_ID))

    def test_M9_a_group_mention_needs_the_member_table(self) -> None:
        text = body("0053")
        members = [m["id"] for m in load_fixture("member_group_other_project")["result"]["members"]]
        self.assertIn(MY_ID, members)

        # 표가 있고 내가 구성원이면 참이다.
        self.assertTrue(mentions_me(text, my_id=MY_ID, group_members=group_table(members)))
        # 같은 표라도 내가 구성원이 아니면 거짓이다.
        self.assertNotIn(OTHER_ID, members)
        self.assertFalse(mentions_me(text, my_id=OTHER_ID, group_members=group_table(members)))
        # 표가 없으면 판정하지 않는다 (§9-1 규칙 4).
        self.assertFalse(mentions_me(text, my_id=MY_ID))
        # 빈 집합도 같다 — 조회에 실패한 그룹이 이 모양이 된다.
        self.assertFalse(mentions_me(text, my_id=MY_ID, group_members={GROUP_ID: frozenset()}))

    def test_M9_the_table_is_not_checked_against_a_project(self) -> None:
        # 멘션된 그룹이 다른 프로젝트 소속일 수 있다 (api-notes §2-7 실측).
        # 순수 함수라 프로젝트를 애초에 모른다.
        other = load_fixture("member_group_other_project")["result"]
        self.assertNotEqual(other["project"]["id"], "1000000000000000018")
        self.assertTrue(
            mentions_me(
                body("0053"),
                my_id=MY_ID,
                group_members=group_table([m["id"] for m in other["members"]]),
            )
        )

    def test_a_quoted_group_mention_is_excluded_too_추정(self) -> None:
        text = f"> [@그룹1](dooray://{TENANT}/member-groups/{GROUP_ID})"
        table = group_table([MY_ID])
        self.assertFalse(mentions_me(text, my_id=MY_ID, group_members=table))
        self.assertTrue(mentions_me(text, my_id=MY_ID, group_members=table, include_quoted=True))

    def test_M12_the_whole_fixture_matches_the_expected_table(self) -> None:
        # `logs.json` 12건 전체. 그룹 표는 주지 않으므로 `…0053`은 거짓이다.
        expected = {
            "0053": (1, False),
            "0054": (12, True),
            "0055": (0, False),
            "0056": (0, False),
            "0057": (0, False),
            "0058": (0, False),
            "0059": (1, False),
            "0060": (4, True),
            "0061": (2, True),
            "0062": (4, False),
            "0063": (2, True),
            "0064": (0, False),
        }
        rows = comments()
        self.assertEqual(sorted(rows), sorted(expected))
        for tail, (count, mine) in expected.items():
            with self.subTest(comment=tail):
                content = rows[tail]["body"]["content"]
                mime = rows[tail]["body"]["mimeType"]
                self.assertEqual(len(find_mentions(content, mime_type=mime)), count)
                self.assertEqual(mentions_me(content, my_id=MY_ID, mime_type=mime), mine)


if __name__ == "__main__":
    unittest.main()


class RegexCostTest(unittest.TestCase):
    """리뷰 H2. 표시 이름 자리에 길이 상한이 없으면 `[`만 가득한 본문에서 O(n²)이 된다.

    `[` 4만 자 댓글 하나로 `find_mentions` 1회가 13초였다(리뷰에서 재현). 상한을 주면 선형이다.
    """

    def test_M13_a_wall_of_open_brackets_is_cheap(self) -> None:
        import time

        body = "[" * 40000
        started = time.perf_counter()
        self.assertEqual(mention.find_mentions(body), [])
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_M13_a_very_long_display_name_is_simply_not_a_mention(self) -> None:
        body = "[@" + "가" * 300 + '](dooray://1/members/1000000000000000001 "member")'
        self.assertEqual(mention.find_mentions(body), [])

    def test_M13_an_ordinary_display_name_still_matches(self) -> None:
        body = "[@" + "가" * 40 + '](dooray://1/members/1000000000000000001 "member")'
        self.assertEqual(len(mention.find_mentions(body)), 1)

    def test_R2_5_inline_code_spans_are_the_old_regex_spans(self) -> None:
        # 012 리뷰 M4 — 정규식을 선형 스캐너로 바꿨다. 판정(어느 구간이 인라인 코드인가)은 같아야 한다.
        # 옛 정규식은 짧은 입력에서는 싸다 — 여기서 정답으로 쓴다.
        import itertools

        old = re.compile(r"(`+)[^\n]*?\1")
        for size in range(11):
            for chars in itertools.product("`a", repeat=size):
                line = "".join(chars)
                with self.subTest(line=line):
                    self.assertEqual(
                        mention._inline_code_spans(line), [one.span() for one in old.finditer(line)]
                    )
        for line in ("```a``b`c````d", "`` ` ``\n", "a```b``c`d``e```", "`````a``"):
            with self.subTest(line=line):
                self.assertEqual(
                    mention._inline_code_spans(line), [one.span() for one in old.finditer(line)]
                )

    def test_R2_5_a_wall_of_backticks_is_cheap(self) -> None:
        # 옛 정규식은 백틱 8,000자에 4초, 4만 자에 60초를 넘겼다 (012 리뷰 M4).
        import time

        for name, wall in (
            ("`", "`" * 40000),
            ("a `", "a " + "`" * 40000),
            ("`a", "`a" * 20000),
            ("``a`", "``a`" * 10000),
        ):
            for step, run in (
                ("find_mentions", lambda: mention.find_mentions(wall)),
                ("replace_once", lambda: mention.replace_once("ZQ " + wall, "ZQ", "ZW", mime_type="text/x-markdown")),
            ):
                with self.subTest(wall=name, step=step):
                    started = time.perf_counter()
                    run()
                    self.assertLess(time.perf_counter() - started, 1.0)

    def test_M14_is_mentioned_judges_a_list_that_was_already_found(self) -> None:
        # `collect`가 같은 본문에 정규식을 두 번 돌리지 않게 한다.
        found = mention.find_mentions(
            '[@멤버1](dooray://1/members/1000000000000000001 "member")'
        )
        self.assertTrue(mention.is_mentioned(found, my_id="1000000000000000001"))
        self.assertFalse(mention.is_mentioned(found, my_id="1000000000000000002"))


# --- 007 — 멘션 만들기 (spec §4-3·§4-4·§5-1, §8-2의 L 계열) --------------------

M18 = Person("1000000000000000012", "멤버18")
M19 = Person("1000000000000000013", "멤버19")
M1 = Person("1000000000000000001", "멤버1")
M15 = Person("1000000000000000011", "멤버15")
PEOPLE = (M18, M19, M1, M15)
T = "T"
# 우리가 만드는 링크 하나의 모양 (§4-3). 이것 말고는 `dooray://`가 나오면 안 된다 (L13).
OUR_LINK = r'\[@[^\]\n]{1,64}\]\(dooray://T/members/[0-9A-Za-z_-]{1,64} "member"\)'


def link_of(person: Person, tenant: str = T) -> str:
    return f'[@{person.name}](dooray://{tenant}/members/{person.id} "member")'


class LinkMentionsTest(unittest.TestCase):
    def test_L1_one_participant_becomes_one_link(self) -> None:
        done = link_mentions("@멤버18 확인", PEOPLE, T)
        self.assertEqual(done.text, link_of(M18) + " 확인")
        self.assertEqual(done.linked, (M18,))
        self.assertEqual(done.skipped, ())
        self.assertFalse(done.capped)

    def test_L2_the_longer_name_wins(self) -> None:
        self.assertEqual(link_mentions("@멤버15 봐", PEOPLE, T).linked, (M15,))
        self.assertEqual(link_mentions("@멤버1 봐", PEOPLE, T).linked, (M1,))
        # 이름 뒤가 문장부호여도 경계다.
        self.assertEqual(link_mentions("@멤버1, 봐", PEOPLE, T).text, link_of(M1) + ", 봐")

    def test_L3_a_name_glued_to_a_suffix_is_not_a_mention(self) -> None:
        done = link_mentions("@멤버18님 봐 주세요", PEOPLE, T)
        self.assertEqual(done.text, "@멤버18님 봐 주세요")
        self.assertEqual(done.linked, ())
        # 이유는 "붙여 씀" — 모델이 한 칸 띄워 고칠 수 있게 (메인 세션, 시험 L에서).
        self.assertEqual(done.skipped, (("멤버18님", "attached"),))

    def test_L4_two_people_with_the_same_name_stay_text(self) -> None:
        twin = Person("1000000000000000077", "멤버18")
        done = link_mentions("@멤버18 확인", (*PEOPLE, twin), T)
        self.assertEqual(done.text, "@멤버18 확인")
        self.assertEqual(done.skipped, (("멤버18", "same_name"),))
        # 같은 사람이 두 번 들어온 것은 동명이인이 아니다.
        again = link_mentions("@멤버18 확인", (*PEOPLE, M18), T)
        self.assertEqual(again.linked, (M18,))

    def test_L5_a_name_outside_the_thread_stays_text(self) -> None:
        done = link_mentions("@사장님 보세요", PEOPLE, T)
        self.assertEqual(done.text, "@사장님 보세요")
        self.assertEqual(done.skipped, (("사장님", "outside"),))

    def test_L6_without_a_tenant_nothing_is_linked(self) -> None:
        text = "@멤버18 확인 @멤버19 봐"
        done = link_mentions(text, PEOPLE, None)
        self.assertEqual(done.text, text)
        self.assertEqual(done.linked, ())
        self.assertEqual(done.skipped, (("멤버18", "no_tenant"), ("멤버19", "no_tenant")))
        # 모양이 이상한 테넌트도 없는 것과 같다.
        self.assertEqual(link_mentions(text, PEOPLE, "a?b").text, text)

    def test_L7_quotes_code_links_and_angles_hide_the_mark(self) -> None:
        for text in (
            "> @멤버18 이렇게 말했다",
            "```\n@멤버18 예시\n```",
            "코드 `a @멤버18 b` 끝",
            "[ @멤버18](주소) 끝",
            "<a @멤버18> 끝",
        ):
            with self.subTest(text=text):
                done = link_mentions(text, PEOPLE, T)
                self.assertEqual(done.text, text)
                self.assertEqual(done.skipped, ())
                self.assertFalse(has_mention_marks(text))

    def test_L8_a_mark_glued_to_the_word_before_is_not_a_candidate(self) -> None:
        for text in ("jm@example.com", "x/@멤버18", "!@멤버18 봐", "]@멤버18 봐"):
            with self.subTest(text=text):
                done = link_mentions(text, PEOPLE, T)
                self.assertEqual(done.text, text)
                self.assertEqual(done.skipped, ())
                self.assertFalse(has_mention_marks(text))
        # 줄바꿈·NBSP 뒤는 후보다.
        self.assertEqual(link_mentions("봐\n@멤버18 확인", PEOPLE, T).linked, (M18,))
        self.assertEqual(link_mentions("봐 @멤버18 확인", PEOPLE, T).linked, (M18,))

    def test_L9_a_task_number_is_not_a_mention(self) -> None:
        done = link_mentions("@hong.gd/108 참고", (Person("1", "hong.gd"),), T)
        self.assertEqual(done.text, "@hong.gd/108 참고")
        self.assertEqual(done.skipped, ())
        self.assertFalse(has_mention_marks("@hong.gd/108 참고"))

    def test_L10_five_people_at_most(self) -> None:
        crowd = tuple(Person(f"10000000000000001{n:02d}", f"사람{n}") for n in range(7))
        text = " ".join(f"@사람{n}" for n in range(7)) + " @사람0 다시"
        done = link_mentions(text, crowd, T)
        self.assertEqual(done.linked, crowd[:5])
        self.assertEqual(done.skipped, (("사람5", "limit"), ("사람6", "limit")))
        # 이미 링크한 사람이 또 나오면 바꾼다 — 사람 수는 늘지 않는다.
        self.assertEqual(done.text.count(link_of(crowd[0])), 2)
        self.assertIn("@사람5", done.text)

    def test_L11_twenty_marks_at_most(self) -> None:
        text = " ".join(["@멤버18"] * 25)
        done = link_mentions(text, PEOPLE, T)
        self.assertTrue(done.capped)
        self.assertEqual(done.text.count(link_of(M18)), mention.MAX_MENTION_MARKS)
        self.assertEqual(done.text.count(" @멤버18"), 5)
        self.assertFalse(link_mentions("@멤버18", PEOPLE, T).capped)

    def test_L12_a_name_that_breaks_the_link_stays_text(self) -> None:
        for name in ("a](javascript:x)", "a]b", "a(b", "a\nb", " a", "가" * 65):
            with self.subTest(name=name):
                person = Person("1000000000000000090", name)
                text = f"@{name} 봐"
                done = link_mentions(text, (person,), T)
                self.assertEqual(done.text, text)
                self.assertEqual(done.linked, ())
                if name.strip() == name:
                    self.assertEqual(done.skipped, ((name, "unsafe_name"),))
        # 공백·점·밑줄·하이픈은 된다.
        ok = Person("1000000000000000091", "Jm Cho.dev_x-1")
        self.assertEqual(link_mentions("@Jm Cho.dev_x-1 봐", (ok,), T).linked, (ok,))

    def test_L13_every_address_in_the_output_is_our_link(self) -> None:
        text = "@멤버18 봐 @멤버19, @멤버1 @멤버15님\n> @멤버18\n`@멤버19` @없는사람 끝 @멤버18"
        done = link_mentions(text, PEOPLE, T)
        rest = re.sub(OUR_LINK, "", done.text)
        self.assertNotIn("dooray://", rest)
        self.assertEqual(len(re.findall(OUR_LINK, done.text)), 4)
        # 사람의 ID는 이름표에서만 온다. 모양이 이상한 ID는 링크가 되지 않는다.
        bad = Person("1/../x", "멤버99")
        self.assertEqual(link_mentions("@멤버99 봐", (bad,), T).text, "@멤버99 봐")

    def test_L14_the_tenant_is_taken_only_when_it_is_unique_and_well_formed(self) -> None:
        one = '[@멤버18](dooray://1000000000000000052/members/1 "member")'
        self.assertEqual(tenant_of([one, one]), "1000000000000000052")
        self.assertIsNone(tenant_of([one, "[x](dooray://evil/members/1)"]))
        self.assertIsNone(tenant_of(["멘션 없는 본문", ""]))
        self.assertIsNone(tenant_of(["[x](dooray://a?b=c/members/1)"]))
        self.assertIsNone(tenant_of([one, "[x](dooray://" + "a" * 65 + "/members/1)"]))
        self.assertEqual(
            tenant_of(['<a data-dooray-href="dooray://T9/members/1">@x</a>']), "T9"
        )
        self.assertEqual(tenant_of([body("0054")]), TENANT)

    def test_L15_every_disguise_of_the_scheme_is_caught(self) -> None:
        for text in (
            '[@x](dooray://t/members/1 "member")',
            "dooray%3A//t/members/1",
            "dooray%253A//t/members/1",
            "dooray&#58;//t/members/1",
            "dooray&#x3a;//t",
            "DOORAY://t/members/1",
            "do​oray://t/members/1",
            "ｄｏｏｒａｙ：//t/members/1",
            '<a data-dooray-href="dooray://t/members/X">@x</a>',
            "[@팀](dooray://t/member-groups/G)",
        ):
            with self.subTest(text=text):
                self.assertTrue(has_dooray_address(text))
        for text in ("https://acme.dooray.com/project/tasks/1", "@멤버18 확인", "50% 진행"):
            with self.subTest(text=text):
                self.assertFalse(has_dooray_address(text))

    def test_L16_pathological_input_is_cheap(self) -> None:
        import time

        for text in ("[" * 40000, "@" * 40000, "<" * 40000, " @" * 20000, "`a` @" * 8000):
            for name, run in (
                ("link_mentions", lambda: link_mentions(text, PEOPLE, T)),
                ("has_mention_marks", lambda: has_mention_marks(text)),
                ("tenant_of", lambda: tenant_of([text])),
                ("has_dooray_address", lambda: has_dooray_address(text)),
            ):
                with self.subTest(text=text[:4], function=name):
                    started = time.perf_counter()
                    run()
                    self.assertLess(time.perf_counter() - started, 1.0)

    def test_L17_the_function_is_pure(self) -> None:
        people = [M18, M19]
        before = list(people)
        first = link_mentions("@멤버18 봐", people, T)
        second = link_mentions("@멤버18 봐", people, T)
        self.assertEqual(first, second)
        self.assertEqual(people, before)

    def test_L23_the_conversion_never_adds_a_person(self) -> None:
        # 완료 기준 23 — 무작위 본문 100개. 링크된 이름은 전부 본문에 `@이름`으로 적혀 있었다.
        import random

        pieces = [
            "@멤버18", "@멤버19", "@멤버1", "@멤버15", "@멤버15님", "@없는사람", "> ", "`",
            "\n", " ", "확인", "[", "](x)", "<", ">", "jm@x.com", "!", "@", "/1", ",",
        ]
        chooser = random.Random(7)
        for round_ in range(100):
            text = "".join(chooser.choice(pieces) for _ in range(chooser.randint(1, 40)))
            with self.subTest(round=round_, text=text):
                done = link_mentions(text, PEOPLE, T)
                for person in done.linked:
                    self.assertIn("@" + person.name, text)
                # 링크를 `@이름`으로 되돌리면 원문과 글자 하나 다르지 않다 — 적힌 **그 자리의 그 이름**만 바꿨다 (리뷰 L4).
                self.assertEqual(re.sub(r'\[(@[^\[\]\n]{1,64})\]\(dooray://[^)\s]+ "member"\)', r'\1', done.text), text)
                self.assertLessEqual(len(done.linked), mention.MAX_MENTIONS)
                self.assertNotIn("dooray://", re.sub(OUR_LINK, "", done.text))


class TenantScanTest(unittest.TestCase):
    """014 spec §3 — 글에 `dooray://`가 있었는가와 테넌트 값을 함께 준다."""

    ONE = '[@멤버18](dooray://1000000000000000052/members/1 "member")'

    def test_TF_1_no_scheme_means_not_seen(self) -> None:
        self.assertEqual(scan_tenant([]), TenantScan(None, False))
        self.assertEqual(scan_tenant(["멘션 없는 본문", ""]), TenantScan(None, False))

    def test_TF_2_one_value_across_texts(self) -> None:
        self.assertEqual(
            scan_tenant(["링크 없는 본문", self.ONE, f"[x](dooray://{TENANT}/files/1)"]),
            TenantScan(TENANT, True),
        )
        self.assertEqual(
            scan_tenant(['<a data-dooray-href="dooray://T9/members/1">@x</a>']),
            TenantScan("T9", True),
        )

    def test_TF_3_two_or_odd_values_are_seen_but_unset(self) -> None:
        for texts in (
            [self.ONE, "[x](dooray://evil/members/1)"],
            ["[x](dooray://a?b=c/members/1)"],
            ["[x](dooray://" + "a" * 65 + "/members/1)"],
            ["[x](dooray:///members/1)"],
            # 66자 — 정규식이 잡지 못한다. 값은 없고 `seen`만 참이다.
            ["[x](dooray://" + "a" * 66 + "/members/1)"],
            # 014 보충(리뷰 L2) — 정상 값의 앞뒤에 모양이 이상한 값. 건너뛰면 정상 값이 살아난다.
            [self.ONE, "[x](dooray://a?b=c/members/1)"],
            ["[x](dooray:///members/1)", self.ONE],
            [self.ONE, "[x](dooray://" + "a" * 65 + "/members/1)"],
        ):
            with self.subTest(texts=texts[-1][:30]):
                self.assertEqual(scan_tenant(texts), TenantScan(None, True))

    def test_TF_4_the_scheme_as_text_is_seen(self) -> None:
        self.assertEqual(scan_tenant(["주소는 dooray:// 로 시작"]), TenantScan(None, True))
        self.assertEqual(scan_tenant(["DOORAY://t/members/1"]), TenantScan(None, False))

    def test_TF_5_tenant_of_is_the_value(self) -> None:
        # L14의 입력 일곱.
        # 생성기 경로를 본다 — 기대값은 L14가 고정한다.
        for texts in (
            [self.ONE, self.ONE],
            [self.ONE, "[x](dooray://evil/members/1)"],
            ["멘션 없는 본문", ""],
            ["[x](dooray://a?b=c/members/1)"],
            [self.ONE, "[x](dooray://" + "a" * 65 + "/members/1)"],
            ['<a data-dooray-href="dooray://T9/members/1">@x</a>'],
            [body("0054")],
        ):
            with self.subTest(texts=texts[-1][:30]):
                self.assertEqual(tenant_of(texts), scan_tenant(texts).value)
                self.assertEqual(tenant_of(iter(texts)), scan_tenant(iter(texts)).value)
                self.assertEqual(scan_tenant(iter(texts)), scan_tenant(texts))

    def test_TF_6_pathological_input_is_cheap(self) -> None:
        for text in ("[" * 40000, "@" * 40000, "<" * 40000, " @" * 20000, "`a` @" * 8000):
            with self.subTest(text=text[:4]):
                started = time.perf_counter()
                scan_tenant([text])
                self.assertLess(time.perf_counter() - started, 1.0)
        chunk = "dooray://" + "a" * 60 + " "
        near_links = (chunk * (40000 // len(chunk) + 1))[:40000]
        for name, text in (("near_links", near_links), ("brackets", "[" * 40000)):
            with self.subTest(set=name):
                started = time.perf_counter()
                scan_tenant([text] * 200)
                self.assertLess(time.perf_counter() - started, 1.0)


class LoneAtTest(unittest.TestCase):
    """007 — 홀로 선 `@`는 후보가 아니다(메인 세션 검토). 참여자를 읽는 GET이 헛돌지 않게."""

    def test_a_lone_at_is_not_a_mark(self) -> None:
        for text in ("@ 확인", "끝에 @", "@\n다음 줄", "a @  b"):
            with self.subTest(text=text):
                self.assertFalse(mention.has_mention_marks(text))

    def test_a_name_after_at_is_still_a_mark(self) -> None:
        self.assertTrue(mention.has_mention_marks("@홍길동 확인"))


class ReviewFixTest(unittest.TestCase):
    """007 리뷰 반영 — M1(이스케이프·여러 겹), M3(이름표 불완전), L1(여러 줄 링크), L2(들여쓰기 코드), L6(`@@`)."""

    def test_M1_an_escaped_or_deeply_wrapped_address_is_caught(self) -> None:
        for text in (
            "dooray\\://t/members/1",
            "[@x](dooray\\://t/members/1)",
            "dooray%252525253A//t/members/1"  # ":"를 다섯 번 quote,
        ):
            with self.subTest(text=text):
                self.assertTrue(mention.has_dooray_address(text))

    def test_M3_an_incomplete_roster_links_no_one(self) -> None:
        done = link_mentions("@멤버18 확인", PEOPLE, T, complete=False)
        self.assertEqual(done.text, "@멤버18 확인")
        self.assertEqual(done.linked, ())
        self.assertEqual(done.skipped, (("멤버18", "incomplete"),))

    def test_L1_a_multiline_link_text_is_hidden(self) -> None:
        for text in ("[\n@멤버18](http://evil)", "![\n@멤버18](http://img)"):
            with self.subTest(text=text):
                self.assertEqual(link_mentions(text, PEOPLE, T).text, text)

    def test_L2_an_indented_code_line_is_hidden(self) -> None:
        text = "예시:\n    @멤버18 확인\n\t@멤버18 확인"
        self.assertEqual(link_mentions(text, PEOPLE, T).text, text)

    def test_L6_a_double_at_is_not_reported(self) -> None:
        done = link_mentions("@@멤버18", PEOPLE, T)
        self.assertEqual(done.skipped, ())


# --- 012 — 고치기: 찾아 바꾸기 (spec 개정 7의 R7-1, §10-1의 RK 계열) --------------

# 멘션 링크 하나.
L = '[@x](dooray://t/members/9 "member")'
MD = "text/x-markdown"
HTML = "text/html"


def replaced(content: str, old: str, new: str, mime: str | None = MD) -> mention.Replaced:
    return mention.replace_once(content, old, new, mime_type=mime)


class ReplaceOnceTest(unittest.TestCase):
    def assertReason(self, result: mention.Replaced, reason: str) -> None:
        self.assertIsNone(result.text, result)
        self.assertEqual(result.reason, reason)

    def assertReplaced(self, result: mention.Replaced, text: str) -> None:
        self.assertIsNone(result.reason, result)
        self.assertEqual(result.text, text)

    # RK-1 ---------------------------------------------------------------

    def test_RK_1_one_place_changes_and_links_files_and_nbsp_stay(self) -> None:
        link = f'[@멤버18](dooray://{TENANT}/members/{OTHER_ID} "member")'
        file = f"[보고서.pdf](dooray://{TENANT}/files/1000000000000000035)"
        content = f"{link} 어제 말씀드린 대로 내일 배포합니다.\n\n첨부: {file}"
        result = replaced(content, "내일", "모레")
        self.assertReplaced(
            result, f"{link} 어제 말씀드린 대로 모레 배포합니다.\n\n첨부: {file}"
        )
        self.assertEqual(
            find_mentions(result.text or ""), find_mentions(content), "멘션이 그대로여야 한다"
        )

    def test_RK_1_the_three_common_cases_pass(self) -> None:
        cases = (
            (f"{L} 내일 배포합니다", "내일", "모레", f"{L} 모레 배포합니다"),
            (f"내일 {L} 확인", "내일", "모레", f"모레 {L} 확인"),
            (
                "[a.pdf](dooray://t/files/1) 첨부합니다. 내일 봐요",
                "내일 봐요",
                "모레 봐요",
                "[a.pdf](dooray://t/files/1) 첨부합니다. 모레 봐요",
            ),
        )
        for content, old, new, expected in cases:
            with self.subTest(content=content):
                self.assertReplaced(replaced(content, old, new), expected)

    def test_RK_1_new_may_be_empty_or_blank_and_old_keeps_its_spaces(self) -> None:
        self.assertReplaced(replaced("내일 오후 배포", " 오후", ""), "내일 배포")
        self.assertReplaced(replaced("내일 오후 배포", "오후", "   "), "내일     배포")
        # 앞뒤 공백도 `old`의 일부다 — 자르지 않는다.
        self.assertReason(replaced("내일배포", " 내일", "모레"), mention.REPLACE_NOT_FOUND)

    def test_RK_1_the_at_sign_in_new_stays_plain_text(self) -> None:
        result = replaced("확인 부탁", "부탁", "부탁 @멤버18")
        self.assertReplaced(result, "확인 부탁 @멤버18")
        self.assertEqual(find_mentions(result.text or ""), [])

    # RK-2 ---------------------------------------------------------------

    def test_RK_2_same_old_empty_not_found_ambiguous_and_empty(self) -> None:
        self.assertReason(replaced("내일 배포", "내일", "내일"), mention.REPLACE_SAME)
        self.assertReason(replaced("내일 배포", "", ""), mention.REPLACE_SAME)
        self.assertReason(replaced("내일 배포", "", "모레"), mention.REPLACE_OLD_EMPTY)
        self.assertReason(replaced("내일 배포", "모레", "글피"), mention.REPLACE_NOT_FOUND)
        self.assertReason(replaced("내일 또 내일", "내일", "모레"), mention.REPLACE_AMBIGUOUS)
        # 겹치는 것도 두 번이다.
        self.assertReason(replaced("aaa", "aa", "b"), mention.REPLACE_AMBIGUOUS)
        self.assertReason(replaced("내일", "내일", ""), mention.REPLACE_EMPTY)
        self.assertReason(replaced(" 내일\n", "내일", " "), mention.REPLACE_EMPTY)

    def test_RK_2_a_blank_body_can_be_written_fresh_with_old_empty(self) -> None:
        for blank in ("", "  ", "\n\t"):
            with self.subTest(blank=repr(blank)):
                self.assertReplaced(replaced(blank, "", "새 본문"), "새 본문")
        self.assertReason(replaced("", "", "  "), mention.REPLACE_EMPTY)


    # RK-4 — HTML 본문의 형식 ---------------------------------------------------------------

    def test_RK_5_an_html_body_takes_no_markup_or_newline_in_new(self) -> None:
        content = "<p>알림: 내일 배포합니다 끝</p>"
        for new in ("<b>모레</b>", "모레 >", "A&B", "모레\n", "모레\r"):
            with self.subTest(new=new):
                self.assertReason(replaced(content, "배포", new, HTML), mention.REPLACE_HTML_TEXT)
        self.assertReplaced(replaced(content, "내일", "모레", HTML), "<p>알림: 모레 배포합니다 끝</p>")
        # 개정 7 — 태그에 맞닿은 글자도 고친다(보호 구간이 없다).
        self.assertReplaced(replaced(content, "알림", "공지", HTML), "<p>공지: 내일 배포합니다 끝</p>")
        # 마크다운 본문이면 같은 new가 통과한다(`post_comment`와 같은 잣대).
        self.assertReplaced(replaced("내일 배포", "배포", "<b>배포</b> & 확인"), "내일 <b>배포</b> & 확인")

    def test_RK_5_the_mime_is_read_like_find_mentions_does(self) -> None:
        self.assertReason(
            replaced("<p>내일 배포 끝</p>", "배포", "A&B", " Text/HTML; charset=utf-8"),
            mention.REPLACE_HTML_TEXT,
        )

    # RK-8 ---------------------------------------------------------------

    def test_RK_8_it_is_a_pure_function(self) -> None:
        # P8(`test_repo_rules`)이 `mention.py`의 I/O를 본다. 여기서는 같은 입력에 같은 답과 불변 결과를 본다.
        first = replaced(f"{L} 내일", "내일", "모레")
        self.assertEqual(first, replaced(f"{L} 내일", "내일", "모레"))
        with self.assertRaises(AttributeError):
            first.text = "바꿈"  # type: ignore[misc]

    def test_has_dooray_address_keeps_its_behavior_after_the_unwrap_split(self) -> None:
        # 풀기 루프를 함수로 뽑았다 (012 spec §4-3의 3). 네 겹 싼 입력은 여전히 거절 쪽이다.
        self.assertTrue(has_dooray_address("dooray%253A//t"))
        self.assertTrue(has_dooray_address("%25252525"))
        self.assertFalse(has_dooray_address("100%25 확인"))

    def test_RK_11_the_five_common_edits_pass(self) -> None:
        cases = (
            (f"{L} 내일 배포합니다", "내일", "모레", f"{L} 모레 배포합니다"),
            (f"내일 {L} 확인", "내일", "모레", f"모레 {L} 확인"),
            (
                "[a.pdf](dooray://t/files/1) 첨부합니다. 내일 봐요",
                "내일 봐요",
                "모레 봐요",
                "[a.pdf](dooray://t/files/1) 첨부합니다. 모레 봐요",
            ),
            (f"{L} (9/30) 배포", "9/30", "10/1", f"{L} (10/1) 배포"),
            (f"{L} 2026-09-30 배포", "2026-09-30", "2026-10-01", f"{L} 2026-10-01 배포"),
        )
        for content, old, new, expected in cases:
            with self.subTest(content=content, old=old):
                self.assertReplaced(replaced(content, old, new), expected)

    def test_RK_11_links_without_a_title_or_with_a_group_are_canonical(self) -> None:
        for link in (
            "[@x](dooray://t/members/9)",
            '[@팀](dooray://t/member-groups/7 "member")',
            "![그림](dooray://t/files/1)",
        ):
            with self.subTest(link=link):
                self.assertReplaced(replaced(f"{link} 내일 봐요", "내일", "모레"), f"{link} 모레 봐요")

    def test_RK_1_symbols_lines_and_code_blocks_change_freely(self) -> None:
        # 기호·줄·코드 블록을 바꾸는 고치기도 된다.
        for content, old, new, expected in (
            ("가 (나) 다", "(나)", "나", "가 나 다"),
            ("가\n나", "가", "가\n\n", "가\n\n\n나"),
            ("    코드\n끝", "끝", "끝!", "    코드\n끝!"),
        ):
            with self.subTest(content=content):
                self.assertReplaced(replaced(content, old, new), expected)

    # 개정 7 — 있는 글의 멘션을 검사하지 않는다(ADR 014). 거절하던 고치기가 통과함을 고정한다.
    # 모델이 쓴 글의 두레이 주소는 도구가 인자 단계에서 막는다(개정 7의 고침) — 여기서 보지 않는다 ----------

    def test_R7_the_text_and_the_id_of_a_mention_link_can_be_edited(self) -> None:
        content = f'확인 [@홍길동](dooray://t/members/{OTHER_ID} "member") 부탁'
        self.assertReplaced(
            replaced(content, OTHER_ID, MY_ID),
            f'확인 [@홍길동](dooray://t/members/{MY_ID} "member") 부탁',
        )
        self.assertReplaced(replaced(content, "홍길동", "김철수"), content.replace("홍길동", "김철수"))

    def test_R7_an_edit_that_changes_the_structure_passes(self) -> None:
        for content, old, new in (
            (f"{L} (가) 끝", "(가)", "가"),  # 기호 수
            (f"{L} 첫 줄", "첫 줄", "첫\n줄"),  # 줄 수
            (f"확인 부탁 !{L} 끝", " !", " "),  # 이미지 표시를 떼어 링크를 살린다
            (f"```\ncode\n```xyz\n{L}\n```", "xyz", ""),  # 펜스
        ):
            with self.subTest(content=content):
                self.assertReplaced(replaced(content, old, new), content.replace(old, new, 1))

    def test_RK_14_forty_thousand_character_walls_are_cheap(self) -> None:
        import time

        walls = {
            "[": "[" * 40000,
            "<": "<" * 40000,
            "(": "(" * 40000,
            ")": ")" * 40000,
            "`": "`" * 40000,
            "~": "~" * 40000,
            "space": " " * 40000,
            "newline": "\n" * 40000,
            "dooray:": "dooray:" * 6000,
            "[a](dooray://": "[a](dooray://" * 3000,
            '[a](dooray://x "': '[a](dooray://x "' * 2500,
            "indent": "    a\n" * 7000,
            "fence": "```\n" * 10000,
            "&#": "&#" * 20000,
        }
        for name, wall in walls.items():
            for mime in (MD, HTML):
                with self.subTest(wall=name, mime=mime):
                    started = time.perf_counter()
                    replaced(f"{wall} {L} 끝 ", "끝", "모레", mime)
                    self.assertLess(time.perf_counter() - started, 1.0)

    # 시간 — 사용자 텍스트에 도는 정규식이다 (CLAUDE.md "`[` 4만 자에 13초") -------------

    def test_RK_a_forty_thousand_character_wall_is_cheap(self) -> None:
        import time

        walls = {
            "[": "[" * 40000,
            "<": "<" * 40000,
            "!": "!" * 40000,
            "\\": "\\" * 40000,
            "dooray:": "dooray:" * 6000,
            # 한 줄에 구간이 수만 개이고 그 앞이 줄 머리 글자뿐이면 넓히기가 O(n²)이 될 수 있다.
            "> + <>": ">" * 20000 + "<>" * 10000,
            "space + <>": " " * 20000 + "<>" * 10000,
            "&a;": "&a;" * 13000,
        }
        for name, wall in walls.items():
            for mime in (MD, HTML):
                with self.subTest(wall=name, mime=mime):
                    started = time.perf_counter()
                    replaced(wall + " 끝 ", "끝", "모레", mime)
                    self.assertLess(time.perf_counter() - started, 1.0)


# --- 012 spec 개정 8 — 고치는 글의 두레이 주소와 업무 웹 주소 (R8-1·R8-2·R8-5) ---------------

WEB = "https://example.dooray.com"
TASKS = "/project/tasks/"  # `render.TASK_PATH` — 이 모듈은 `render`를 모른다


def web_links(text: str, web_base: str | None = WEB) -> list[str]:
    return [link.post_id for link in mention.task_web_links(text, web_base, TASKS)]


class EditAddressTest(unittest.TestCase):
    """R8-1 — 허용하는 모양(업무·파일 링크)만 받는다. 나머지에 두레이 주소가 있으면 거절이다 (완료 기준 68)."""

    ALLOWED = (
        "[회의록](dooray://t/tasks/1)",
        '[회의록](dooray://t/tasks/1 "registered")',
        '[회의록](dooray://t/tasks/1 "")',
        "[보고서.pdf](dooray://t/files/2)",
        '[보고서.pdf](dooray://t/files/2 "file")',
        "![그림](dooray://t/files/3)",
        f"[sample/7](dooray://{TENANT}/tasks/1000000000000000026) 참고해 주세요",
        "[](dooray://t/tasks/1)",  # 글자 칸이 비어도 모양은 맞다
        "[멤버18 @문서](dooray://t/tasks/1)",  # `@`가 맨 앞이 아니면 글자다
        "첨부 [a](dooray://t/files/1)/[b](dooray://t/files/2) 두 개",
        "그냥 글 @멤버18 확인 부탁",
        "",
        # 개정 9의 R9-3 — 뒤보기가 받는 뒤
        "[a](dooray://t/files/1)입니다",
        "[a](dooray://t/files/1). 다음",
        "[a](dooray://t/files/1), [b](dooray://t/files/2)",
        "[a](dooray://t/files/1)/ 다음",
        "[a](dooray://t/files/1)/",
        "[a](dooray://t/files/1)\n/members",  # 다음 줄은 링크 뒤가 아니다
        '[a](dooray://t/tasks/1 "member")',  # title은 받는다 — 보내기 전에 서버가 뗀다
    )
    REFUSED = (
        # 멤버·그룹 주소와 007의 변형 전부
        f'[@x](dooray://t/members/{OTHER_ID} "member")',
        "[@팀](dooray://t/member-groups/G)",
        "dooray%3A//t/members/1",
        "dooray%253A//t/members/1",
        "dooray&#58;//t/members/1",
        "dooray&#x3a;//t",
        "DOORAY://t/members/1",
        "do​oray://t/members/1",
        "ｄｏｏｒａｙ：//t/members/1",
        '<a data-dooray-href="dooray://t/members/X">@x</a>',
        "[@x](dooray\\://t/members/1)",
        "dooray%252525253A//t/members/1",
        # 알 수 없는 종류, 링크 문법 밖의 맨 주소, 인코딩한 업무 링크
        "[a](dooray://t/wiki/1)",
        "[a](dooray://t/projects/1)",
        "dooray://t/tasks/1",
        "<dooray://t/tasks/1>",
        "[a](dooray%3A//t/tasks/1)",
        "[a](dooray&#58;//t/tasks/1)",
        # 대소문자·전각은 그대로 본다 — 모양이 아니다
        "[a](DOORAY://t/tasks/1)",
        "[a](dooray://t/Tasks/1)",
        "[a](dooray://t/ｔａｓｋｓ/1)",
        "[a](ｄｏｏｒａｙ://t/tasks/1)",
        # 주소 칸의 군더더기
        "[a](dooray://t/tasks/1/members/2)",
        "[a](dooray://t/tasks/../members/2)",
        "[a](dooray://t/tasks/1?x=/members/2)",
        "[a](dooray://t/tasks/1#members)",
        "[a](dooray://t/tasks/1%2F..%2Fmembers%2F2)",
        "[a](dooray://t/tasks/1%2Fmembers%2F2)",
        "[a](dooray://t/../t/members/2/tasks/1)",
        "[a](dooray://t:1/tasks/1)",
        "[a](dooray://t/tasks/1 'x')",  # 작은따옴표 title은 모양이 아니다
        "[a](dooray://t/tasks/1  \"x\")",  # 공백 둘
        "[a](dooray://" + "t" * 65 + "/tasks/1)",
        "[a](dooray://t/tasks/" + "1" * 65 + ")",
        # 허용 링크 안에 숨긴 주소 — 글자 자리·title
        f"[dooray://t/members/{OTHER_ID}](dooray://t/tasks/1)",
        f'[a](dooray://t/tasks/1 "dooray://t/members/{OTHER_ID}")',
        f'[a](dooray://t/tasks/1 ")[@b](dooray://t/members/{OTHER_ID}")',
        f"[dooray%3A//t/members/{OTHER_ID}](dooray://t/files/1)",
        # 허용 링크 둘 사이에 걸친 조각, 맞닿은 조각
        f"doo[a](dooray://t/tasks/1)ray://t/members/{OTHER_ID}",
        f"dooray[a](dooray://t/tasks/1)://t/members/{OTHER_ID}",
        f"dooray:[a](dooray://t/tasks/1)[b](dooray://t/files/2)//t/members/{OTHER_ID}",
        f"[a](dooray://t/tasks/1)dooray://t/members/{OTHER_ID}",
        f"dooray&#58[a](dooray://t/tasks/1)//t/members/{OTHER_ID}",
        f"do[o](dooray://t/tasks/1)ray://t/members/{OTHER_ID}",  # 글자까지 이어 읽으면 주소다
        f"doo[](dooray://t/tasks/1)ray://t/members/{OTHER_ID}",
        f"[@x](doo[](dooray://t/tasks/1)ray://t/members/{OTHER_ID})",
        f'doo[x](dooray://t/tasks/1 "ray:")//t/members/{OTHER_ID}',  # title만 이어 읽으면
        f'doo[x](dooray://t/tasks/1 "ray:") //t/members/{OTHER_ID}',  # 뒤보기를 비켜 가도 title만 이어 읽으면
        f'[@x](doo[](dooray://t/tasks/1 "ray:")//t/members/{OTHER_ID})',
        f'[do](dooray://t/tasks/1 "o")ray://t/members/{OTHER_ID}',  # 글자와 title을 이어 읽으면
        f"[doo](dooray://t/tasks/1)[ray://t/members/{OTHER_ID}](dooray://t/tasks/2)",
        # 링크 안의 링크 — 글자 칸에 `[`가 들면 모양이 아니다
        f"[[@b](dooray://t/members/{OTHER_ID})](dooray://t/tasks/1)",
        f"[a\n](dooray://t/tasks/1)",
        # 개정 9의 R9-3 — 링크 바로 뒤에 주소를 이어 읽게 하는 글자(리뷰 M2)
        f"[a](dooray://t/files/1)/members/{OTHER_ID})",
        f"[a](dooray://t/files/1)(//t/members/{OTHER_ID})",
        f'[a](dooray://t/tasks/1 "x")/members/{OTHER_ID}',
        "[a](dooray://t/files/1)%2Fmembers%2F2",
        "[a](dooray://t/files/1)\\/members/2",
        "[a](dooray://t/files/1)/[b](dooray://t/files/2)/members/2)",
        # 글자 칸의 맨 앞 `@` — 멘션처럼 보이는 업무·파일 링크 (리뷰 L3)
        "[@홍길동](dooray://t/tasks/1)",
        '[@홍길동](dooray://t/tasks/1 "member")',
        "![@홍길동](dooray://t/files/1)",
        "[ @홍길동](dooray://t/tasks/1)",
        "[\u200b@홍길동](dooray://t/tasks/1)",
        "[＠홍길동](dooray://t/tasks/1)",
        "[@](dooray://t/files/1)",
        "[**@홍길동**](dooray://t/tasks/1)",
        "[&#64;홍길동](dooray://t/tasks/1)",
        # 리뷰 L1의 두 입력 — 링크를 걷거나 글자로 읽으면 이어진다
        f'doo[a](dooray://t/files/1 "x")ray://t/members/{OTHER_ID}',
        f'do[o](dooray://t/files/1 "x")ray://t/members/{OTHER_ID}',
    )

    def test_R8_1_the_allowed_shapes_pass(self) -> None:
        for text in self.ALLOWED:
            with self.subTest(text=text):
                self.assertFalse(mention.has_mention_address(text))

    def test_R8_1_everything_else_is_refused(self) -> None:
        for text in self.REFUSED:
            with self.subTest(text=text):
                self.assertTrue(mention.has_mention_address(text))

    def test_R8_1_the_old_check_still_refuses_every_address(self) -> None:
        # 댓글 등록·업무 만들기는 기존 검사 그대로다 — 업무·파일 링크도 거절한다 (ADR 014 결정 5).
        for text in self.ALLOWED:
            with self.subTest(text=text):
                self.assertEqual(has_dooray_address(text), "dooray" in text)

    def test_R9_3_the_title_is_taken_off_only_from_the_allowed_links(self) -> None:
        for text, expected in (
            ('[a](dooray://t/tasks/1 "member")', "[a](dooray://t/tasks/1)"),
            ('![그림](dooray://t/files/2 "file") 끝', "![그림](dooray://t/files/2) 끝"),
            ('[a](dooray://t/tasks/1 "") [b](dooray://t/files/2 "x")', "[a](dooray://t/tasks/1) [b](dooray://t/files/2)"),
            ("[a](dooray://t/tasks/1)", "[a](dooray://t/tasks/1)"),
            # 허용하는 모양이 아닌 것은 건드리지 않는다(도구가 이미 거절했다)
            (f'[@x](dooray://t/members/{OTHER_ID} "member")', f'[@x](dooray://t/members/{OTHER_ID} "member")'),
            ('[a](https://example.dooray.com/x "t")', '[a](https://example.dooray.com/x "t")'),
            ("글 @멤버18", "글 @멤버18"),
        ):
            with self.subTest(text=text):
                self.assertEqual(mention.strip_link_titles(text), expected)

    def test_R9_3_a_personal_project_task_number_is_not_a_mention(self) -> None:
        # 0.2.1 등록 시험에서 나왔다: 개인 프로젝트의 업무 번호는 `@`로 시작하고, 두레이는 업무 링크의 글자를
        # 업무 번호와 제목으로 바꿔 저장한다. 보이는 글에서 옮긴 링크가 멘션으로 오인돼 거절됐다.
        for label in ("@hong.gd/12 테스트용 쓰레드", "@hong.gd/12", "**@hong.gd/12** 제목", "&#64;hong.gd/12 제목"):
            with self.subTest(label=label):
                self.assertFalse(mention.mention_like_label(label))
        # 번호가 없으면 여전히 멘션처럼 보이는 글자다.
        for label in ("@hong.gd", "@hong.gd/", "@hong.gd/제목", "@홍길동 12/3", "@/12"):
            with self.subTest(label=label):
                self.assertTrue(mention.mention_like_label(label))

    def test_R9_3_a_label_that_looks_like_a_mention(self) -> None:
        for label in ("@홍길동", " @홍길동", "\u200b@x", "＠x", "@", "**@홍길동**", "_@x_", "~~@x~~", "`@x`", "\\@x", "&#64;x", "%40x"):
            with self.subTest(label=label):
                self.assertTrue(mention.mention_like_label(label))
        for label in ("", "홍길동 @x", "a@b.co", "sample/7", "hong.gd/12"):
            with self.subTest(label=label):
                self.assertFalse(mention.mention_like_label(label))

    def test_L1_017_html_tags_before_the_at_sign_are_seen_through(self) -> None:
        # 017 리뷰 L1 — 화면에서 보이지 않는 태그를 지우고 본다. 지운 뒤에도 `<`가 남으면 거절 쪽이다.
        # 태그는 `[`·`]`를 품지 않는다 — 링크 글자 칸이 그 글자를 받지 않는다.
        for label in (
            "<b>@홍길동</b>",
            "<span>@홍길동",
            "<!---->@홍길동",
            "<" * 2000 + "@홍",  # 상한 너머
            "<scr<b></b>ipt>",  # 지우고 나서 다시 조립된다
            "&lt;b&gt;@홍길동",  # 인코딩을 푼 단계에서도
            "**<i>@홍길동</i>**",
        ):
            with self.subTest(label=label[:40]):
                self.assertTrue(mention.mention_like_label(label))
        for label in ("<b>@hong.gd/12 제목</b>", "a <b>x</b>", "<b>sample/7</b> 제목"):
            with self.subTest(label=label):
                self.assertFalse(mention.mention_like_label(label))
        # 고치는 글의 허용 모양에서도 같다.
        self.assertTrue(mention.has_mention_address("[<b>@홍길동</b>](dooray://t/tasks/1)"))
        self.assertFalse(mention.has_mention_address("[<b>@hong.gd/12 제목</b>](dooray://t/tasks/1)"))

    def test_R8_1_the_links_are_counted(self) -> None:
        self.assertEqual(mention.MAX_EDIT_LINKS, 10)
        self.assertEqual(mention.count_edit_links(""), 0)
        self.assertEqual(mention.count_edit_links("[a](dooray://t/files/1)/[b](dooray://t/files/2)"), 2)
        eleven = " ".join(f"[a{n}](dooray://t/tasks/{n})" for n in range(11))
        self.assertEqual(mention.count_edit_links(eleven), 11)
        self.assertFalse(mention.has_mention_address(eleven))  # 수는 도구가 본다

    def test_R8_1_the_function_is_pure(self) -> None:
        text = "[a](dooray://t/tasks/1) 끝"
        self.assertEqual(mention.has_mention_address(text), mention.has_mention_address(text))
        self.assertEqual(text, "[a](dooray://t/tasks/1) 끝")


class TaskWebLinkTest(unittest.TestCase):
    """R8-2 — 설정된 웹 주소의 업무 주소를 가리키는 마크다운 링크만 두레이 업무 링크로 바꾼다 (완료 기준 69)."""

    def test_R8_2_a_task_web_link_becomes_a_dooray_task_link_without_a_title(self) -> None:
        text = f"참고: [회의록]({WEB}{TASKS}1000000000000000301) 끝"
        links = mention.task_web_links(text, WEB, TASKS)
        self.assertEqual([link.post_id for link in links], ["1000000000000000301"])
        self.assertEqual(
            mention.link_tasks(text, links, TENANT),
            f"참고: [회의록](dooray://{TENANT}/tasks/1000000000000000301) 끝",
        )

    def test_R8_2_two_links_keep_their_order_and_the_text_between(self) -> None:
        text = f"[a]({WEB}{TASKS}1), [b]({WEB}{TASKS}2)."
        links = mention.task_web_links(text, WEB, TASKS)
        self.assertEqual(
            mention.link_tasks(text, links, "t"), "[a](dooray://t/tasks/1), [b](dooray://t/tasks/2)."
        )

    def test_R8_2_other_addresses_are_not_task_links(self) -> None:
        for address in (
            "https://other.dooray.com/project/tasks/1",  # 다른 호스트
            "https://example.dooray.com.evil.example/project/tasks/1",  # 호스트 흉내
            "https://example.dooray.com@evil.example/project/tasks/1",  # 사용자 정보
            "https://user@example.dooray.com/project/tasks/1",
            "https://user:pw@example.dooray.com/project/tasks/1",
            "https://EXAMPLE.dooray.com/project/tasks/1",  # 대소문자
            "https://Example.Dooray.com/project/tasks/1",
            "HTTPS://example.dooray.com/project/tasks/1",
            "https://example.dooray.com./project/tasks/1",  # 끝의 점
            "https://example.dooray.com:443/project/tasks/1",  # 포트
            "https://example.dooray.com:/project/tasks/1",
            "http://example.dooray.com/project/tasks/1",  # 스킴
            "//example.dooray.com/project/tasks/1",
            "https://example.dooray.com\\@evil.example/project/tasks/1",
            "https://example.dooray.com/project/posts/1",  # 경로
            "https://example.dooray.com/project/tasks/",
            "https://example.dooray.com/project//tasks/1",
            "https://example.dooray.com/Project/tasks/1",
            "https://example.dooray.com/project/tasks/../tasks/1",
            "https://example.dooray.com/project/tasks/1?x=",  # 뒤에 붙은 것
            "https://example.dooray.com/project/tasks/1?",
            "https://example.dooray.com/project/tasks/1#c",
            "https://example.dooray.com/project/tasks/1/edit",
            "https://example.dooray.com/project/tasks/1/",
            "https://example.dooray.com/project/tasks/1%2F..%2F2",
            "https://example.dooray.com/project/tasks/%31",
            "https://example.dooray.com/project/tasks/1.2",
            "https://example.dooray.com/project/tasks/" + "1" * 65,
            "https://example.dooray.com/project/tasks/１",  # 전각 숫자
            "https://[example.dooray.com/project/tasks/1",  # 풀지 못하는 주소
            "dooray://t/tasks/1",
        ):
            with self.subTest(address=address):
                self.assertEqual(web_links(f"[a]({address})"), [])

    def test_R8_2_only_markdown_links_outside_code_are_taken(self) -> None:
        address = f"{WEB}{TASKS}1"
        for text in (
            address,  # 맨 주소
            f"<{address}>",
            f"![a]({address})",  # 이미지
            f'[a]({address} "title")',  # title이 붙은 링크
            f"`[a]({address})`",  # 인라인 코드
            f"```\n[a]({address})\n```",  # 펜스
            f"> [a]({address})",  # 인용
            f"[a\n]({address})",
        ):
            with self.subTest(text=text):
                self.assertEqual(web_links(text), [])
        self.assertEqual(web_links(f"[a]({address})", None), [])  # 웹 주소 설정이 없다
        self.assertEqual(web_links(f"[a]({address})", ""), [])

    def test_R8_2_the_sixth_is_over_the_cap(self) -> None:
        self.assertEqual(mention.MAX_TASK_LINKS, 5)
        five = " ".join(f"[a{n}]({WEB}{TASKS}{n})" for n in range(5))
        self.assertEqual(len(web_links(five)), 5)
        many = " ".join(f"[a{n}]({WEB}{TASKS}{n})" for n in range(40))
        self.assertEqual(len(web_links(many)), 6)  # 하나 더 담고 멈춘다 — 넘친 것을 도구가 안다

    def test_R9_3_a_web_link_followed_by_a_path_is_not_taken(self) -> None:
        link = f"[a]({WEB}{TASKS}1)"
        for tail in ("/members/2)", "(//t/members/2)", "%2F", "\\x"):
            with self.subTest(tail=tail):
                self.assertEqual(web_links(link + tail), [])
        for tail in ("", " 끝", "/[b](x)", "/ 끝", "."):
            with self.subTest(tail=tail):
                self.assertEqual(web_links(link + tail), ["1"])

    def test_R8_2_a_bad_tenant_changes_nothing(self) -> None:
        text = f"[a]({WEB}{TASKS}1)"
        links = mention.task_web_links(text, WEB, TASKS)
        for tenant in (None, "", "t/members", "t x", "t" * 65):
            with self.subTest(tenant=tenant):
                self.assertEqual(mention.link_tasks(text, links, tenant), text)

    def test_R8_2_the_label_is_kept_and_nothing_else_moves(self) -> None:
        text = f"앞 @멤버18 [멤버19 자료]({WEB}{TASKS}7) 뒤"
        links = mention.task_web_links(text, WEB, TASKS)
        self.assertEqual(
            mention.link_tasks(text, links, "t"), "앞 @멤버18 [멤버19 자료](dooray://t/tasks/7) 뒤"
        )
        # 바꾼 글은 허용하는 모양이다 — 고치는 도구가 바꾼 뒤에 다시 보아도 걸리지 않는다.
        self.assertFalse(mention.has_mention_address(mention.link_tasks(text, links, "t")))


class EditAddressCostTest(unittest.TestCase):
    """R8-5 — 사용자 텍스트에 도는 정규식이다. 4만 자 반복 입력 1초 안."""

    def test_R8_5_forty_thousand_character_walls_are_cheap(self) -> None:
        link = f"[a]({WEB}{TASKS}1)"
        walls = {
            "[": "[" * 40000,
            "![": "![" * 20000,
            "](": "](" * 20000,
            "[](": "[](" * 13000,
            "[a](dooray://t/tasks/": "[a](dooray://t/tasks/" * 1900,
            "[a](dooray://t/tasks/1)": "[a](dooray://t/tasks/1)" * 1700,
            '[a](dooray://t/tasks/1 "': '[a](dooray://t/tasks/1 "' * 1600,
            "web link": link * 800,
            "web link in code": f"`{link}` " * 700,
            "[a](x…": "[a](" + "x" * 40000,
            "[a](https://…": "[a](https://" + "e" * 40000,
            "dooray:": "dooray:" * 6000,
            "`": "`" * 40000,
            "&#": "&#" * 20000,
            # 개정 9 — 뒤보기와 title 떼기
            "[a](dooray://t/tasks/1)/": "[a](dooray://t/tasks/1)/" * 1700,
            "[a](dooray://t/tasks/1)(": "[a](dooray://t/tasks/1)(" * 1700,
            '[a](dooray://t/tasks/1 "x")': '[a](dooray://t/tasks/1 "x")' * 1500,
            "[@a](dooray://t/tasks/1)": "[@a](dooray://t/tasks/1)" * 1700,
            "web link/": (link + "/") * 800,
        }
        for name, wall in walls.items():
            for label, run in (
                ("has_mention_address", lambda: mention.has_mention_address(wall)),
                ("strip_link_titles", lambda: mention.strip_link_titles(wall)),
                ("count_edit_links", lambda: mention.count_edit_links(wall)),
                ("task_web_links", lambda: mention.task_web_links(wall, WEB, TASKS)),
                (
                    "link_tasks",
                    lambda: mention.link_tasks(wall, mention.task_web_links(wall, WEB, TASKS), "t"),
                ),
            ):
                with self.subTest(wall=name, function=label):
                    started = time.perf_counter()
                    run()
                    self.assertLess(time.perf_counter() - started, 1.0)


class NewWriteTaskLinkTest(unittest.TestCase):
    """017 TR-1 — 댓글 앵커가 붙은 업무 웹 주소는 업무 링크가 아니다(새 댓글·새 업무에서도 바꾸지 않는다)."""

    def test_TR_1_a_comment_anchor_is_not_a_task_link(self) -> None:
        for address in (
            f"{WEB}{TASKS}1000000000000000301#comment-1000000000000000056",
            f"{WEB}{TASKS}1000000000000000301#comment-",
            f"{WEB}{TASKS}1000000000000000301#",
        ):
            with self.subTest(address=address):
                self.assertEqual(web_links(f"[a]({address})"), [])
        # 앵커가 없는 같은 주소는 업무 링크다 — 위의 빈 결과가 다른 이유로 나온 것이 아니다.
        self.assertEqual(web_links(f"[a]({WEB}{TASKS}1000000000000000301)"), ["1000000000000000301"])
