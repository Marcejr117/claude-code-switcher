"""Regression tests use temporary profiles and never contact provider APIs."""
import copy
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

CSW_PATH = os.environ.get("CSW_BIN", str(Path(__file__).resolve().parent.parent / "csw"))
loader = importlib.machinery.SourceFileLoader("csw", CSW_PATH)
spec = importlib.util.spec_from_loader(loader.name, loader)
csw = importlib.util.module_from_spec(spec)
loader.exec_module(csw)


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="csw-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in {"ROOT": root, "LIVE": root / "settings.json",
                            "ACTIVE": root / ".settings-active", "NAMES": root / "settings.names",
                            "CONFIG_ROOT": root / "proxy-config",
                            "CODEX_NATIVE_CONFIG": root / "codex.config.toml",
                            "CODEX_NATIVE_CATALOG": root / "codex.models.json"}.items():
            p = patch.object(csw, name, value)
            p.start()
            self.addCleanup(p.stop)
        # Service assertions describe the systemd calls; Windows has its own test below.
        p = patch.object(csw.portable_module(), "WINDOWS", False)
        p.start()
        self.addCleanup(p.stop)
        self.direct = {
            "env": {"ANTHROPIC_AUTH_TOKEN": "test-deepseek-key", "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                    "ANTHROPIC_MODEL": "deepseek-v4-pro", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash", "SHARED_PATH": "/new/path"},
            "model": "sonnet", "effortLevel": "high", "hooks": {"SessionStart": ["new-hook"]},
            "permissions": {"deny": ["example"]}, "enabledPlugins": {"example": True},
            "modelSettings": {"keep": True}, "theme": "new",
        }
        self.native = {"env": {"SHARED_PATH": "/old/path"}, "model": "opus", "theme": "old"}
        self.proxy = {"env": {"ANTHROPIC_AUTH_TOKEN": "unused", "ANTHROPIC_BASE_URL": "http://127.0.0.1:18765",
                              "ANTHROPIC_MODEL": "gpt-5.6-sol", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "272000"},
                      "model": "gpt-5.6-sol"}
        for path, data in [(csw.LIVE, self.direct), (root / csw.ALIASES["deepseek"], self.direct),
                           (root / csw.ALIASES["claude"], self.native), (root / csw.ALIASES["codex"], self.proxy)]:
            csw.write_json(path, data)
        csw.atomic_write(csw.ACTIVE, csw.ALIASES["deepseek"] + "\n")
        csw.atomic_write(csw.NAMES, "settings.json.old\tSnapshot viejo\n")
        csw.write_json(root / ".credentials.json", {"claudeAiOauth": {
            "accessToken": "synthetic-native-access", "expiresAt": (csw.time.time() + 3600) * 1000}})
        (root / "model-catalogs").mkdir()
        for name, model in (("deepseek", "deepseek-v4-pro"), ("codex", "gpt-5.6-sol")):
            csw.write_json(csw.model_cache(csw.resolve(name)), {"source": "Synthetic fixture",
                "fetched_at": int(csw.time.time()), "models": [{"model": model, "label": model}]})
        no_external_sessions = patch.object(csw, "legacy_session_running", return_value=False)
        no_external_sessions.start()
        self.addCleanup(no_external_sessions.stop)

    def test_switch_preserves_shared_configuration_and_removes_credentials(self):
        csw.switch("claude")
        data = csw.read_json(csw.LIVE)
        for key in ("permissions", "enabledPlugins", "modelSettings", "theme"):
            self.assertEqual(data[key], self.direct[key])
        self.assertEqual(data["hooks"]["SessionStart"][0], "new-hook")
        self.assertEqual(data["env"]["SHARED_PATH"], "/new/path")
        self.assertEqual(data["env"]["ANTHROPIC_AUTH_TOKEN"], "")
        # The primary login is Claude Code's own: no pinned token or endpoint.
        self.assertEqual(data["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "")
        self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "")
        self.assertEqual(data["model"], "opus[1m]")
        self.assertEqual(csw.active(), csw.ALIASES["claude"])

    def test_republishing_primary_claude_drops_a_previously_pinned_token(self):
        csw.switch("claude")
        live = csw.read_json(csw.LIVE)
        live["env"].update(CLAUDE_CODE_OAUTH_TOKEN="old-pinned", CLAUDE_CODE_OAUTH_SCOPES="user:inference",
                           ANTHROPIC_BASE_URL="https://api.anthropic.com")
        csw.write_json(csw.LIVE, live)
        csw.switch("claude")
        env = csw.read_json(csw.LIVE)["env"]
        self.assertEqual((env["CLAUDE_CODE_OAUTH_TOKEN"], env["CLAUDE_CODE_OAUTH_SCOPES"], env["ANTHROPIC_BASE_URL"]),
                         ("", "", ""))

    def test_claude_account_switch_swaps_the_native_login_and_pins_nothing(self):
        # Every Claude account becomes the login Claude Code itself uses and renews:
        # a token pinned in settings.json expired and turned Claude in Chrome off.
        root = csw.ROOT
        csw.write_json(root / ".credentials.json", {"mcpOAuth": {"keep": 1}, "claudeAiOauth": {
            "accessToken": "main-access", "refreshToken": "main-refresh"}})
        csw.write_json(root / ".claude.json", {"userID": "keep", "oauthAccount": {"emailAddress": "main@example.invalid"}})
        csw.write_json(root / "settings.json.CLAUDE.work.bak", self.native)
        (root / "accounts/claude/work").mkdir(parents=True)
        csw.write_json(root / "accounts/claude/work/.credentials.json", {"claudeAiOauth": {
            "accessToken": "work-access", "refreshToken": "work-refresh"}})
        csw.write_json(root / "accounts/claude/work/.claude.json", {"oauthAccount": {"emailAddress": "work@example.invalid"}})

        csw.switch("claude:work")
        self.assertEqual(csw.load_json(root / ".credentials.json"), {"mcpOAuth": {"keep": 1}, "claudeAiOauth": {
            "accessToken": "work-access", "refreshToken": "work-refresh"}})
        self.assertEqual(csw.load_json(root / ".claude.json"),
                         {"userID": "keep", "oauthAccount": {"emailAddress": "work@example.invalid"}})
        env = csw.read_json(csw.LIVE)["env"]
        self.assertEqual((env["CLAUDE_CODE_OAUTH_TOKEN"], env["CLAUDE_CODE_OAUTH_SCOPES"], env["ANTHROPIC_BASE_URL"]), ("", "", ""))
        self.assertEqual(csw.native_environment(csw.resolve("claude:work"))["CLAUDE_CONFIG_DIR"], str(root))  # login and run use ROOT now

        # Claude renewed the work login while it was selected; another provider in between changes nothing.
        csw.write_json(root / ".credentials.json", {"claudeAiOauth": {"accessToken": "work-renewed", "refreshToken": "work-refresh-2"}})
        csw.switch("deepseek")
        self.assertEqual(csw.claude_login(), "settings.json.CLAUDE.work.bak")
        csw.switch("claude")
        self.assertEqual(csw.load_json(root / ".credentials.json")["claudeAiOauth"]["refreshToken"], "main-refresh")
        self.assertEqual(csw.load_json(root / ".claude.json")["oauthAccount"]["emailAddress"], "main@example.invalid")
        self.assertEqual(csw.load_json(root / "accounts/claude/work/.credentials.json")["claudeAiOauth"]["refreshToken"], "work-refresh-2")
        self.assertTrue(Path(csw.native_environment(csw.resolve("claude:work"))["CLAUDE_CONFIG_DIR"]).as_posix().endswith("accounts/claude/work"))

    def test_claude_account_without_login_changes_nothing(self):
        csw.write_json(csw.ROOT / "settings.json.CLAUDE.work.bak", self.native)
        before = (csw.LIVE.read_bytes(), (csw.ROOT / ".credentials.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "csw login claude:work"):
            csw.switch("claude:work")
        self.assertEqual(before, (csw.LIVE.read_bytes(), (csw.ROOT / ".credentials.json").read_bytes()))
        self.assertEqual(csw.active(), csw.ALIASES["deepseek"])

    def test_other_providers_get_the_chrome_extension_as_mcp_server(self):
        config = csw.ROOT / ".claude.json"
        csw.write_json(config, {"mcpServers": {"mine": {"command": "x"}}})
        csw.switch("deepseek")
        servers = csw.load_json(config)["mcpServers"]
        self.assertEqual(servers["chrome"]["args"], ["--claude-in-chrome-mcp"])
        csw.switch("claude")  # native Claude has its own integration: no duplicate tools
        self.assertEqual(csw.load_json(config)["mcpServers"], {"mine": {"command": "x"}})
        with patch.object(csw, "run_child", return_value=0) as call:
            csw.run_claude(["deepseek", "--", "-p", "hello"])
            external = call.call_args.args[0]
            csw.run_claude(["claude", "--", "-p", "hello"])
        self.assertEqual(json.loads(next(a for a in external if a.startswith("--mcp-config=")).split("=", 1)[1]),
                         {"mcpServers": {"chrome": csw.chrome_server()}})
        self.assertEqual(external[-2:], ["-p", "hello"])
        self.assertFalse(any(a.startswith("--mcp-config") for a in call.call_args.args[0]))

    def test_proxy_providers_load_tool_schemas_upfront_and_claude_clears_it(self):
        with patch.object(csw, "check_proxy"):
            csw.switch("codex")
        env = csw.read_json(csw.LIVE)["env"]
        self.assertEqual((env["ENABLE_TOOL_SEARCH"], env["CLAUDE_CODE_AUTO_MODE_SERVER"]), ("false", "0"))
        csw.switch("claude")
        env = csw.read_json(csw.LIVE)["env"]
        self.assertEqual((env["ENABLE_TOOL_SEARCH"], env["CLAUDE_CODE_AUTO_MODE_SERVER"]), ("", ""))

    def test_short_names_resolve_when_unique(self):
        for name in ("CLAUDE.work", "CLAUDE.home", "GLM.work"):
            csw.write_json(csw.ROOT / f"settings.json.{name}.bak", self.native)
            csw.set_name(csw.ROOT / f"settings.json.{name}.bak", name)
        self.assertEqual(csw.resolve("home").name, "settings.json.CLAUDE.home.bak")
        self.assertEqual(csw.resolve("glm").name, "settings.json.GLM.work.bak")  # no primary glm profile exists
        with self.assertRaisesRegex(ValueError, "claude:work.*glm:work"):
            csw.resolve("work")
        with self.assertRaisesRegex(ValueError, "No existe"):
            csw.resolve("settings.json.nothing")

    def test_rename_moves_the_login_with_the_account(self):
        root = csw.ROOT
        csw.write_json(root / ".credentials.json", {"claudeAiOauth": {"refreshToken": "main-refresh"}})
        csw.write_json(root / "settings.json.CLAUDE.work.bak", self.native)
        (root / "accounts/claude/work").mkdir(parents=True)
        csw.write_json(root / "accounts/claude/work/.credentials.json", {"claudeAiOauth": {"refreshToken": "work-refresh"}})
        csw.switch("claude")
        csw.main(["rename", "claude", "first"])  # the selected primary account
        self.assertEqual((csw.active(), csw.claude_login()), ("settings.json.CLAUDE.first.bak",) * 2)
        self.assertEqual(csw.read_json(csw.LIVE)["env"]["CSW_PROFILE"], "claude:first")
        self.assertEqual(csw.names()["settings.json.CLAUDE.first.bak"], "Claude (first)")
        csw.main(["rename", "claude:work", "job"])  # a stored one
        csw.switch("claude:job")
        self.assertEqual(csw.load_json(root / ".credentials.json")["claudeAiOauth"]["refreshToken"], "work-refresh")
        csw.switch("first")
        self.assertEqual(csw.load_json(root / ".credentials.json")["claudeAiOauth"]["refreshToken"], "main-refresh")
        with self.assertRaisesRegex(ValueError, "Ya existe"):
            csw.rename_account("claude:job", "first")
        with self.assertRaisesRegex(ValueError, "Codex y Cursor"):
            csw.rename_account("codex", "other")

    def test_doctor_reports_only_existing_accounts(self):
        csw.write_json(csw.ROOT / "settings.json.CLAUDE.work.bak", self.native)
        csw.set_name(csw.ROOT / "settings.json.CLAUDE.work.bak", "Work")
        with patch.object(csw, "authenticated", return_value=True), patch.object(csw, "proxy_ready", return_value=False), \
                patch("builtins.print") as output:
            self.assertEqual(csw.doctor(), 1)
        lines = [call.args[0] for call in output.call_args_list]
        self.assertTrue(any(line.startswith("!!") and "csw login claude:work" in line for line in lines))
        self.assertEqual(sum(line.startswith("ok") for line in lines), 3)  # deepseek, claude, codex
        self.assertFalse(any("glm" in line or "cursor" in line for line in lines[:-1]))

    def test_a_user_defined_chrome_server_is_never_replaced_or_removed(self):
        config = csw.ROOT / ".claude.json"
        csw.write_json(config, {"mcpServers": {"chrome": {"command": "other"}}})
        csw.switch("deepseek")
        csw.switch("claude")
        self.assertEqual(csw.load_json(config)["mcpServers"], {"chrome": {"command": "other"}})

    def test_roundtrip_keeps_edits_and_correct_provider(self):
        csw.switch("claude")
        data = csw.read_json(csw.LIVE)
        data["theme"] = "edited"
        data["model"] = "haiku"
        csw.write_json(csw.LIVE, data)
        csw.switch("deepseek")
        self.assertEqual(csw.read_json(csw.LIVE)["theme"], "edited")
        self.assertEqual(csw.read_json(csw.LIVE)["env"]["ANTHROPIC_AUTH_TOKEN"], "test-deepseek-key")
        csw.switch("claude")
        self.assertEqual(csw.read_json(csw.LIVE)["model"], "haiku")
        self.assertEqual(csw.read_json(csw.LIVE)["env"]["ANTHROPIC_AUTH_TOKEN"], "")

    def test_switch_same_profile_does_not_write(self):
        csw.switch("deepseek")  # First selection prepares the reloadable settings.
        before = csw.LIVE.read_bytes()
        with patch.object(csw, "write_json", side_effect=AssertionError("unexpected write")):
            csw.switch("deepseek")
        self.assertEqual(before, csw.LIVE.read_bytes())

    def test_bad_json_never_changes_live_or_slot(self):
        csw.atomic_write(csw.ROOT / csw.ALIASES["claude"], "{bad json")
        before = csw.LIVE.read_bytes()
        with self.assertRaises(ValueError):
            csw.switch("claude")
        self.assertEqual(before, csw.LIVE.read_bytes())
        self.assertEqual(csw.active(), csw.ALIASES["deepseek"])

    def test_missing_proxy_auth_never_changes_live(self):
        before = csw.LIVE.read_bytes()
        with patch.object(csw, "authenticated", return_value=False), self.assertRaises(ValueError):
            csw.switch("codex")
        self.assertEqual(before, csw.LIVE.read_bytes())
        self.assertEqual(csw.active(), csw.ALIASES["deepseek"])

    def test_proxy_switch_replaces_token_and_context(self):
        with patch.object(csw, "authenticated", return_value=True), patch.object(csw, "proxy_ready", return_value=True):
            csw.switch("codex")
        env = csw.read_json(csw.LIVE)["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "unused")
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "272000")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "")

    def test_api_helper_and_cloud_auth_removed_for_native(self):
        current = copy.deepcopy(self.direct)
        current["apiKeyHelper"] = "old-helper"
        current["forceLoginMethod"] = "console"
        current["env"].update({"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_OAUTH_TOKEN": "old"})
        data = csw.merge_provider(current, self.native)
        self.assertNotIn("apiKeyHelper", data)
        self.assertNotIn("forceLoginMethod", data)
        self.assertEqual(data["env"], {"SHARED_PATH": "/new/path"})

    def test_private_atomic_files(self):
        csw.switch("claude")
        for path in (csw.LIVE, csw.ACTIVE, csw.ROOT / csw.ALIASES["deepseek"]):
            if os.name != "nt":  # POSIX modes only; Windows relies on the profile ACL
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(csw.ROOT.glob(".csw-*")), [])

    def test_rejects_paths_symlinks_and_bad_indices(self):
        for value in ("../elsewhere", "/tmp/profile", "settings.json.bad/name", "0", "999"):
            with self.assertRaises(ValueError):
                csw.resolve(value)
        path = csw.ROOT / "settings.json.link"
        path.symlink_to(csw.LIVE)
        with self.assertRaises(ValueError):
            csw.resolve(path.name)

    def test_primary_codex_short_name_is_a_working_account_alias(self):
        profile = csw.resolve("codex")
        csw.set_name(profile, "demoacc")
        self.assertEqual(csw.names()[profile.name], "Codex (demoacc)")
        self.assertEqual(csw.profile_label(profile), "codex:demoacc")
        self.assertEqual(csw.resolve("codex:demoacc"), profile)
        self.assertEqual(csw.resolve("codex"), profile)
        with patch("builtins.print") as output:
            csw.show_list()
        self.assertIn("codex:demoacc", "\n".join(call.args[0] for call in output.call_args_list))

    def test_save_does_not_overwrite(self):
        before = csw.LIVE.read_bytes()
        with self.assertRaises(ValueError):
            csw.main(["save", "DS.bak"])
        self.assertEqual(before, csw.LIVE.read_bytes())
        csw.main(["save", "custom", "Custom"])
        self.assertIn("settings.json.custom", csw.profiles())
        self.assertEqual(csw.active(), "settings.json.custom")

    def test_archive_listing_does_not_change_indices(self):
        csw.write_json(csw.ROOT / "settings.json.old", self.native)
        before = csw.profiles()
        self.assertNotIn("settings.json.old", before)
        self.assertIn("settings.json.old", csw.profiles(True))
        self.assertEqual(csw.resolve("1").name, before[0])

    def test_extra_accounts_have_separate_ports_and_credentials(self):
        before = csw.LIVE.read_bytes()
        with patch.object(csw, "login", return_value=0):
            csw.add_account("codex", "work")
            csw.add_account("codex", "personal")
        first = csw.read_json(csw.resolve("codex:work"))
        second = csw.read_json(csw.resolve("codex:personal"))
        self.assertNotEqual(first["env"]["ANTHROPIC_BASE_URL"], second["env"]["ANTHROPIC_BASE_URL"])
        _, env, unit = csw.proxy_context(first)
        self.assertTrue(Path(env["CCP_CONFIG_DIR"]).as_posix().endswith("accounts/codex-work"))
        self.assertEqual(unit, "claude-code-proxy@codex-work.service")
        self.assertEqual(before, csw.LIVE.read_bytes())
        if os.name != "nt":  # POSIX modes only; Windows relies on the profile ACL
            self.assertEqual((csw.CONFIG_ROOT / "accounts/codex-work").stat().st_mode & 0o777, 0o700)
        self.assertIn(csw.resolve("codex:work").name, csw.profiles())

    def test_account_relogin_selects_own_store(self):
        with patch.object(csw, "login", return_value=0):
            csw.add_account("codex", "work")
        with patch.object(csw.subprocess, "call", return_value=0) as call, patch.object(csw.subprocess, "run") as run:
            csw.login("codex:work")
        self.assertEqual(call.call_args.args[0], ["claude-code-proxy", "codex", "auth", "device"])
        self.assertTrue(Path(call.call_args.kwargs["env"]["CCP_CONFIG_DIR"]).as_posix().endswith("accounts/codex-work"))
        self.assertIn("claude-code-proxy@codex-work.service", run.call_args.args[0])

    def test_codex_browser_login_requires_explicit_option(self):
        with patch.object(csw.subprocess, "call", return_value=0) as call, patch.object(csw.subprocess, "run"):
            csw.login("codex", browser=True)
        self.assertEqual(call.call_args.args[0], ["claude-code-proxy", "codex", "auth", "login"])

    def test_add_codex_defaults_to_device_login(self):
        with patch.object(csw, "login", return_value=0) as login:
            csw.add_account("codex", "device_default")
        login.assert_called_once_with("codex:device_default", device=None, browser=False)

    def test_cli_codex_login_defaults_to_device_and_browser_is_opt_in(self):
        with patch.object(csw, "login", return_value=0) as login:
            csw.main(["login", "codex"])
            csw.main(["login", "codex", "--browser"])
        self.assertEqual(login.call_args_list[0].args, ("codex",))
        self.assertEqual(login.call_args_list[0].kwargs, {"device": False, "browser": False})
        self.assertEqual(login.call_args_list[1].kwargs, {"device": False, "browser": True})

    def test_extra_account_switch_starts_only_its_service(self):
        with patch.object(csw, "login", return_value=0):
            csw.add_account("codex", "work")
        csw.write_json(csw.model_cache(csw.resolve("codex:work")), csw.read_json(csw.model_cache(csw.resolve("codex"))))
        with patch.object(csw, "authenticated", return_value=True), patch.object(csw, "proxy_ready", side_effect=[False, True]), patch.object(csw.subprocess, "run") as run:
            csw.switch("codex:work")
        self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "start", "claude-code-proxy@codex-work.service"])

    def test_account_selection_ignores_inherited_cursor_token(self):
        with patch.dict(os.environ, {"CCP_CURSOR_AUTH_TOKEN": "wrong-account", "CURSOR_AUTH_TOKEN": "wrong"}):
            _, env, _ = csw.proxy_context(self.proxy)
        self.assertNotIn("CCP_CURSOR_AUTH_TOKEN", env)
        self.assertNotIn("CURSOR_AUTH_TOKEN", env)

    def test_native_login_does_not_switch_profile_or_inherit_api_auth(self):
        before = csw.LIVE.read_bytes()
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "wrong", "ANTHROPIC_BASE_URL": "https://example.invalid"}), patch.object(csw.subprocess, "call", return_value=0) as call:
            self.assertEqual(csw.main(["login", "claude"]), 0)
        self.assertEqual(call.call_args.args[0], [csw.CLAUDE_BIN, "--setting-sources", "", "auth", "login", "--claudeai"])
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", call.call_args.kwargs["env"])
        self.assertEqual(before, csw.LIVE.read_bytes())

    def test_native_accounts_never_share_credentials_or_oauth_metadata(self):
        csw.write_json(csw.ROOT / ".credentials.json", {"claudeAiOauth": {"accessToken": "main-only"}})
        (csw.ROOT / "skills").mkdir()
        (csw.ROOT / "workflows").mkdir()
        with patch.object(csw, "login", return_value=0):
            csw.add_account("claude", "max2")
            csw.add_account("claude", "max3")
        first = csw.resolve("claude:max2")
        second = csw.resolve("claude:max3")
        env1 = csw.native_environment(first)
        env2 = csw.native_environment(second)
        self.assertNotEqual(env1["CLAUDE_CONFIG_DIR"], env2["CLAUDE_CONFIG_DIR"])
        directory = Path(env1["CLAUDE_CONFIG_DIR"])
        self.assertFalse((directory / ".credentials.json").exists())
        self.assertEqual((directory / "skills").resolve(), csw.ROOT / "skills")
        self.assertEqual((directory / "workflows").resolve(), csw.ROOT / "workflows")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", csw.read_json(directory / "settings.json")["env"])
        self.assertEqual(csw.native_environment(csw.resolve("claude")).get("CLAUDE_CONFIG_DIR"), str(csw.ROOT))

    def test_add_api_account_stores_only_the_new_key_privately(self):
        with patch.object(csw.getpass, "getpass", return_value="new-key"):
            csw.add_account("deepseek", "second")
        profile = csw.resolve("deepseek:second")
        self.assertEqual(csw.read_json(profile)["env"]["ANTHROPIC_AUTH_TOKEN"], "new-key")
        if os.name != "nt":  # POSIX modes only; Windows relies on the profile ACL
            self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        self.assertEqual(csw.read_json(csw.LIVE)["env"]["ANTHROPIC_AUTH_TOKEN"], "test-deepseek-key")

    def test_empty_api_key_creates_nothing(self):
        with patch.object(csw.getpass, "getpass", return_value=""), self.assertRaises(ValueError):
            csw.add_account("deepseek", "empty")
        self.assertFalse((csw.ROOT / "settings.json.DEEPSEEK.empty.bak").exists())

    def test_add_orcarouter_account_targets_anthropic_endpoint_privately(self):
        with patch.object(csw.getpass, "getpass", return_value="orca-test-key"):
            csw.add_account("orcarouter", "principal")
        profile = csw.resolve("orcarouter:principal")
        data = csw.read_json(profile)
        self.assertEqual(csw.provider(data), "OrcaRouter")
        self.assertEqual(data["env"]["ANTHROPIC_AUTH_TOKEN"], "orca-test-key")
        self.assertEqual(csw.effective_model(data), "orcarouter/auto")
        self.assertEqual(csw.normalize_model("orcarouter/auto", data), "orcarouter/auto")
        listing = io.BytesIO(json.dumps({"data": [{"id": "anthropic/claude-sonnet-5"}, {"id": "orcarouter/auto"}],
                                         "has_more": False}).encode())
        with patch.object(csw.urllib.request, "urlopen", return_value=listing):
            models = [row["model"] for row in csw.model_catalog(profile, data)["models"]]
        self.assertEqual(models, ["orcarouter/auto", "anthropic/claude-sonnet-5"])
        if os.name != "nt":  # POSIX modes only; Windows relies on the profile ACL
            self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        self.assertEqual(csw.read_json(csw.LIVE)["env"]["ANTHROPIC_AUTH_TOKEN"], "test-deepseek-key")

    def test_add_accounts_on_fresh_install_uses_builtin_templates(self):
        for filename in set(csw.ALIASES.values()):
            (csw.ROOT / filename).unlink(missing_ok=True)
        for backend, url, model in (("glm", "https://api.z.ai/api/anthropic", "glm-5.3"),
                                    ("deepseek", "https://api.deepseek.com/anthropic", "deepseek-v4-pro")):
            with patch.object(csw.getpass, "getpass", return_value=f"fresh-{backend}-key"):
                csw.add_account(backend, "fresh")
            data = csw.read_json(csw.resolve(f"{backend}:fresh"))
            self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], url)
            self.assertEqual(data["env"]["ANTHROPIC_AUTH_TOKEN"], f"fresh-{backend}-key")
            self.assertEqual(csw.effective_model(data), model)
            self.assertEqual(csw.provider(data), "GLM" if backend == "glm" else "DeepSeek")
        base = csw.account_seed("codex")
        self.assertEqual(base["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:18765")
        self.assertEqual(base["env"]["ANTHROPIC_AUTH_TOKEN"], "unused")
        self.assertEqual(csw.provider(base), "Codex")

    def test_orcarouter_context_windows_tag_and_limits(self):
        with patch.object(csw.getpass, "getpass", return_value="orca-test-key"):
            csw.add_account("orcarouter", "principal")
        profile = csw.resolve("orcarouter:principal")
        data = csw.read_json(profile)
        listing = io.BytesIO(json.dumps({"data": [
            {"id": "orcarouter/auto"},
            {"id": "orca/orcacyber-glm-5.3-flash", "context_length": 1048576,
             "name": "OrcaCyber: GLM 5.3 Flash Abliterated / Uncensored"},
            {"id": "grok/grok-4.5", "context_length": 500000},
            {"id": "orcarouter/pod"}], "has_more": False}).encode())
        with patch.object(csw.urllib.request, "urlopen", return_value=listing) as fetch:
            catalog = csw.model_catalog(profile, data)
        self.assertNotIn("Anthropic-version", fetch.call_args[0][0].headers)
        labels = {row["model"]: row["label"] for row in catalog["models"]}
        self.assertEqual(labels["orca/orcacyber-glm-5.3-flash"], "OrcaCyber: GLM 5.3 Flash Abliterated / Uncensored")
        self.assertEqual(labels["orcarouter/pod"], "orcarouter/pod")  # no display_name: the id
        self.assertEqual(csw.documented_context("orca/orcacyber-glm-5.3-flash", data), 1048576)
        self.assertIsNone(csw.documented_context("orcarouter/pod", data))
        self.assertEqual(csw.preferred_context_model("orca/orcacyber-glm-5.3-flash", data),
                         "orca/orcacyber-glm-5.3-flash[1m]")
        self.assertEqual(csw.preferred_context_model("grok/grok-4.5", data), "grok/grok-4.5")
        self.assertEqual(csw.context_label("orca/orcacyber-glm-5.3-flash", data), "1M")
        self.assertEqual(csw.context_label("grok/grok-4.5", data), "500K")
        target = {"env": {"ANTHROPIC_BASE_URL": "https://api.orcarouter.ai",
                          "ANTHROPIC_MODEL": "orca/orcacyber-glm-5.3-flash"}}
        csw.prefer_supported_context(target)
        self.assertEqual(target["env"]["ANTHROPIC_MODEL"], "orca/orcacyber-glm-5.3-flash[1m]")
        self.assertEqual(target["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "786432")
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", target["env"])
        small = {"env": {"ANTHROPIC_BASE_URL": "https://api.orcarouter.ai",
                         "ANTHROPIC_MODEL": "grok/grok-4.5"}}
        csw.prefer_supported_context(small)
        self.assertEqual(small["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "500000")
        self.assertEqual(small["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "375000")

    def test_run_native_account_uses_isolated_root_and_snapshot(self):
        before = csw.LIVE.read_bytes()
        with patch.object(csw, "login", return_value=0):
            csw.add_account("claude", "max2")
        seen = {}
        def launch(command, env, tick=None):
            self.assertEqual(command[0], csw.CLAUDE_BIN)
            self.assertTrue(Path(env["CLAUDE_CONFIG_DIR"]).as_posix().endswith("accounts/claude/max2"))
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            seen["snapshot"] = Path(command[command.index("--settings") + 1])
            data = csw.read_json(seen["snapshot"])
            self.assertEqual(data["env"]["ANTHROPIC_AUTH_TOKEN"], "")
            self.assertEqual(data["env"]["ANTHROPIC_API_KEY"], "")
            self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
            self.assertNotIn("hooks", data)
            self.assertEqual(command[command.index("--setting-sources") + 1], "user,project,local")
            return 0
        with patch.object(csw, "run_child", side_effect=launch):
            self.assertEqual(csw.run_claude(["claude:max2", "--", "-p", "test"]), 0)
        self.assertEqual(before, csw.LIVE.read_bytes())
        self.assertFalse(seen["snapshot"].exists())

    def test_run_selected_native_account_and_literal_prompt(self):
        with patch.object(csw, "login", return_value=0):
            csw.add_account("claude", "max2")
        csw.write_json(csw.ROOT / "accounts/claude/max2/.credentials.json", {"claudeAiOauth": {"refreshToken": "max2-refresh"}})
        csw.switch("claude:max2")
        with patch.object(csw, "run_child", return_value=0) as call:
            csw.run_claude(["--", "hello world"])
        self.assertEqual(call.call_args.args[0][-1], "hello world")
        self.assertEqual(call.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"], str(csw.ROOT))  # the selected account's login is ROOT's

    def test_run_default_model_choice_stays_out_of_shared_settings(self):
        csw.switch("claude")
        before = csw.read_json(csw.LIVE).get("model")
        profile = csw.resolve("codex")

        def set_live_model(model):
            live = csw.read_json(csw.LIVE)
            live["model"] = model
            csw.write_json(csw.LIVE, live)

        def launch(command, env, tick=None):
            set_live_model("gpt-6.2-astra")  # Claude saving "/model … set as default"
            tick()
            self.assertEqual(csw.read_json(csw.LIVE).get("model"), before)
            set_live_model("sonnet")  # another native session's own default stays
            tick()
            self.assertEqual(csw.read_json(csw.LIVE)["model"], "sonnet")
            return 0

        with patch.object(csw, "check_proxy"), patch.object(csw, "configure_model_picker"), \
                patch.object(csw, "run_child", side_effect=launch):
            self.assertEqual(csw.run_claude(["codex", "--", "-p", "test"]), 0)
        self.assertEqual(csw.read_json(profile)["model"], "gpt-6.2-astra")

    def test_run_child_waits_for_claude_and_ticks(self):
        ticks = []
        code = csw.run_child([sys.executable, "-c", "import time; time.sleep(1.5); raise SystemExit(3)"],
                             env=os.environ.copy(), tick=lambda: ticks.append(1))
        self.assertEqual(code, 3)
        self.assertGreaterEqual(len(ticks), 2)  # while running and once at exit

    def test_run_auth_login_ignores_selected_api_credentials(self):
        with patch.object(csw.subprocess, "call", return_value=0) as call:
            csw.run_claude(["--", "auth", "login"])
        self.assertEqual(call.call_args.args[0], [csw.CLAUDE_BIN, "--setting-sources", "", "auth", "login"])
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", call.call_args.kwargs["env"])

    def test_effective_model_respects_environment_priority_and_aliases(self):
        self.assertEqual(csw.effective_model(self.direct), "deepseek-v4-pro")
        data = {"model": "sonnet", "env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3-flash"}}
        self.assertEqual(csw.effective_model(data), "glm-5.3-flash")

    def test_model_selection_updates_only_the_requested_profile(self):
        catalog = {"models": [{"model": "gpt-6-astra", "label": "Astra"}]}
        before = csw.LIVE.read_bytes()
        with patch.object(csw, "model_catalog", return_value=catalog):
            csw.select_model("codex", "gpt-6-astra")
        self.assertEqual(csw.LIVE.read_bytes(), before)
        target = csw.read_json(csw.resolve("codex"))
        self.assertEqual(target["model"], "gpt-6-astra")
        self.assertEqual(target["env"]["ANTHROPIC_MODEL"], "gpt-6-astra")

    def test_active_model_selection_updates_both_files_and_keeps_shared_settings(self):
        catalog = {"models": [{"model": "deepseek-flash", "label": "Flash"}]}
        with patch.object(csw, "model_catalog", return_value=catalog):
            csw.select_model("deepseek", "deepseek-flash")
        self.assertEqual(csw.read_json(csw.LIVE)["model"], "deepseek-flash[1m]")
        self.assertEqual(csw.read_json(csw.LIVE), csw.read_json(csw.resolve("deepseek")))
        self.assertEqual(csw.read_json(csw.LIVE)["hooks"], self.direct["hooks"])

    def test_unknown_model_does_not_change_configuration(self):
        before = csw.LIVE.read_bytes()
        with patch.object(csw, "model_catalog", return_value={"models": []}), self.assertRaises(ValueError):
            csw.select_model("deepseek", "deepseek-unknown")
        self.assertEqual(csw.LIVE.read_bytes(), before)

    def test_cursor_ids_stay_on_cursor_even_for_gpt_models(self):
        target = copy.deepcopy(self.proxy)
        target["env"]["ANTHROPIC_MODEL"] = "cursor:composer-2.5"
        self.assertEqual(csw.normalize_model("gpt-5.6-sol-high", target), "cursor:gpt-5.6-sol-high")
        self.assertEqual(csw.normalize_model("cursor-ask:composer-2.5", target), "cursor-ask:composer-2.5")
        with self.assertRaises(ValueError):
            csw.normalize_model("cursor:composer-2.5", self.proxy)

    def test_picker_uses_ids_and_provider_labels(self):
        target = copy.deepcopy(self.proxy)
        catalog = {"models": [{"model": "gpt-6-astra", "label": "Astra"}]}
        with patch.object(csw, "model_catalog", return_value=catalog):
            csw.configure_model_picker(target, csw.resolve("codex"))
        self.assertTrue(target["modelPicker"]["replaceBuiltInOptions"])
        self.assertEqual(target["modelPicker"]["options"][0], {"model": "gpt-6-astra", "label": "Codex · Astra", "description": "gpt-6-astra · contexto 272K"})

    def test_codex_picker_mirrors_codex_cli_catalog_without_the_proxy(self):
        csw.write_json(csw.CODEX_NATIVE_CATALOG, {"models": [
            {"slug": "gpt-daybreak-blue-latest", "display_name": "Daybreak Blue", "visibility": "list", "supported_in_api": True, "priority": 12},
            {"slug": "gpt-6.2-sol", "display_name": "GPT-6.2-Sol", "visibility": "list", "supported_in_api": True, "priority": 1},
            {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide", "supported_in_api": True, "priority": 4},
            {"slug": "gpt-no-api", "visibility": "list", "supported_in_api": False, "priority": 2},
            {"slug": "codex-auto-review", "visibility": "list", "supported_in_api": True, "priority": 3},
            {"slug": "gpt-6.1-terra", "display_name": "GPT-6.1 Terra", "visibility": "list", "supported_in_api": True, "priority": 3},
        ]})
        with patch.object(csw.subprocess, "run", side_effect=AssertionError("proxy not needed")):
            catalog = csw.fetch_model_catalog(csw.resolve("codex"), self.proxy)
        self.assertEqual([(r["model"], r["label"]) for r in catalog["models"]],
                         [("gpt-6.2-sol", "GPT-6.2-Sol"), ("gpt-6.1-terra", "GPT-6.1 Terra"),
                          ("gpt-daybreak-blue-latest", "Daybreak Blue")])

    def test_catalog_cache_is_per_account_and_refreshes_explicitly(self):
        path = csw.resolve("codex")
        csw.model_cache(path).unlink()
        catalog = {"source": "test", "models": [{"model": "gpt-6-astra", "label": "Astra"}], "fetched_at": int(csw.time.time())}
        with patch.object(csw, "fetch_model_catalog", return_value=catalog) as fetch:
            csw.model_catalog(path, self.proxy)
            csw.model_catalog(path, self.proxy)
            self.assertEqual(fetch.call_count, 1)
            csw.model_catalog(path, self.proxy, refresh=True)
            self.assertEqual(fetch.call_count, 2)
        if os.name != "nt":  # POSIX modes only; Windows relies on the profile ACL
            self.assertEqual(csw.model_cache(path).stat().st_mode & 0o777, 0o600)

    def test_cursor_catalog_uses_only_access_token_in_child_environment(self):
        target = copy.deepcopy(self.proxy)
        target["env"]["ANTHROPIC_MODEL"] = "cursor:composer-2.5"
        directory = csw.CONFIG_ROOT / "cursor"
        directory.mkdir(parents=True)
        csw.write_json(directory / "auth.json", {"accessToken": "fake-access", "refreshToken": "do-not-share"})
        result = csw.subprocess.CompletedProcess([], 0, stdout="Available models\n\ngpt-5.6-sol-high - Sol\ncomposer-2.5 - Composer\n", stderr="")
        with patch.object(csw, "authenticated", return_value=True), patch.object(csw.subprocess, "run", return_value=result) as run:
            catalog = csw.fetch_model_catalog(csw.ROOT / "settings.json.CURSOR.bak", target)
        self.assertEqual([m["model"] for m in catalog["models"]], ["cursor:gpt-5.6-sol-high", "cursor:composer-2.5"])
        self.assertEqual(run.call_args.kwargs["env"]["CURSOR_AUTH_TOKEN"], "fake-access")
        self.assertNotIn("fake-access", str(run.call_args.args))
        self.assertNotIn("do-not-share", str(run.call_args))

    def test_statusline_uses_session_id_instead_of_generic_display_name(self):
        env = {"CSW_PROFILE": "cursor:work", "CSW_PROVIDER": "Cursor", "ANTHROPIC_BASE_URL": "http://127.0.0.1:18766"}
        line = csw.status_model({"model": {"id": "cursor:gpt-5.6-sol-high", "display_name": "Sonnet"}}, env)
        self.assertEqual(line, "Cursor · cursor:work · cursor:gpt-5.6-sol-high")
        self.assertIn("Codex", csw.status_model({"model": {"id": "gpt-5.6-sol"}}, env))
        self.assertIn("variable", csw.status_model({"model": {"id": "cursor:auto"}}, env))

    def test_statusline_resolves_direct_provider_alias(self):
        env = {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic", "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash", "CSW_PROFILE": "deepseek"}
        self.assertEqual(csw.status_model({"model": {"id": "sonnet"}}, env), "DeepSeek · deepseek · deepseek-flash")

    def test_remove_selected_profile_is_blocked(self):
        with self.assertRaises(ValueError):
            csw.remove_account("deepseek")
        self.assertTrue(csw.resolve("deepseek").exists())
        self.assertFalse((csw.ROOT / "removed-accounts").exists())

    def test_remove_running_account_is_blocked_by_shared_lock(self):
        with csw.account_lock(csw.resolve("codex")), self.assertRaises(ValueError):
            csw.remove_account("codex")
        self.assertTrue(csw.resolve("codex").exists())

    def test_remove_legacy_session_is_blocked(self):
        with patch.object(csw, "legacy_session_running", return_value=True), self.assertRaises(ValueError):
            csw.remove_account("codex")
        self.assertTrue(csw.resolve("codex").exists())

    def test_remove_and_restore_api_profile_is_private_and_reversible(self):
        with patch.object(csw.getpass, "getpass", return_value="extra-key"):
            csw.add_account("deepseek", "extra")
        profile = csw.resolve("deepseek:extra")
        original = profile.read_bytes()
        selected = csw.active()
        csw.remove_account("deepseek:extra")
        self.assertFalse(profile.exists())
        self.assertNotIn(profile.name, csw.names())
        archive = next((csw.ROOT / "removed-accounts").iterdir())
        if os.name != "nt":  # POSIX modes only; Windows relies on the profile ACL
            self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
        self.assertEqual((archive / "profile.json").read_bytes(), original)
        csw.restore_account(archive.name)
        self.assertEqual(profile.read_bytes(), original)
        self.assertEqual(csw.active(), selected)

    def test_remove_extra_native_account_preserves_shared_customizations(self):
        (csw.ROOT / "skills").mkdir()
        with patch.object(csw, "login", return_value=0):
            csw.add_account("claude", "extra")
        directory, _ = csw.account_directory(csw.resolve("claude:extra"))
        csw.write_json(directory / ".credentials.json", {"token": "fake"})
        csw.remove_account("claude:extra")
        self.assertFalse(directory.exists())
        self.assertTrue((csw.ROOT / "skills").is_dir())
        archive = next((csw.ROOT / "removed-accounts").iterdir())
        self.assertEqual(csw.read_json(archive / "account/.credentials.json"), {"token": "fake"})

    def test_remove_proxy_account_disables_only_its_unit(self):
        with patch.object(csw, "login", return_value=0):
            csw.add_account("codex", "extra")
        with patch.object(csw.subprocess, "run", return_value=csw.subprocess.CompletedProcess([], 0)) as run:
            csw.remove_account("codex:extra")
        self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "disable", "--now", "claude-code-proxy@codex-extra.service"])
        self.assertTrue(csw.resolve("codex").exists())

    def test_remove_main_proxy_profile_keeps_shared_login_and_service(self):
        directory = csw.CONFIG_ROOT / "codex"
        directory.mkdir(parents=True)
        csw.write_json(directory / "auth.json", {"access": "fake"})
        with patch.object(csw.subprocess, "run") as run:
            csw.remove_account("codex")
        run.assert_not_called()
        self.assertTrue((directory / "auth.json").exists())

    def test_remove_failure_restores_profile(self):
        write = csw.atomic_write
        def fail_names(path, text):
            if path == csw.NAMES:
                raise OSError("test write failure")
            return write(path, text)
        original = csw.resolve("codex").read_bytes()
        with patch.object(csw, "atomic_write", side_effect=fail_names), self.assertRaises(OSError):
            csw.remove_account("codex")
        self.assertEqual(csw.resolve("codex").read_bytes(), original)

    def test_restore_rejects_paths_and_existing_profiles(self):
        for identifier in ("../outside", "..", "/tmp/something"):
            with self.assertRaises(ValueError):
                csw.restore_account(identifier)
        csw.remove_account("codex")
        archive = next((csw.ROOT / "removed-accounts").iterdir())
        csw.write_json(csw.ROOT / csw.ALIASES["codex"], self.proxy)
        with self.assertRaises(ValueError):
            csw.restore_account(archive.name)

    def test_removal_archives_catalog_and_restore_recovers_it(self):
        path = csw.resolve("codex")
        csw.model_cache(path).parent.mkdir(exist_ok=True)
        catalog = {"source": "test", "models": [{"model": "gpt-6-astra", "label": "Astra"}]}
        csw.write_json(csw.model_cache(path), catalog)
        csw.remove_account("codex")
        self.assertFalse(csw.model_cache(path).exists())
        archive = next((csw.ROOT / "removed-accounts").iterdir())
        csw.restore_account(archive.name)
        self.assertEqual(csw.read_json(csw.model_cache(path)), catalog)
        self.assertEqual(csw.profile_label(path), "codex")

    def test_codex_catalog_prioritizes_current_models_and_labels_blue(self):
        result = csw.subprocess.CompletedProcess([], 0, stdout="codex: gpt-5.2, gpt-daybreak-blue-latest, gpt-6-astra\n", stderr="")
        with patch.object(csw.subprocess, "run", return_value=result):
            catalog = csw.fetch_model_catalog(csw.resolve("codex"), self.proxy)
        self.assertEqual([row["model"] for row in catalog["models"]], ["gpt-6-astra", "gpt-daybreak-blue-latest", "gpt-5.2"])
        self.assertIn("Daybreak Blue", catalog["models"][1]["label"])

    def test_launch_overlay_restores_user_scope_without_duplicating_shared_settings(self):
        overlay = csw.launch_overlay(self.proxy, self.direct)
        self.assertNotIn("hooks", overlay)
        self.assertNotIn("permissions", overlay)
        self.assertNotIn("enabledPlugins", overlay)
        self.assertNotIn("SHARED_PATH", overlay["env"])
        self.assertEqual(overlay["env"]["ANTHROPIC_AUTH_TOKEN"], "unused")
        self.assertEqual(overlay["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:18765")
        self.assertEqual(overlay["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "")

    def test_launch_overlay_masks_native_credentials_and_resets_external_key_helper(self):
        inherited = copy.deepcopy(self.direct)
        inherited["apiKeyHelper"] = "previous-provider-helper"
        overlay = csw.launch_overlay(self.native, inherited)
        self.assertEqual(overlay["env"]["ANTHROPIC_API_KEY"], "")
        self.assertEqual(overlay["env"]["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertEqual(overlay["env"]["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
        self.assertEqual(overlay["apiKeyHelper"], "")
        self.assertEqual(overlay["model"], "opus")

    def test_existing_native_account_receives_missing_workflow_link_without_replacing_local_files(self):
        with patch.object(csw, "login", return_value=0):
            csw.add_account("claude", "existing")
        directory, _ = csw.account_directory(csw.resolve("claude:existing"))
        (csw.ROOT / "workflows").mkdir()
        (directory / "rules").mkdir()
        local_rules = directory / "rules/local.md"
        local_rules.write_text("keep local rule")
        csw.native_environment(csw.resolve("claude:existing"))
        self.assertEqual((directory / "workflows").resolve(), csw.ROOT / "workflows")
        self.assertEqual(local_rules.read_text(), "keep local rule")

    def test_mcp_administration_loads_user_scope_without_needing_provider_login(self):
        with patch.object(csw, "check_proxy", side_effect=AssertionError("unnecessary provider login")), patch.object(csw, "configure_model_picker", side_effect=AssertionError("unnecessary catalog fetch")), patch.object(csw, "run_child", return_value=0) as call:
            self.assertEqual(csw.run_claude(["codex", "--", "mcp", "list"]), 0)
        self.assertEqual(call.call_args.args[0][1:3], ["--setting-sources", "user,project,local"])
        self.assertEqual(call.call_args.args[0][-2:], ["mcp", "list"])

    def test_extra_settings_do_not_discard_provider_isolation(self):
        seen = {}
        def launch(command, env, tick=None):
            self.assertEqual(command.count("--settings"), 1)
            data = csw.read_json(Path(command[command.index("--settings") + 1]))
            seen.update(data)
            return 0
        with patch.object(csw, "run_child", side_effect=launch):
            csw.run_claude(["claude", "--", "--settings", '{"disableAllHooks":true,"env":{"EXTRA_TEST":"yes"}}', '-p', 'test'])
        self.assertTrue(seen["disableAllHooks"])
        self.assertEqual(seen["env"]["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertEqual(seen["env"]["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
        self.assertEqual(seen["env"]["EXTRA_TEST"], "yes")

    def test_extra_settings_file_and_last_option_are_respected(self):
        file = csw.ROOT / "extra.json"
        csw.write_json(file, {"theme": "light"})
        forwarded, extra = csw.extract_extra_settings(["--settings", str(file), "-p", "text"])
        self.assertEqual(extra, {"theme": "light"})
        self.assertEqual(forwarded, ["-p", "text"])
        forwarded, extra = csw.extract_extra_settings(["--settings", str(file), '--settings={"theme":"dark"}'])
        self.assertEqual(extra, {"theme": "dark"})
        self.assertEqual(forwarded, [])

    def test_context_preference_tags_supported_models_only(self):
        self.assertEqual(csw.normalize_model("deepseek-flash", self.direct), "deepseek-flash[1m]")
        self.assertEqual(csw.normalize_model("opus", self.native), "opus[1m]")
        self.assertEqual(csw.normalize_model("haiku", self.native), "haiku")
        self.assertEqual(csw.documented_context("haiku", self.native), 200000)
        self.assertEqual(csw.normalize_model("gpt-daybreak-blue-latest[1m]", self.proxy), "gpt-daybreak-blue-latest")

    def test_codex_context_is_not_increased_to_the_api_specification(self):
        target = copy.deepcopy(self.proxy)
        target["env"]["ANTHROPIC_MODEL"] = "gpt-daybreak-blue-latest[1m]"
        target["env"]["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "1"
        csw.prefer_supported_context(target)
        self.assertEqual(target["env"]["ANTHROPIC_MODEL"], "gpt-daybreak-blue-latest")
        self.assertEqual(target["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "272000")
        self.assertEqual(target["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "272000")
        self.assertNotIn("CLAUDE_CODE_DISABLE_1M_CONTEXT", target["env"])

    def test_context_preference_is_idempotent_and_keeps_compaction_margin(self):
        target = copy.deepcopy(self.direct)
        csw.prefer_supported_context(target)
        once = copy.deepcopy(target)
        csw.prefer_supported_context(target)
        self.assertEqual(target, once)
        self.assertEqual(target["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "deepseek-flash[1m]")
        self.assertEqual(target["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "786432")

    def test_codex_custom_window_flags_do_not_leak_into_claude(self):
        with patch.object(csw, "authenticated", return_value=True), patch.object(csw, "proxy_ready", return_value=True):
            csw.switch("codex")
        self.assertEqual(csw.read_json(csw.LIVE)["env"]["CLAUDE_CODE_DISABLE_1M_CONTEXT"], "")
        csw.switch("claude")
        current = csw.read_json(csw.LIVE)
        self.assertEqual(current["model"], "opus[1m]")
        self.assertEqual(current["env"]["CLAUDE_CODE_DISABLE_1M_CONTEXT"], "")
        self.assertEqual(current["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "")

    def test_cursor_context_is_not_inferred_from_claude_model_name(self):
        target = copy.deepcopy(self.proxy)
        target["env"]["ANTHROPIC_MODEL"] = "cursor:composer-2.5"
        self.assertIsNone(csw.documented_context("cursor:claude-opus-5-thinking-high", target))
        self.assertEqual(csw.context_label("cursor:claude-opus-5-thinking-high", target), "sin verificar")

    def test_statusline_reports_actual_client_window(self):
        text = csw.status_model({"model": {"id": "gpt-daybreak-blue-latest"}, "context_window": {"context_window_size": 272000}}, {"CSW_PROFILE": "codex", "CSW_PROVIDER": "Codex"})
        self.assertTrue(text.endswith("ctx 272K"))

    def test_native_codex_override_uses_extended_window_despite_experimental_flag(self):
        csw.write_json(csw.CODEX_NATIVE_CATALOG, {"models": [{"slug": "gpt-daybreak-blue-latest", "context_window": 272000, "max_context_window": 872000, "effective_context_window_percent": 95, "supports_experimental_context": False}]})
        csw.CODEX_NATIVE_CONFIG.write_text("model_context_window = 1050000\nmodel_auto_compact_token_limit = 997500\n")
        budget = csw.codex_context_budget("gpt-daybreak-blue-latest")
        self.assertEqual(budget["window"], 872000)
        self.assertEqual(budget["compact"], 872000)
        self.assertEqual(budget["native_effective"], 828400)
        self.assertEqual(budget["requested"], 1050000)
        target = copy.deepcopy(self.proxy)
        target["env"]["ANTHROPIC_MODEL"] = "gpt-daybreak-blue-latest"
        csw.prefer_supported_context(target)
        self.assertEqual(target["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "872000")
        self.assertEqual(target["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "872000")

    def test_native_codex_default_is_not_treated_as_the_maximum(self):
        csw.write_json(csw.CODEX_NATIVE_CATALOG, {"models": [{"slug": "gpt-6-astra", "context_window": 272000, "max_context_window": 872000, "effective_context_window_percent": 95}]})
        self.assertEqual(csw.codex_context_budget("gpt-6-astra")["window"], 272000)
        csw.CODEX_NATIVE_CONFIG.write_text("model_context_window = 800000\n")
        self.assertEqual(csw.codex_context_budget("gpt-6-astra")["window"], 800000)
        self.assertEqual(csw.codex_context_budget("gpt-6-astra")["compact"], 800000)
        self.assertEqual(csw.codex_context_budget("gpt-6-astra")["native_effective"], 760000)

    def test_native_codex_honors_smaller_compaction_setting_and_per_model_maximum(self):
        csw.write_json(csw.CODEX_NATIVE_CATALOG, {"models": [{"slug": "gpt-5.5", "context_window": 272000, "max_context_window": 272000, "effective_context_window_percent": 95}]})
        csw.CODEX_NATIVE_CONFIG.write_text("model_context_window = 1050000\nmodel_auto_compact_token_limit = 200000\n")
        budget = csw.codex_context_budget("gpt-5.5-fast")
        self.assertEqual(budget["window"], 272000)
        self.assertEqual(budget["compact"], 200000)

    def test_launch_model_override_recalculates_context_without_changing_profile(self):
        csw.write_json(csw.CODEX_NATIVE_CATALOG, {"models": [
            {"slug": "gpt-5.6-sol", "context_window": 272000, "max_context_window": 872000, "effective_context_window_percent": 95},
            {"slug": "gpt-5.5", "context_window": 272000, "max_context_window": 272000, "effective_context_window_percent": 95},
        ]})
        csw.CODEX_NATIVE_CONFIG.write_text("model_context_window = 1050000\n")
        before = csw.LIVE.read_bytes()
        profile = csw.ROOT / csw.ALIASES["codex"]
        profile_before = profile.read_bytes()

        def launch(command, env, tick=None):
            data = csw.read_json(Path(command[command.index("--settings") + 1]))
            for values in (env, data["env"]):
                self.assertEqual(values["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "272000")
                self.assertEqual(values["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "272000")
            return 0

        for arguments in (["--model", "gpt-5.5"], ["--model=gpt-5.5"]):
            with self.subTest(arguments=arguments), patch.object(csw, "check_proxy"), patch.object(csw, "configure_model_picker"), patch.object(csw, "run_child", side_effect=launch):
                self.assertEqual(csw.run_claude(["codex", "--", *arguments, "-p", "test"]), 0)
        self.assertEqual(csw.LIVE.read_bytes(), before)
        self.assertEqual(profile.read_bytes(), profile_before)


@unittest.skipUnless(os.name == "nt", "Windows service emulation")
class WindowsServiceTest(unittest.TestCase):
    def setUp(self):
        # Never touch the real logon Run key from tests.
        import winreg
        portable = csw.portable_module()
        p = patch.object(portable, "RUN_KEY", r"Software\csw-test-run")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\csw-test-run")
                        if self._key_exists() else None)

    @staticmethod
    def _key_exists():
        import winreg
        try:
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\csw-test-run").Close()
            return True
        except FileNotFoundError:
            return False

    def test_enable_registers_a_windowless_logon_start_and_disable_removes_it(self):
        import shlex, winreg
        portable = csw.portable_module()
        with tempfile.TemporaryDirectory(prefix="csw-svc-") as folder:
            unit, root = "csw-deepseek-compat.service", Path(folder)
            run = lambda *a, **k: portable.systemctl(list(a), root, root / "proxy", **k)
            self.assertEqual(run("is-enabled", unit).returncode, 1)
            run("enable", unit, check=True)
            self.addCleanup(run, "disable", "--now", unit)
            self.assertEqual(run("is-enabled", unit).returncode, 0)
            self.assertEqual(run("is-active", unit).returncode, 3)  # enable alone does not start
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\csw-test-run") as key:
                command, _ = winreg.QueryValueEx(key, "csw " + unit)
            argv = shlex.split(command, posix=False)
            self.assertTrue(argv[0].strip('"').lower().endswith("pythonw.exe"), argv[0])
            # What Windows runs at logon starts the unit.
            subprocess_args = [sys.executable] + [a.strip('"') for a in argv[1:]]
            self.assertEqual(csw.subprocess.run(subprocess_args, timeout=30).returncode, 0)
            self.assertEqual(run("is-active", unit).returncode, 0)
            run("disable", "--now", unit, check=True)
            self.assertEqual(run("is-enabled", unit).returncode, 1)
            self.assertEqual(run("is-active", unit).returncode, 3)

    def test_start_status_stop_tracks_a_real_detached_process(self):
        portable = csw.portable_module()
        with tempfile.TemporaryDirectory(prefix="csw-svc-") as folder:
            unit, root = "csw-deepseek-compat.service", Path(folder)
            run = lambda *a, **k: portable.systemctl(list(a), root, root / "proxy", **k)
            self.assertEqual(run("is-active", unit).returncode, 3)
            run("start", unit, check=True)
            self.addCleanup(run, "stop", unit)
            self.assertEqual(run("is-active", unit).returncode, 0)
            pid = (root / "services" / (unit + ".pid")).read_text().split()[0]
            run("start", unit, check=True)  # already running: no second process
            self.assertEqual((root / "services" / (unit + ".pid")).read_text().split()[0], pid)
            run("disable", "--now", unit, check=True)
            self.assertEqual(run("is-active", unit).returncode, 3)
            self.assertIsNone(portable.process_start(pid))


if __name__ == "__main__":
    unittest.main()
