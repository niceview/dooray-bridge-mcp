"""005 spec §7-3 — 문서와 코드가 어긋나면 실패한다 (D1~D11).

문서의 목록은 HTML 주석 두 줄로 감싼다(렌더링하면 보이지 않는다):
`<!-- 코드와 맞춤: 이름 -->` … `<!-- /코드와 맞춤 -->`. 그 구간의 값은 인라인 코드(백틱) 조각이다.
표식 이름은 문서마다 정확히 한 번 나온다 — 두 번이면 한쪽만 고치는 일이 생긴다.
기대값은 코드에서 뽑는다. 문서의 목록을 이 파일에 베끼지 않는다.
"""

from __future__ import annotations

import os
import re
import tomllib
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from dooray_core import client, config, errors
from mcp_server import runtime

from support import tenant_addresses
from test_mcp_server import ServerCase

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
SECURITY = REPO / "SECURITY.md"
DEVELOPMENT = REPO / "docs" / "development.md"
DOCS = (README, SECURITY, DEVELOPMENT)

MARK_OPEN = re.compile(r"<!-- 코드와 맞춤: ([a-z-]+) -->")
MARK_CLOSE = "<!-- /코드와 맞춤 -->"
TICKS = re.compile(r"`([^`\n]+)`")

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
VERSION_LINE = re.compile(r"이 문서는 dooray-bridge \*\*([0-9][0-9A-Za-z.+-]*)\*\* 기준")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def region(text: str, name: str) -> str:
    """표식 `name`의 구간. 없거나 둘 이상이거나 닫히지 않았으면 AssertionError."""
    opening = f"<!-- 코드와 맞춤: {name} -->"
    count = text.count(opening)
    if count != 1:
        raise AssertionError(f"표식 {name}이(가) {count}번 나옵니다. 정확히 한 번이어야 합니다.")
    start = text.index(opening) + len(opening)
    end = text.find(MARK_CLOSE, start)
    if end < 0:
        raise AssertionError(f"표식 {name}이(가) 닫히지 않았습니다.")
    body = text[start:end]
    if MARK_OPEN.search(body):
        raise AssertionError(f"표식 {name} 안에 다른 표식이 열렸습니다.")
    return body


def ticks(segment: str) -> list[str]:
    return TICKS.findall(segment)


def section_of(text: str, name: str) -> str:
    """표식 `name`이 든 `## ` 절 전체에서 표식 구간을 뺀 글."""
    opening = f"<!-- 코드와 맞춤: {name} -->"
    at = text.index(opening)
    start = text.rfind("\n## ", 0, at)
    end = text.find("\n## ", at)
    whole = text[start : len(text) if end < 0 else end]
    return whole.replace(region(text, name), "")


def lock() -> dict:
    return tomllib.loads(read(REPO / "uv.lock"))


def lock_hosts(value: object) -> set[str]:
    """`uv.lock`의 `url`·`registry`에서 호스트를 모은다."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("url", "registry") and isinstance(item, str) and item.startswith("https://"):
                found.add(urlsplit(item).hostname or "")
            else:
                found |= lock_hosts(item)
    elif isinstance(value, list):
        for item in value:
            found |= lock_hosts(item)
    return found


class MarkerTest(unittest.TestCase):
    def test_every_marker_opens_once_and_closes(self) -> None:
        for path in DOCS:
            text = read(path)
            names = MARK_OPEN.findall(text)
            with self.subTest(doc=path.name):
                self.assertEqual(
                    [name for name, n in Counter(names).items() if n > 1], []
                )
                self.assertEqual(len(names), text.count(MARK_CLOSE))
                for name in names:
                    region(text, name)


class SecurityListsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read(SECURITY)

    def test_D1_the_api_addresses_are_the_allowed_four(self) -> None:
        values = ticks(region(self.text, "api-bases"))
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(set(values), set(config.ALLOWED_API_BASES))

    def test_D2_the_link_suffixes_are_the_allowed_four(self) -> None:
        values = ticks(region(self.text, "web-suffixes"))
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(set(values), set(config.ALLOWED_WEB_SUFFIXES))

    def test_D3_the_write_table_matches_the_transport_rules(self) -> None:
        rows = [
            line
            for line in region(self.text, "write-paths").splitlines()
            if line.startswith("|") and "`" in line
        ]
        # 012 spec §10-2 #28 — 여덟 행. 요청 칸은 (동사, 경로)이고, 동사와 경로가 **함께** 맞는 행이 하나다.
        # 필드 칸에 백틱이 없으면 빈 집합이다(`set-done` — 본문 없음).
        self.assertEqual(len(rows), 8)
        used: list[int] = []
        for row in rows:
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            with self.subTest(row=cells[0]):
                request = ticks(cells[0])
                self.assertEqual(len(request), 1)
                verb, path = request[0].split(" ", 1)
                self.assertIn(verb, ("POST", "PUT"))
                concrete = re.sub(r"\{[^}]*\}", "1", path)
                matched = [
                    index
                    for index, (row_verb, pattern, _) in enumerate(client._WRITE_PATHS)
                    if row_verb == verb and pattern.fullmatch(concrete)
                ]
                self.assertEqual(len(matched), 1, f"{verb} {path}에 맞는 행이 하나가 아닙니다.")
                self.assertEqual(set(ticks(cells[1])), set(client._WRITE_PATHS[matched[0]][2]))
                used.extend(matched)
        self.assertEqual(sorted(used), list(range(len(client._WRITE_PATHS))))

    def test_D6_the_audit_fields_are_the_code_fields(self) -> None:
        values = ticks(region(self.text, "audit-fields"))
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(set(values), set(client.AUDIT_FIELDS))

    def test_D6_the_audit_section_says_writes_are_the_POST_lines(self) -> None:
        # `mode`가 write인 도구 줄은 "쓰기 도구를 불렀다"이지 "썼다"가 아니다 (005 M6 관찰).
        section = section_of(self.text, "audit-fields")
        self.assertIn("`method`", section)
        self.assertIn("`POST`", section)
        self.assertIn("`PUT`", section)  # 012 — 고치기는 PUT으로도 나간다

    def test_D7_the_third_party_list_is_the_lock_file(self) -> None:
        values = ticks(region(self.text, "packages"))
        locked = {package["name"] for package in lock()["package"]} - {"dooray-bridge"}
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(set(values), locked)

    def test_D9_the_install_hosts_cover_the_lock_file(self) -> None:
        hosts = lock_hosts(lock())
        self.assertTrue(hosts)
        self.assertNotIn("", hosts)
        self.assertLessEqual(hosts, set(ticks(region(self.text, "install-hosts"))))

    def test_D10_the_document_names_the_project_version(self) -> None:
        project = tomllib.loads(read(REPO / "pyproject.toml"))["project"]["version"]
        found = list(VERSION_LINE.finditer(self.text))
        self.assertEqual(len(found), 1)
        # 첫머리다 — 첫 번째 `## ` 절보다 앞에 있다.
        self.assertLess(found[0].start(), self.text.index("\n## "))
        self.assertEqual(found[0].group(1), project)

    def test_D14_the_edit_limits_say_what_the_code_does(self) -> None:
        # 012 spec 개정 7과 그 고침(ADR 014) — 막는 것은 모델이 쓴 두레이 주소 하나이고, 남는 길을 적는다.
        # 012 spec 개정 8의 R8-4 — 막는 것은 사람을 가리키는 주소와 알 수 없는 종류다. 업무·첨부 링크는 받는다.
        self.assertIn(
            "Claude가 쓴 글에 **사람을 가리키는** 두레이 내부 주소(멤버·그룹)나 알 수 없는 종류의 주소가 있으면 고치지 않습니다",
            self.text,
        )
        self.assertIn("업무 링크와 첨부 링크는 쓸 수 있습니다", self.text)
        self.assertIn("Claude가 직접 쓴 링크는 확인하지 않습니다", self.text)
        self.assertIn("그 업무에 없는 사람에게 알림이 가는 고치기가 만들어질 수 있습니다", self.text)
        # 012 spec 개정 9의 R9-1 — 남은 길은 "글에 이미 있는 링크"보다 넓다(리뷰 H1). 허락 창 안내에 members.
        self.assertIn("한 번의 고치기에서 Claude가 쓴 글로는, 멘션은 서버가 @이름을 바꿔서만 만들어집니다", self.text)
        self.assertNotIn("멘션은 서버가 @이름을 바꿔서만 만듭니다", self.text)
        for text in (self.text, read(README)):
            with self.subTest(file="SECURITY" if text is self.text else "README"):
                self.assertIn(
                    "고치기를 두 번 이상 이으면, 링크가 없던 글에서도 아무 사람이나 그룹을 부르는 멘션을 만들 수 있습니다",
                    text,
                )
                self.assertIn("이 프로그램은 이것을 막지 않습니다", text)
                self.assertIn("dooray://, members, 긴 숫자가 보이면", text)
        self.assertIn("dooray://, members, 긴 숫자가 보이면 멈추고 확인하세요", self.text)
        # 댓글 고치기 행은 운영자가 확인한 것을 적는다(R5-4) — "확인하지 못했다"가 남지 않는다.
        row = next(
            line for line in region(self.text, "write-paths").splitlines() if "/logs/{댓글}" in line
        )
        self.assertIn("수정된 시각만 남고", row)
        self.assertNotIn("확인하지 못", row)


class ToolListsTest(ServerCase):
    """D4·D5 — `ServerCase`가 `runtime.WRITE_TOOLS_REGISTERED`를 패치로 되돌린다."""

    async def tool_names(self, *, write: bool) -> set[str]:
        if write:
            with mock.patch.dict(os.environ, {"DOORAY_ENABLE_WRITE": "1"}):
                found = await self.build(write=True).list_tools()
        else:
            found = await self.build(write=False).list_tools()
        return {tool.name for tool in found}

    async def test_D4_the_read_lists_are_the_gate_off_tools(self) -> None:
        expected = await self.tool_names(write=False)
        for path in (SECURITY, README):
            values = ticks(region(read(path), "tools-read"))
            with self.subTest(doc=path.name):
                self.assertEqual(len(values), len(set(values)))
                self.assertEqual(set(values), expected)

    async def test_D5_the_write_lists_are_what_the_gate_adds(self) -> None:
        added = await self.tool_names(write=True) - await self.tool_names(write=False)
        self.assertTrue(added)
        for path in (SECURITY, README):
            values = ticks(region(read(path), "tools-write"))
            with self.subTest(doc=path.name):
                self.assertEqual(len(values), len(set(values)))
                self.assertEqual(set(values), added)


class ErrorHeadsTest(unittest.TestCase):
    """D12 — README의 문제 해결 표가 옮긴 문장 첫머리가 코드의 문장과 같다 (운영자 결정 2026-09-28)."""

    QUOTED = re.compile(r'"([^"\n]+)"')

    def messages(self) -> list[str]:
        found = []
        for name, value in vars(errors).items():
            if name.endswith("_MESSAGE") and isinstance(value, str):
                # 상한은 도구가 쓰는 값으로 채운다. 그 밖의 자리표시는 그 앞까지만 본다.
                found.append(value.replace("{limit}", str(runtime.TOOL_CALL_LIMIT)).split("{")[0])
        return found

    # 첫머리가 이보다 짧으면 거의 아무 문장의 시작과도 맞는다 — 조용히 통과한다 (005 리뷰 L1).
    MIN_HEAD = 8
    SEPARATOR = re.compile(r"\|[\s|:-]+\|")

    def rows(self) -> list[str]:
        """표식 구간의 데이터 행. 머리 행(첫 행)과 구분 행은 뺀다."""
        table = [
            line.strip()
            for line in region(read(README), "error-heads").splitlines()
            if line.startswith("|")
        ]
        return [line for line in table[1:] if not self.SEPARATOR.fullmatch(line)]

    def test_D12_every_row_carries_one_quoted_head(self) -> None:
        # 따옴표 없는 행(문장이 없는 증상)은 이 표 밖에 둔다 — 행을 더하면서 따옴표를 빠뜨려도 통과하지 않게.
        # 행 수를 상수로 고정하지 않는다. 행 수와 첫머리 수가 같아야 한다.
        rows = self.rows()
        self.assertTrue(rows)
        heads = [head for row in rows for head in self.QUOTED.findall(row.split("|")[1])]
        self.assertEqual(len(heads), len(rows), rows)
        for head in heads:
            with self.subTest(head=head):
                self.assertGreaterEqual(len(head), self.MIN_HEAD, head)

    def test_D12_every_quoted_head_begins_a_message_in_the_code(self) -> None:
        heads = [
            head
            for row in self.rows()
            # 첫 칸(증상)만 본다.
            for head in self.QUOTED.findall(row.split("|")[1])
        ]
        self.assertTrue(heads)
        messages = self.messages()
        self.assertTrue(messages)
        for head in heads:
            with self.subTest(head=head):
                self.assertTrue(any(message.startswith(head) for message in messages), head)


class TenantCheckTest(unittest.TestCase):
    """005 리뷰 M5 — 검사 함수를 검사한다. 리뷰가 든 빠져나가는 모양 여덟을 전부 잡아야 한다."""

    CAUGHT = (
        ("대문자 호스트", "https://ACME.dooray.com"),
        ("대문자 스킴", "HTTPS://acme.dooray.com"),
        ("문장 끝 점", "주소는 https://acme.dooray.com."),
        ("http", "http://acme.dooray.com"),
        ("스킴 없음", "주소는 acme.dooray.com 입니다"),
        ("경로 포함", "https://acme.dooray.com/project/tasks/1"),
        ("포트 포함", "https://acme.dooray.com:8443"),
        ("dooray://숫자/", "[@홍길동](dooray://1234567890/members/42)"),
        # 여덟 밖에서 같은 판정이 잡아야 하는 것.
        ("공공 도메인", "https://acme.gov-dooray.co.kr"),
        ("자리표시 뒤 실제 이름", "https://회사이름.acme.dooray.com"),
        ("dooray://이름", "dooray://acme/members/1"),
    )
    ALLOWED = (
        ("API 주소", "https://api.dooray.com, https://api.gov-dooray.co.kr"),
        ("API 호스트만", "api.dooray.co.kr"),
        ("접미만", "`.dooray.com` `.gov-dooray.com` `.gov-dooray.co.kr` `.dooray.co.kr`"),
        ("자리표시", "예: https://회사이름.dooray.com"),
        ("자리표시(스킴 없음)", "`회사이름.dooray.com`처럼"),
        ("dooray:// 설명", "두레이 내부 주소(`dooray://`)를 쓰지 않는다. dooray://로 시작하는 링크"),
        ("dooray:// 자리표시", "dooray://{테넌트}/{종류}/{ID}, dooray://{usable_tenant}/members/1, dooray://…/members/{id}"),
        ("dooray:// 정규식", r"\(dooray://[^/\s)]+/members/ dooray://([^/\s\"'<>()\[\]]{0,65})/"),
        ("도메인 이름만", "dooray.com에서 발급한다"),
    )

    def test_M5_every_shape_is_caught(self) -> None:
        for label, text in self.CAUGHT:
            with self.subTest(shape=label):
                self.assertNotEqual(tenant_addresses(text), [], text)

    def test_M5_the_allowed_forms_pass(self) -> None:
        for label, text in self.ALLOWED:
            with self.subTest(shape=label):
                self.assertEqual(tenant_addresses(text), [], text)


class UvIsNotAPrerequisiteTest(unittest.TestCase):
    """D13 — `uv`는 준비물이 아니다. 없으면 Claude Desktop이 받는다 (ADR 013)."""

    def test_D13_the_documents_do_not_say_uv_must_be_installed_first(self) -> None:
        self.assertNotIn("운영자가 설치해 줍니다", read(README))

    def test_D13_the_documents_do_not_describe_the_withdrawn_launcher(self) -> None:
        # 0.1.2의 시작 스크립트는 걷어 냈다 (ADR 013). 읽는 사람용 문서에 남아 있으면 없는 동작을 약속한다.
        for path in (README, SECURITY):
            with self.subTest(doc=path.name):
                self.assertNotIn("launch.sh", read(path))
                self.assertNotIn("astral.sh", read(path))


class NoSecretTest(unittest.TestCase):
    # `DOORAY_API_TOKEN` 뒤에 `=`·`:`·`"`가 오는 자리 (005 리뷰 L2). 셸(`=`), YAML·JSON(`:`, `"…": "…"`),
    # 공백을 둔 대입(`DOORAY_API_TOKEN = "…"`)을 다 본다.
    TOKEN_AT = re.compile(r'DOORAY_API_TOKEN(?=[ \t]*[=:"])')
    TOKEN_VALUE = re.compile(r'DOORAY_API_TOKEN"?[ \t]*[=:][ \t]*["\']?([^\s"\'`]*)')

    def test_D8_no_document_passes_a_token_in_a_command(self) -> None:
        for path in (README, SECURITY):
            with self.subTest(doc=path.name):
                self.assertNotIn("DOORAY_API_TOKEN=", read(path))
        for path in DOCS:
            text = read(path)
            for at in self.TOKEN_AT.finditer(text):
                value = self.TOKEN_VALUE.match(text, at.start())
                shown = text[at.start() : at.start() + 60].splitlines()[0]
                with self.subTest(doc=path.name, example=shown):
                    # 값을 주는 모양이면 그 값은 가짜뿐이다. 값을 읽을 수 없는 모양도 실패다.
                    self.assertIsNotNone(value, shown)
                    self.assertEqual(value.group(1), "fake-token", shown)
        for path in DOCS:
            text = read(path)
            with self.subTest(doc=path.name):
                self.assertNotIn("-e DOORAY_API_TOKEN", text)
                self.assertNotIn("--env DOORAY_API_TOKEN", text)
            for line in text.splitlines():
                if "security add-generic-password" in line:
                    with self.subTest(doc=path.name, line=line):
                        # 값을 인자로 주지 않는다 — `-w`를 끝에 두면 물어본다 (mcp-notes §9-3).
                        self.assertTrue(line.strip().rstrip("` ").endswith(" -w"))

    def test_D11_no_document_carries_a_real_address_or_name(self) -> None:
        for path in DOCS:
            text = read(path)
            with self.subTest(doc=path.name):
                self.assertEqual(EMAIL.findall(text), [])
                # 두레이 도메인의 실제 호스트는 API 주소 넷뿐이다. 테넌트 주소는 자리표시로만 쓴다 (B7ⓔ와 같은 판정).
                self.assertEqual(tenant_addresses(text), [])


if __name__ == "__main__":
    unittest.main()
