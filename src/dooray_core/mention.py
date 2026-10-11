"""멘션 판별 (spec §9). 순수 함수다 — 파일도 네트워크도 건드리지 않는다.

판정은 **멤버 ID로만** 한다 (api-notes §2-7). 링크의 title(`member`·`me`·`admin`·`guest`·없음)은
읽기만 하고 쓰지 않는다 — `me`는 서버가 조회자에 맞춰 바꿔 주는 값이라 문서에 없는 동작이고,
같은 댓글을 다른 토큰으로 읽으면 다른 멘션이 `me`가 된다.

첫 세그먼트(테넌트 ID)도 버린다. 그룹 구성원 집합은 **인자로** 받는다 —
표를 채우려면 API를 불러야 하고, 그 일은 `collect.py`가 한다.
"""

from __future__ import annotations

import bisect
import html
import re
import unicodedata
import urllib.parse
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

# --- 정규식 (§9-2) -----------------------------------------------------------
#
# `.`은 쓰지 않는다. 그래도 title 앞의 `\s+`가 줄바꿈을 받아 title이 다음 줄인 링크도 잡힌다 (023 설계 리뷰 2).
# ID는 `\d+`가 아니라 `[^/\s")]+`로 느슨하게 받는다 — 19자리 숫자가 관찰됐지만 형태를 전제하지 않는다.
# title 자리 `(?:\s+"[^"\n]*")?`가 관찰된 네 값과 "없음"을 전부 흡수한다.

MEMBER_LINK = re.compile(
    r'\[@?([^\]\n]{0,256})\]\(dooray://[^/\s)]+/members/([^/\s")]+)(?:\s+"[^"\n]*")?\)'
)
GROUP_LINK = re.compile(
    r'\[@?([^\]\n]{0,256})\]\(dooray://[^/\s)]+/member-groups/([^/\s")]+)(?:\s+"[^"\n]*")?\)'
)

# html 링크의 주소 값 (§9-4). markdown과 같은 규칙으로 읽는다.
HREF_TARGET = re.compile(r'dooray://[^/\s)]+/(members|member-groups)/([^/\s")]+)')

# 인용문·코드 구간 (§9-3). 인라인 코드는 정규식이 아니라 `_inline_code_spans`가 찾는다 —
# `(`+)[^\n]*?\1`은 한 줄의 백틱 벽에서 O(n²) 이상이었다(8,000자에 4초, 012 리뷰 M4).
FENCE_LINE = re.compile(r" {0,3}(`{3,}|~{3,})")
QUOTE_LINE = re.compile(r" {0,3}>")

HTML_MIME = "text/html"
# html 멘션 앵커의 흔적 (api-notes "추가 실측": `data-dooray-href="dooray://…"`).
_HTML_MENTION_HINT = "data-dooray-href"

_KINDS = {"members": "member", "member-groups": "group"}


@dataclass(frozen=True)
class Mention:
    kind: str  # "member" | "group"
    target_id: str  # 멤버 ID 또는 그룹 ID
    display: str  # 링크 텍스트에서 앞의 @를 뗀 표시 이름
    quoted: bool  # 인용문(>) 또는 코드 안에 있으면 True


def find_mentions(content: str, *, mime_type: str | None = None) -> list[Mention]:
    """본문에서 멘션을 전부 찾는다. 나오는 순서 그대로 돌려준다."""
    if not content:
        return []
    # `mimeType`의 정확한 값에 판정을 걸지 않는다 (003 리뷰 H1). `text/html; charset=…`이거나
    # 비어 있거나, 마크다운 본문 안에 html 멘션 앵커가 끼어 오면 `[]`가 되어 멘션을 **조용히**
    # 놓친다 — 빈 목록은 "멘션 없음"과 구분되지 않는다. 앵커의 흔적이 있으면 html 쪽도 읽는다.
    if (mime_type or "").strip().lower().startswith(HTML_MIME):
        return _html_mentions(content)
    found = _markdown_mentions(content)
    if _HTML_MENTION_HINT in content:
        found = [*found, *_html_mentions(content)]
    return found


def mentions_me(
    content: str,
    *,
    my_id: str,
    group_members: Mapping[str, Collection[str]] | None = None,
    mime_type: str | None = None,
    include_quoted: bool = False,
) -> bool:
    """이 본문이 나를 멘션했는가.

    그룹 멘션은 `group_members[그룹ID]`에 내가 있으면 나를 멘션한 것이다.
    표에 그 그룹이 없으면 **판정하지 않는다**(거짓). 인용은 남의 글을 옮긴 것이라 기본 제외다.
    """
    return is_mentioned(
        find_mentions(content, mime_type=mime_type),
        my_id=my_id,
        group_members=group_members,
        include_quoted=include_quoted,
    )


def is_mentioned(
    found: Iterable[Mention],
    *,
    my_id: str,
    group_members: Mapping[str, Collection[str]] | None = None,
    include_quoted: bool = False,
) -> bool:
    """이미 찾아 둔 멘션 목록으로 판정한다. 같은 본문에 정규식을 두 번 돌리지 않게 한다."""
    groups = group_members or {}
    for mention in found:
        if mention.quoted and not include_quoted:
            continue
        if mention.kind == "member":
            if mention.target_id == my_id:
                return True
        elif my_id in groups.get(mention.target_id, ()):
            return True
    return False


# --- markdown ----------------------------------------------------------------


def _markdown_mentions(content: str) -> list[Mention]:
    masked = _masked_spans(content)
    found: list[tuple[int, Mention]] = []
    for kind, pattern in (("member", MEMBER_LINK), ("group", GROUP_LINK)):
        for matched in pattern.finditer(content):
            found.append(
                (
                    matched.start(),
                    Mention(
                        kind=kind,
                        target_id=matched.group(2),
                        display=matched.group(1).strip(),
                        quoted=_inside(masked, matched.start()),
                    ),
                )
            )
    found.sort(key=lambda item: item[0])
    return [mention for _, mention in found]


def _masked_spans(content: str) -> list[tuple[int, int]]:
    """인용문·펜스 코드블록·인라인 코드가 차지하는 구간 (§9-3).

    여기서 **시작하는** 멘션이 `quoted=True`가 된다.
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    fence: str | None = None
    for line in content.splitlines(keepends=True):
        start, end = offset, offset + len(line)
        offset = end
        opener = FENCE_LINE.match(line)
        if fence is not None:
            spans.append((start, end))
            if opener is not None and opener.group(1)[0] == fence:
                fence = None
            continue
        if opener is not None:
            fence = opener.group(1)[0]
            spans.append((start, end))
            continue
        if QUOTE_LINE.match(line) is not None:
            spans.append((start, end))
            continue
        for code_start, code_end in _inline_code_spans(line):
            spans.append((start + code_start, start + code_end))
    return spans


def _inline_code_spans(line: str) -> list[tuple[int, int]]:
    """한 줄의 인라인 코드 구간. 옛 정규식 `(`+)[^\\n]*?\\1`의 `finditer`와 **같은 구간**을 선형으로 낸다.

    정규식은 여는 자리 p에서 백틱 k개(가장 긴 것부터)를 잡고, 그 뒤 처음 나오는 백틱 k개에서 닫는다.
    p에서 이어지는 백틱이 R개이면: 뒤의 백틱 연속 가운데 가장 긴 것이 M일 때
    - k = min(R, M)이 R의 절반보다 크면, 뒤에서 처음 나오는 길이 k 이상의 연속의 앞 k개에서 닫는다.
    - 아니면 R이 2 이상일 때 여는 연속 안에서 닫는다: [p, p + 2·(R//2)).
    - R이 1이고 뒤에 백틱이 없으면 그 뒤로는 구간이 없다.
    닫은 자리가 연속의 가운데이면 거기서 다음 구간을 찾는다(정규식도 그렇다).
    한 줄이므로 줄바꿈은 끝에만 있다 — `[^\\n]`은 닫는 자리를 막지 않는다.
    """
    runs: list[tuple[int, int]] = []  # (시작, 길이) — 최대 백틱 연속
    at = line.find("`")
    while at != -1:
        end = at
        while end < len(line) and line[end] == "`":
            end += 1
        runs.append((at, end - at))
        at = line.find("`", end)
    # later_max[i] — i번째 연속부터 끝까지 가장 긴 연속의 길이.
    later_max = [0] * (len(runs) + 1)
    for index in range(len(runs) - 1, -1, -1):
        later_max[index] = max(runs[index][1], later_max[index + 1])

    spans: list[tuple[int, int]] = []
    index = 0
    position = runs[0][0] if runs else 0
    while index < len(runs):
        run_start, run_length = runs[index]
        length = run_start + run_length - position  # R
        close = min(length, later_max[index + 1])  # k
        if 2 * close > length:
            # 뒤에서 처음 나오는 길이 k 이상의 연속. 건너뛰는 연속은 전부 이 구간 안이다 — 모두 합쳐 선형이다.
            index += 1
            while runs[index][1] < close:
                index += 1
            end = runs[index][0] + close
        elif length >= 2:
            end = position + 2 * (length // 2)
        else:
            break
        spans.append((position, end))
        position = end
        if position == runs[index][0] + runs[index][1]:
            index += 1
            if index < len(runs):
                position = runs[index][0]
    return spans


def _inside(spans: list[tuple[int, int]], position: int) -> bool:
    return any(start <= position < end for start, end in spans)


# --- html (§9-4) — 실측한 모양 하나와 추정 하나 (003 spec §5-2) ---------------


def _html_mentions(content: str) -> list[Mention]:
    parser = _HtmlMentions()
    parser.feed(content)
    parser.close()
    return parser.found


class _HtmlMentions(HTMLParser):
    """`<a … data-dooray-href="dooray://…/members/{id}">…</a>`를 markdown과 같은 규칙으로 읽는다.

    실측한 모양은 주소를 **`data-dooray-href`**에 담는다 (003 spec §5-2, `streams.json`).
    `href`도 계속 읽는다 — 그 모양은 아직 관찰하지 못했다 (api-notes §5 U-1).
    `<blockquote>` 안이면 인용으로 본다. 줄 머리 `>`는 html에서 인용이 아니다.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[Mention] = []
        self._quote_depth = 0
        self._links: list[tuple[tuple[str, str] | None, bool, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "blockquote":
            self._quote_depth += 1
        elif tag == "a":
            attributes = dict(attrs)
            # 실측이 먼저, 추정이 뒤다.
            address = attributes.get("data-dooray-href") or attributes.get("href") or ""
            self._links.append((_target(address), self._quote_depth > 0, []))

    def handle_endtag(self, tag: str) -> None:
        if tag == "blockquote":
            self._quote_depth = max(0, self._quote_depth - 1)
        elif tag == "a" and self._links:
            target, quoted, parts = self._links.pop()
            if target is not None:
                kind, target_id = target
                self.found.append(
                    Mention(
                        kind=kind,
                        target_id=target_id,
                        display=_without_at("".join(parts).strip()),
                        quoted=quoted,
                    )
                )

    def handle_data(self, data: str) -> None:
        if self._links:
            self._links[-1][2].append(data)


def _target(href: str) -> tuple[str, str] | None:
    matched = HREF_TARGET.fullmatch(href.strip())
    if matched is None:
        return None
    return _KINDS[matched.group(1)], matched.group(2)


def _without_at(text: str) -> str:
    return text[1:] if text.startswith("@") else text


# --- 멘션 만들기 (007 spec §4-3·§4-4·§5-1) -----------------------------------
#
# 두레이로 나가는 본문의 멘션 링크는 **여기서만** 만든다. 판정도 전부 순수하다 —
# 참여자 이름표와 테넌트는 호출부(`collect.thread_people`)가 두레이 응답에서 거둬 넘긴다.
# 변환은 사람을 **더하지 않는다**: 바꾸는 자리는 언제나 본문에 적힌 `@이름`이다.

# 한 본문에서 살펴보는 `@` 자리. 사용자 텍스트에서 나온 값으로 루프를 돈다 (001 리뷰 H1).
MAX_MENTION_MARKS = 20
# 한 댓글이 멘션하는 서로 다른 사람. 멘션은 알림과 참조 추가다.
MAX_MENTIONS = 5

# 가려진 구간 둘 — 링크 안의 `@`는 이미 링크고, 꺾쇠 안은 html이다.
# 길이 상한은 `render.DOORAY_LINK`·`IMAGE_LINK`·`HTML_IMAGE`와 같은 값이다. 없으면 `[` 4만 자에 O(n²)이다.
# 링크 텍스트(과 이미지 설명)는 CommonMark에서 여러 줄일 수 있다 — `[\n@이름](…)` 안에 링크를 넣지 않게 (007 리뷰 L1).
_LINK_SPAN = re.compile(r"\[[^\]]{0,256}\]\([^)\n]{0,512}\)")
# 들여쓰기 코드 줄(공백 4칸 또는 탭). 목록의 들여쓴 줄도 걸리지만 그쪽은 "바꾸지 않음"이라 안전한 방향이다 (L2).
_INDENTED_LINE = re.compile(r"(?m)^(?: {4}|\t)[^\n]{0,4000}")
_ANGLE_SPAN = re.compile(r"<[^<>\n]{0,1000}>")
# `@hong.gd/12` — 개인 프로젝트의 업무 번호다. 멘션이 아니다. 상한은 `tools.POST_REF`와 같다.
_TASK_LIKE = re.compile(r"[^\s/]{1,100}/\d{1,9}")
# 테넌트 자리. 64자 + 1 — 넘치면 아래 모양 검사에서 떨어진다.
_TENANT_IN_LINK = re.compile(r"""dooray://([^/\s"'<>()\[\]]{0,65})/""")
# 링크 주소 자리에 들어가는 값의 모양 (`client.path_id`와 같은 규칙).
_LINK_VALUE = re.compile(r"[0-9A-Za-z_-]{1,64}")
# 적힌 이름(보고용)의 길이.
_WRITTEN_LIMIT = 30
# 이름에 공백 말고 허용하는 글자. 그 밖은 `isalnum()`이어야 한다 — 허용을 나열한다.
_NAME_EXTRA = frozenset(" ._-")
_NAME_LIMIT = 64
# 인코딩을 푸는 횟수 (§5-1 (a)).
_UNWRAP_ROUNDS = 3
# `render._squeezed`와 같은 분류다. `mention.py`는 `render`를 import하지 못한다 (P8).
_HANGUL_FILLERS = frozenset("ᅟᅠㅤﾠ")
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc", "Mn", "Me"})

# 이유 코드 (§4-3).
REASON_OUTSIDE = "outside"
REASON_SAME_NAME = "same_name"
REASON_UNSAFE_NAME = "unsafe_name"
REASON_LIMIT = "limit"
REASON_NO_TENANT = "no_tenant"
REASON_ATTACHED = "attached"  # 이름 뒤에 글자가 붙었다(`@홍길동님`)
REASON_INCOMPLETE = "incomplete"  # 참여자 이름표가 완전하지 않아 동명이인을 판정할 수 없다


@dataclass(frozen=True)
class Person:
    id: str
    name: str


@dataclass(frozen=True)
class Linked:
    text: str  # 등록할 본문
    linked: tuple[Person, ...]  # 멘션한 사람. 처음 나온 순, 중복 없음
    skipped: tuple[tuple[str, str], ...]  # (적힌 이름, 이유 코드). 적힌 순
    capped: bool  # @ 후보가 MAX_MENTION_MARKS를 넘어 뒤를 보지 않았다


def has_dooray_address(text: str) -> bool:
    """두레이 내부 주소(`dooray:`)가 들어 있는가 (§5-1 (a)).

    종류(멤버·그룹·업무·파일)를 나열하지 않고 scheme 전체를 본다. 인코딩을 번갈아 풀고(최대 3번),
    NFKC로 펴고, 보이지 않는 글자를 뺀 뒤 casefold로 찾는다. 푸는 중간 단계도 전부 본다.
    오탐("Dooray: 설정")은 거절 쪽이다 — 거절 문장이 고치는 법을 말한다.
    """
    stages, settled = _unwrap_stages(text)
    if not settled:
        # 정해 둔 횟수를 풀고도 아직 바뀐다 — 몇 겹 싸서 비켜 가려는 입력이다. 거절 쪽으로 닫는다 (007 리뷰 M1).
        return True
    return any("dooray:" in _folded(stage) for stage in stages)


def _unwrap_stages(text: str) -> tuple[list[str], bool]:
    """인코딩(`%`·HTML 엔티티)을 번갈아 최대 3번 푼 단계들. 둘째 값은 "더 풀어도 그대로인가"."""
    stages = [text]
    current = text
    for _ in range(_UNWRAP_ROUNDS):
        unwrapped = html.unescape(urllib.parse.unquote(current))
        if unwrapped == current:
            return stages, True
        stages.append(unwrapped)
        current = unwrapped
    return stages, html.unescape(urllib.parse.unquote(current)) == current


def _folded(text: str) -> str:
    kept = (
        char
        for char in unicodedata.normalize("NFKC", text)
        if not char.isspace()
        and char != "\\"  # 마크다운 이스케이프 `dooray\://` — 렌더러에서 `:`가 된다 (007 리뷰 M1)
        and char not in _HANGUL_FILLERS
        and unicodedata.category(char) not in _INVISIBLE_CATEGORIES
    )
    return "".join(kept).casefold()


def has_mention_marks(text: str) -> bool:
    """`link_mentions`가 살펴볼 `@` 후보가 하나라도 있는가. 참여자를 읽을지 정하는 데만 쓴다."""
    return next(_candidates(text), None) is not None


@dataclass(frozen=True)
class TenantScan:
    value: str | None  # 정확히 하나이고 모양이 맞을 때만 값
    seen: bool  # 글 어딘가에 `dooray://`가 있었다


def scan_tenant(texts: Iterable[str]) -> TenantScan:
    """글 안 `dooray://` 링크의 테넌트 칸과, 글에 `dooray://`가 있었는가 (014 spec §3).

    `seen`은 글자 `dooray://`가 들어 있는가다 — 정규식이 잡았는가가 아니다.
    `value`의 규칙은 `tenant_of`와 같다. 값이 `None`으로 정해지면 거기서 멈춘다. 입력은 한 번만 돈다.
    """
    found: str | None = None
    seen = False
    for text in texts:
        if "dooray://" in text:
            seen = True
        for matched in _TENANT_IN_LINK.finditer(text):
            value = matched.group(1)
            if _LINK_VALUE.fullmatch(value) is None:
                return TenantScan(None, True)
            if found is None:
                found = value
            elif value != found:
                return TenantScan(None, True)
    return TenantScan(found, seen)


def tenant_of(texts: Iterable[str]) -> str | None:
    """글 안 `dooray://` 링크의 테넌트 칸. **정확히 하나이고 모양이 맞을 때만** 값이다.

    본문에서 오는 값이라 조작될 수 있다. 모양이 이상한 값이 하나라도 있거나 값이 둘이면 믿지 않는다.
    값이 가짜여도 링크의 ID는 참여자의 것이다 (§5-1 (d)).
    """
    return scan_tenant(texts).value


def link_mentions(
    content: str, people: Sequence[Person], tenant: str | None, *, complete: bool = True
) -> Linked:
    """본문의 `@이름`을 참여자 이름표로 멘션 링크로 바꾼다 (§4-3).

    바꾸지 못한 것은 글자로 남기고 이유를 싣는다. 글자 `@이름`은 두레이에서 멘션이 아니다(007 시험 O).
    """
    usable_tenant = tenant if tenant and _LINK_VALUE.fullmatch(tenant) else None
    by_name: dict[str, dict[str, Person]] = {}
    for person in people:
        if person.name and _LINK_VALUE.fullmatch(person.id or ""):
            by_name.setdefault(person.name, {}).setdefault(person.id, person)
    # 긴 이름부터 — `@멤버15`가 `멤버1`에 걸리지 않게.
    names = sorted(by_name, key=len, reverse=True)

    parts: list[str] = []
    at = 0
    linked: dict[str, Person] = {}
    skipped: list[tuple[str, str]] = []
    capped = False
    for count, position in enumerate(_candidates(content)):
        if count >= MAX_MENTION_MARKS:
            capped = True
            break
        start = position + 1
        written = _written(content, start)
        if not written:
            continue  # `@@…`처럼 적힌 이름이 없다 — 부르는 것이 아니다 (리뷰 L6)
        if usable_tenant is None:
            skipped.append((written, REASON_NO_TENANT))
            continue
        if not complete:
            # 이름을 얻지 못한 참여자가 있으면 "이름이 하나뿐"을 믿을 수 없다 — 보이지 않는 동명이인에게
            # 링크가 걸린다(007 리뷰 M3). 알림은 되돌릴 수 없으니 바꾸지 않는다.
            skipped.append((written, REASON_INCOMPLETE))
            continue
        name = next((one for one in names if _named_at(content, start, one)), None)
        if name is None:
            attached = any(content.startswith(one, start) for one in names)
            skipped.append((written, REASON_ATTACHED if attached else REASON_OUTSIDE))
            continue
        owners = by_name[name]
        if len(owners) > 1:
            skipped.append((name, REASON_SAME_NAME))
            continue
        if not _linkable(name):
            skipped.append((name, REASON_UNSAFE_NAME))
            continue
        person = next(iter(owners.values()))
        if person.id not in linked and len(linked) >= MAX_MENTIONS:
            skipped.append((name, REASON_LIMIT))
            continue
        linked.setdefault(person.id, person)
        parts.append(content[at:position])
        parts.append(_dooray_link(f"@{name}", usable_tenant, "members", person.id, "member"))
        at = start + len(name)
    parts.append(content[at:])
    return Linked(
        text="".join(parts),
        linked=tuple(linked.values()),
        skipped=tuple(skipped),
        capped=capped,
    )


def _dooray_link(text: str, tenant: str, kind: str, target: str, title: str = "") -> str:
    """두레이 링크 한 개. 두레이로 나가는 링크 주소를 만드는 자리는 이 함수 하나다 (007 §8-3, 012 개정 8)."""
    tail = f' "{title}"' if title else ""
    return f"[{text}](dooray://{tenant}/{kind}/{target}{tail})"


def _candidates(content: str) -> Iterator[int]:
    """`@` 후보의 자리를 앞에서부터 낸다 (§4-3의 후보 규칙 1~3).

    1. 맨 앞이거나 바로 앞 글자가 공백이다 — 허용을 나열한다. 이메일·`/@`·`!@`·`]@`가 여기서 빠진다.
    2. 인용·코드·링크·꺾쇠 구간 밖이다.
    3. 업무 번호 모양(`@hong.gd/12`)이 아니다.
    4. `@` 바로 뒤에 글자가 있다 — 홀로 선 `@`("@ ", 끝의 "@")는 부르는 것이 아니다. 없으면 참여자를 읽는 GET이 헛돈다.
    """
    if "@" not in content:
        return
    covered = _merged(_hidden_spans(content))
    starts = [span[0] for span in covered]
    position = content.find("@")
    while position != -1:
        if (position == 0 or content[position - 1].isspace()) and not _covered(
            covered, starts, position
        ):
            after = content[position + 1 : position + 2]
            if after and not after.isspace() and _TASK_LIKE.match(content, position + 1) is None:
                yield position
        position = content.find("@", position + 1)


def _hidden_spans(content: str) -> list[tuple[int, int]]:
    spans = _masked_spans(content)
    for pattern in (_LINK_SPAN, _ANGLE_SPAN, _INDENTED_LINE):
        spans.extend(matched.span() for matched in pattern.finditer(content))
    return spans


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """겹치는 구간을 합친다. 자리 하나를 이분 탐색으로 판정하려고 — 구간이 수만 개여도 선형에 가깝다."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _covered(spans: list[tuple[int, int]], starts: list[int], position: int) -> bool:
    place = bisect.bisect_right(starts, position) - 1
    return place >= 0 and position < spans[place][1]


def _named_at(content: str, start: int, name: str) -> bool:
    """`start`에서 이 이름이 시작하고 **바로 뒤가 경계**인가. 한글은 `isalnum()`이 참이라 `님`은 경계가 아니다."""
    if not content.startswith(name, start):
        return False
    end = start + len(name)
    return end == len(content) or not content[end].isalnum()


def _linkable(name: str) -> bool:
    """링크 텍스트 자리에 넣어도 되는 이름인가. `]`·`(`·줄바꿈이 섞이면 링크가 깨지거나 다른 문법이 된다."""
    if not 1 <= len(name) <= _NAME_LIMIT or name != name.strip():
        return False
    return all(char.isalnum() or char in _NAME_EXTRA for char in name)


def _written(content: str, start: int) -> str:
    """`@` 뒤의 낱말 — 공백·문장부호 전까지, 30자까지. 보고용이다."""
    end = start
    limit = min(len(content), start + _WRITTEN_LIMIT)
    while end < limit and (content[end].isalnum() or content[end] in "._-"):
        end += 1
    return content[start:end]


# --- 고치는 글의 두레이 주소 (012 spec 개정 8) --------------------------------
#
# 모델이 쓴 `new`에는 업무·파일 링크의 정해진 모양만 둔다. **허용할 것을 나열한다** — 막을 것을 나열하지 않는다.
# 그 모양을 뺀 나머지에 두레이 주소가 하나라도 있으면(멤버·그룹·알 수 없는 종류·인코딩·맨 주소) 거절이다.
# 업무의 웹 주소 링크는 서버가 두레이 업무 링크로 바꾼다 — 찾기·바꾸기는 여기, 읽기는 도구가 한다.

# 한 번에 받는 업무·파일 링크 수 (R8-1).
MAX_EDIT_LINKS = 10
# 한 번에 바꾸는 업무 웹 주소 링크 수 (R8-2). 바꿀 업무마다 조회가 하나 나간다 (001 리뷰 H1).
MAX_TASK_LINKS = 5
# 링크 바로 뒤에 올 수 없는 것 (개정 9의 R9-3). 허용 링크와 웹 주소 링크가 같이 쓴다.
_AFTER_LINK = r"(?![\\%(]|/[^\s\[])"
# 업무 웹 주소를 품을 수 있는 마크다운 링크. 이미지(`![…](…)`)와 title이 붙은 링크는 대상이 아니다.
_WEB_LINK = re.compile(r"(?<!!)\[([^\[\]\n]{0,256})\]\(([^\s()<>\"]{1,512})\)" + _AFTER_LINK)
# 허용하는 모양. 테넌트·ID는 `client.path_id`와 같은 글자. 풀지 않은 글에, 대소문자 그대로 본다.
# 글자 칸에 `[` `]`가 없다 — 링크 안에 링크를 품지 못한다. 길이 상한은 `render.DOORAY_LINK`와 같은 이유다.
# 묶음: 1 이미지 표시, 2 글자, 3 주소, 4 title.
# 뒤보기(개정 9의 R9-3): 바로 뒤에 `\`·`%`·`(`가 오거나 `/` 뒤에 공백·`[`가 아닌 글자가 오면 모양이 아니다 —
# `…)/members/2)`·`…)(//t/members/2)`처럼 주소를 이어 읽게 하는 글을 받지 않는다. `…)/[b](…)`는 된다.
_EDIT_LINK = re.compile(
    r'(!?)\[([^\[\]\n]{0,256})\]\((dooray://[0-9A-Za-z_-]{1,64}/(?:tasks|files)/[0-9A-Za-z_-]{1,64})'
    r'(?: "([^"\n]{0,64})")?\)' + _AFTER_LINK
)


@dataclass(frozen=True)
class TaskLink:
    start: int  # 글에서 링크가 차지하는 구간
    end: int
    text: str  # 링크 글자 — 그대로 둔다
    post_id: str


def has_mention_address(text: str) -> bool:
    """고치는 글에 쓸 수 없는 두레이 주소가 있는가 (012 spec 개정 8의 R8-1, 개정 9의 R9-3).

    허용하는 모양(업무·파일 링크)이라도 글자가 `@`로 시작하면 거절이다 — 멘션처럼 보이는 링크를 만들지 않는다.
    그 모양의 **주소**를 뺀 나머지를 `_LINK_VIEWS`의 네 가지로 펴서 `has_dooray_address`를 돌린다.
    하나라도 걸리면 거절이다 — 오탐은 거절 쪽이다.
    """
    if any(mention_like_label(found.group(2)) for found in _EDIT_LINK.finditer(text)):
        return True
    return any(has_dooray_address(_EDIT_LINK.sub(view, text)) for view in _LINK_VIEWS)


# 허용한 링크를 펴는 방법. 어느 쪽에도 주소는 남기지 않는다. 링크를 걷거나 글자·title로 읽을 때
# 양쪽 조각이 이어져 주소가 되는 글(`doo` + 링크 + `ray:`)을 잡는다. 글자·title에 숨긴 주소도 여기서 걸린다.
_LINK_VIEWS: tuple[Callable[[re.Match[str]], str], ...] = (
    lambda found: "",  # 통째로 걷는다 — `doo` + `[a](… "x")` + `ray:`
    lambda found: found.group(2),  # 글자만 — `do` + `[o](…)` + `ray:`
    lambda found: found.group(4) or "",  # title만 — `doo` + `[x](… "ray:")`
    lambda found: found.group(2) + (found.group(4) or ""),  # 글자와 title — `[do](… "o")` + `ray:`
)


def mention_like_label(label: str) -> bool:
    """링크 글자가 멘션처럼 보이는가 (R9-3). 인코딩을 풀고, 공백·보이지 않는 글자를 빼고, 앞의 서식 기호를 떼면
    맨 앞이 `@`다 — `[**@홍길동**](…)`·`[&#64;홍길동](…)`도 화면에서는 `@홍길동`이다.
    개인 프로젝트의 업무 번호(`@hong.gd/12 제목`)는 멘션이 아니다 — 두레이가 업무 링크의 글자를 업무 번호와
    제목으로 바꿔 저장하므로(012 실측 넷째), 보이는 글에서 옮긴 링크의 글자가 `@`로 시작한다.
    HTML 태그(`<b>@홍길동</b>`)도 화면에서는 보이지 않는다 — 지우고 본다(017 리뷰 L1). 지운 뒤에도 `<`가 남으면
    (상한 너머, `<scr<b></b>ipt>`처럼 다시 조립되는 글) 멘션처럼 보이는 것으로 본다 — 오탐은 거절 쪽이다.
    """
    stages, _ = _unwrap_stages(label)
    for stage in stages:
        bare = _ANGLE_SPAN.sub("", stage)
        if "<" in bare:
            return True
        if not _folded(bare).lstrip(_LABEL_MARKS).startswith("@"):
            continue
        # 업무 번호인지는 공백을 지우기 **전의** 글로 본다 — `@홍길동 12/3`은 지우면 업무 번호처럼 붙는다.
        plain = unicodedata.normalize("NFKC", bare).lstrip(" \t" + _LABEL_MARKS)
        if _TASK_LABEL.match(plain) is None:
            return True
    return False


# 링크 글자 앞에서 떼어도 되는 마크다운 서식 기호. 떼어서 보는 것이라 나열해도 안전하다.
_LABEL_MARKS = "*_~`"
# 글자 맨 앞의 개인 프로젝트 업무 번호: `@코드/번호` 뒤가 끝이거나 공백이다(서식 기호는 사이에 와도 된다).
_TASK_LABEL = re.compile(r"@[^\s/]{1,100}/\d{1,9}[*_~`]{0,4}(?!\S)")


def strip_link_titles(text: str) -> str:
    """허용하는 모양의 링크에서 title을 뗀다 (R9-3). 두레이가 채운다(실측 다섯째).

    고치는 글(`new`)에만 쓴다 — 원문의 링크는 건드리지 않는다.
    """
    return _EDIT_LINK.sub(lambda found: f"{found.group(1)}[{found.group(2)}]({found.group(3)})", text)


def count_edit_links(text: str) -> int:
    """허용하는 모양의 두레이 링크 수. 도구가 `MAX_EDIT_LINKS`와 비교한다."""
    return sum(1 for _ in _EDIT_LINK.finditer(text))


def task_web_links(text: str, web_base: str | None, task_path: str) -> list[TaskLink]:
    """글의 마크다운 링크 가운데 설정된 웹 주소의 업무 주소를 가리키는 것 (R8-2).

    맨 주소·인용·코드 안은 보지 않는다. `MAX_TASK_LINKS`를 넘으면 하나 더 담고 멈춘다 — 넘친 것을 호출부가 안다.
    `task_path`는 `render.TASK_PATH`다 (이 모듈은 `render`를 모른다 — P8).
    """
    if not web_base or "](" not in text:
        return []
    covered = _merged(_masked_spans(text))
    starts = [span[0] for span in covered]
    found: list[TaskLink] = []
    for matched in _WEB_LINK.finditer(text):
        post_id = _web_task_id(matched.group(2), web_base, task_path)
        if post_id is None or _covered(covered, starts, matched.start()):
            continue
        found.append(TaskLink(matched.start(), matched.end(), matched.group(1), post_id))
        if len(found) > MAX_TASK_LINKS:
            break
    return found


def link_tasks(text: str, links: Sequence[TaskLink], tenant: str | None) -> str:
    """`task_web_links`가 찾은 링크를 두레이 업무 링크로 바꾼다. title은 싣지 않는다 — 두레이가 채운다(실측 다섯째).

    테넌트가 모양에 맞지 않으면 바꾸지 않는다. `links`는 같은 `text`에서 찾은 것이어야 한다.
    """
    if not tenant or _LINK_VALUE.fullmatch(tenant) is None:
        return text
    parts: list[str] = []
    at = 0
    for link in links:
        if link.start < at or _LINK_VALUE.fullmatch(link.post_id) is None:
            continue
        parts.append(text[at : link.start])
        parts.append(_dooray_link(link.text, tenant, "tasks", link.post_id))
        at = link.end
    parts.append(text[at:])
    return "".join(parts)


def _web_task_id(address: str, web_base: str, task_path: str) -> str | None:
    """주소가 설정된 웹 주소의 업무 주소이면 업무 ID. 글자 앞부분이 아니라 **주소를 풀어** 본다.

    스킴·호스트가 같고, 사용자 정보·포트·쿼리·조각이 없고, 경로가 업무 경로 + ID 하나일 때만.
    호스트의 대소문자·끝의 점도 다르게 본다 — 바꾸지 않으면 웹 주소 그대로 나갈 뿐이다.
    """
    parts = _split_url(address)
    base = _split_url(web_base)
    if parts is None or base is None or not parts.path.startswith(task_path):
        return None
    post_id = parts.path[len(task_path) :]
    same = (
        parts.scheme == base.scheme
        and parts.netloc == base.netloc
        and parts.hostname == base.hostname
        and parts.username is None
        and parts.password is None
        and parts.query == ""
        and parts.fragment == ""
        and _LINK_VALUE.fullmatch(post_id) is not None
        and address == web_base + task_path + post_id
    )
    return post_id if same else None


def _split_url(address: str) -> urllib.parse.SplitResult | None:
    parts: urllib.parse.SplitResult | None = None
    try:
        parts = urllib.parse.urlsplit(address)
    except ValueError:
        parts = None
    return parts


# --- 고치기: 찾아 바꾸기 (012 spec §4-2, 개정 7) -----------------------------
#
# 고치는 도구는 글 전문을 다시 쓰지 않는다. 지금 글에 정확히 한 번 나오는 `old`를 `new`로 바꾼다.
# `get_thread`가 보여 주는 글은 정리된 글이라 저장된 글과 다르다 — 전문을 다시 보내면 링크·파일·서식을 잃는다.
# 멘션은 검사하지 않는다(ADR 014). 지키는 것은 형식 하나다 — HTML 본문에 날 글자를 끼우지 않는다.

# 이유 코드 여섯 (012 spec 개정 7의 R7-1). 도구가 이 코드를 문장으로 바꾼다.
REPLACE_SAME = "same"
REPLACE_OLD_EMPTY = "old_empty"
REPLACE_NOT_FOUND = "not_found"
REPLACE_AMBIGUOUS = "ambiguous"
REPLACE_HTML_TEXT = "html_text"
REPLACE_EMPTY = "empty"


@dataclass(frozen=True)
class Replaced:
    text: str | None  # 바꾼 글. 못 바꿨으면 None
    reason: str | None  # 못 바꾼 이유 코드. 바꿨으면 None


def replace_once(content: str, old: str, new: str, *, mime_type: str | None) -> Replaced:
    """`content`에 정확히 한 번 나오는 `old`를 `new`로 바꾼다 (012 spec 개정 7의 R7-1). 앞에서 걸리면 멈춘다.

    1. `old == new`면 `same`. `old`가 비었으면 글이 비어 있을 때만 새로 쓰고, 아니면 `old_empty`.
    2. 없으면 `not_found`, 두 번 이상(겹치는 것까지)이면 `ambiguous`.
    3. HTML 본문인데 `new`에 `<` `>` `&` 줄바꿈이 있으면 `html_text` — 이스케이프해서 넣지 않는다.
    4. 바꾼 글이 공백뿐이면 `empty`.
    """
    html_body = (mime_type or "").strip().lower().startswith(HTML_MIME)
    if old == new:
        return Replaced(None, REPLACE_SAME)
    if old == "":
        if content.strip() != "":
            return Replaced(None, REPLACE_OLD_EMPTY)
        result = new  # 빈 글에 새로 쓴다 (012 spec §2-4)
    else:
        at = content.find(old)
        if at == -1:
            return Replaced(None, REPLACE_NOT_FOUND)
        if content.find(old, at + 1) != -1:
            return Replaced(None, REPLACE_AMBIGUOUS)
        result = content[:at] + new + content[at + len(old) :]
    if html_body and ("\n" in new or "\r" in new or any(char in new for char in "<>&")):
        return Replaced(None, REPLACE_HTML_TEXT)
    if not result.strip():
        return Replaced(None, REPLACE_EMPTY)
    return Replaced(result, None)
