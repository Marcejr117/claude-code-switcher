"""Hot-session routing tests. All accounts and credentials are synthetic."""
import copy
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_csw

csw = test_csw.csw

hot = csw.hot_module()


class HotTest(unittest.TestCase):
    def setUp(self):
        test_csw.ProfilesTest.setUp(self)
        self.directory = csw.ROOT / "hot-sessions/0123456789ab"
        self.directory.mkdir(parents=True, mode=0o700)
        self.catalog = [{"model": "deepseek-v4-pro", "label": "Pro"}, {"model": "deepseek-flash", "label": "Flash"}]
        self.state = {"version": 1, "id": self.directory.name, "owner_pid": os.getpid(),
                      "owner_start": hot.process_identity(os.getpid()), "status": "running",
                      "profile": csw.ALIASES["deepseek"], "label": "deepseek", "provider": "DeepSeek",
                      "catalog": self.catalog, "model": "deepseek-v4-pro[1m]", "phase": "ready",
                      "revision": 1, "busy": False, "agents": [], "cwd": "/tmp", "auth_error": None}
        csw.write_json(self.directory / "state.json", self.state)
        csw.write_json(self.directory / "settings.json", hot.configured(csw, self.directory, {}, self.direct, self.state))

    def read_state(self):
        return csw.read_json(self.directory / "state.json")

    def environment(self):
        return csw.read_json(self.directory / "settings.json")["env"]

    def call(self, event, **extra):
        return hot.hook(csw, self.directory.name, {"hook_event_name": event, **extra}, self.environment())

    def goal_history(self, attachments):
        transcript = self.directory / "projects/fixture/session.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        csw.atomic_write(transcript, "".join(json.dumps({"type": "attachment", "attachment": item}) + "\n" for item in attachments))
        state = self.read_state()
        state.update(transcript_path=str(transcript), prompt_seen=True)
        csw.write_json(self.directory / "state.json", state)
        return transcript

    def native_fixture(self):
        profile = csw.resolve("claude")
        csw.write_json(csw.ROOT / ".credentials.json", {"claudeAiOauth": {
            "accessToken": "native-access", "expiresAt": (time.time() + 3600) * 1000}})
        state = {**self.state, "provider": "Claude sub", "profile": profile.name, "label": "claude",
                 "model": "opus[1m]", "catalog": [{"model": "opus", "label": "Opus"}]}
        target = hot.credentials(csw, profile, self.native)
        csw.write_json(self.directory / "state.json", state)
        csw.write_json(self.directory / "settings.json", hot.configured(csw, self.directory, {}, target, state))
        return profile

    def test_native_oauth_hidden_from_hook_environment_is_not_a_reload_failure(self):
        self.native_fixture()
        environment = self.environment()
        environment.pop("CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual(hot.hook(csw, self.directory.name, {"hook_event_name": "UserPromptSubmit"}, environment), {})
        self.assertTrue(self.read_state()["busy"])

    def test_goal_in_evaluation_blocks_account_change_even_after_stop(self):
        self.goal_history([{"type": "goal_status", "met": False, "sentinel": True, "condition": "fixture"}])
        self.call("Stop")
        self.assertFalse(self.read_state()["busy"])
        before = (self.directory / "settings.json").read_bytes()
        with patch.object(hot, "prepare_target", return_value=(self.proxy, [{"model": "gpt-5.6-sol", "label": "Sol"}])), self.assertRaisesRegex(ValueError, "goal"):
            hot.use_profile(csw, self.directory, csw.resolve("codex"))
        self.assertEqual(before, (self.directory / "settings.json").read_bytes())

    def test_native_oauth_visible_mismatch_still_blocks(self):
        self.native_fixture()
        environment = {**self.environment(), "CLAUDE_CODE_OAUTH_TOKEN": "another-account"}
        with self.assertRaises(ValueError):
            hot.hook(csw, self.directory.name, {"hook_event_name": "UserPromptSubmit"}, environment)

    def test_native_oauth_hidden_token_does_not_allow_stale_configured_access(self):
        self.native_fixture()
        settings = csw.read_json(self.directory / "settings.json")
        settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] = "stale-access"
        csw.write_json(self.directory / "settings.json", settings)
        environment = self.environment()
        environment.pop("CLAUDE_CODE_OAUTH_TOKEN")
        with self.assertRaisesRegex(ValueError, "OAuth"):
            hot.hook(csw, self.directory.name, {"hook_event_name": "UserPromptSubmit"}, environment)

    def test_goal_clear_achievement_and_impossible_release_switching(self):
        for final in ({"met": True, "sentinel": True}, {"met": True}, {"met": False, "failed": True}):
            with self.subTest(final=final):
                self.goal_history([{"type": "goal_status", "met": False, "sentinel": True},
                                   {"type": "goal_status", **final}])
                self.assertFalse(hot.goal_active(self.read_state()))
                hot.ensure_switch_idle(self.read_state())

    def test_goal_parser_ignores_sidechains_and_quoted_user_text(self):
        transcript = self.goal_history([])
        records = [
            {"type": "user", "message": {"content": {"type": "goal_status", "met": False}}},
            {"type": "attachment", "isSidechain": True, "attachment": {"type": "goal_status", "met": False}},
        ]
        csw.atomic_write(transcript, "".join(json.dumps(x) + "\n" for x in records))
        self.assertFalse(hot.goal_active(self.read_state()))

    def test_active_goal_allows_model_changes_and_keeps_the_goal(self):
        transcript = self.goal_history([{"type": "goal_status", "met": False, "sentinel": True}])
        before = transcript.read_bytes()
        self.assertEqual(self.call("PreModelSwitch", to_model="deepseek-flash"), {})
        self.call("PostModelSwitch", to_model="deepseek-flash")
        self.assertEqual(transcript.read_bytes(), before)
        self.assertTrue(hot.goal_active(self.read_state()))
        self.assertEqual(self.read_state()["model"], "deepseek-flash")
        self.call("UserPromptSubmit", prompt="Continue the harmless goal")
        self.assertTrue(self.read_state()["busy"])

    def test_model_change_with_goal_still_waits_for_turns_and_subagents(self):
        self.goal_history([{"type": "goal_status", "met": False, "sentinel": True}])
        for busy, agents in ((True, []), (False, ["worker"])):
            state = self.read_state()
            state.update(busy=busy, agents=agents)
            csw.write_json(self.directory / "state.json", state)
            with self.assertRaisesRegex(ValueError, "trabajando"):
                self.call("PreModelSwitch", to_model="deepseek-flash")

    def test_missing_goal_history_fails_closed_after_a_prompt(self):
        state = self.read_state()
        state.update(transcript_path=str(self.directory / "missing.jsonl"), prompt_seen=True)
        with self.assertRaisesRegex(ValueError, "goal"):
            hot.goal_active(state)
        self.assertFalse(hot.goal_active({**state, "prompt_seen": False}))

    def test_incomplete_goal_history_never_means_idle(self):
        transcript = self.goal_history([{"type": "goal_status", "met": True}])
        with transcript.open("a") as stream:
            stream.write('{"type":"attachment"')
        with self.assertRaisesRegex(ValueError, "actualizando"):
            hot.goal_active(self.read_state())

    def test_new_conversation_does_not_keep_previous_goal_history(self):
        self.goal_history([{"type": "goal_status", "met": False, "sentinel": True}])
        self.call("SessionStart", session_id="new-session", transcript_path=str(self.directory / "new-session.jsonl"))
        self.assertFalse(self.read_state()["prompt_seen"])
        self.assertFalse(hot.goal_active(self.read_state()))

    def test_session_identity_ignores_reused_or_dead_pids(self):
        self.assertTrue(hot.running(self.state))
        self.assertFalse(hot.running({**self.state, "owner_start": "different"}))
        self.assertFalse(hot.running({**self.state, "owner_pid": -1}))

    def test_session_paths_reject_traversal_and_symlinks(self):
        for value in ("../other", "anything", "/tmp/0123456789ab"):
            with self.assertRaises(ValueError):
                hot.session_path(csw, value)
        (self.directory.parent / "abcdef012345").symlink_to(self.directory)
        with self.assertRaises(ValueError):
            hot.session_path(csw, "abcdef012345")

    def test_live_child_remains_registered_if_supervisor_exits(self):
        state = {**self.state, "owner_pid": -1, "claude_pid": os.getpid(), "claude_start": hot.process_identity(os.getpid())}
        self.assertTrue(hot.running(state))
        self.assertFalse(hot.running({**state, "claude_start": "reused"}))
        self.assertFalse(hot.running({**state, "status": "closed"}))

    def test_config_masks_previous_auth_and_does_not_pin_model(self):
        current = {"env": {"ANTHROPIC_API_KEY": "old", "CLAUDE_CODE_OAUTH_TOKEN": "old-oauth"},
                   "permissions": {"deny": ["keep"]}}
        data = hot.configured(csw, self.directory, current, self.proxy, self.state)
        self.assertEqual(data["env"]["ANTHROPIC_API_KEY"], "")
        self.assertEqual(data["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "")
        self.assertEqual(data["env"]["ANTHROPIC_AUTH_TOKEN"], "unused")
        self.assertEqual(data["env"]["ANTHROPIC_MODEL"], "")
        self.assertEqual(data["permissions"], current["permissions"])

    def test_config_preserves_hooks_without_duplicating_guards(self):
        current = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing"}]}]}}
        once = hot.configured(csw, self.directory, current, self.direct, self.state)
        twice = hot.configured(csw, self.directory, once, self.direct, self.state)
        self.assertEqual(once, twice)
        self.assertEqual(len(twice["hooks"]["Stop"]), 2)

    def test_turn_and_subagent_lifecycle(self):
        self.call("UserPromptSubmit")
        self.assertTrue(self.read_state()["busy"])
        self.call("SubagentStart", agent_id="worker")
        self.call("Stop", agent_id="worker")
        self.assertTrue(self.read_state()["busy"])
        self.call("Stop")
        self.assertFalse(self.read_state()["busy"])
        self.assertEqual(self.read_state()["agents"], ["worker"])
        self.call("SubagentStop", agent_id="worker")
        self.assertEqual(self.read_state()["agents"], [])

    def test_interrupted_turn_allows_model_change_without_stop_hook(self):
        transcript = self.directory / "interrupted.jsonl"
        self.call("SessionStart", session_id="test-session", transcript_path=str(transcript))
        with patch.object(hot.time, "time", return_value=10.0):
            self.call("UserPromptSubmit")
        csw.atomic_write(transcript, json.dumps({
            "type": "user", "isSidechain": False, "timestamp": "1970-01-01T00:00:11.000Z",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        }) + "\n")
        self.assertEqual(self.call("PreModelSwitch", to_model="deepseek-flash"), {})
        self.assertFalse(self.read_state()["busy"])
        self.call("PostModelSwitch", to_model="deepseek-flash")
        self.assertEqual(self.read_state()["model"], "deepseek-flash")

    def test_prompt_hook_records_time_before_transcript_is_appended(self):
        transcript = self.directory / "interrupted.jsonl"
        csw.atomic_write(transcript, json.dumps({
            "type": "user", "isSidechain": False, "timestamp": "1970-01-01T00:00:11.000Z",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        }) + "\n")
        self.call("SessionStart", session_id="test-session", transcript_path=str(transcript))
        with patch.object(hot.time, "time", return_value=12.0):
            self.call("UserPromptSubmit")
        self.assertEqual(self.read_state()["busy_since"], 12.0)
        with self.assertRaisesRegex(ValueError, "trabajando"):
            self.call("PreModelSwitch", to_model="deepseek-flash")

    def test_completed_subagent_releases_model_switch_without_subagent_stop_hook(self):
        transcript = self.directory / "agent-finished.jsonl"
        self.call("SessionStart", session_id="test-session", transcript_path=str(transcript))
        with patch.object(hot.time, "time", return_value=10.0):
            self.call("SubagentStart", agent_id="worker")
        self.assertEqual(self.read_state()["agents_started"], {"worker": 10.0})
        csw.atomic_write(transcript, json.dumps({
            "type": "user", "isSidechain": False, "timestamp": "1970-01-01T00:00:11.000Z",
            "origin": {"kind": "task-notification"}, "promptSource": "system",
            "message": {"role": "user", "content": "<task-notification>\n<task-id>worker</task-id>\n<status>completed</status>\n</task-notification>"},
        }) + "\n")
        self.assertEqual(self.call("PreModelSwitch", to_model="deepseek-flash"), {})
        self.assertEqual(self.read_state()["agents"], [])
        self.assertNotIn("agents_started", self.read_state())

    def test_idle_background_job_releases_a_turn_that_never_reported_stop(self):
        root = self.directory / "claude"
        transcript = root / "projects" / "proj" / "0c239207-full-session-id.jsonl"
        transcript.parent.mkdir(parents=True)
        csw.atomic_write(transcript, "")
        job = root / "jobs" / "0c239207" / "state.json"
        job.parent.mkdir(parents=True)
        state = {"busy": True, "busy_since": 100.0, "agents": [], "transcript_path": str(transcript)}

        def write(tempo, tasks, when):
            csw.write_json(job, {"tempo": tempo, "inFlight": {"tasks": tasks, "queued": 0},
                                 "updatedAt": hot.datetime.fromtimestamp(when, hot.timezone.utc).isoformat()})
        for tempo, tasks, when in (("busy", 0, 200), ("idle", 1, 200), ("idle", 0, 50)):
            write(tempo, tasks, when)
            with self.subTest(tempo=tempo, tasks=tasks, when=when), self.assertRaisesRegex(ValueError, "trabajando"):
                hot.ensure_model_switch_idle(dict(state))
        write("idle", 0, 200)
        hot.ensure_model_switch_idle(state)
        self.assertFalse(state["busy"])

    def test_finished_nested_subagent_releases_model_switch(self):
        # A grandchild agent's notification goes to its parent agent; only its meta file says it ended.
        transcript = self.directory / "nested.jsonl"
        csw.atomic_write(transcript, json.dumps({"type": "queue-operation", "content": "<task-id>grandchild</task-id>"}) + "\n")
        self.call("SessionStart", session_id="test-session", transcript_path=str(transcript))
        self.call("SubagentStart", agent_id="grandchild")
        meta = self.directory / "nested" / "subagents" / "agent-grandchild.meta.json"
        meta.parent.mkdir(parents=True)
        csw.write_json(meta, {"finished": False})
        with self.assertRaisesRegex(ValueError, "subagentes"):
            self.call("PreModelSwitch", to_model="deepseek-flash")
        csw.write_json(meta, {"finished": True})
        self.assertEqual(self.call("PreModelSwitch", to_model="deepseek-flash"), {})
        self.assertEqual(self.read_state()["agents"], [])

    def test_project_overrides_and_reload_races_block_requests(self):
        env = self.environment()
        for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CSW_HOT_REVISION"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                hot.hook(csw, self.directory.name, {"hook_event_name": "UserPromptSubmit"}, {**env, key: "different"})
        self.assertFalse(self.read_state()["busy"])

    def test_wrong_provider_model_switch_is_blocked(self):
        with self.assertRaises(ValueError):
            self.call("PreModelSwitch", to_model="gpt-daybreak-blue-latest")

    def test_switch_while_busy_or_with_subagents_changes_nothing(self):
        for fields in ({"busy": True}, {"agents": ["worker"]}):
            state = {**self.state, **fields}
            csw.write_json(self.directory / "state.json", state)
            before = (self.directory / "settings.json").read_bytes()
            with patch.object(hot, "prepare_target", return_value=(self.proxy, [{"model": "gpt-5.6-sol", "label": "Sol"}])), self.assertRaises(ValueError):
                hot.use_profile(csw, self.directory, csw.resolve("codex"))
            self.assertEqual(before, (self.directory / "settings.json").read_bytes())
            self.assertEqual(state, self.read_state())

    def test_account_switch_blocks_prompt_until_native_model_selection(self):
        global_before = csw.LIVE.read_bytes()
        with patch.object(hot, "prepare_target", return_value=(self.proxy, [{"model": "gpt-5.6-sol", "label": "Sol"}])):
            hot.use_profile(csw, self.directory, csw.resolve("codex"))
        self.assertEqual(self.read_state()["phase"], "choose-model")
        with self.assertRaises(ValueError):
            self.call("UserPromptSubmit")
        self.call("PreModelSwitch", to_model="gpt-5.6-sol")
        self.call("PostModelSwitch", to_model="gpt-5.6-sol")
        self.assertEqual(self.read_state()["phase"], "ready")
        self.call("UserPromptSubmit")
        self.assertEqual(csw.LIVE.read_bytes(), global_before)

    def test_same_model_account_change_does_not_need_a_noop_model_hook(self):
        other = csw.ROOT / "settings.json.DEEPSEEK.other.bak"
        target = copy.deepcopy(self.direct)
        target["env"]["ANTHROPIC_AUTH_TOKEN"] = "other-account"
        csw.write_json(other, target)
        with patch.object(hot, "prepare_target", return_value=(target, self.catalog)):
            hot.use_profile(csw, self.directory, other)
        self.assertEqual(self.read_state()["phase"], "ready")
        self.assertEqual(self.environment()["ANTHROPIC_AUTH_TOKEN"], "other-account")

    def test_model_change_recalculates_context_for_smaller_codex_model(self):
        state = {**self.state, "provider": "Codex", "profile": csw.ALIASES["codex"],
                 "catalog": [{"model": "gpt-5.5", "label": "5.5"}]}
        csw.write_json(self.directory / "state.json", state)
        self.call("PostModelSwitch", to_model="gpt-5.5")
        self.assertEqual(self.environment()["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "272000")

    def test_claude_uses_only_short_access_from_own_store(self):
        with patch.object(csw, "login", return_value=0):
            csw.add_account("claude", "work")
        profile = csw.resolve("claude:work")
        store, _ = csw.account_directory(profile)
        data = {"claudeAiOauth": {"accessToken": "own-access", "refreshToken": "never-copy-me", "expiresAt": (time.time() + 3600) * 1000}}
        csw.write_json(store / ".credentials.json", data)
        target = hot.credentials(csw, profile, self.native)
        self.assertEqual(target["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "own-access")
        self.assertNotIn("never-copy-me", str(target))
        self.assertEqual(csw.read_json(store / ".credentials.json"), data)

    def test_claude_expired_token_fails_before_switch(self):
        csw.write_json(csw.ROOT / ".credentials.json", {"claudeAiOauth": {"accessToken": "expired", "expiresAt": 1}})
        before = (self.directory / "settings.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "csw login claude"):
            hot.use_profile(csw, self.directory, csw.resolve("claude"))
        self.assertEqual(before, (self.directory / "settings.json").read_bytes())

    def test_claude_refreshed_access_is_loaded_without_modifying_owning_store(self):
        state = {**self.state, "provider": "Claude sub", "profile": csw.ALIASES["claude"]}
        csw.write_json(self.directory / "state.json", state)
        store = csw.ROOT / ".credentials.json"
        data = {"claudeAiOauth": {"accessToken": "new-access", "refreshToken": "private-refresh", "expiresAt": (time.time() + 3600) * 1000}}
        csw.write_json(store, data)
        hot.refresh_access(csw, self.directory)
        self.assertEqual(self.environment()["CLAUDE_CODE_OAUTH_TOKEN"], "new-access")
        self.assertEqual(csw.read_json(store), data)
        before = (self.directory / "state.json").read_bytes()
        hot.refresh_access(csw, self.directory)
        self.assertEqual(before, (self.directory / "state.json").read_bytes())

    def test_live_profile_cannot_be_removed(self):
        csw.atomic_write(csw.ACTIVE, csw.ALIASES["codex"])
        with self.assertRaisesRegex(ValueError, "csw hot"):
            csw.remove_account("deepseek")

    def test_protection_disabling_flags_are_rejected(self):
        for flag in ("--safe-mode", "--bare", "--disable-hooks", "--no-session-persistence", "--settings", "--setting-sources=user"):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                hot.start(csw, csw.resolve("deepseek"), [flag])

    def test_picker_can_cancel_without_writes(self):
        before = csw.LIVE.read_bytes()
        with patch("builtins.input", return_value="q"):
            self.assertIsNone(hot.choose_profile(csw))
        self.assertEqual(before, csw.LIVE.read_bytes())

    def test_private_state_and_no_secret_in_model_picker(self):
        settings = csw.read_json(self.directory / "settings.json")
        self.assertNotIn("test-deepseek-key", str(settings["modelPicker"]))
        if os.name != "nt":  # POSIX modes only; Windows relies on the profile ACL
            self.assertEqual((self.directory / "state.json").stat().st_mode & 0o777, 0o600)
        if os.name != "nt":  # POSIX modes only; Windows relies on the profile ACL
            self.assertEqual((self.directory / "settings.json").stat().st_mode & 0o777, 0o600)

    def test_default_menu_opens_guided_home_in_a_terminal(self):
        ui = csw.ui_module()
        with patch.object(csw.sys.stdin, "isatty", return_value=True), patch.object(csw, "ui_module", return_value=ui), patch.object(ui, "home", return_value=0) as menu:
            self.assertEqual(csw.main([]), 0)
        menu.assert_called_once()

    def test_noninteractive_listing_never_launches_hot_menu(self):
        with patch.object(csw.sys.stdin, "isatty", return_value=False), patch.object(csw, "show_list") as listing, patch.object(hot, "main", side_effect=AssertionError("unexpected hot menu")):
            csw.main([])
        listing.assert_called_once()


if __name__ == "__main__":
    unittest.main()
