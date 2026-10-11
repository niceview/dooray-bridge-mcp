"""홈 폴더 경로, settings.json, 토큰 읽기, 허용 주소, 원자적 파일 쓰기 (spec §3).

파일 캐시는 두지 않는다 (ADR 005). 코어가 홈 폴더에 만드는 파일은
감사 로그와 settings.json뿐이다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import (
    API_BASE_NOT_ALLOWED_MESSAGE,
    HOME_NOT_ABSOLUTE_MESSAGE,
    SETTINGS_BROKEN_MESSAGE,
    SETTINGS_NOT_OBJECT_MESSAGE,
    TOKEN_MISSING_MESSAGE,
    WEB_BASE_NOT_ALLOWED_MESSAGE,
    ConfigError,
    TokenMissingError,
)

ALLOWED_API_BASES: tuple[str, ...] = (
    "https://api.dooray.com",  # 민간 클라우드
    "https://api.gov-dooray.com",  # 공공 클라우드
    "https://api.gov-dooray.co.kr",  # 공공 업무망 클라우드
    "https://api.dooray.co.kr",  # 금융 클라우드
)

ALLOWED_WEB_SUFFIXES: tuple[str, ...] = (
    ".dooray.com",
    ".gov-dooray.com",
    ".gov-dooray.co.kr",
    ".dooray.co.kr",
)

HOME_ENV = "DOORAY_BRIDGE_HOME"
DEFAULT_HOME_NAME = ".dooray-bridge"
KEYCHAIN_SERVICE = "dooray-bridge"
KEYCHAIN_ACCOUNT = "api-token"
KEYCHAIN_ARGV: tuple[str, ...] = (
    "/usr/bin/security",
    "find-generic-password",
    "-s",
    KEYCHAIN_SERVICE,
    "-a",
    KEYCHAIN_ACCOUNT,
    "-w",
)

KeychainRunner = Callable[[list[str]], str]

# 라벨은 영문 소문자·숫자·하이픈, 하이픈으로 시작하거나 끝나지 않고, 점으로 두 개 이상 이어진다.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_PLAIN_HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")


# --- 경로 (§3-1) ------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    home: Path
    settings_file: Path
    logs_dir: Path
    state_dir: Path


def paths() -> Paths:
    """홈 폴더와 그 아래 경로. 언제나 절대 경로다."""
    raw = os.environ.get(HOME_ENV, "").strip()
    if raw:
        given = Path(raw).expanduser()
        if not given.is_absolute():
            # 서버는 아무 폴더에서나 뜬다. 상대 경로는 그날 열린 폴더마다 다른 곳을 가리킨다 (리뷰 L4).
            raise ConfigError(HOME_NOT_ABSOLUTE_MESSAGE)
        home = given.resolve()
    else:
        home = (Path.home() / DEFAULT_HOME_NAME).resolve()
    return Paths(
        home=home,
        settings_file=home / "settings.json",
        logs_dir=home / "logs",
        state_dir=home / "state",
    )


def ensure_home(p: Paths | None = None) -> Paths:
    """없는 폴더만 0o700으로 만든다. 캐시 폴더는 없다 (ADR 005).

    **이미 있는 폴더의 권한은 바꾸지 않는다** (§3-1). DOORAY_BRIDGE_HOME이 기존 폴더를
    잘못 가리켰을 때 그 폴더를 0o700으로 바꿔 버리는 부작용을 막는다.
    파일(settings.json, 감사 로그)은 언제나 0o600이라 내용 보호는 그대로다.
    """
    p = p or paths()
    for folder in (p.home, p.logs_dir, p.state_dir):
        if folder.is_dir():
            continue
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)
    return p


# --- 설정 (§3-2) ------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    api_base: str
    web_base: str | None
    projects: tuple[str, ...]
    reply_style: str | None


def _env(name: str) -> str | None:
    """환경변수 하나. strip 후 비어 있으면 미설정으로 본다."""
    value = os.environ.get(name, "").strip()
    return value or None


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


def check_api_base(value: str) -> str:
    """끝의 슬래시를 떼고 허용 목록과 정확히 대조한다 (api-notes §1-1).

    전송 계층도 만들어질 때 이 함수로 주소를 검사한다 (§5-1, T10).
    """
    base = value.rstrip("/")
    if base not in ALLOWED_API_BASES:
        raise ConfigError(
            API_BASE_NOT_ALLOWED_MESSAGE.format(allowed=", ".join(ALLOWED_API_BASES))
        )
    return base


def _check_web_base(value: str) -> str:
    """스킴·호스트·사용자 정보·포트·경로를 urlsplit으로 판정한다 (§3-2).

    통과하면 **검증한 값을 다시 조립해** 돌려준다 — urlsplit이 소문자로 정규화한
    스킴과 hostname만 쓴다. 원문에 남아 있던 대소문자나 군더더기가 링크 앞부분으로
    새어 나가지 않는다.
    """
    base = value.rstrip("/")
    try:
        parts = urlsplit(base)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        host, port, parts = "", None, None
    ok = (
        parts is not None
        and parts.scheme == "https"
        # `urlsplit`은 역슬래시·`%`를 호스트의 일반 문자로 읽지만 브라우저는 다르게 읽는다.
        # 호스트가 평범한 도메인 이름 모양이고, netloc에 호스트 말고 아무것도 없어야 한다 (리뷰 M1).
        and _PLAIN_HOST.fullmatch(host) is not None
        and parts.netloc.lower() == host
        and parts.username is None
        and parts.password is None
        and port is None
        and parts.path == ""
        and parts.query == ""
        and parts.fragment == ""
        and any(host.endswith(suffix) for suffix in ALLOWED_WEB_SUFFIXES)
    )
    if not ok or parts is None:
        raise ConfigError(
            WEB_BASE_NOT_ALLOWED_MESSAGE.format(allowed=", ".join(ALLOWED_WEB_SUFFIXES))
        )
    return f"{parts.scheme}://{host}"


def _read_settings_file(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        raise ConfigError(SETTINGS_BROKEN_MESSAGE.format(path=path)) from None
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigError(SETTINGS_BROKEN_MESSAGE.format(path=path)) from None
    if not isinstance(data, dict):
        raise ConfigError(SETTINGS_NOT_OBJECT_MESSAGE.format(path=path))
    return data


def _projects_from(stored: object) -> tuple[str, ...]:
    raw = _env("DOORAY_PROJECTS")
    if raw is not None:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if isinstance(stored, list):
        return tuple(item.strip() for item in stored if isinstance(item, str) and item.strip())
    return ()


def load_settings(p: Paths | None = None) -> Settings:
    """우선순위: 환경변수 > settings.json > 기본값."""
    p = p or paths()
    stored = _read_settings_file(p.settings_file)

    api_base = _env("DOORAY_API_BASE") or _text(stored.get("api_base")) or ALLOWED_API_BASES[0]
    web_raw = _env("DOORAY_WEB_BASE") or _text(stored.get("web_base"))
    return Settings(
        api_base=check_api_base(api_base),
        web_base=_check_web_base(web_raw) if web_raw else None,
        projects=_projects_from(stored.get("projects")),
        reply_style=_text(stored.get("reply_style")),
    )


def save_settings(changes: Mapping[str, object], p: Paths | None = None) -> Settings:
    """현재 파일에 changes를 덮어쓴다. 모르는 키는 그대로 둔다."""
    p = ensure_home(p)
    stored = _read_settings_file(p.settings_file)
    stored.update(changes)
    text = json.dumps(stored, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_atomic(p.settings_file, text)
    return load_settings(p)


def write_enabled() -> bool:
    """쓰기 게이트. settings.json에 키를 두지 않는다 (제약 2)."""
    return os.environ.get("DOORAY_ENABLE_WRITE", "").strip() == "1"


SKIP_WRITE_ASK_ENV = "DOORAY_SKIP_WRITE_ASK"
SKIP_WRITE_ASK_VALUE = "1"


def ask_every_write() -> bool:
    """쓰기마다 허락 묻기 (021, ADR 019). 표시를 빼는 값은 `1` 하나다 — 게이트와 같은 규칙. 그 밖은 모두 묻는다."""
    return os.environ.get(SKIP_WRITE_ASK_ENV, "").strip() != SKIP_WRITE_ASK_VALUE


# --- 토큰 (§3-3) ------------------------------------------------------------


def _run_security(argv: list[str]) -> str:
    """기본 키체인 실행기. 인자에 토큰이 없고, 실패해도 stdout·stderr를 밖으로 내지 않는다."""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    if done.returncode != 0:
        raise RuntimeError("키체인 조회에 실패했습니다.")
    return done.stdout


def read_token(runner: KeychainRunner | None = None) -> str:
    """환경변수 → (macOS면) 키체인 → TokenMissingError (ADR 004)."""
    from_env = os.environ.get("DOORAY_API_TOKEN", "").strip()
    if from_env:
        return from_env

    if sys.platform == "darwin":
        run = runner or _run_security
        stdout = ""
        try:
            stdout = run(list(KEYCHAIN_ARGV))
        except Exception:
            stdout = ""
        token = stdout.rstrip("\r\n")
        if token.strip():
            return token

    raise TokenMissingError(TOKEN_MISSING_MESSAGE)


# --- 원자적 파일 쓰기 (§3-4) -------------------------------------------------


def write_atomic(path: Path, text: str, *, mode: int = 0o600) -> None:
    """같은 폴더의 임시 파일에 쓴 뒤 os.replace로 바꿔 끼운다."""
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False
    )
    temp_name = handle.name
    try:
        with handle as opened:
            opened.write(text)
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
