"""Shared-file routing and safety tests. Synthetic credentials and processes."""
import os
import io
import json
import unittest
from unittest.mock import patch

import test_csw

csw = test_csw.csw
shared = csw.shared_module()
hot = csw.hot_module()


class SharedTest(unittest.TestCase):
    def setUp(self):
        test_csw.ProfilesTest.setUp(self)
        csw.switch("deepseek")
        self.payload = {"session_id": "test-session", "model": "deepseek-v4-pro[1m]"}
        self.path = csw.ROOT / "shared-sessions/test-session.json"

    def hook(self, event, **values):
        with patch.object(shared, "claude_parent", return_value=os.getpid()):
            return shared.hook(csw, {**self.payload, "hook_event_name": event, **values}, csw.read_json(csw.LIVE)["env"])

    def test_published_picker_and_model_are_not_pinned(self):
        data = csw.read_json(csw.LIVE)
        self.assertEqual(data["env"]["ANTHROPIC_MODEL"], "")
        self.assertEqual(data["model"], "deepseek-v4-pro[1m]")
        self.assertEqual(data["modelPicker"]["options"][0]["model"], "deepseek-v4-pro[1m]")
        self.assertEqual(data["env"]["CSW_PROFILE"], "deepseek")

    def test_aliases_and_cursor_provider_survive_empty_env_mask(self):
        self.assertEqual(csw.provider({"model": "cursor:composer-2.5", "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:18765", "ANTHROPIC_MODEL": ""}}), "Cursor")
        self.assertEqual(csw.normalize_model("opus", {"model": "opus", "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": ""}}), "opus[1m]")

    def test_shared_launch_passes_no_snapshot_or_account_env(self):
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "stale", "CSW_SHARED_DISABLED": "1", "CSW_HOT_ID": "old"}), patch.object(csw, "exec_claude", return_value=0) as launch:
            self.assertEqual(csw.main(["launch", "--", "--resume"]), 0)
        self.assertEqual(launch.call_args.args[1], [csw.CLAUDE_BIN, "--resume", "--model", "deepseek-v4-pro[1m]"])
        env = launch.call_args.args[2]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(csw.ROOT))
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("CSW_SHARED_DISABLED", env)
        self.assertNotIn("CSW_HOT_ID", env)

    def test_claude_accounts_use_separate_canonical_access_not_refresh_tokens(self):
        profile = csw.ROOT / "settings.json.CLAUDE.work.bak"
        csw.write_json(profile, self.native)
        directory = csw.ROOT / "accounts/claude/work"
        directory.mkdir(parents=True)
        creds = {"claudeAiOauth": {"accessToken": "work-access", "refreshToken": "never-copy", "expiresAt": (csw.time.time() + 3600) * 1000}}
        csw.write_json(directory / ".credentials.json", creds)
        csw.switch("claude:work")
        self.assertEqual(csw.read_json(csw.LIVE)["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "work-access")
        self.assertNotIn("never-copy", csw.LIVE.read_text())
        csw.switch("claude")
        self.assertEqual(csw.read_json(csw.LIVE)["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "")

    def test_native_management_commands_do_not_receive_model_flags(self):
        for arguments in (["--help"], ["--version"], ["mcp", "list"], ["logs", "example"], ["agents", "list"]):
            with patch.object(csw, "read_json", side_effect=AssertionError("No provider needed")), patch.object(csw, "exec_claude", return_value=0) as launch:
                csw.launch_claude(arguments)
            self.assertEqual(launch.call_args.args[1], [csw.CLAUDE_BIN, *arguments])

    def test_missing_native_login_cannot_change_settings(self):
        (csw.ROOT / ".credentials.json").unlink()
        before = csw.LIVE.read_bytes()
        with self.assertRaisesRegex(ValueError, "login"):
            csw.switch("claude")
        self.assertEqual(csw.LIVE.read_bytes(), before)

    def test_shared_switch_is_allowed_during_turn_or_subagent(self):
        self.hook("SessionStart")
        self.hook("UserPromptSubmit")
        csw.switch("claude")
        self.assertEqual(csw.active(), csw.ALIASES["claude"])
        self.hook("Stop")
        self.hook("SubagentStart", agent_id="worker")
        csw.switch("deepseek")
        self.assertEqual(csw.active(), csw.ALIASES["deepseek"])

    def test_busy_same_model_account_switch_uses_new_credentials_after_reload(self):
        self.hook("SessionStart")
        self.hook("UserPromptSubmit")
        profile = csw.ROOT / "settings.json.DEEPSEEK.other.bak"
        target = csw.read_json(csw.resolve("deepseek"))
        target["env"]["ANTHROPIC_AUTH_TOKEN"] = "second-account-token"
        csw.write_json(profile, target)
        csw.write_json(csw.model_cache(profile), csw.read_json(csw.model_cache(csw.resolve("deepseek"))))

        csw.switch("deepseek:other")
        self.assertEqual(csw.read_json(csw.LIVE)["env"]["ANTHROPIC_AUTH_TOKEN"], "second-account-token")
        self.hook("Stop")
        self.assertEqual(self.hook("UserPromptSubmit"), {})

    def test_busy_cross_provider_switch_gates_next_prompt_until_reload_and_model_change(self):
        self.hook("SessionStart")
        old_environment = csw.read_json(csw.LIVE)["env"]
        self.hook("UserPromptSubmit")
        csw.switch("claude")
        self.hook("Stop")
        with patch.object(shared, "claude_parent", return_value=os.getpid()), self.assertRaisesRegex(ValueError, "recargado"):
            shared.hook(csw, {**self.payload, "hook_event_name": "UserPromptSubmit"}, old_environment)
        with self.assertRaisesRegex(ValueError, "/model"):
            self.hook("UserPromptSubmit")

    def test_goal_survives_shared_account_switch(self):
        transcript = csw.ROOT / "test.jsonl"
        csw.atomic_write(transcript, '{"type":"attachment","attachment":{"type":"goal_status","met":false}}\n')
        self.hook("SessionStart", transcript_path=str(transcript))
        self.hook("Stop")
        before = csw.LIVE.read_bytes()
        csw.switch("claude")
        self.assertEqual(csw.active(), csw.ALIASES["claude"])
        self.assertNotEqual(csw.LIVE.read_bytes(), before)
        self.assertTrue(hot.goal_active(csw.read_json(self.path)))

    def test_model_change_during_active_goal_preserves_goal_and_account(self):
        transcript = csw.ROOT / "test.jsonl"
        csw.atomic_write(transcript, '{"type":"attachment","attachment":{"type":"goal_status","met":false}}\n')
        self.hook("SessionStart", transcript_path=str(transcript))
        settings = csw.read_json(csw.LIVE)
        settings["modelPicker"]["options"].append({"model": "deepseek-flash[1m]", "label": "Flash"})
        csw.write_json(csw.LIVE, settings)
        before = transcript.read_bytes()
        self.assertEqual(self.hook("PreModelSwitch", to_model="deepseek-flash[1m]"), {})
        self.hook("PostModelSwitch", to_model="deepseek-flash[1m]")
        self.assertEqual(transcript.read_bytes(), before)
        self.assertTrue(hot.goal_active(csw.read_json(self.path)))
        self.assertEqual(csw.read_json(self.path)["model"], "deepseek-flash[1m]")
        self.assertEqual(csw.active(), csw.ALIASES["deepseek"])
        csw.switch("claude")
        self.assertEqual(csw.active(), csw.ALIASES["claude"])
        self.assertTrue(hot.goal_active(csw.read_json(self.path)))

    def test_model_change_with_active_goal_still_waits_for_busy_session(self):
        self.hook("SessionStart")
        self.hook("UserPromptSubmit")
        with self.assertRaisesRegex(ValueError, "trabajando"):
            self.hook("PreModelSwitch", to_model="deepseek-v4-pro[1m]")

    def test_interrupted_turn_allows_model_switch_without_stop_hook(self):
        transcript = csw.ROOT / "interrupted.jsonl"
        self.hook("SessionStart", transcript_path=str(transcript))
        with patch.object(csw.time, "time", return_value=10.0):
            self.hook("UserPromptSubmit")
        csw.atomic_write(transcript, json.dumps({
            "type": "user", "isSidechain": False, "timestamp": "1970-01-01T00:00:11.000Z",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        }) + "\n")
        before = csw.LIVE.read_bytes()
        self.assertEqual(self.hook("PreModelSwitch", to_model="deepseek-v4-pro[1m]"), {})
        state = csw.read_json(self.path)
        self.assertFalse(state["busy"])
        self.assertEqual(state["agents"], [])
        self.assertEqual(csw.LIVE.read_bytes(), before)

    def test_new_prompt_records_start_time_and_keeps_previous_interrupt_blocked(self):
        transcript = csw.ROOT / "interrupted.jsonl"
        csw.atomic_write(transcript, json.dumps({
            "type": "user", "isSidechain": False, "timestamp": "1970-01-01T00:00:11.000Z",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        }) + "\n")
        self.hook("SessionStart", transcript_path=str(transcript))
        with patch.object(csw.time, "time", return_value=12.0):
            self.hook("UserPromptSubmit")
        self.assertEqual(csw.read_json(self.path)["busy_since"], 12.0)
        with self.assertRaisesRegex(ValueError, "trabajando"):
            self.hook("PreModelSwitch", to_model="deepseek-v4-pro[1m]")

    def test_finished_subagent_allows_provider_model_recovery_with_active_goal(self):
        transcript = csw.ROOT / "agent-finished.jsonl"
        self.hook("SessionStart", model="glm-5.3-flash[1m]", transcript_path=str(transcript))
        with patch.object(csw.time, "time", return_value=10.0):
            self.hook("SubagentStart", agent_id="worker")
        self.assertEqual(csw.read_json(self.path)["agents_started"], {"worker": 10.0})
        records = [
            {"type": "attachment", "attachment": {"type": "goal_status", "met": False}},
            {"type": "user", "isSidechain": False, "timestamp": "1970-01-01T00:00:11.000Z",
             "origin": {"kind": "task-notification"}, "promptSource": "system",
             "message": {"role": "user", "content": "<task-notification>\n<task-id>worker</task-id>\n<status>failed</status>\n</task-notification>"}},
        ]
        csw.atomic_write(transcript, "".join(json.dumps(record) + "\n" for record in records))
        with self.assertRaisesRegex(ValueError, "/model"):
            self.hook("UserPromptSubmit")
        before = transcript.read_bytes()
        self.assertEqual(self.hook("PreModelSwitch", to_model="deepseek-v4-pro[1m]"), {})
        self.assertEqual(csw.read_json(self.path)["agents"], [])
        self.assertTrue(hot.goal_active(csw.read_json(self.path)))
        self.assertEqual(transcript.read_bytes(), before)
        self.hook("PostModelSwitch", to_model="deepseek-v4-pro[1m]")
        self.assertEqual(self.hook("UserPromptSubmit"), {})

    def test_failed_workflow_agents_allow_model_recovery_without_clearing_goal(self):
        transcript = csw.ROOT / "workflow-session.jsonl"
        csw.atomic_write(transcript, '{"type":"attachment","attachment":{"type":"goal_status","met":false}}\n')
        self.hook("SessionStart", model="glm-5.3[1m]", transcript_path=str(transcript))
        state = csw.read_json(self.path)
        state["agents"] = ["workflow-worker"]
        csw.write_json(self.path, state)
        journal = csw.ROOT / "workflow-session/subagents/workflows/wf_fixture/journal.jsonl"
        journal.parent.mkdir(parents=True)
        csw.atomic_write(journal, '{"type":"started","agentId":"workflow-worker"}\n'
                                 '{"type":"failed","agentId":"workflow-worker"}\n')
        before = transcript.read_bytes()
        self.assertEqual(self.hook("PreModelSwitch", to_model="deepseek-v4-pro[1m]"), {})
        self.assertEqual(csw.read_json(self.path)["agents"], [])
        self.assertEqual(transcript.read_bytes(), before)
        self.assertTrue(hot.goal_active(csw.read_json(self.path)))

    def test_old_provider_model_blocks_prompt_until_model_selected(self):
        self.hook("SessionStart")
        csw.switch("claude")
        with self.assertRaisesRegex(ValueError, "/model"):
            self.hook("UserPromptSubmit")
        self.hook("PreModelSwitch", to_model="opus[1m]")
        self.hook("PostModelSwitch", to_model="opus[1m]")
        self.assertEqual(self.hook("UserPromptSubmit"), {})

    def test_stale_credential_environment_blocks_prompt(self):
        self.hook("SessionStart")
        env = csw.read_json(csw.LIVE)["env"]
        env["ANTHROPIC_AUTH_TOKEN"] = "stale"
        with self.assertRaisesRegex(ValueError, "recargado"):
            shared.hook(csw, {**self.payload, "hook_event_name": "UserPromptSubmit"}, env)

    def test_isolated_sessions_ignore_shared_hooks(self):
        for env in ({"CSW_HOT_ID": "abc"}, {"CSW_SHARED_DISABLED": "1"}):
            self.assertEqual(shared.hook(csw, {}, env), {})
        self.assertFalse(self.path.exists())

    def test_unexpected_hook_failure_returns_blocking_status(self):
        with patch.object(csw.json, "load", return_value=[]), patch.object(csw.sys, "stderr", new=io.StringIO()):
            self.assertEqual(csw.main(["shared-hook"]), 2)

    def test_resumed_session_updates_owner_and_initial_model(self):
        self.hook("SessionStart")
        state = csw.read_json(self.path)
        state.update(owner_pid=1, owner_start="old", status="closed", model="wrong")
        csw.write_json(self.path, state)
        self.hook("SessionStart")
        state = csw.read_json(self.path)
        self.assertEqual(state["owner_pid"], os.getpid())
        self.assertEqual(state["model"], "deepseek-v4-pro[1m]")
        self.assertTrue(hot.running(state))

    def test_native_hook_without_optional_startup_model_does_not_false_block(self):
        self.hook("SessionStart", model=None)
        self.assertEqual(self.hook("UserPromptSubmit"), {})
        state = csw.read_json(self.path)
        self.assertIsNone(state["model"])
        self.assertTrue(state["busy"])

    def test_first_event_in_preexisting_session_can_be_prompt_without_model(self):
        with patch.object(shared, "claude_parent", return_value=os.getpid()):
            result = shared.hook(csw, {"session_id": "preexisting-session", "hook_event_name": "UserPromptSubmit"},
                                 csw.read_json(csw.LIVE)["env"])
        self.assertEqual(result, {})
        state = csw.read_json(csw.ROOT / "shared-sessions/preexisting-session.json")
        self.assertIsNone(state["model"])
        self.assertTrue(state["busy"])

    def test_unknown_model_still_checks_reloaded_credentials(self):
        self.hook("SessionStart", model=None)
        environment = csw.read_json(csw.LIVE)["env"]
        environment["ANTHROPIC_AUTH_TOKEN"] = "stale"
        with self.assertRaisesRegex(ValueError, "recargado"):
            shared.hook(csw, {**self.payload, "hook_event_name": "UserPromptSubmit"}, environment)

    def test_statusline_supplies_native_model_when_session_start_omits_it(self):
        self.hook("SessionStart", model=None)
        with patch.object(shared, "claude_parent", return_value=os.getpid()):
            shared.observe_model(csw, {"session_id": "test-session", "model": {"id": "deepseek-v4-pro[1m]"}}, {})
        self.assertEqual(csw.read_json(self.path)["model"], "deepseek-v4-pro[1m]")
        self.assertEqual(self.hook("UserPromptSubmit"), {})

    def test_delayed_statusline_cannot_overwrite_a_model_switch(self):
        self.hook("SessionStart", model=None)
        self.hook("PostModelSwitch", to_model="deepseek-v4-pro[1m]")
        with patch.object(shared, "claude_parent", return_value=os.getpid()):
            shared.observe_model(csw, {"session_id": "test-session", "model": {"id": "gpt-5.6-sol"}}, {})
        self.assertEqual(csw.read_json(self.path)["model"], "deepseek-v4-pro[1m]")

    def test_statusline_repairs_previous_provider_model_in_preexisting_session(self):
        self.hook("SessionStart", model="deepseek-flash[1m]")
        glm = {"model": "glm-5.3-flash[1m]", "env": {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic", "ANTHROPIC_AUTH_TOKEN": "glm-test"}}
        profile = csw.ROOT / csw.ALIASES["glm"]
        csw.write_json(profile, glm)
        catalog = {"models": [{"model": "glm-5.3-flash", "label": "Flash"}]}
        with patch.object(csw, "model_catalog", return_value=catalog):
            csw.switch("glm")
        with patch.object(shared, "claude_parent", return_value=os.getpid()):
            shared.observe_model(csw, {"session_id": "test-session", "model": {"id": "glm-5.3-flash[1m]"}}, {})
        self.assertEqual(csw.read_json(self.path)["model"], "glm-5.3-flash[1m]")
        self.assertEqual(self.hook("UserPromptSubmit"), {})

    def test_unknown_model_does_not_allow_an_explicit_wrong_model_switch(self):
        self.hook("SessionStart", model=None)
        with self.assertRaisesRegex(ValueError, "/model"):
            self.hook("PreModelSwitch", to_model="gpt-5.6-sol")

    def test_clear_keeps_current_model_instead_of_startup_provider(self):
        self.hook("SessionStart")
        state = csw.read_json(self.path)
        state.update(model="deepseek-flash[1m]", status="closed")
        csw.write_json(self.path, state)
        with patch.object(shared, "claude_parent", return_value=os.getpid()):
            shared.hook(csw, {"session_id": "after-clear", "hook_event_name": "SessionStart"},
                        {**csw.read_json(csw.LIVE)["env"], "CSW_INITIAL_MODEL": "old-startup-model"})
        self.assertEqual(csw.read_json(csw.ROOT / "shared-sessions/after-clear.json")["model"], "deepseek-flash[1m]")

    def test_active_native_relogin_republishes_without_pinning_access(self):
        csw.switch("claude")
        def login(command, env):
            csw.write_json(csw.ROOT / ".credentials.json", {"claudeAiOauth": {"accessToken": "renewed", "expiresAt": (csw.time.time() + 3600) * 1000}})
            return 0
        with patch.object(csw.subprocess, "call", side_effect=login):
            self.assertEqual(csw.login("claude"), 0)
        self.assertEqual(csw.read_json(csw.LIVE)["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "")
        self.assertNotIn("renewed", csw.LIVE.read_text())

    def test_primary_login_renewal_or_expiry_does_not_block_prompts(self):
        csw.switch("claude")
        self.payload["model"] = "opus[1m]"
        self.hook("SessionStart")
        # Claude Code renews its own token; an about-to-expire one is its business too.
        csw.write_json(csw.ROOT / ".credentials.json", {"claudeAiOauth": {
            "accessToken": "renewed-by-claude", "refreshToken": "r", "expiresAt": (csw.time.time() + 30) * 1000}})
        self.assertEqual(self.hook("UserPromptSubmit"), {})

    def test_model_change_updates_codex_context_without_pinning_model(self):
        csw.write_json(csw.CODEX_NATIVE_CATALOG, {"models": [{"slug": "gpt-5.6-sol", "context_window": 272000, "max_context_window": 872000}, {"slug": "gpt-5.5", "context_window": 272000, "max_context_window": 272000}]})
        csw.atomic_write(csw.CODEX_NATIVE_CONFIG, "model_context_window = 1050000\n")
        catalog = {"models": [{"model": "gpt-5.6-sol", "label": "Sol"}, {"model": "gpt-5.5", "label": "5.5"}]}
        with patch.object(csw, "check_proxy"), patch.object(csw, "model_catalog", return_value=catalog):
            csw.switch("codex")
        self.hook("SessionStart", model="gpt-5.6-sol")
        self.hook("PreModelSwitch", to_model="gpt-5.5")
        self.hook("PostModelSwitch", to_model="gpt-5.5")
        settings = csw.read_json(csw.LIVE)
        self.assertEqual(settings["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "272000")
        self.assertEqual(settings["env"]["ANTHROPIC_MODEL"], "")
        self.assertEqual(settings["model"], "gpt-5.5")


if __name__ == "__main__":
    unittest.main()
