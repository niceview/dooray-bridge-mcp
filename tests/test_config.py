"""spec §3(`config.py`) — §11-3의 C 케이스."""

from __future__ import annotations

import json
import os
import stat
from unittest import mock

from dooray_core import config
from dooray_core.errors import ConfigError, TokenMissingError
from support import FAKE_TOKEN, CoreTestCase


class PathsTest(CoreTestCase):
    def test_paths_are_absolute(self) -> None:
        p = config.paths()
        self.assertEqual(p.home, self.home)
        for value in (p.home, p.settings_file, p.logs_dir, p.state_dir):
            self.assertTrue(value.is_absolute())

    def test_C14_ensure_home_makes_two_subdirs_and_no_cache(self) -> None:
        p = config.ensure_home()
        made = sorted(child.name for child in self.home.iterdir() if child.is_dir())
        # 초안은 어디에도 저장하지 않는다 — `drafts/`를 만들지 않는다 (004 spec §6-1).
        self.assertEqual(made, ["logs", "state"])
        self.assertNotIn("cache", made)
        self.assertFalse(hasattr(p, "cache_dir"))
        self.assertFalse(hasattr(p, "drafts_dir"))

    def test_C2_home_is_0700(self) -> None:
        p = config.ensure_home()
        for folder in (p.home, p.logs_dir, p.state_dir):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700, folder.name)

    def test_C2_new_home_and_subdirs_are_0700(self) -> None:
        fresh = self.home / "fresh"
        os.environ["DOORAY_BRIDGE_HOME"] = str(fresh)
        p = config.ensure_home()
        for folder in (p.home, p.logs_dir, p.state_dir):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700, folder.name)

    def test_C2_an_existing_folder_keeps_its_permissions(self) -> None:
        # spec §3-1: 이미 있는 폴더의 권한은 바꾸지 않는다.
        # DOORAY_BRIDGE_HOME이 ~/Documents 같은 기존 폴더를 가리켜도 0o700으로 바꾸지 않는다.
        self.home.chmod(0o755)
        existing_logs = self.home / "logs"
        existing_logs.mkdir(mode=0o755)
        existing_logs.chmod(0o755)

        config.ensure_home()

        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(existing_logs.stat().st_mode), 0o755)
        # 새로 만든 하위 폴더는 그대로 0o700이다.
        self.assertEqual(stat.S_IMODE((self.home / "state").stat().st_mode), 0o700)

    def test_C2_settings_file_is_0600(self) -> None:
        config.save_settings({"reply_style": "정중하게"})
        mode = stat.S_IMODE(config.paths().settings_file.stat().st_mode)
        self.assertEqual(mode, 0o600)


class SettingsTest(CoreTestCase):
    def write_settings(self, data: dict[str, object]) -> None:
        config.ensure_home()
        config.paths().settings_file.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def test_C1_defaults_when_nothing_is_set(self) -> None:
        settings = config.load_settings()
        self.assertEqual(settings.api_base, config.ALLOWED_API_BASES[0])
        self.assertIsNone(settings.web_base)
        self.assertEqual(settings.projects, ())
        self.assertIsNone(settings.reply_style)

    def test_C1_settings_file_beats_default(self) -> None:
        self.write_settings(
            {
                "api_base": config.ALLOWED_API_BASES[1],
                "web_base": "https://acme.dooray.com",
                "projects": ["sample", "other"],
            }
        )
        settings = config.load_settings()
        self.assertEqual(settings.api_base, config.ALLOWED_API_BASES[1])
        self.assertEqual(settings.web_base, "https://acme.dooray.com")
        self.assertEqual(settings.projects, ("sample", "other"))

    def test_C1_env_beats_settings_file(self) -> None:
        self.write_settings(
            {
                "api_base": config.ALLOWED_API_BASES[1],
                "web_base": "https://acme.dooray.com",
                "projects": ["sample"],
            }
        )
        os.environ["DOORAY_API_BASE"] = config.ALLOWED_API_BASES[2]
        os.environ["DOORAY_WEB_BASE"] = "https://other.gov-dooray.com"
        os.environ["DOORAY_PROJECTS"] = " alpha , beta "
        settings = config.load_settings()
        self.assertEqual(settings.api_base, config.ALLOWED_API_BASES[2])
        self.assertEqual(settings.web_base, "https://other.gov-dooray.com")
        self.assertEqual(settings.projects, ("alpha", "beta"))

    def test_C1_blank_env_counts_as_unset(self) -> None:
        self.write_settings({"api_base": config.ALLOWED_API_BASES[1]})
        os.environ["DOORAY_API_BASE"] = "   "
        self.assertEqual(config.load_settings().api_base, config.ALLOWED_API_BASES[1])

    def test_C1_api_base_trailing_slash_is_stripped(self) -> None:
        os.environ["DOORAY_API_BASE"] = config.ALLOWED_API_BASES[0] + "/"
        self.assertEqual(config.load_settings().api_base, config.ALLOWED_API_BASES[0])

    def test_C1_api_base_outside_the_list_is_rejected(self) -> None:
        os.environ["DOORAY_API_BASE"] = "https://api.example.com"
        with self.assertRaises(ConfigError) as caught:
            config.load_settings()
        self.record(caught.exception)
        # 받은 값을 되풀이하지 않는다 (005 리뷰 H1). 무엇이 틀렸는지는 고정 문장이 알린다.
        self.assertNotIn("api.example.com", str(caught.exception))
        self.assertTrue(str(caught.exception).startswith("허용되지 않은 두레이 API 주소입니다."))

    def test_C3_broken_settings_file_raises_config_error(self) -> None:
        config.ensure_home()
        config.paths().settings_file.write_text("{ 이건 JSON이 아니다", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            config.load_settings()
        self.record(caught.exception)
        self.assertIn("settings.json", str(caught.exception))

    def test_C3_top_level_array_raises_config_error(self) -> None:
        self.write_settings({})
        config.paths().settings_file.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ConfigError):
            config.load_settings()

    def test_C8_save_keeps_unknown_keys(self) -> None:
        self.write_settings({"unknown_key": 5, "reply_style": "정중하게"})
        config.save_settings({"reply_style": "간결하게"})
        stored = json.loads(config.paths().settings_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["unknown_key"], 5)
        self.assertEqual(stored["reply_style"], "간결하게")

    def test_C8_save_leaves_no_temp_file(self) -> None:
        config.save_settings({"reply_style": "정중하게"})
        self.assertEqual(self.home_files(), ["settings.json"])

    def test_C8_save_returns_new_settings(self) -> None:
        settings = config.save_settings({"web_base": "https://acme.dooray.com"})
        self.assertEqual(settings.web_base, "https://acme.dooray.com")

    def test_C9_reply_style_unset_is_none(self) -> None:
        self.assertIsNone(config.load_settings().reply_style)
        config.save_settings({"web_base": "https://acme.dooray.com"})
        stored = json.loads(config.paths().settings_file.read_text(encoding="utf-8"))
        self.assertNotIn("reply_style", stored)
        self.assertIsNone(config.load_settings().reply_style)

    def test_C13_web_base_allowed_suffix_passes_and_loses_trailing_slash(self) -> None:
        for value in (
            "https://acme.dooray.com/",
            "https://x.gov-dooray.com",
            "https://x.gov-dooray.co.kr",
            "https://x.dooray.co.kr",
        ):
            with self.subTest(value=value):
                os.environ["DOORAY_WEB_BASE"] = value
                self.assertEqual(config.load_settings().web_base, value.rstrip("/"))

    def test_C13_web_base_is_rebuilt_from_the_checked_parts(self) -> None:
        # 검증한 값을 그대로 돌려준다 — urlsplit이 정규화한 소문자 스킴과 hostname으로 다시 조립한다.
        for value, expected in (
            ("HTTPS://Acme.Dooray.COM", "https://acme.dooray.com"),
            ("https://ACME.dooray.com/", "https://acme.dooray.com"),
        ):
            with self.subTest(value=value):
                os.environ["DOORAY_WEB_BASE"] = value
                self.assertEqual(config.load_settings().web_base, expected)

    def test_C15_web_base_host_must_be_a_plain_hostname(self) -> None:
        # `urlsplit`은 역슬래시를 호스트의 일반 문자로 읽지만 브라우저는 `/`로 읽는다.
        # `https://evil.example\\.dooray.com`은 접미사 검사를 통과하고 브라우저에서는 evil.example로 간다
        # (리뷰 M1에서 재현). 호스트 모양을 직접 못 박는다.
        for bad in (
            "https://evil.example\\.dooray.com",
            "https://evil.example%2f.dooray.com",
            "https://.dooray.com",
            "https://-x.dooray.com",
            "https://x..dooray.com",
            "https://dooray.com",
            "https://x.dooray.com.",
            "https://x_y.dooray.com",
            "https://한글.dooray.com",
        ):
            with self.subTest(bad=bad):
                with mock.patch.dict(os.environ, {"DOORAY_WEB_BASE": bad}):
                    with self.assertRaises(ConfigError):
                        config.load_settings()

    def test_C16_a_relative_home_is_refused(self) -> None:
        # MCP 서버는 아무 폴더에서나 뜬다. 상대 경로 홈은 그날 열린 폴더마다 다른 곳을 가리킨다 (리뷰 L4).
        with mock.patch.dict(os.environ, {"DOORAY_BRIDGE_HOME": "./somewhere"}):
            with self.assertRaises(ConfigError):
                config.paths()

    def test_C13_web_base_rejects_bad_values(self) -> None:
        bad = [
            "https://evil.example",
            "http://acme.dooray.com",
            "https://acme.dooray.com/project",
            "https://acme.dooray.com?a=1",
            "https://acme.dooray.com#x",
            "https://user@acme.dooray.com",
            "https://acme.dooray.com:8443",
            "https://evil.example/.dooray.com",
            "https://dooray.com.evil.example",
            "acme.dooray.com",
        ]
        for value in bad:
            with self.subTest(value=value):
                os.environ["DOORAY_WEB_BASE"] = value
                with self.assertRaises(ConfigError):
                    config.load_settings()

    def test_C17_a_token_in_an_address_field_is_not_repeated(self) -> None:
        # 005 리뷰 H1 — 번들 설치 화면에서 토큰을 웹 주소 칸에 잘못 붙이면 그 토큰이 오류 문장으로
        # 대화에 실렸다. 거절 문장은 받은 값을 되풀이하지 않는다. tearDown이 `recorded`·파일·stderr·로그를 본다.
        shapes = (
            FAKE_TOKEN,
            f"https://{FAKE_TOKEN}",
            f"https://{FAKE_TOKEN}.dooray.com.evil",
            f"https://x.dooray.com/{FAKE_TOKEN}",
            f"https://{FAKE_TOKEN}@x.dooray.com",
        )
        for name in ("DOORAY_WEB_BASE", "DOORAY_API_BASE"):
            for value in shapes:
                with self.subTest(env=name, value=value):
                    with mock.patch.dict(os.environ, {name: value}):
                        with self.assertRaises(ConfigError) as caught:
                            config.load_settings()
                    exc = self.record(caught.exception)
                    self.assertNotIn(FAKE_TOKEN, str(exc))
                    self.assertNotIn(FAKE_TOKEN, repr(exc))
                    self.assertTrue(str(exc).startswith("허용되지 않은 두레이 "), str(exc))

    def test_C17_a_token_in_the_settings_file_is_not_repeated_either(self) -> None:
        for key in ("web_base", "api_base"):
            with self.subTest(key=key):
                self.write_settings({key: FAKE_TOKEN})
                with self.assertRaises(ConfigError) as caught:
                    config.load_settings()
                self.assertNotIn(FAKE_TOKEN, str(self.record(caught.exception)))
                config.paths().settings_file.unlink()

    def test_write_enabled_only_on_exactly_one(self) -> None:
        self.assertFalse(config.write_enabled())
        for value, expected in (("1", True), ("0", False), ("true", False), ("", False)):
            with self.subTest(value=value):
                os.environ["DOORAY_ENABLE_WRITE"] = value
                self.assertIs(config.write_enabled(), expected)

    def test_SW1_only_exactly_one_drops_the_ask_mark(self) -> None:
        # 021 spec §2-1 — 부정형 스위치. 빼는 값은 `1` 하나(앞뒤 공백 무시). 그 밖은 모두 묻는다.
        self.assertNotIn("DOORAY_SKIP_WRITE_ASK", os.environ)
        self.assertIs(config.ask_every_write(), True)
        asks = (
            "", " ", "0", "true", "True", "TRUE", "yes", "on", "false",
            "1 1", "11", "01", "１", "${user_config.skip_write_ask}",
        )
        for value in asks:
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"DOORAY_SKIP_WRITE_ASK": value}):
                    self.assertIs(config.ask_every_write(), True)
        for value in ("1", " 1 ", " 1\n"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"DOORAY_SKIP_WRITE_ASK": value}):
                    self.assertIs(config.ask_every_write(), False)
        # 다른 이름·설정 파일로는 빠지지 않는다 (§4 c).
        with mock.patch.dict(os.environ, {"DOORAY_SKIP_ASK": "1"}):
            self.assertIs(config.ask_every_write(), True)
        self.write_settings({"skip_write_ask": "1"})
        self.assertIs(config.ask_every_write(), True)


class WriteAtomicTest(CoreTestCase):
    def test_write_atomic_sets_mode_and_replaces(self) -> None:
        config.ensure_home()
        target = config.paths().settings_file
        config.write_atomic(target, '{"a": 1}\n')
        config.write_atomic(target, '{"a": 2}\n')
        self.assertEqual(target.read_text(encoding="utf-8"), '{"a": 2}\n')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(self.home_files(), ["settings.json"])

    def test_write_atomic_removes_temp_file_on_failure(self) -> None:
        config.ensure_home()
        target = config.paths().settings_file
        with mock.patch.object(config.os, "replace", side_effect=OSError("실패")):
            with self.assertRaises(OSError):
                config.write_atomic(target, "내용")
        self.assertEqual(self.home_files(), [])


class TokenTest(CoreTestCase):
    def test_C4_environment_variable_wins(self) -> None:
        self.assertEqual(config.read_token(), FAKE_TOKEN)

    def test_C4_keychain_is_used_when_environment_is_empty(self) -> None:
        del os.environ["DOORAY_API_TOKEN"]
        calls: list[list[str]] = []

        def runner(argv: list[str]) -> str:
            calls.append(argv)
            return "keychain-token-abc\n"

        self.assertEqual(config.read_token(runner), "keychain-token-abc")
        self.assertEqual(len(calls), 1)

    def test_C4_missing_everywhere_raises_token_missing(self) -> None:
        del os.environ["DOORAY_API_TOKEN"]

        def runner(argv: list[str]) -> str:
            return ""

        with self.assertRaises(TokenMissingError) as caught:
            config.read_token(runner)
        message = str(self.record(caught.exception))
        self.assertIn("DOORAY_API_TOKEN", message)
        self.assertIn("dooray-bridge", message)

    def test_C5_keychain_argv_has_no_token(self) -> None:
        del os.environ["DOORAY_API_TOKEN"]
        seen: list[list[str]] = []

        def runner(argv: list[str]) -> str:
            seen.append(argv)
            return "keychain-token-abc"

        config.read_token(runner)
        flat = " ".join(seen[0])
        self.assertNotIn(FAKE_TOKEN, flat)
        self.assertNotIn("keychain-token-abc", flat)
        self.assertIn("find-generic-password", flat)
        self.assertIn("dooray-bridge", flat)
        self.assertIn("api-token", flat)

    def test_C6_keychain_failure_hides_stdout_and_stderr(self) -> None:
        del os.environ["DOORAY_API_TOKEN"]
        secret = "표준출력에-섞인-비밀-3910"

        def runner(argv: list[str]) -> str:
            raise RuntimeError(secret)

        with self.assertRaises(TokenMissingError) as caught:
            config.read_token(runner)
        text = self.record(caught.exception)
        self.assertNotIn(secret, str(text))
        # 원인 사슬이 끊겨 있어야 트레이스백에 stdout·stderr가 딸려 오지 않는다 (spec §2-2).
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_C7_non_darwin_skips_the_keychain(self) -> None:
        del os.environ["DOORAY_API_TOKEN"]
        calls: list[list[str]] = []

        def runner(argv: list[str]) -> str:
            calls.append(argv)
            return "keychain-token-abc"

        with mock.patch.object(config.sys, "platform", "linux"):
            with self.assertRaises(TokenMissingError):
                config.read_token(runner)
        self.assertEqual(calls, [])

    def test_blank_token_environment_variable_falls_through(self) -> None:
        os.environ["DOORAY_API_TOKEN"] = "   "

        def runner(argv: list[str]) -> str:
            return "keychain-token-abc"

        self.assertEqual(config.read_token(runner), "keychain-token-abc")
