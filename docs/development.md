# 개발 문서

독자는 개발자다. 명령을 친다. 사용자용 설치는 `README.md`, 신뢰 판단 자료는 `SECURITY.md`다.

## 1. 이 문서와 다른 문서

- **사람이 하는 절차의 정본은 이 문서다**: 준비, 토큰, 등록, 시험, 번들 만들기, 공개 저장소에 내보내기(10절).
- `README.md`는 사용자용이다. 터미널 명령이 없다. `SECURITY.md`는 둘이 함께 읽는다.
- 공개 저장소(`niceview/dooray-bridge-mcp`)에는 코드·테스트·이 세 문서만 있다. 결정 기록(ADR), 두레이 API와 클라이언트·SDK의 실측 기록(`api-notes`·`mcp-notes`), Claude용 규칙(`CLAUDE.md`)은 비공개 개발 저장소에 있다. 이 문서가 그것들을 가리키면 그 기록이다.

## 2. 준비

```
uv sync                                          의존성 설치 (Python 3.14는 uv가 받아 온다)
uv run python -m unittest discover -s tests      전체 테스트 (작업 완료 기준)
uv run python -m unittest discover -s tests -p 'test_view.py'   테스트 파일 하나만
```

테스트 파일 하나를 `-m unittest tests.test_view`로 돌리면 `support`를 못 찾는다. 위의 `-p`를 쓴다.
테스트는 네트워크를 타지 않는다. 가짜 토큰과 임시 홈 폴더로 돈다.

MCP 서버 실행 파일은 `uv sync`가 만든 `<저장소 절대 경로>/.venv/bin/dooray-mcp`다. 셸에서 직접 띄우지 않는다 — stdio 서버라 입력을 기다리며 멈춘다.

**이 PC의 키체인에는 실제 토큰이 있다.** 테스트 장치 밖에서 도구·Reader·Writer를 부르면 실제 API로 나가고 `~/.dooray-bridge/`에 기록한다. 임시 실행에는 앞에 가짜 값을 붙인다(환경변수가 키체인보다 먼저다):

```
DOORAY_API_TOKEN=fake-token DOORAY_BRIDGE_HOME=<절대 경로> uv run python -c '…'
```

## 3. 토큰을 키체인에

개발용 설치(터미널 CLI 등록)는 토큰을 macOS 키체인에서 읽는다. 서비스 `dooray-bridge`, 계정 `api-token`.

처음 넣을 때:

```
security add-generic-password -s dooray-bridge -a api-token -w
```

바꿀 때:

```
security add-generic-password -U -s dooray-bridge -a api-token -w
```

- `-w`를 **끝에 두고 값을 주지 않는다.** 그러면 입력을 물어본다. 값이 셸 기록과 프로세스 목록에 남지 않는다.
- `-A`(모든 앱에 허용)를 쓰지 않는다.
- 토큰을 셸 환경변수로 주지 않는다. 클라이언트 앱이 띄운 서버는 셸의 환경변수를 받지 못할 수 있다(mcp-notes §9-5).

<!-- 실측 대기: M18 — 값을 물을 때 입력이 화면에 보이는지, 확인 입력을 한 번 더 받는지 시험용 서비스 이름으로 확인하면 여기 적는다 -->

## 4. Claude Code(터미널 CLI) 등록

개발자가 직접 친다. Claude는 명령을 제시만 한다 — 사용자 설정(`~/.claude.json`)을 바꾸는 일이다.

```
claude mcp add -s user dooray-bridge -e DOORAY_WEB_BASE=https://회사이름.dooray.com -e DOORAY_ENABLE_WRITE=1 -- <저장소 절대 경로>/.venv/bin/dooray-mcp
```

- 이름(`dooray-bridge`)을 `-e`보다 **앞에** 둔다. 뒤에 두면 `-e`가 이름까지 삼킨다(mcp-notes §6-1).
- 명령은 절대 경로다. 서버는 클라이언트가 아무 폴더에서나 띄운다.
- 토큰은 `-e`로 주지 않는다. 키체인(3절)에서 읽는다.
- 읽기 전용으로 쓰려면 `-e DOORAY_ENABLE_WRITE=1`을 뺀다. 쓰기 도구가 등록되지 않는다.
- 개발 중 실제 호출을 검증할 때는 실제 사용 폴더와 섞지 않게 `-e DOORAY_BRIDGE_HOME=<저장소 절대 경로>/.dev-home`을 더한다(절대 경로여야 한다).

## 5. 코드를 고친 뒤

떠 있는 서버 프로세스에는 반영되지 않는다.
- 터미널 CLI: `/mcp` 메뉴의 Reconnect.
- 데스크톱 앱(Code 탭): 다시 연결이 없다. 새 세션을 연다.
- Claude Desktop 확장: 번들을 다시 만들어 다시 설치한다(7절).

`settings.json`은 도구 호출마다 새로 읽으므로 고쳐도 재시작이 필요 없다.

## 6. 등록 시험

- **헤드리스 시험은 읽기와 "쓰기 전에 멈춤"까지다.** 방법의 정본은 `CLAUDE.md` "환경 특성"의 헤드리스 문단이다.
- **쓰기 도구는 헤드리스(`dontAsk`)에서 거절된다.** 쓰기 도구 여덟과 `set_reply_style`에 "매번 허락" 표시가 붙어 있기 때문이다(ADR 009). 허용 목록에 넣어도 거절되고 서버에 닿지 않는다(005 M8 실측).
- **쓰기 호출 시험은 사람이 허락 창을 누른다.** 실제 쓰기는 지정된 시험 업무에만 한다.
- **표시가 살아 있는지는 쓰기 없는 탐침으로 본다**: `set_parent_post`에 옮길 업무와 상위 업무를 **같은 업무**로 준다. 허락하면 서버가 대상을 읽은 뒤 "옮길 업무와 상위 업무가 같습니다."로 돌아오고 쓰기 요청은 0회다.
  확인: 그날 감사 로그에 `set_parent_post` 도구 줄이 있고, `"method": "POST"`인 줄이 없다. 거절했다면 도구 줄도 없다.
  같은 식으로 `set_post_status`에 지금 상태를 주면 `STATUS_ALREADY`로 돌아오고 쓰기는 0회다.
- 서버 로직(게이트, 인자 검사, 전송 계층 제한, 반환 문장)은 프로세스 안 시험 장치(`tests/test_mcp_server.py`의 `ServerCase`)가 증명한다. 도구를 프로세스 안에서 돌려 보려면 그처럼 `server.build_server()` 뒤 `await mcp.call_tool(name, args)`를 쓴다.

## 7. 번들 만들기

```
uv lock --check
uv run python packaging/build.py
```

- `uv lock --check`는 `uv.lock`이 `pyproject.toml`과 맞는지 본다. 확장은 `--frozen`으로 돌아 낡은 잠금을 고치지 않으므로 빌드 전에 확인한다. 이 PC(uv 0.12.10)에서는 `--offline`을 붙여도 통과했다(2026-09-28).
- 산출물은 `dist/`(git 밖)에 하나: `dooray-bridge-<버전>.mcpb`(확장). 같은 입력이면 같은 바이트가 나온다 — `shasum -a 256 dist/*`로 두 빌드를 비교할 수 있다.
- 번들에는 허용 목록의 19개 파일만 들어간다(`packaging/build.py`, `tests/test_bundle.py`).
- 버전을 올리는 순서: `pyproject.toml`의 `version` → `uv lock` → `packaging/manifest.json`의 `version` → 전체 테스트 → `build.py`. 셋이 다르면 테스트(B4)가 실패하고 `build.py`가 멈춘다. `SECURITY.md` 첫머리의 버전도 같이 바꾼다(테스트 D10).
- 매니페스트의 `author.name`(`niceview`)은 확장 ID에 들어간다. 바꾸면 다른 확장이 된다 — 옛것을 지우고 새로 설치해야 한다.

**버전을 올리지 않은 번들은 덮어 설치되지 않는다**(005 실측): 같은 버전의 새 번들을 설치하면 설치 화면에는 새 매니페스트가 보이지만 설치된 파일은 바뀌지 않았다. **코드나 매니페스트를 한 글자라도 바꿨으면 버전을 올린다.** 설치 뒤에는 설치된 파일을 번들과 비교한다(`~/Library/Application Support/Claude/Claude Extensions/local.mcpb.niceview.dooray-bridge/`).

**설치 시험**: Claude Desktop 설정 > 확장 프로그램 > 고급 설정 > "확장 설치…"에서 `.mcpb`를 고른다. 설정 칸은 둘(토큰, 웹 주소).

**개발 PC에는 서버가 둘 뜬다**: 번들은 표시 이름 `Dooray Bridge`로 뜬다(도구 접두 `mcp__Dooray_Bridge__`). 개발 등록(`dooray-bridge`)과 이름이 달라 Code 탭에 둘 다 보이고, 모델이 어느 쪽을 부를지 고른다(005 실측 — 개발 등록 쪽이 답했다). 두 서버는 같은 `~/.dooray-bridge/`를 쓰고 속도 제한을 따로 센다. 설치 시험이 끝나면 하나를 끈다.

### `uv` — 없으면 Claude Desktop이 받는다

`server.type`이 `uv`인 확장은 Claude Desktop이 `uv`로 띄운다(ADR 013, mcp-notes §9-12).
- **매니페스트의 `command`는 쓰이지 않는다.** Claude Desktop이 자기가 찾은 `uv`에 `args`만 넘긴다. `args`는 `uv`의 인자여야 한다(`tests/test_bundle.py` B1). 설정 화면의 "명령어"는 매니페스트를 그대로 보여 줄 뿐이다 — 실제로 실행된 것은 `~/Library/Logs/Claude/main.log`의 `[MCP Launch] [UV Runtime] Launching:` 줄로 본다.
- Claude Desktop은 셸 설정을 읽지 않고 스스로 만든 검색 목록에서 `uv`를 찾는다. 그 목록에 `~/.local/bin`·`/opt/homebrew/bin`·`/usr/local/bin`이 있었다(005 실측, 개발 PC의 서버 로그). 있는 `uv`의 버전은 묻지 않는다 — 너무 오래돼 `--directory`·`--frozen`을 모르면 서버가 뜨지 않는다.
- **없으면 Claude Desktop이 받는다**(2.9939.2의 코드에서 읽었다 — 실측 전): `uv` 0.9.7을 GitHub 릴리스에서 받아 sha256을 대조하고 `~/Library/Application Support/Claude/uv-runtime/`에 둔다. 설치할 때 확장 폴더에서 `uv sync --quiet`도 돌린다.
- **0.9.7로 이 번들이 뜬다**(실측 2026-09-29, mcp-notes §9-12): 깨끗한 환경에서 `uv sync` → `uv run --frozen dooray-mcp` → 도구 12개.
- **확인하지 못한 것**: `uv`가 없는 Mac에서 앱이 실제로 `uv`를 받는 것. 다른 컴퓨터의 설치에서 본다. 막히면 그 Mac에 `uv`를 손으로 설치한다(아래).

손으로 설치할 때(막혔을 때만):

```
uv --version
brew install uv
```

Homebrew는 받은 파일을 대조한다. 공식 설치 스크립트(`curl … | sh`)는 `sha256sum`이 없는 Mac에서 실행 파일의 대조를 건너뛴다(설치 스크립트 0.12.10의 본문에서 읽었다) — 쓰지 않는다.

## 8. Codex 등록 예시

**시험하지 않았다.** Codex 문서(developers.openai.com/codex/mcp, …/codex/config-reference, 2026-09-28 확인)만 보고 쓴 예시다.

`~/.codex/config.toml`:

```toml
[mcp_servers.dooray-bridge]
command = "<저장소 절대 경로>/.venv/bin/dooray-mcp"
default_tools_approval_mode = "writes"

[mcp_servers.dooray-bridge.env]
DOORAY_WEB_BASE = "https://회사이름.dooray.com"
DOORAY_ENABLE_WRITE = "1"
```

- **토큰은 적지 않는다.** 서버가 키체인(3절)에서 읽는다. 셸의 환경변수를 넘기는 `env_vars`도 필요 없다.
- Codex는 "매번 허락" 표시(`anthropic/requiresUserInteraction`)를 모른다. 도구 승인 설정이 그 자리다.
  - `default_tools_approval_mode`·`tools.<도구>.approval_mode`의 값은 `auto`·`prompt`·`writes`·`approve`다.
  - 문서가 뜻을 풀어 적은 것은 `writes` 하나다: 읽기 전용 표시가 없는 도구에 묻는다. 우리 쓰기 도구 여덟과 `set_reply_style`에는 읽기 전용 표시가 없고, 읽기 아홉(`guide` 포함)에는 있다.
  - `writes`가 **매번** 묻는지, `prompt`·`approve`가 무슨 뜻인지는 문서에서 확인하지 못했다.
- 읽기 전용으로 쓰려면 `DOORAY_ENABLE_WRITE` 줄을 뺀다.

## 9. 개발 폴더

| 폴더·파일 | 쓰임 | git |
|---|---|---|
| `.dev-home/` | 개발 중 실제 호출 검증의 홈 폴더(`DOORAY_BRIDGE_HOME`) | 밖 |
| `scratch/` | 실측 스크립트, 개인정보를 지우기 전의 원본 응답 | 밖 |
| `.env` | 실측 스크립트용 토큰(권한 600). 제품 코드는 모른다. Claude는 읽지 않는다 | 밖 |
| `dist/` | 번들 산출물 | 밖 |
| `tests/fixtures/` | 개인정보를 지운 실제 응답 | 안 |

`.env`는 명령 안에서 `set -a; . ./.env; set +a`로만 불러 쓰고 값을 출력하지 않는다.
실측 응답을 `tests/fixtures/`에 넣기 전에 이름·이메일·ID·테넌트 정보를 지운다.

## 10. 공개 저장소와 릴리스

공개 저장소는 `niceview/dooray-bridge-mcp`다. 이 개발 저장소에서 **허용 목록의 파일만** 복사해 만든다 — 같은 파일 한 벌이고, 공개 쪽을 따로 고치지 않는다.

```
uv run python packaging/publish.py <공개 checkout 절대 경로>
```

- `packaging/publish.py`가 허용 목록(코드·테스트·`README.md`·`SECURITY.md`·`LICENSE`·이 문서·`pyproject.toml`·`uv.lock`·`.gitignore`·`packaging/manifest.json`·`packaging/build.py`)을 복사하고, 대상의 `.git` 밖 나머지를 지운다. `specs/`·`CLAUDE.md`·`docs/`의 다른 문서·`publish.py` 자신은 나가지 않는다.
- 복사 전에 내보낼 파일 전부에서 **금지어**(실제 테넌트·개인 코드·이름)를 찾고, 하나라도 있으면 파일과 줄 번호만 적고 멈춘다. 금지어 목록은 `publish.py` 안에만 있다. 걸리면 이 저장소에서 자리표시(`acme`·`@hong.gd`·`멤버N`)로 바꾼 뒤 다시 돌린다.
- 공개 checkout에서 `uv sync && uv run python -m unittest discover -s tests`가 **단독으로** 통과해야 한다. 그 뒤 커밋·푸시한다.

릴리스는 번들을 만든 뒤(7절) GitHub Releases에 올린다. 태그는 `v<버전>`, 첨부는 `.mcpb` 하나, 노트에 sha256을 적는다.

```
shasum -a 256 dist/dooray-bridge-<버전>.mcpb
gh release create v<버전> dist/dooray-bridge-<버전>.mcpb --repo niceview/dooray-bridge-mcp --title "dooray-bridge <버전>" --notes "sha256: <값>"
```

`README.md`의 설치 절은 Releases의 최신 릴리스를 가리킨다. 번들 없이 코드만 바뀐 커밋은 릴리스하지 않는다.
