"""Claude Desktop 확장 번들(`.mcpb`)을 만든다 (005 spec §4-6).

표준 라이브러리만 쓴다. **넣을 것만 나열한다**(허용 목록) — 빼는 목록은 새 파일을 새어 들게 한다(§4-1).
쓰는 곳은 `out_dir` 하나다. 저장소 파일을 고치지 않는다.
같은 입력이면 같은 파일이 나온다: 항목을 이름순으로 넣고, 시각·권한·압축 수준을 고정한다.

실행: `uv run python packaging/build.py` → 저장소의 `dist/`.
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "dooray-bridge"

# 번들 안 경로 → 저장소 뿌리에서의 원본 경로.
BUNDLE_FILES = {
    "manifest.json": Path("packaging/manifest.json"),
    "pyproject.toml": Path("pyproject.toml"),
    "uv.lock": Path("uv.lock"),
}
# 패키지 둘의 파일을 이름으로 적는다(글롭이 아니다 — 005 리뷰 M2). 폴더에 이 목록 밖의 `.py`가 있거나
# 목록의 파일이 없으면 빌드가 멈춘다 — 실측용 임시 파일이 조용히 들어가거나 빠지지 않고 사람이 알게.
BUNDLE_PACKAGES = {
    "src/dooray_core": (
        "__init__.py",
        "client.py",
        "collect.py",
        "config.py",
        "errors.py",
        "mention.py",
        "models.py",
        "render.py",
        "writer.py",
    ),
    "src/mcp_server": (
        "__init__.py",
        "guides.py",
        "runtime.py",
        "server.py",
        "tools.py",
        "view.py",
        "write_tools.py",
    ),
}

# zip이 담을 수 있는 가장 이른 시각. 파일의 수정 시각을 싣지 않는다 — 같은 입력이면 같은 바이트다.
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644


@dataclass(frozen=True)
class Built:
    bundle: Path


def build(out_dir: Path, repo: Path = REPO) -> Built:
    """`out_dir`(절대 경로)에 번들을 만든다. 버전 셋이 다르거나 패키지 폴더가 목록과
    다르면 아무것도 만들지 않는다. `repo`는 테스트가 가짜 저장소를 줄 때만 바꾼다."""
    if not Path(out_dir).is_absolute():
        raise ValueError(f"out_dir은 절대 경로여야 합니다: {out_dir}")
    out_dir = Path(out_dir)
    repo = Path(repo)
    version = _checked_version(repo)

    bundle_entries = {name: _plain_file(repo / path) for name, path in BUNDLE_FILES.items()}
    for package, names in BUNDLE_PACKAGES.items():
        _check_package(repo / package, names)
        for name in names:
            bundle_entries[f"{package}/{name}"] = _plain_file(repo / package / name)

    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = out_dir / f"{PACKAGE_NAME}-{version}.mcpb"
    _write_zip(bundle, bundle_entries)
    return Built(bundle=bundle)


def _check_package(folder: Path, names: tuple[str, ...]) -> None:
    """폴더의 `.py`가 목록과 정확히 같아야 한다. 하위 폴더(`__pycache__`)는 보지 않는다."""
    found = {path.name for path in folder.glob("*.py")}
    extra = sorted(found - set(names))
    missing = sorted(set(names) - found)
    if extra:
        raise ValueError(f"번들 목록에 없는 파일이 있습니다({folder}): {extra}")
    if missing:
        raise ValueError(f"번들 목록의 파일이 없습니다({folder}): {missing}")


def _checked_version(repo: Path) -> str:
    """`pyproject.toml`·`manifest.json`·`uv.lock`의 버전이 같을 때만 그 값을 돌려준다 (§4-9)."""
    project = tomllib.loads((repo / BUNDLE_FILES["pyproject.toml"]).read_text(encoding="utf-8"))
    manifest = json.loads((repo / BUNDLE_FILES["manifest.json"]).read_text(encoding="utf-8"))
    lock = tomllib.loads((repo / BUNDLE_FILES["uv.lock"]).read_text(encoding="utf-8"))
    locked = [
        package.get("version")
        for package in lock.get("package", [])
        if package.get("name") == PACKAGE_NAME
    ]
    versions = {
        "pyproject.toml": project["project"]["version"],
        "manifest.json": manifest.get("version"),
        "uv.lock": locked[0] if len(locked) == 1 else None,
    }
    if len(set(versions.values())) != 1:
        raise ValueError(f"버전이 서로 다릅니다: {versions}")
    return versions["pyproject.toml"]


def _plain_file(path: Path) -> Path:
    """저장소 안의 보통 파일만 받는다. 심볼릭 링크는 경로 어디에 있든 거절한다."""
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise ValueError(f"보통 파일이 아닙니다: {path}")
    return path


def _write_zip(target: Path, entries: dict[str, Path]) -> None:
    """같은 폴더의 임시 파일에 쓴 뒤 `os.replace`로 바꿔 넣는다."""
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    os.close(handle)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name in sorted(entries):
                info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
                info.create_system = 3  # 유닉스. 기본값은 실행하는 운영체제를 따른다
                info.external_attr = (0o100000 | FILE_MODE) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, entries[name].read_bytes(), compresslevel=9)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> None:
    built = build(REPO / "dist")
    with zipfile.ZipFile(built.bundle) as archive:
        count = len(archive.namelist())
    print(built.bundle)
    print(f"번들 안 파일: {count}개")


if __name__ == "__main__":
    main()
