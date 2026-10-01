"""예외 계층과 사용자에게 보일 한국어 문장 (spec §2).

문장에는 상태 코드와 원인·해결 방법만 담는다. 주소·쿼리·요청 헤더·응답 본문은 담지 않는다.
`header.resultMessage`도 쓰지 않는다 — 사람용이고 예고 없이 바뀐다.
"""

from __future__ import annotations


class DoorayError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 한국어 문장 하나를 가진다."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigError(DoorayError):
    """허용되지 않은 API 주소, 깨진 settings.json."""


class TokenMissingError(DoorayError):
    """환경변수에도 키체인에도 토큰이 없다."""


class NetworkError(DoorayError):
    """연결 실패, 타임아웃."""


class CallLimitError(DoorayError):
    """호출 상한 초과."""

    def __init__(self, limit: int, used: int) -> None:
        super().__init__(CALL_LIMIT_MESSAGE.format(limit=limit))
        self.limit = limit
        self.used = used


class WriteNotAllowedError(DoorayError):
    """읽기 전용 전송 계층에 GET 아닌 요청이 들어왔다."""


class InvalidIdError(DoorayError):
    """ID나 경로가 우리가 조립한 모양이 아니다. 요청을 보내지 않았다."""


class ApiError(DoorayError):
    """두레이가 응답했으나 성공이 아니다."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class AuthError(ApiError):
    """401."""


class ForbiddenError(ApiError):
    """403. 내장 PermissionError와 이름이 겹치지 않게 지었다."""


class NotFoundError(ApiError):
    """404."""


class RateLimitError(ApiError):
    """429. 재시도를 다 쓴 뒤."""


class ServerError(ApiError):
    """5xx. 재시도를 다 쓴 뒤."""


class RedirectError(ApiError):
    """3xx. 따라가지 않았다."""


# --- §2-1 상태별 문장 -------------------------------------------------------

AUTH_MESSAGE = (
    "두레이 토큰이 유효하지 않습니다. 개인설정 > API에서 토큰을 다시 발급한 뒤 "
    "DOORAY_API_TOKEN 환경변수나 키체인에 넣어 주세요. "
    "Claude Desktop 확장으로 설치했다면 확장 설정의 토큰 칸에 다시 넣어 주세요."
)
FORBIDDEN_MESSAGE = (
    "이 항목에 접근할 권한이 없습니다. 프로젝트 멤버인지 확인해 주세요. "
    "사무실 밖에서 접속 중이면 IP 접근 제어에 걸렸을 수 있습니다."
)
NOT_FOUND_MESSAGE = (
    "요청한 항목을 찾을 수 없습니다(상태 404). 업무 번호를 확인해 주세요. "
    "권한이 없는 항목도 같은 응답이 옵니다."
)
RATE_LIMIT_MESSAGE = "두레이 요청 제한에 걸렸습니다. 잠시 후 다시 시도해 주세요."
SERVER_MESSAGE = "두레이 서버가 일시적으로 응답하지 못했습니다(상태 {status}). 잠시 후 다시 시도해 주세요."
REDIRECT_MESSAGE = (
    "두레이가 다른 주소로 이동을 요청했습니다(상태 {status}). "
    "토큰이 다른 호스트로 가지 않도록 따라가지 않았습니다. API 주소 설정을 확인해 주세요."
)
OTHER_CLIENT_MESSAGE = "두레이가 요청을 거절했습니다(상태 {status}). 조회 조건을 확인해 주세요."
NOT_JSON_MESSAGE = "두레이 응답을 해석하지 못했습니다(상태 {status})."
NETWORK_MESSAGE = "두레이에 연결하지 못했습니다. 네트워크 연결을 확인해 주세요."
CALL_LIMIT_MESSAGE = "API 호출 상한 {limit}회를 넘었습니다. 조회 범위를 좁혀 다시 시도해 주세요."
WRITE_NOT_ALLOWED_MESSAGE = (
    "읽기 전용 연결에서는 쓰기 요청을 보낼 수 없습니다. 이 요청은 두레이로 나가지 않았습니다."
)
TOKEN_MISSING_MESSAGE = (
    "두레이 토큰을 찾지 못했습니다. DOORAY_API_TOKEN 환경변수를 설정하거나 "
    "macOS 키체인에 서비스 이름 dooray-bridge로 저장해 주세요. "
    "Claude Desktop 확장으로 설치했다면 확장 설정의 토큰 칸에 넣어 주세요."
)
# 받은 값을 되풀이하지 않는다 — 토큰을 주소 칸에 잘못 넣으면 그 토큰이 오류 문장으로 대화에 실린다 (005 리뷰 H1).
API_BASE_NOT_ALLOWED_MESSAGE = (
    "허용되지 않은 두레이 API 주소입니다. 다음 중 하나여야 합니다: {allowed}"
)
WEB_BASE_NOT_ALLOWED_MESSAGE = (
    "허용되지 않은 두레이 웹 주소입니다. "
    "https 주소여야 하고 호스트가 다음 중 하나로 끝나야 합니다: {allowed}. "
    "호스트는 영문·숫자·하이픈으로 된 이름이고, 앞뒤에 아무것도 붙이지 않습니다"
    "(사용자 이름·포트·경로·?·# 없이)."
)
INVALID_ID_MESSAGE = (
    "ID 형식이 올바르지 않습니다. 영문·숫자·밑줄·하이픈만 쓸 수 있습니다. 이 요청은 두레이로 나가지 않았습니다."
)
WRITE_PATH_NOT_ALLOWED_MESSAGE = (
    "이 도구가 보낼 수 있는 쓰기는 정해진 것(댓글 등록, 업무 만들기, 상위 업무 지정, 업무·댓글 고치기)뿐입니다. "
    "이 요청은 두레이로 나가지 않았습니다."
)
HOME_NOT_ABSOLUTE_MESSAGE = "DOORAY_BRIDGE_HOME은 절대 경로여야 합니다."
SETTINGS_BROKEN_MESSAGE = "설정 파일을 읽지 못했습니다: {path}"
SETTINGS_NOT_OBJECT_MESSAGE = "설정 파일의 최상위가 객체가 아닙니다: {path}"


def api_message(status: int) -> str:
    """HTTP 상태 하나를 §2-1 표의 문장으로 바꾼다."""
    if status == 401:
        return AUTH_MESSAGE
    if status == 403:
        return FORBIDDEN_MESSAGE
    if status == 404:
        return NOT_FOUND_MESSAGE
    if status == 429:
        return RATE_LIMIT_MESSAGE
    if 300 <= status < 400:
        return REDIRECT_MESSAGE.format(status=status)
    if 500 <= status:
        return SERVER_MESSAGE.format(status=status)
    return OTHER_CLIENT_MESSAGE.format(status=status)


def not_json_message(status: int) -> str:
    """2xx인데 본문이 JSON이 아닐 때의 문장."""
    return NOT_JSON_MESSAGE.format(status=status)
