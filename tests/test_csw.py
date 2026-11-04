"""Regression tests use temporary profiles and never contact provider APIs."""
import copy
import importlib.machinery
import importlib.util
import io
import json
import os
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
        self.assertEqual(data["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "synthetic-native-access")
        self.assertEqual(data["model"], "opus[1m]")
        self.assertEqual(csw.active(), csw.ALIASES["claude"])

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
        self.assertTrue(env["CCP_CONFIG_DIR"].endswith("accounts/codex-work"))
        self.assertEqual(unit, "claude-code-proxy@codex-work.service")
        self.assertEqual(before, csw.LIVE.read_bytes())
        self.assertEqual((csw.CONFIG_ROOT / "accounts/codex-work").stat().st_mode & 0o777, 0o700)
        self.assertIn(csw.resolve("codex:work").name, csw.profiles())

    def test_account_relogin_selects_own_store(self):
        with patch.object(csw, "login", return_value=0):
            csw.add_account("codex", "work")
        with patch.object(csw.subprocess, "call", return_value=0) as call, patch.object(csw.subprocess, "run") as run:
            csw.login("codex:work")
        self.assertEqual(call.call_args.args[0], ["claude-code-proxy", "codex", "auth", "device"])
        self.assertTrue(call.call_args.kwargs["env"]["CCP_CONFIG_DIR"].endswith("accounts/codex-work"))
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
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        self.assertEqual(csw.read_json(csw.LIVE)["env"]["ANTHROPIC_AUTH_TOKEN"], "test-deepseek-key")

    def test_orcarouter_context_windows_tag_and_limits(self):
        with patch.object(csw.getpass, "getpass", return_value="orca-test-key"):
            csw.add_account("orcarouter", "principal")
        profile = csw.resolve("orcarouter:principal")
        data = csw.read_json(profile)
        listing = io.BytesIO(json.dumps({"data": [
            {"id": "orcarouter/auto"},
            {"id": "orca/orcacyber-glm-5.3-flash", "context_length": 1048576},
            {"id": "grok/grok-4.5", "context_length": 500000},
            {"id": "orcarouter/pod"}], "has_more": False}).encode())
        with patch.object(csw.urllib.request, "urlopen", return_value=listing) as fetch:
            csw.model_catalog(profile, data)
        self.assertNotIn("Anthropic-version", fetch.call_args[0][0].headers)
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
        def launch(command, env):
            self.assertEqual(command[0], csw.CLAUDE_BIN)
            self.assertTrue(env["CLAUDE_CONFIG_DIR"].endswith("accounts/claude/max2"))
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            seen["snapshot"] = Path(command[command.index("--settings") + 1])
            data = csw.read_json(seen["snapshot"])
            self.assertEqual(data["env"]["ANTHROPIC_AUTH_TOKEN"], "")
            self.assertEqual(data["env"]["ANTHROPIC_API_KEY"], "")
            self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
            self.assertNotIn("hooks", data)
            self.assertEqual(command[command.index("--setting-sources") + 1], "user,project,local")
            return 0
        with patch.object(csw.subprocess, "call", side_effect=launch):
            self.assertEqual(csw.run_claude(["claude:max2", "--", "-p", "test"]), 0)
        self.assertEqual(before, csw.LIVE.read_bytes())
        self.assertFalse(seen["snapshot"].exists())

    def test_run_selected_native_account_and_literal_prompt(self):
        with patch.object(csw, "login", return_value=0):
            csw.add_account("claude", "max2")
        csw.write_json(csw.ROOT / "accounts/claude/max2/.credentials.json", csw.read_json(csw.ROOT / ".credentials.json"))
        csw.switch("claude:max2")
        with patch.object(csw.subprocess, "call", return_value=0) as call:
            csw.run_claude(["--", "hello world"])
        self.assertEqual(call.call_args.args[0][-1], "hello world")
        self.assertTrue(call.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"].endswith("accounts/claude/max2"))

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
        with patch.object(csw, "check_proxy", side_effect=AssertionError("unnecessary provider login")), patch.object(csw, "configure_model_picker", side_effect=AssertionError("unnecessary catalog fetch")), patch.object(csw.subprocess, "call", return_value=0) as call:
            self.assertEqual(csw.run_claude(["codex", "--", "mcp", "list"]), 0)
        self.assertEqual(call.call_args.args[0][1:3], ["--setting-sources", "user,project,local"])
        self.assertEqual(call.call_args.args[0][-2:], ["mcp", "list"])

    def test_extra_settings_do_not_discard_provider_isolation(self):
        seen = {}
        def launch(command, env):
            self.assertEqual(command.count("--settings"), 1)
            data = csw.read_json(Path(command[command.index("--settings") + 1]))
            seen.update(data)
            return 0
        with patch.object(csw.subprocess, "call", side_effect=launch):
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

        def launch(command, env):
            data = csw.read_json(Path(command[command.index("--settings") + 1]))
            for values in (env, data["env"]):
                self.assertEqual(values["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "272000")
                self.assertEqual(values["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "272000")
            return 0

        for arguments in (["--model", "gpt-5.5"], ["--model=gpt-5.5"]):
            with self.subTest(arguments=arguments), patch.object(csw, "check_proxy"), patch.object(csw, "configure_model_picker"), patch.object(csw.subprocess, "call", side_effect=launch):
                self.assertEqual(csw.run_claude(["codex", "--", *arguments, "-p", "test"]), 0)
        self.assertEqual(csw.LIVE.read_bytes(), before)
        self.assertEqual(profile.read_bytes(), profile_before)


if __name__ == "__main__":
    unittest.main()
