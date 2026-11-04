"""Interrupted turns release stale flags without releasing active work."""
import copy
import json
import unittest

import test_csw

csw = test_csw.csw
hot = csw.hot_module()


class CancellationTest(unittest.TestCase):
    def setUp(self):
        test_csw.ProfilesTest.setUp(self)
        self.transcript = csw.ROOT / "session.jsonl"
        self.state = {"busy": True, "agents": [], "transcript_path": str(self.transcript)}
        self.interruption = {
            "type": "user", "isSidechain": False,
            "timestamp": "1970-01-01T00:00:11.000Z",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        }

    def history(self, *records):
        csw.atomic_write(self.transcript, "".join(json.dumps(record) + "\n" for record in records))

    def assert_blocked(self, state=None):
        state = self.state if state is None else state
        with self.assertRaisesRegex(ValueError, "trabajando"):
            hot.ensure_model_switch_idle(state)

    def test_native_interruptions_release_legacy_busy_flag(self):
        for message in ("[Request interrupted by user]", "[Request interrupted by user for tool use]"):
            for content in (message, [{"type": "text", "text": message}]):
                with self.subTest(content=content):
                    record = copy.deepcopy(self.interruption)
                    record["message"]["content"] = content
                    self.history(record)
                    state = dict(self.state)
                    hot.ensure_model_switch_idle(state)
                    self.assertFalse(state["busy"])

    def test_model_commands_and_metadata_after_interrupt_are_ignored(self):
        self.history(self.interruption,
                     {"type": "system", "subtype": "local_command", "content": "/model"},
                     {"type": "attachment", "attachment": {"type": "output_style"}},
                     {"type": "system", "subtype": "away_summary"})
        hot.ensure_model_switch_idle(self.state)
        self.assertFalse(self.state["busy"])

    def test_new_conversation_activity_after_interrupt_remains_busy(self):
        for record in (
            {"type": "user", "message": {"role": "user", "content": "Continue"}},
            {"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use",
                                               "content": [{"type": "tool_use", "name": "Read"}]}},
        ):
            with self.subTest(record=record):
                self.history(self.interruption, record)
                self.assert_blocked()
                self.assertTrue(self.state["busy"])

    def test_new_prompt_hook_prevents_using_older_interrupt(self):
        self.history(self.interruption)
        state = {**self.state, "busy_since": 12.0}
        self.assert_blocked(state)
        self.assertTrue(state["busy"])

    def test_current_turn_interruption_clears_its_timestamp(self):
        self.history(self.interruption)
        state = {**self.state, "busy_since": 10.0}
        hot.ensure_model_switch_idle(state)
        self.assertFalse(state["busy"])
        self.assertNotIn("busy_since", state)

    def test_subagents_remain_protected_when_parent_is_interrupted(self):
        self.history(self.interruption)
        state = {**self.state, "agents": ["worker"]}
        self.assert_blocked(state)
        self.assertEqual(state["agents"], ["worker"])

    def test_sidechain_and_meta_interruptions_cannot_clear_root(self):
        for flags in ({"isSidechain": True}, {"isMeta": True}, {"agentId": "worker"}):
            with self.subTest(flags=flags):
                self.history({**self.interruption, **flags})
                self.assert_blocked()
                self.assertTrue(self.state["busy"])

    def test_quoted_text_and_tool_results_are_not_cancellation_receipts(self):
        for content in (
            "Quoted example: [Request interrupted by user]",
            [{"type": "tool_result", "content": "[Request interrupted by user]"}],
            [{"type": "text", "text": "[Request interrupted by user]"},
             {"type": "text", "text": "Continue"}],
        ):
            with self.subTest(content=content):
                record = copy.deepcopy(self.interruption)
                record["message"]["content"] = content
                self.history(record)
                self.assert_blocked()

    def test_unreadable_incomplete_or_invalid_histories_stay_blocked(self):
        self.assert_blocked()
        for data in ("{bad json}\n", json.dumps(self.interruption),
                     json.dumps(self.interruption) + '\n{"type":"user"', "[]\n"):
            with self.subTest(data=data):
                csw.atomic_write(self.transcript, data)
                self.assert_blocked()
                self.assertTrue(self.state["busy"])

    def test_missing_or_invalid_interruption_timestamps_stay_blocked(self):
        for timestamp in (None, "invalid", "1970-01-01T00:00:11"):
            with self.subTest(timestamp=timestamp):
                self.history({**self.interruption, "timestamp": timestamp})
                self.assert_blocked()


class AgentCompletionTest(unittest.TestCase):
    def setUp(self):
        test_csw.ProfilesTest.setUp(self)
        self.transcript = csw.ROOT / "session.jsonl"
        self.state = {"busy": False, "agents": ["worker"], "transcript_path": str(self.transcript)}

    def history(self, *records):
        csw.atomic_write(self.transcript, "".join(json.dumps(record) + "\n" for record in records))

    def notification(self, status="completed", agent="worker"):
        return {
            "type": "user", "isSidechain": False, "timestamp": "1970-01-01T00:00:11.000Z",
            "origin": {"kind": "task-notification"}, "promptSource": "system",
            "message": {"role": "user", "content":
                        f"<task-notification>\n<task-id>{agent}</task-id>\n"
                        f"<status>{status}</status>\n</task-notification>"},
        }

    def assert_blocked(self, state=None):
        with self.assertRaisesRegex(ValueError, "trabajando"):
            hot.ensure_model_switch_idle(self.state if state is None else state)

    def test_terminal_task_notifications_release_stale_agents(self):
        for status in ("completed", "failed", "killed"):
            with self.subTest(status=status):
                self.history(self.notification(status))
                state = dict(self.state)
                hot.ensure_model_switch_idle(state)
                self.assertEqual(state["agents"], [])

    def test_structured_completed_agent_result_releases_stale_agent(self):
        self.history({"type": "user", "isSidechain": False, "timestamp": "1970-01-01T00:00:11.000Z",
                      "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call"}]},
                      "toolUseResult": {"agentId": "worker", "status": "completed"}})
        hot.ensure_model_switch_idle(self.state)
        self.assertEqual(self.state["agents"], [])

    def test_user_and_sidechain_notifications_cannot_release_agents(self):
        for fields in ({"origin": None}, {"promptSource": "user"}, {"isSidechain": True},
                       {"agentId": "child"}, {"timestamp": None}):
            with self.subTest(fields=fields):
                self.history({**self.notification(), **fields})
                self.assert_blocked()
                self.assertEqual(self.state["agents"], ["worker"])

    def test_active_unknown_or_unreadable_agent_stays_registered(self):
        self.assert_blocked()
        for status in ("running", "async_launched", "unknown"):
            with self.subTest(status=status):
                self.history(self.notification(status))
                self.assert_blocked()
                self.assertEqual(self.state["agents"], ["worker"])

    def test_later_relaunch_overrides_previous_completion(self):
        self.history(self.notification(),
                     {"type": "user", "isSidechain": False, "timestamp": "1970-01-01T00:00:12.000Z",
                      "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "new"}]},
                      "toolUseResult": {"agentId": "worker", "status": "async_launched", "isAsync": True}})
        self.assert_blocked()
        self.assertEqual(self.state["agents"], ["worker"])

    def test_new_agent_start_cannot_use_old_completion_receipt(self):
        self.history(self.notification())
        state = {**self.state, "agents_started": {"worker": 12.0}}
        self.assert_blocked(state)
        self.assertEqual(state["agents"], ["worker"])

    def test_only_completed_agents_are_pruned(self):
        self.history(self.notification("failed", "finished"), self.notification("running", "worker"))
        state = {**self.state, "agents": ["finished", "worker"],
                 "agents_started": {"finished": 10.0, "worker": 10.0}}
        self.assert_blocked(state)
        self.assertEqual(state["agents"], ["worker"])
        self.assertEqual(state["agents_started"], {"worker": 10.0})

    def test_old_completion_is_found_beyond_the_cancellation_tail(self):
        self.history(self.notification(), {"type": "system", "content": "x" * (1024 * 1024 + 200)})
        hot.ensure_model_switch_idle(self.state)
        self.assertEqual(self.state["agents"], [])

    def test_incomplete_history_cannot_release_agents(self):
        csw.atomic_write(self.transcript, json.dumps(self.notification()) + '\n{"type":"user"')
        self.assert_blocked()
        self.assertEqual(self.state["agents"], ["worker"])


class WorkflowCompletionTest(unittest.TestCase):
    def setUp(self):
        test_csw.ProfilesTest.setUp(self)
        self.transcript = csw.ROOT / "session.jsonl"
        self.state = {"busy": False, "agents": ["worker"], "transcript_path": str(self.transcript)}
        csw.atomic_write(self.transcript, "")
        self.workflow = csw.ROOT / "session/subagents/workflows/wf_fixture"
        self.workflow.mkdir(parents=True)
        self.journal = self.workflow / "journal.jsonl"

    def journal_records(self, *records):
        csw.atomic_write(self.journal, "".join(json.dumps(row) + "\n" for row in records))

    def assert_blocked(self, state=None):
        with self.assertRaisesRegex(ValueError, "trabajando"):
            hot.ensure_model_switch_idle(self.state if state is None else state)

    def task_output(self, status, retrieval="success"):
        csw.atomic_write(self.transcript, json.dumps({
            "type": "user", "isSidechain": False, "timestamp": "1970-01-01T00:00:11.000Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call"}]},
            "toolUseResult": {"retrieval_status": retrieval,
                              "task": {"task_id": "worker", "task_type": "local_agent", "status": status}},
        }) + "\n")

    def test_taskoutput_success_clears_finished_agent(self):
        for status in ("completed", "failed", "killed"):
            with self.subTest(status=status):
                self.task_output(status)
                state = dict(self.state)
                hot.ensure_model_switch_idle(state)
                self.assertEqual(state["agents"], [])

    def test_taskoutput_timeout_and_running_status_keep_agent(self):
        for status, retrieval in (("running", "timeout"), ("running", "success"), ("failed", "timeout")):
            with self.subTest(status=status, retrieval=retrieval):
                self.task_output(status, retrieval)
                self.assert_blocked()

    def test_latest_workflow_failure_or_result_clears_legacy_agent(self):
        for kind in ("failed", "result"):
            with self.subTest(kind=kind):
                self.journal_records({"type": "started", "agentId": "worker"},
                                     {"type": kind, "agentId": "worker"})
                state = dict(self.state)
                hot.ensure_model_switch_idle(state)
                self.assertEqual(state["agents"], [])

    def test_later_workflow_start_and_unknown_status_keep_agent(self):
        for kind in ("started", "unknown"):
            with self.subTest(kind=kind):
                self.journal_records({"type": "failed", "agentId": "worker"},
                                     {"type": kind, "agentId": "worker"})
                self.assert_blocked()

    def test_workflows_are_scoped_to_the_registered_session(self):
        foreign = csw.ROOT / "other/subagents/workflows/wf_other/journal.jsonl"
        foreign.parent.mkdir(parents=True)
        csw.atomic_write(foreign, json.dumps({"type": "failed", "agentId": "worker"}) + "\n")
        self.assert_blocked()

    def test_partial_workflow_journal_does_not_release_agents(self):
        csw.atomic_write(self.journal, json.dumps({"type": "failed", "agentId": "worker"}) + '\n{"type":')
        self.assert_blocked()

    def test_new_agent_start_requires_a_current_workflow_receipt(self):
        self.journal_records({"type": "failed", "agentId": "worker"})
        sidechain = self.workflow / "agent-worker.jsonl"
        csw.atomic_write(sidechain, json.dumps({"type": "assistant", "timestamp": "1970-01-01T00:00:11.000Z",
                                             "agentId": "worker", "isSidechain": True}) + "\n")
        state = {**self.state, "agents_started": {"worker": 12.0}}
        self.assert_blocked(state)
        current = {**self.state, "agents_started": {"worker": 10.0}}
        hot.ensure_model_switch_idle(current)
        self.assertEqual(current["agents"], [])

    def test_new_agent_without_dated_sidechain_stays_registered(self):
        self.journal_records({"type": "failed", "agentId": "worker"})
        self.assert_blocked({**self.state, "agents_started": {"worker": 12.0}})

    def test_active_conflicting_workflow_keeps_agent(self):
        self.journal_records({"type": "failed", "agentId": "worker"})
        other = self.workflow.parent / "wf_second/journal.jsonl"
        other.parent.mkdir()
        csw.atomic_write(other, json.dumps({"type": "started", "agentId": "worker"}) + "\n")
        self.assert_blocked()

    def test_completed_sidechain_without_workflow_receipt_is_insufficient(self):
        sidechain = self.workflow / "agent-worker.jsonl"
        csw.atomic_write(sidechain, json.dumps({"type": "assistant", "timestamp": "1970-01-01T00:00:11.000Z",
                                             "message": {"role": "assistant", "stop_reason": "end_turn"}}) + "\n")
        self.assert_blocked()


if __name__ == "__main__":
    unittest.main()
