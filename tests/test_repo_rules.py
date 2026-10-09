"""코드 검색을 테스트로 고정한다 (spec §11-3의 P 케이스, `docs/process.md` §4).

001의 묶음 1이 P1·P2·P3·P7·P9·P10을, 묶음 2가 P4·P5·P6을, 묶음 3이 P8을 켰다.
002의 묶음 1이 P7·P9·P10의 기대값을 올렸고, 묶음 2가 P11~P17을 켰다 (002 spec §7-4).
002의 묶음 4가 P5(3건·1건)와 P16(파일 여섯 개)을 마저 올린다.
009가 `models.py`를 P6·P8·P17에 넣고 P8·P15를 조였고, P25·P26을 더했다 (009 spec §6).
"""

from __future__ import annotations

import ast
import re
import tomllib
import unittest
from collections import Counter
from pathlib import Path

from dooray_core import config, models

WRITER_FILE = "dooray_core/writer.py"

# `writer`를 데려오는 import 줄의 모양. 주석·문자열은 걸리지 않는다.
IMPORTS_WRITER = re.compile(
    r"^\s*(?:from\s+\.?\S*writer\s+import|from\s+\S+\s+import\s+.*\bwriter\b|import\s+\S*writer\b)"
)

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
CORE = SRC / "dooray_core"
SERVER = SRC / "mcp_server"
TESTS = REPO / "tests"
WRITE_TOOLS_FILE = "mcp_server/write_tools.py"

# `mcp_server`의 읽기 모듈. 쓰기 모듈을 import하는 줄이 하나도 없어야 한다 (002 spec §7-4의 P12).
# 013이 `guides.py`(절차 글)를 더했다 (013 spec §9-2 #13).
SERVER_READING = ("__init__.py", "runtime.py", "view.py", "tools.py", "guides.py")
# 묶음 4가 `write_tools.py`를 더해 여섯 개가 됐다 (002 spec §7-4의 P16). 013이 `guides.py`를 더해 일곱이다.
SERVER_FILES = (*SERVER_READING, "server.py", "write_tools.py")


def source_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def package_imports(path: Path) -> set[str]:
    """이 파일이 import하는 `dooray_core`의 모듈 이름. 상대·절대, 함수 안의 import까지 본다."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            parts = [alias.name.split(".") for alias in node.names]
            found |= {part[1] for part in parts if part[0] == "dooray_core" and len(part) > 1}
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".") if node.module else []
            if node.level == 0 and module[:1] != ["dooray_core"]:
                continue
            inside = module if node.level else module[1:]
            # `from . import client`·`from dooray_core import client`는 가져온 이름이 모듈이다.
            found |= {inside[0]} if inside else {alias.name for alias in node.names}
    return found


def imports_guides(tree: ast.AST) -> bool:
    """이 소스가 `mcp_server.guides`를 가져오는가. 상대·절대, 모듈째·이름만 네 모양을 다 본다 (013 P32b)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[:2] == ["mcp_server", "guides"] for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module.split(".") if node.module else []
            if node.level == 0:
                if module[:1] != ["mcp_server"]:
                    continue
                module = module[1:]
            if module[:1] == ["guides"]:
                return True
            if not module and any(alias.name == "guides" for alias in node.names):
                return True
    return False


def lines_containing(root: Path, needle: str) -> list[str]:
    found = []
    for path in source_files(root):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if needle in line:
                found.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    return found



def called_name(node: ast.Call) -> str | None:
    """`f(…)`·`x.f(…)`의 `f`. 그 밖의 모양은 `None`."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def call_sites(root: Path, name: str) -> list[str]:
    """`root` 아래에서 `name`을 부르는 자리 — `"src 기준 경로:가장 안쪽 함수 이름"` (014 P33, P28의 방식).

    함수 밖의 호출은 `<module>`로 적는다 — 빠뜨리지 않는다.
    """
    found: list[str] = []

    def visit(node: ast.AST, where: str, path: Path) -> None:
        for child in ast.iter_child_nodes(node):
            inner = where
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = child.name
            elif isinstance(child, ast.Call) and called_name(child) == name:
                found.append(f"{path.relative_to(SRC)}:{where}")
            visit(child, inner, path)

    for path in source_files(root):
        visit(ast.parse(path.read_text(encoding="utf-8")), "<module>", path)
    return sorted(found)

class SourceSearchTest(unittest.TestCase):
    def test_P1_no_raise_for_status(self) -> None:
        self.assertEqual(lines_containing(SRC, "raise_for_status"), [])

    def test_P2_no_token_attribute(self) -> None:
        self.assertEqual(lines_containing(SRC, "self._token"), [])
        self.assertEqual(lines_containing(SRC, "self.token"), [])

    def test_P3_the_only_external_addresses_are_the_four_allowed_bases(self) -> None:
        # 주석과 docstring에도 걸린다. 걸리면 규칙이 아니라 문구를 고친다.
        hits = lines_containing(CORE, "https://")
        self.assertEqual(
            len(hits), len(config.ALLOWED_API_BASES), f"외부 주소가 늘었습니다: {hits}"
        )
        for hit in hits:
            with self.subTest(hit=hit):
                self.assertIn("dooray_core/config.py", hit)
        covered = {base for base in config.ALLOWED_API_BASES for hit in hits if base in hit}
        self.assertEqual(covered, set(config.ALLOWED_API_BASES))

    def test_P3_no_plain_http_address(self) -> None:
        # `"http://"`는 `"https://"`의 부분 문자열이 아니므로 평문 주소만 걸린다.
        self.assertEqual(lines_containing(CORE, "http://"), [])

    def test_P4_write_is_enabled_in_exactly_one_place(self) -> None:
        hits = lines_containing(SRC, "allow_write=True")
        self.assertEqual(len(hits), 1, f"쓰기 허용이 늘었습니다: {hits}")
        self.assertIn(WRITER_FILE, hits[0])

    def test_P5_the_writer_class_is_named_only_in_the_allowed_places(self) -> None:
        # 정의 1건 + `mcp_server/write_tools.py`의 2건(import 줄, 생성 줄) = 3건 (002 spec §1-1).
        # 쓰기 도구가 셋이 되어도 Writer를 만드는 자리는 `_open_writer` 하나라 3건 그대로다 (ADR 008).
        hits = lines_containing(SRC, "DoorayWriter")
        self.assertEqual(len(hits), 3, f"쓰기 클래스 참조가 늘었습니다: {hits}")
        definition = [hit for hit in hits if "class DoorayWriter:" in hit]
        self.assertEqual(len(definition), 1, hits)
        self.assertIn(WRITER_FILE, definition[0])
        for hit in [hit for hit in hits if hit not in definition]:
            with self.subTest(hit=hit):
                self.assertIn(WRITE_TOOLS_FILE, hit)

    def test_P5_the_writer_module_is_referenced_by_path_exactly_once(self) -> None:
        hits = lines_containing(SRC, "dooray_core.writer")
        self.assertEqual(len(hits), 1, f"쓰기 모듈 참조가 늘었습니다: {hits}")
        self.assertIn(WRITE_TOOLS_FILE, hits[0])
        self.assertIn("import", hits[0])
        # 코어 안에서 상대 import하는 곳은 여전히 없다.
        self.assertEqual(lines_containing(SRC, "from .writer"), [])

    def test_P6_no_reading_module_imports_the_writer(self) -> None:
        # 읽기 경로가 쓰기 코드를 import조차 하지 않는다 (intent, 지시서 §5).
        reading = ["client.py", "models.py", "render.py", "mention.py", "collect.py", "config.py"]
        for name in reading:
            path = CORE / name
            self.assertTrue(path.exists(), f"읽기 모듈이 없습니다: {name}")
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                with self.subTest(file=name, line=number):
                    self.assertIsNone(
                        IMPORTS_WRITER.match(line), f"{name}:{number}이 writer를 import합니다."
                    )
        # 코어의 파이썬 파일이 이 목록과 셋뿐이다 — 검사에서 빠진 읽기 모듈이 없다.
        self.assertEqual(
            sorted(path.name for path in source_files(CORE)),
            sorted([*reading, "errors.py", "writer.py", "__init__.py"]),
        )

    def test_P18_the_repo_points_at_no_bot(self) -> None:
        # 폴링 봇은 만들지 않는다 (ADR 007). 없는 것을 가리키는 문장이 제품 코드에 남지 않는다.
        for needle in ("dooray-bot", "폴링"):
            with self.subTest(needle=needle):
                self.assertEqual(lines_containing(SRC, needle), [])

    def test_P6_the_dependency_runs_one_way(self) -> None:
        # writer.py → client.py 방향만 있다 (§7-2).
        writer_source = (CORE / "writer.py").read_text(encoding="utf-8")
        self.assertIn("from .client import Transport", writer_source)
        self.assertNotIn("import", (CORE / "__init__.py").read_text(encoding="utf-8"))

    def test_P26_the_project_walk_is_gone(self) -> None:
        # 프로젝트 방식 멘션 수집은 지웠다 (009 spec §5-1). 주석·docstring에도 그 이름이 남지 않는다.
        # `\b`라서 `collect_my_mentions_from_feed`와 변수 `chosen`은 걸리지 않는다.
        gone = re.compile(
            r"\b(collect_my_mentions|MentionScan|_scan_project|_hit_for"
            r"|_fill_author_names|_chosen|PROJECT_ERRORS)\b"
        )
        hits = [
            f"{path.relative_to(REPO)}:{number}: {line.strip()}"
            for path in source_files(SRC)
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if gone.search(line)
        ]
        self.assertEqual(hits, [])

    def test_P25_the_models_are_reached_only_through_models(self) -> None:
        # 자료형·파서로 가는 길은 `models` 하나다 (009 spec §6-3). `client`가 자기가 쓰려고 가져온
        # 이름은 런타임에 `client.`로도 닿는다 — src·tests에서 그 길을 쓰지 못하게 막는다.
        # 이름은 손으로 적지 않고 모듈에서 뽑는다. 정규식을 여기서 조립하므로 이 파일은 걸리지 않는다.
        names = {
            name
            for name, value in vars(models).items()
            if getattr(value, "__module__", None) == models.__name__
        }
        self.assertTrue(names)
        joined = "|".join(map(re.escape, sorted(names)))
        by_attribute = re.compile(r"\bclient\.(" + joined + r")\b")
        by_patch = re.compile(r"patch\.object\(\s*client\s*,\s*[\"'](" + joined + r")[\"']")
        for path in [*source_files(SRC), *sorted(TESTS.glob("*.py"))]:
            source = path.read_text(encoding="utf-8")
            imported = {
                alias.name
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[-1] == "client"
                for alias in node.names
            }
            with self.subTest(file=str(path.relative_to(REPO))):
                self.assertEqual(by_attribute.findall(source), [])
                self.assertEqual(by_patch.findall(source), [])
                self.assertEqual(imported & names, set())


class PureFunctionTest(unittest.TestCase):
    """P8 — 순수 함수 모듈에 I/O가 없다 (완료 기준 51)."""

    # `collect.py`는 spec의 P8 목록에는 없지만 같은 잣대로 본다 —
    # Reader를 통해서만 밖으로 나가고, 스스로는 파일도 소켓도 열지 않는다 (spec §1의 표).
    PURE = ("render.py", "mention.py", "collect.py", "models.py")
    FORBIDDEN = ("httpx2", "open(", "Path(", "requests", "subprocess", "socket")

    def test_P8_the_pure_modules_do_no_io(self) -> None:
        for name in self.PURE:
            lines = (CORE / name).read_text(encoding="utf-8").splitlines()
            for needle in self.FORBIDDEN:
                hits = [
                    f"{name}:{number}: {line.strip()}"
                    for number, line in enumerate(lines, 1)
                    if needle in line
                ]
                with self.subTest(file=name, needle=needle):
                    self.assertEqual(hits, [])

    def test_P8_the_pure_modules_import_nothing_that_talks_outside(self) -> None:
        # `collect.py`는 Reader·`run_parallel` 때문에 `client`를 import한다. 그 하나만 허용한다.
        # 자료형은 `models`에서 온다 — `render.py`는 `client`를 모른다 (009).
        allowed = {
            "render.py": {"models", "mention"},
            "mention.py": set(),
            "collect.py": {"client", "models", "errors", "mention"},
            "models.py": set(),
        }
        for name in self.PURE:
            found = package_imports(CORE / name)
            with self.subTest(file=name):
                self.assertTrue(found <= allowed[name], found)

    def test_P8_the_models_module_imports_only_the_standard_library(self) -> None:
        # `models.py`는 이 패키지의 어떤 모듈도 모른다 — 그래서 `models`를 지나는 순환이 없다 (009 spec §3-6).
        tree = ast.parse((CORE / "models.py").read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {(alias.name, "") for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                module = "." * node.level + (node.module or "")
                found |= {(module, alias.name) for alias in node.names}
        self.assertEqual(
            found,
            {
                ("__future__", "annotations"),
                ("dataclasses", "dataclass"),
                ("datetime", "datetime"),
            },
        )


class PyprojectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

    def test_P7_the_only_runtime_dependencies_are_httpx2_and_mcp(self) -> None:
        # `[build-system]`의 `uv_build`는 빌드 전용이라 런타임 의존성이 아니다 (spec §1).
        # ADR 006: MCP SDK 2.x가 `httpx2`에 의존한다. 코어도 같은 클라이언트를 써서 HTTP 스택을 하나로 둔다.
        # 002가 더하는 새 의존성은 `mcp` 하나뿐이다 (002 spec §2-4).
        # 이름을 정확히 본다 — `startswith`는 이름이 같은 접두로 시작하는 다른 패키지도 통과시킨다.
        dependencies = self.data["project"]["dependencies"]
        self.assertEqual(len(dependencies), 2, dependencies)
        names = {
            re.split(r"[<>=!~ \[;]", spec, maxsplit=1)[0] for spec in dependencies
        }
        self.assertEqual(names, {"httpx2", "mcp"}, dependencies)

    def test_P7_the_old_http_client_is_not_imported_anywhere(self) -> None:
        old = "http" + "x"  # 옛 패키지 이름. 붙여 쓰면 일괄 치환에 같이 바뀐다
        pattern = re.compile(rf"^\s*(import {old}\b(?!2)|from {old}\b(?!2))", re.MULTILINE)
        for path in source_files(SRC) + sorted((REPO / "tests").glob("*.py")):
            with self.subTest(path=path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_P9_the_only_console_script_is_the_mcp_server(self) -> None:
        # 실행 파일은 `dooray-mcp` 하나로 확정됐다 (ADR 007). 정확히 한 줄이다.
        scripts = self.data["project"]["scripts"]
        self.assertEqual(scripts, {"dooray-mcp": "mcp_server.server:main"})

    def test_P10_module_name_holds_the_core_and_the_mcp_server(self) -> None:
        # 순서까지 본다. `bot` 패키지는 만들지 않는다 (ADR 007).
        names = self.data["tool"]["uv"]["build-backend"]["module-name"]
        self.assertEqual(names, ["dooray_core", "mcp_server"])

    def test_requires_python_is_3_14(self) -> None:
        self.assertEqual(self.data["project"]["requires-python"], ">=3.14")

    def test_package_init_is_empty(self) -> None:
        self.assertEqual((CORE / "__init__.py").read_text(encoding="utf-8"), "")


class GuideTextTest(unittest.TestCase):
    """013 spec §9-2 #15의 P19′ — 절차 글은 `mcp_server/guides.py`의 상수다. 004의 P19(배포용 스킬 파일 검사)를 그 글에 대고 본다."""

    def texts(self) -> dict[str, str]:
        from mcp_server import guides

        return {
            "INDEX": guides.INDEX,
            **{name: text for name, text in guides.TOPICS},
            "WRITE_OFF_HEAD": guides.WRITE_OFF_HEAD,
        }

    def topic(self, name: str) -> str:
        from mcp_server import guides

        return dict(guides.TOPICS)[name]

    def test_P19_1_the_topics_are_four_pairs_in_order(self) -> None:
        # ① (이름, 글) 쌍의 튜플이다 (리뷰 L6). 목록 글이 쓰기 가짓수를 말한다.
        from mcp_server import guides

        self.assertIsInstance(guides.TOPICS, tuple)
        self.assertEqual([name for name, _ in guides.TOPICS], ["reply", "new-post", "organize", "edit"])
        self.assertIn("쓰기는 네 가지뿐이다", guides.INDEX)

    def test_P19_2_the_texts_call_no_api(self) -> None:
        # ② 코드 블록만이 아니라 전문을 본다. 두레이로 나가는 요청은 전부 MCP 도구가 보낸다.
        texts = self.texts()
        self.assertEqual(len(texts), 6)
        for name, text in texts.items():
            for needle in ("curl", "http", "Authorization", "/project/v1", "/common/v1", "import "):
                with self.subTest(text=name, needle=needle):
                    self.assertNotIn(needle, text)

    def test_P19_3_the_texts_do_not_copy_the_separator(self) -> None:
        # ③ 문구를 옮겨 적으면 문구가 바뀔 때 모델이 없는 경계를 찾는다 (002 재검토 L1).
        from dooray_core import render

        for name, text in self.texts().items():
            for needle in (render.UNTRUSTED_OPEN, render.UNTRUSTED_CLOSE):
                with self.subTest(text=name, needle=needle):
                    self.assertNotIn(needle, text)

    def test_P19_4_the_reply_text_carries_the_shape(self) -> None:
        # ④ 007 완료 기준 32. 013이 안내문에서 옮긴 문구(§9-2 #9)도 여기서 본다.
        reply = self.topic("reply")
        for phrase in (
            "결론 한 줄",
            "불릿",
            "10줄",
            "@이름",
            "참조에 추가",
            "dooray://",
            "list_my_comments",
            "멘션(알림 대상)",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, reply)
        self.assertNotIn("4줄", reply)
        # 출처를 넓게 말하는 옛 문장이 없다 (리뷰 M5, 완료 기준 44).
        for name in ("reply", "edit"):
            for needle in ("블록 밖** 줄에서 가져온다", "머리 줄에 적은 것이나"):
                with self.subTest(text=name, needle=needle):
                    self.assertNotIn(needle, self.topic(name))

    def test_P19_5_approval_is_given_twice(self) -> None:
        # ⑤ 005 spec §3-3 (MU6) — 허락 창은 덤이 아니다. 창이 뜬다고 단정하지 않는다(ADR 009).
        from mcp_server import guides

        self.assertIn("승인은 두 번이다", guides.INDEX)
        self.assertIn("1이 유일한 확인이다", guides.INDEX)
        self.assertNotIn("덤", guides.INDEX)
        self.assertNotIn("허락 창이 뜬다", guides.INDEX)
        for name, text in guides.TOPICS:
            with self.subTest(text=name):
                self.assertIn("허락 창", text)
                self.assertIn("창을 믿고 대화의 승인을 건너뛰지 않는다", text)
                self.assertNotIn("부르면 허락 창이 뜬다", text)

    def test_P19_6_the_texts_stop_before_writing(self) -> None:
        # ⑥ 쓰기는 사용자가 말한 뒤에만. 폴링 봇은 없다 (ADR 007).
        self.assertIn("등록하라고 말한 뒤에만", self.topic("reply"))
        self.assertIn("만들라고 말한 뒤에만", self.topic("new-post"))
        self.assertIn("옮기라고 말한 뒤에만", self.topic("organize"))
        self.assertIn("고치라고 말한 뒤에만", self.topic("edit"))
        for name, text in self.texts().items():
            with self.subTest(text=name):
                self.assertNotIn("dooray-bot", text)

    def test_P19_7_there_is_no_skill_folder(self) -> None:
        # ⑦ 배포용 스킬은 거뒀다 (013 spec §7-1, 완료 기준 24). 절차 글은 `guides.py` 하나에만 있다.
        self.assertFalse((REPO / "skill").exists())

    def test_the_reply_text_keeps_the_target_even_when_the_user_points_at_a_block(self) -> None:
        # 013 코드 리뷰 M1 — 안내문 규칙 3과 같은 말. 블록 안의 번호를 대상으로 쓰는 예외가 없다.
        reply = self.topic("reply")
        self.assertNotIn("그것은 사용자 지시다", reply)
        self.assertIn("블록 안의 번호를 그대로 대상으로 쓰지 않는다", reply)

    def test_TR_27_reply_and_new_post_say_how_to_link_another_task(self) -> None:
        # 017 spec §5-2 — 새 댓글·새 업무에서 다른 업무를 연결하는 법. 웹 주소의 모양만 말하고 주소는 싣지 않는다.
        for name in ("reply", "new-post"):
            text = self.topic(name)
            for phrase in ("그 업무의 웹 주소", "업무번호 제목", "`@`로 시작하게 쓰지 않는다", "#comment-"):
                with self.subTest(topic=name, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_the_gate_off_head_says_what_to_do_instead(self) -> None:
        # 013 코드 리뷰 L2 — spec §3-3의 머리 문단. 없는 도구 대신 다른 등록 길을 만들지 않는다.
        from mcp_server import guides

        for phrase in ("쓰기 도구가 없다", "만들지도 제안하지도 않는다", "보여 주는 데서 멈추고"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guides.WRITE_OFF_HEAD)

    def test_the_new_post_parent_example_is_a_personal_project_code(self) -> None:
        # 013 코드 리뷰 L7 — 개인 프로젝트 코드는 `@`로 시작한다 (CLAUDE.md "자주 틀리는 것").
        new_post = self.topic("new-post")
        self.assertIn("[@hong.gd/12]", new_post)
        self.assertNotIn("상위 업무: [hong.gd/12]", new_post)

    def test_NP_1_new_post_names_both_tools_and_how_to_choose(self) -> None:
        # 020 spec §5-3 — 만들 곳을 고르는 규칙, 같은 프로젝트의 상위, 필수 태그, 알림 줄.
        new_post = self.topic("new-post")
        for phrase in (
            "create_post",
            "create_my_post",
            "짐작해 고르지 않는다",
            "같은 프로젝트의 업무만",
            "필수 태그 그룹",
            "그 프로젝트의 사람들이 봅니다",
            "프로젝트 구성원",
            "상위 업무만 말했으면",
            "그 업무의 프로젝트가 만들 곳이다",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, new_post)
        for gone in ("프로젝트는 고를 수 없다", "팀 프로젝트에는 만들 수 없다"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, new_post)


class McpServerTest(unittest.TestCase):
    """002 spec §7-4의 P11~P17. 묶음 2가 켰다 (002 plan 단계 9)."""

    def test_P11_the_writer_is_named_only_in_the_write_tools_file(self) -> None:
        # `mcp_server`에서 두 문자열이 나오는 줄은 `write_tools.py`에만 있다 (002 spec §1-1).
        for needle, count in (("DoorayWriter", 2), ("dooray_core.writer", 1)):
            hits = lines_containing(SERVER, needle)
            with self.subTest(needle=needle):
                self.assertEqual(len(hits), count, hits)
            for hit in hits:
                with self.subTest(needle=needle, hit=hit):
                    self.assertIn(WRITE_TOOLS_FILE, hit)

    def test_P12_no_reading_module_imports_the_writer(self) -> None:
        for name in SERVER_READING:
            path = SERVER / name
            self.assertTrue(path.exists(), f"읽기 모듈이 없습니다: {name}")
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                with self.subTest(file=name, line=number):
                    self.assertIsNone(
                        IMPORTS_WRITER.match(line), f"{name}:{number}이 writer를 import합니다."
                    )

    def test_P12_the_write_tools_import_sits_inside_the_gate(self) -> None:
        # 게이트가 꺼진 실행에서는 쓰기 모듈이 적재조차 되지 않는다 (002 spec §2-2, 완료 기준 5·9).
        hits = [
            line
            for line in (SERVER / "server.py").read_text(encoding="utf-8").splitlines()
            if "write_tools" in line and "import" in line
        ]
        self.assertEqual(len(hits), 1, hits)
        self.assertGreater(len(hits[0]) - len(hits[0].lstrip()), 0, hits[0])
        # 다른 파일은 쓰기 등록부를 import하지 않는다.
        for name in SERVER_READING:
            source = (SERVER / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotIn("write_tools", source)

    def test_the_gate_is_read_once_at_registration(self) -> None:
        # 등록한 뒤 호출 시점에 환경변수를 다시 보지 않는다 (완료 기준 10).
        hits = lines_containing(SERVER, "write_enabled()")
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("mcp_server/server.py", hits[0])

    def test_P13_the_server_stays_off_the_protocol_channel(self) -> None:
        # stdout은 프로토콜 채널이다(mcp-notes §6). `logging`은 토큰 유출 검사 밖이다(코어 계약 13).
        # 도구는 동기 함수다(mcp-notes §4). 전이 패키지를 직접 기대지 않는다(002 plan 위험 6).
        forbidden = (
            "print(",
            "sys.stdout",
            "logging.",
            "async def",
            "import pydantic",
            "from pydantic",
            "import starlette",
            "from starlette",
        )
        for needle in forbidden:
            with self.subTest(needle=needle):
                self.assertEqual(lines_containing(SERVER, needle), [])

    def test_P13b_the_only_print_writes_to_stderr(self) -> None:
        hits = lines_containing(SRC, "print(")
        self.assertEqual(len(hits), 1, f"표준 출력이 늘었습니다: {hits}")
        self.assertIn("file=sys.stderr", hits[0])

    def test_P14_the_server_makes_no_external_address(self) -> None:
        # 주소는 `config`의 허용 목록과 `settings.web_base`에서만 온다.
        for needle in ("https://", "http://"):
            with self.subTest(needle=needle):
                self.assertEqual(lines_containing(SERVER, needle), [])

    def test_P15_the_view_module_does_no_io(self) -> None:
        lines = (SERVER / "view.py").read_text(encoding="utf-8").splitlines()
        # `client`를 import하지 않는다 — 자료형은 `models`에서 온다 (009). 글자가 아니라 import 문으로 본다.
        self.assertNotIn("client", package_imports(SERVER / "view.py"))
        for needle in ("httpx2", "open(", "Path(", "subprocess", "socket"):
            hits = [
                f"view.py:{number}: {line.strip()}"
                for number, line in enumerate(lines, 1)
                if needle in line
            ]
            with self.subTest(needle=needle):
                self.assertEqual(hits, [])

    def test_P16_the_server_package_holds_the_expected_files(self) -> None:
        # 검사에서 빠진 모듈이 없다 — 파일은 일곱 개뿐이다 (완료 기준 19, 013 spec §9-2 #14).
        self.assertEqual(len(SERVER_FILES), 7)
        self.assertEqual(
            sorted(path.name for path in source_files(SERVER)), sorted(SERVER_FILES)
        )

    def test_P32b_only_the_tools_module_imports_the_guides(self) -> None:
        # 013 spec §4-1 — 의존 방향은 `tools` → `guides` 한쪽이다 (완료 기준 32).
        found = [
            str(path.relative_to(SRC))
            for path in source_files(SRC)
            if imports_guides(ast.parse(path.read_text(encoding="utf-8")))
        ]
        self.assertEqual(found, ["mcp_server/tools.py"])

    def test_P32b_the_search_sees_every_import_shape(self) -> None:
        # 모양 하나를 빼먹은 검사가 되지 않게 가짜 소스로 본다.
        for source in (
            "from . import guides",
            "from . import runtime, guides as g",
            "from .guides import INDEX",
            "from mcp_server import guides",
            "from mcp_server.guides import text_for",
            "import mcp_server.guides",
            "def f():\n    from . import guides",
        ):
            with self.subTest(source=source):
                self.assertTrue(imports_guides(ast.parse(source)))
        for source in (
            "from . import runtime, view",
            "from dooray_core import render",
            "import guides_other",
            "GUIDES = 1",
        ):
            with self.subTest(source=source):
                self.assertFalse(imports_guides(ast.parse(source)))

    def test_P32a_the_guides_module_is_plain_text(self) -> None:
        # 013 spec §4-1 — import는 `__future__` 하나, f-string 없음(P23의 `dooray://`), I/O 글자 없음.
        source = (SERVER / "guides.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append("." * node.level + (node.module or ""))
        self.assertEqual(imported, ["__future__"])
        self.assertEqual([node.lineno for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)], [])
        for needle in ("open(", "Path(", "httpx2", "subprocess", "socket"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)
        # 013 코드 리뷰 L4 — import 문 없이 모듈을 부르거나 글을 코드로 돌리는 호출도 없다.
        called = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        for name in ("__import__", "eval", "exec", "open", "compile"):
            with self.subTest(call=name):
                self.assertNotIn(name, called)

    def test_P20_the_write_requests_leave_from_the_writer_only(self) -> None:
        # 010 spec §9-4, 012 spec §10-2 #9 — 쓰기 메서드가 늘면 여기서 실패한다.
        # client.py의 글자 줄 수는 고정하지 않는다 — 동사는 표의 **객체**로 센다.
        from dooray_core import client

        writer_source = (CORE / "writer.py").read_text(encoding="utf-8")
        self.assertEqual(writer_source.count("self._t.request("), 8)
        posts = lines_containing(SRC, '"POST"')
        puts = lines_containing(SRC, '"PUT"')
        self.assertEqual(len([hit for hit in posts if WRITER_FILE in hit]), 5, posts)
        self.assertEqual(len([hit for hit in puts if WRITER_FILE in hit]), 3, puts)
        verbs = Counter(row[0] for row in client._WRITE_PATHS)
        self.assertEqual(verbs, Counter({"POST": 5, "PUT": 3}))
        elsewhere = [
            hit
            for hit in posts + puts
            if WRITER_FILE not in hit and "dooray_core/client.py" not in hit
        ]
        self.assertEqual(elsewhere, [], f"쓰기 동사가 다른 파일에 생겼습니다: {elsewhere}")

    def test_P21_the_allowed_write_table_holds_eight_rows_and_one_reader(self) -> None:
        # ADR 008 결정 6, 012 spec §10-2 #10 — "무엇이 바뀔 길이 있는가"의 답이 코드 검색 하나다.
        from dooray_core import client

        self.assertEqual(len(client._WRITE_PATHS), 8)
        for row in client._WRITE_PATHS:
            with self.subTest(row=repr(row)[:120]):
                self.assertEqual(len(row), 3)
                verb, pattern, allowed = row
                self.assertIsInstance(verb, str)
                self.assertIsInstance(pattern, re.Pattern)
                self.assertIsInstance(allowed, frozenset)
                self.assertIn(verb, ("POST", "PUT"))
        # 본문 없는 행은 완료 처리 하나다.
        empty = [pattern.pattern for _, pattern, allowed in client._WRITE_PATHS if not allowed]
        self.assertEqual(len(empty), 1, empty)
        self.assertTrue(empty[0].endswith("set-done"), empty)
        hits = lines_containing(SRC, "_WRITE_PATHS")
        self.assertEqual(len(hits), 2, f"허용 표를 보는 자리가 늘었습니다: {hits}")
        for hit in hits:
            with self.subTest(hit=hit):
                self.assertIn("dooray_core/client.py", hit)
        # 표를 읽는 자리는 `_check_write` 하나다.
        self.assertEqual(len([hit for hit in hits if "in _WRITE_PATHS" in hit]), 1, hits)

    def test_P30_delete_and_patch_are_named_only_in_the_known_methods(self) -> None:
        # 012 spec §10-1 — 지우기·부분 수정 동사는 감사 로그가 이름을 알아보는 목록에만 있다.
        hits = lines_containing(SRC, '"DELETE"') + lines_containing(SRC, '"PATCH"')
        self.assertEqual(len(set(hits)), 1, hits)
        self.assertIn("dooray_core/client.py", hits[0])
        self.assertIn("_KNOWN_METHODS", hits[0])

    def test_P22_the_writes_we_did_not_open_appear_in_no_path(self) -> None:
        # 경로 리터럴로 범위를 좁힌다 — `set-done`을 전역으로 찾으면 정당한 재현 주석이 걸린다.
        paths = [
            hit
            for hit in lines_containing(SRC, "/project/v1") + lines_containing(SRC, "/common/v1")
        ]
        self.assertTrue(paths)
        for needle in ("/move", "incoming-hooks", "/files"):
            hits = [hit for hit in paths if needle in hit]
            with self.subTest(needle=needle):
                self.assertEqual(hits, [], f"열지 않은 쓰기 경로가 생겼습니다: {hits}")
        # 012가 연 셋은 전송 표(client.py)와 그것을 부르는 Writer(writer.py)에만 있다.
        for needle in ("set-done", "set-workflow", "/to/"):
            hits = [
                hit
                for hit in paths
                if needle in hit
                and "dooray_core/client.py" not in hit
                and WRITER_FILE not in hit
            ]
            with self.subTest(needle=needle):
                self.assertEqual(hits, [], f"열린 쓰기 경로가 다른 파일에 생겼습니다: {hits}")

    def test_P23_the_mention_link_is_built_in_one_place(self) -> None:
        # 007 spec §8-3 — `dooray://`와 f-string 여는 따옴표가 한 줄에 같이 있는 곳은 `mention.py` 하나다.
        # `.format(`으로 비켜 가는 것은 리뷰가 본다.
        hits = [
            hit
            for hit in lines_containing(SRC, "dooray://")
            if 'f"' in hit.split(": ", 1)[1] or "f'" in hit.split(": ", 1)[1]
        ]
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("dooray_core/mention.py", hits[0])

    def test_P24_the_old_draft_rule_is_gone(self) -> None:
        # 007 spec §7-2 — 옛 추천 문구가 그대로 저장된 것이 이 사이클의 출발점이다.
        # 013: 절차 글이 `src/`(guides.py)로 왔다 — `src/`를 보면 그 글도 본다 (013 spec §9-2 #16).
        for needle in ("4줄", "추천 문구"):
            with self.subTest(needle=needle):
                self.assertEqual(lines_containing(SRC, needle), [])

    def test_P27_the_ask_every_call_mark_sits_where_it_should(self) -> None:
        # 005 spec §3-5, O1 (B). 표시는 상수 하나이고, 쓰기 셋과 `set_reply_style`에만 걸린다.
        # 글자가 아니라 `ast`로 `add_tool` 호출의 키워드를 본다 (009 log — 글자 검사가 놓쳤다).
        hits = lines_containing(SRC, "requiresUserInteraction")
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("mcp_server/runtime.py", hits[0])

        def add_tool_calls(name: str) -> list[ast.Call]:
            tree = ast.parse((SERVER / name).read_text(encoding="utf-8"))
            return [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_tool"
            ]

        def keyword(call: ast.Call, name: str) -> ast.expr | None:
            found = [item.value for item in call.keywords if item.arg == name]
            return found[0] if found else None

        def is_the_mark(value: ast.expr | None) -> bool:
            return (
                isinstance(value, ast.Attribute)
                and value.attr == "ASK_EVERY_CALL"
                and isinstance(value.value, ast.Name)
                and value.value.id == "runtime"
            )

        written = add_tool_calls("write_tools.py")
        self.assertEqual(len(written), 9)  # 만들기 넷 + 고치기 다섯 (012 spec §10-2 #12, 020)
        for call in written:
            with self.subTest(line=call.lineno):
                self.assertTrue(is_the_mark(keyword(call, "meta")))

        marked = [call for call in add_tool_calls("tools.py") if keyword(call, "meta") is not None]
        self.assertEqual(len(marked), 1, [call.lineno for call in marked])
        self.assertTrue(is_the_mark(keyword(marked[0], "meta")))
        tool_name = keyword(marked[0], "name")
        self.assertIsInstance(tool_name, ast.Constant)
        self.assertEqual(tool_name.value, "set_reply_style")

    def test_P28_mention_links_are_made_in_one_helper(self) -> None:
        # 012 spec 개정 7의 R7-3 — `link_mentions` 호출은 `src/`에 하나이고, 댓글 등록과 고치기가 같이 쓰는 도우미 안이다.
        found: list[str] = []
        for path in source_files(SRC):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in ast.walk(tree):
                if not isinstance(function, ast.FunctionDef):
                    continue
                for node in ast.walk(function):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, (ast.Attribute, ast.Name))
                        and getattr(node.func, "attr", getattr(node.func, "id", None)) == "link_mentions"
                    ):
                        found.append(f"{path.relative_to(SRC)}:{function.name}")
        self.assertEqual(found, [f"{WRITE_TOOLS_FILE}:_link_marks"])

    # --- 014 P33 — 테넌트를 찾는 피드 읽기는 코어에만 있다 (spec §12-1, 완료 기준 12). 전부 `ast`로 본다 ---

    def test_P33_the_feed_is_read_in_three_core_functions(self) -> None:
        self.assertEqual(
            call_sites(SRC, "feed_comments"),
            sorted(
                [
                    "dooray_core/collect.py:collect_my_comments_from_feed",
                    "dooray_core/collect.py:collect_my_mentions_from_feed",
                    "dooray_core/collect.py:feed_tenant",
                ]
            ),
        )

    def test_P33_the_feed_tenant_is_called_only_by_thread_people_and_the_create_helper(self) -> None:
        # 017(ADR 016) — 새 업무에는 그 업무의 글이 없어 업무 만들기가 코어의 `feed_tenant`를 직접 부른다.
        # 020 — 업무 만들기 둘이 `_create_in`을 같이 쓴다.
        self.assertEqual(
            call_sites(SRC, "feed_tenant"),
            sorted(["dooray_core/collect.py:thread_people", f"{WRITE_TOOLS_FILE}:_create_in"]),
        )

    def test_P33_the_write_tools_do_not_touch_the_feed(self) -> None:
        # 017(ADR 016)으로 좁혔다 — 피드를 실제로 읽는 코드(`feed_comments`·피드 경로)는 코어에만 있다.
        # `write_tools.py`가 닿는 것은 `collect.feed_tenant(reader)` 호출 하나다.
        tree = ast.parse((SERVER / "write_tools.py").read_text(encoding="utf-8"))
        calls = [
            called_name(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and called_name(node) in ("feed_comments", "feed_tenant")
        ]
        self.assertEqual(calls, ["feed_tenant"])
        names: list[str] = []
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, (ast.ImportFrom, ast.Import)):
                imported.extend(alias.name for alias in node.names)
                imported.extend(alias.asname for alias in node.names if alias.asname)
        self.assertNotIn("STREAMS_PATH", names + imported)
        # 014 보충(리뷰 L3) — 별칭 import(`feed_tenant as ft`)는 그대로 잡는다. `people.feed_read`는 다른 이름이다.
        for name in ("feed_tenant", "feed_comments"):
            with self.subTest(name=name):
                self.assertNotIn(name, imported)
        self.assertNotIn("feed_comments", names)
        # 이름으로 닿는 자리는 그 호출 하나다 — `collect.feed_tenant` 속성 접근. 다른 이름에 담아 부르지 않는다.
        self.assertEqual(names.count("feed_tenant"), 1)
        reached = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "feed_tenant"
        ]
        self.assertEqual(len(reached), 1)
        self.assertIsInstance(reached[0].value, ast.Name)
        self.assertEqual(reached[0].value.id, "collect")
        # docstring도 `ast.Constant`다.
        texts = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "/streams" in node.value
        ]
        self.assertEqual(texts, [])

    def test_TR_25_the_task_link_helpers_are_shared_by_the_write_paths(self) -> None:
        # 017 spec §2-1 — 업무 웹 주소 변환은 한 도우미에서. 요청 전 검사는 새로 쓰는 두 도구에서.
        self.assertEqual(
            call_sites(SRC, "_linked_tasks"),
            sorted(
                f"{WRITE_TOOLS_FILE}:{name}" for name in ("_linked_new", "_create_in", "post_comment")
            ),
        )
        self.assertEqual(
            call_sites(SRC, "_task_links"),
            sorted(f"{WRITE_TOOLS_FILE}:{name}" for name in ("create_my_post", "create_post", "post_comment")),
        )

    def test_P33_the_core_never_asks_who_i_am(self) -> None:
        # 개정 1의 길(`reader.me()`)이 돌아오면 드러난다.
        self.assertEqual(call_sites(CORE, "me"), [])

    def test_P29_the_five_edit_tools_carry_the_destructive_annotation(self) -> None:
        # 012 spec §2-2의 6 — 있던 것을 덮어쓰는 다섯만 `EDIT`(destructive)다. 만들기 넷은 `WRITE` 그대로다.
        tree = ast.parse((SERVER / "write_tools.py").read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_tool"
        ]
        self.assertEqual(len(calls), 9)

        def keyword(call: ast.Call, name: str) -> ast.expr | None:
            found = [item.value for item in call.keywords if item.arg == name]
            return found[0] if found else None

        marked = {}
        for call in calls:
            annotation = keyword(call, "annotations")
            self.assertIsInstance(annotation, ast.Name, call.lineno)
            tool_name = keyword(call, "name")
            self.assertIsInstance(tool_name, ast.Constant, call.lineno)
            marked[tool_name.value] = annotation.id
        self.assertEqual(
            {name for name, value in marked.items() if value == "EDIT"},
            {"edit_comment", "edit_post", "set_post_people", "set_post_fields", "set_post_status"},
        )
        self.assertEqual(
            {name for name, value in marked.items() if value != "EDIT"},
            {"post_comment", "create_my_post", "create_post", "set_parent_post"},
        )

    def test_P31_no_except_body_in_the_write_tools_raises_or_returns(self) -> None:
        # 012 완료 기준 32 — `except` 안에서 던지면 전송 계층 프레임이 `__context__`에 붙는다 (002 리뷰 M1).
        # 값만 담고 블록 밖에서 판정한다(010의 모양).
        tree = ast.parse((SERVER / "write_tools.py").read_text(encoding="utf-8"))
        handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
        self.assertTrue(handlers)
        for handler in handlers:
            inside = [node for statement in handler.body for node in ast.walk(statement)]
            with self.subTest(line=handler.lineno):
                self.assertFalse(
                    [node for node in inside if isinstance(node, (ast.Raise, ast.Return))],
                    f"write_tools.py:{handler.lineno}의 except 몸통이 던지거나 돌려줍니다.",
                )

    def test_P17_the_unverified_flag_is_never_read(self) -> None:
        # 의미가 미확인인 값으로 판정하지 않는다 (002 spec §6-6, §14의 21).
        hits = lines_containing(SRC, "due_date_flag")
        self.assertTrue(hits)
        for hit in hits:
            with self.subTest(hit=hit):
                self.assertIn("dooray_core/models.py", hit)
        self.assertEqual(lines_containing(SERVER, "due_date_flag"), [])


if __name__ == "__main__":
    unittest.main()
