"""005 spec §4-7 — 번들 매니페스트와 빌드 스크립트 (B1~B8).

`packaging/`은 패키지가 아니다 — `build.py`를 파일 경로로 불러온다. 네트워크를 타지 않는다.
만드는 곳은 임시 폴더(절대 경로)뿐이다. 저장소의 `dist/`를 만들지 않는다.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from support import FAKE_TOKEN, tenant_addresses
from test_mcp_server import ServerCase

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "packaging" / "manifest.json"


def load_build():
    spec = importlib.util.spec_from_file_location(
        "dooray_bridge_packaging_build", REPO / "packaging" / "build.py"
    )
    module = importlib.util.module_from_spec(spec)
    # `dataclass`가 모듈을 `sys.modules`에서 찾는다 — 실행 전에 올려 둔다.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# 번들에 들어갈 파일 (spec §4-1). 스크립트의 목록을 베끼지 않고 손으로 적는다.
# spec은 "17개"라고 적었지만 `dooray_core`는 009가 `models.py`를 더해 아홉이다(P6의 목록) — 18개다.
# 013이 `mcp_server/guides.py`를 더해 19개다 (013 spec §9-2 #17).
EXPECTED_BUNDLE = sorted(
    [
        "manifest.json",
        "pyproject.toml",
        "uv.lock",
        "src/dooray_core/__init__.py",
        "src/dooray_core/client.py",
        "src/dooray_core/collect.py",
        "src/dooray_core/config.py",
        "src/dooray_core/errors.py",
        "src/dooray_core/mention.py",
        "src/dooray_core/models.py",
        "src/dooray_core/render.py",
        "src/dooray_core/writer.py",
        "src/mcp_server/__init__.py",
        "src/mcp_server/guides.py",
        "src/mcp_server/runtime.py",
        "src/mcp_server/server.py",
        "src/mcp_server/tools.py",
        "src/mcp_server/view.py",
        "src/mcp_server/write_tools.py",
    ]
)

# B7ⓐ — 이름에 이것이 들어간 항목이 없다.
FORBIDDEN_NAME_PARTS = (
    ".env",
    ".dev-home",
    "tests/",
    "scratch/",
    "specs/",
    ".git",
    ".claude",
    ".venv",
    "__pycache__",
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# 번들 파일 이름의 버전은 `pyproject.toml`에서 읽는다. 버전 셋이 같은지는 B4가 본다.
BUNDLE_NAME = "dooray-bridge-{}.mcpb".format(
    tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
)

class ManifestTest(unittest.TestCase):
    def test_B1_the_manifest_runs_the_uv_server(self) -> None:
        manifest = read_manifest()
        self.assertEqual(manifest["manifest_version"], "0.4")
        self.assertEqual(manifest["name"], "dooray-bridge")
        server = manifest["server"]
        self.assertEqual(server["type"], "uv")
        self.assertEqual(server["mcp_config"]["command"], "uv")
        args = server["mcp_config"]["args"]
        for wanted in ("${__dirname}", "--frozen", "dooray-mcp"):
            with self.subTest(arg=wanted):
                self.assertIn(wanted, args)
        self.assertEqual(manifest["compatibility"]["platforms"], ["darwin", "win32"])

    def test_B1_the_arguments_are_what_uv_takes(self) -> None:
        # `server.type`이 `uv`이면 Claude Desktop은 `command`를 쓰지 않는다 — 자기가 찾은 `uv`에 `args`만 넘긴다
        # (0.1.2 실측: `/bin/sh launch.sh`가 `uv …/launch.sh`로 실행돼 서버가 뜨지 않았다. ADR 013).
        # 그래서 `args`는 `uv`의 인자여야 한다.
        args = read_manifest()["server"]["mcp_config"]["args"]
        self.assertEqual(args, ["run", "--directory", "${__dirname}", "--frozen", "dooray-mcp"])

    def test_B1_005_the_manifest_does_not_promise_a_prompt(self) -> None:
        # 005 리뷰 H2 — "매번 허락"은 Claude Desktop에서 보장되지 않는다(ADR 009). 조건은 SECURITY.md가 적는다.
        text = MANIFEST.read_text(encoding="utf-8")
        self.assertNotIn("매번 허락", text)
        self.assertIn("SECURITY.md", read_manifest()["description"])

    def test_MF_1_the_manifest_has_no_ask_switch(self) -> None:
        # 021 spec §2-4(J11) — 번들에는 허락 표시를 빼는 칸이 없다. 확장의 서버는 언제나 표시를 붙인다.
        text = MANIFEST.read_text(encoding="utf-8")
        for needle in ("SKIP_WRITE_ASK", "skip_write_ask"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

    def test_B2_the_environment_holds_three_keys_and_a_fixed_gate(self) -> None:
        manifest = read_manifest()
        env = manifest["server"]["mcp_config"]["env"]
        # 딕셔너리 전체로 본다 (005 리뷰 M4) — 키 집합과 값의 모양만 보면 토큰 자리표시가
        # 웹 주소 칸으로 옮겨 가도 통과한다. 게이트는 코드가 켬으로 읽는 유일한 값이다(`config.write_enabled`).
        self.assertEqual(
            env,
            {
                "DOORAY_API_TOKEN": "${user_config.api_token}",
                "DOORAY_WEB_BASE": "${user_config.web_base}",
                "DOORAY_ENABLE_WRITE": "1",
            },
        )
        # 토큰 자리표시는 원문 전체에서 딱 한 번(env) 나온다 — 인자·다른 칸으로 새지 않는다.
        self.assertEqual(MANIFEST.read_text(encoding="utf-8").count("user_config.api_token"), 1)
        for arg in manifest["server"]["mcp_config"]["args"]:
            with self.subTest(arg=arg):
                self.assertNotIn("${user_config.", arg)
        user_config = manifest["user_config"]
        self.assertEqual(set(user_config), {"api_token", "web_base"})
        token = user_config["api_token"]
        self.assertEqual(token["type"], "string")
        self.assertIs(token["sensitive"], True)
        self.assertIs(token["required"], True)
        self.assertNotIn("default", token)

    def test_B4_the_three_versions_agree(self) -> None:
        project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
        locked = [
            package["version"]
            for package in lock["package"]
            if package["name"] == "dooray-bridge"
        ]
        self.assertEqual(len(locked), 1)
        self.assertEqual(project["project"]["version"], read_manifest()["version"])
        self.assertEqual(project["project"]["version"], locked[0])


class ManifestToolsTest(ServerCase):
    async def test_B3_the_manifest_names_the_tools_of_the_gate_on_server(self) -> None:
        # 기대값을 손으로 적지 않고 서버에서 뽑는다 — 도구가 늘면 매니페스트를 고치라는 실패가 난다.
        with mock.patch.dict(os.environ, {"DOORAY_ENABLE_WRITE": "1"}):
            found = await self.build(write=True).list_tools()
        listed = [tool["name"] for tool in read_manifest()["tools"]]
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(set(listed), {tool.name for tool in found})


class BuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.build_module = load_build()
        self.dist_existed = (REPO / "dist").exists()
        scratch = tempfile.TemporaryDirectory(prefix="dooray-bridge-bundle-")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name).resolve()
        self.out = self.scratch / "out"

    def tearDown(self) -> None:
        # 저장소의 `dist/`가 새로 생기지 않았다 (B8).
        if not self.dist_existed:
            self.assertFalse((REPO / "dist").exists(), "빌드가 저장소에 dist/를 만들었습니다.")

    def names(self, path: Path) -> list[str]:
        with zipfile.ZipFile(path) as archive:
            return sorted(archive.namelist())

    def contents(self, path: Path) -> dict[str, str]:
        with zipfile.ZipFile(path) as archive:
            return {
                name: archive.read(name).decode("utf-8") for name in archive.namelist()
            }

    def test_B5_the_bundle_holds_exactly_the_allowed_files(self) -> None:
        built = self.build_module.build(self.out)
        self.assertEqual(built.bundle, self.out / BUNDLE_NAME)
        self.assertEqual(self.names(built.bundle), EXPECTED_BUNDLE)
        # 넣은 내용이 저장소의 원본과 같다.
        for name, text in self.contents(built.bundle).items():
            source = REPO / "packaging" / name if name == "manifest.json" else REPO / name
            with self.subTest(file=name):
                self.assertEqual(text, source.read_text(encoding="utf-8"))

    def test_B6_the_build_makes_the_bundle_only(self) -> None:
        # 013 spec §9-1의 B6′ — 절차 글은 번들 안(`guides.py`)에 있다. 따로 올릴 파일을 만들지 않는다.
        built = self.build_module.build(self.out)
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), [BUNDLE_NAME])
        self.assertEqual(built.bundle, self.out / BUNDLE_NAME)
        self.assertEqual([path.name for path in self.out.iterdir() if "skill" in path.name.lower()], [])
        for name in ("SKILL_FILES", "SKILL_DIR"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(self.build_module, name))

    def test_B7_the_bundle_carries_no_secret_or_real_name(self) -> None:
        built = self.build_module.build(self.out)
        for path in (built.bundle,):
            for name, text in self.contents(path).items():
                with self.subTest(zip=path.name, file=name):
                    for part in FORBIDDEN_NAME_PARTS:
                        self.assertNotIn(part, name)
                    self.assertNotIn(FAKE_TOKEN, text)
                    self.assertEqual(EMAIL.findall(text), [])
                    self.assertNotIn("DOORAY_API_TOKEN=", text)
                    # 두레이 도메인의 주소는 API 주소 넷(`config.py`)·접미·자리표시뿐이다 — 테넌트 주소가 없다.
                    # 판정은 `support.tenant_addresses` 하나다(D11과 같다, 005 리뷰 M5).
                    self.assertEqual(tenant_addresses(text), [])

    def test_B8_a_relative_out_dir_is_refused_before_anything_is_written(self) -> None:
        relative = Path("dooray-bridge-relative-out")
        before = {path: path.exists() for path in (relative, REPO / relative)}
        with self.assertRaises(ValueError):
            self.build_module.build(relative)
        for path, existed in before.items():
            with self.subTest(path=str(path)):
                self.assertEqual(path.exists(), existed)
        self.assertFalse(self.out.exists())

    def test_B8_an_absolute_out_dir_is_the_only_place_written(self) -> None:
        self.build_module.build(self.out)
        # 임시 파일이 남지 않았고, `out_dir` 밖에 새 파일이 없다.
        self.assertEqual(
            sorted(path.name for path in self.out.iterdir()),
            [BUNDLE_NAME],
        )
        self.assertEqual([path.name for path in self.scratch.iterdir()], ["out"])

    def test_the_same_input_gives_the_same_bytes(self) -> None:
        # 리뷰에서 두 빌드를 비교할 수 있게 한다 — 시각·순서·권한을 고정했다.
        first = self.build_module.build(self.out)
        second = self.build_module.build(self.scratch / "again")
        self.assertEqual(first.bundle.read_bytes(), second.bundle.read_bytes())

    def test_a_version_mismatch_stops_the_build(self) -> None:
        manifest = dict(read_manifest(), version="9.9.9")
        altered = self.scratch / "manifest.json"
        altered.write_text(json.dumps(manifest), encoding="utf-8")
        files = dict(self.build_module.BUNDLE_FILES, **{"manifest.json": altered})
        with mock.patch.object(self.build_module, "BUNDLE_FILES", files):
            with self.assertRaises(ValueError):
                self.build_module.build(self.out)
        self.assertFalse(self.out.exists())

    def fake_repo(self) -> Path:
        """저장소를 흉내 낸 임시 폴더. 번들에 드는 19개(손으로 적은 목록)만 복사한다."""
        root = self.scratch / "repo"
        for name in EXPECTED_BUNDLE:
            source = REPO / "packaging" / name if name == "manifest.json" else REPO / name
            target = root / "packaging" / name if name == "manifest.json" else root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        return root

    def test_M2_the_fake_repository_builds(self) -> None:
        # 아래 둘이 "가짜 저장소가 원래 깨져 있어서" 실패하는 것이 아님을 고정한다.
        built = self.build_module.build(self.out, self.fake_repo())
        self.assertEqual(self.names(built.bundle), EXPECTED_BUNDLE)

    def test_M2_a_python_file_outside_the_list_stops_the_build(self) -> None:
        # 005 리뷰 M2 — 실측용 임시 파일이 조용히 번들에 들어가거나 빠지지 않고 빌드가 멈춘다.
        for package in ("src/dooray_core", "src/mcp_server"):
            with self.subTest(package=package):
                root = self.fake_repo()
                (root / package / "scratch_probe.py").write_text("x = 1\n", encoding="utf-8")
                with self.assertRaises(ValueError) as caught:
                    self.build_module.build(self.out, root)
                self.assertIn("scratch_probe.py", str(caught.exception))
                self.assertFalse(self.out.exists())
                (root / package / "scratch_probe.py").unlink()

    def test_M2_a_missing_listed_file_stops_the_build(self) -> None:
        for name in ("src/dooray_core/models.py", "src/mcp_server/view.py"):
            with self.subTest(file=name):
                root = self.fake_repo()
                (root / name).unlink()
                with self.assertRaises(ValueError) as caught:
                    self.build_module.build(self.out, root)
                self.assertIn(Path(name).name, str(caught.exception))
                self.assertFalse(self.out.exists())

    def test_a_symbolic_link_stops_the_build(self) -> None:
        link = self.scratch / "pyproject.toml"
        link.symlink_to(REPO / "pyproject.toml")
        files = dict(self.build_module.BUNDLE_FILES, **{"pyproject.toml": link})
        with mock.patch.object(self.build_module, "BUNDLE_FILES", files):
            with self.assertRaises(ValueError):
                self.build_module.build(self.out)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
