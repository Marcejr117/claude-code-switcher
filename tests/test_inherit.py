"""Global model/effort inheritance tests. No provider traffic."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

import test_csw

csw = test_csw.csw
shared = csw.shared_module()


class InheritanceTest(unittest.TestCase):
    def setUp(self):
        # The hook reads the live provider; never the developer's real settings.json.
        temp = tempfile.TemporaryDirectory(prefix="csw-test-")
        self.addCleanup(temp.cleanup)
        live, self.old_live = Path(temp.name) / "settings.json", csw.LIVE
        csw.LIVE = live
        self.addCleanup(setattr, csw, "LIVE", self.old_live)
        csw.write_json(live, {"env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"}})

    def test_native_claude_agents_keep_their_own_model_and_effort(self):
        csw.write_json(csw.LIVE, {"env": {}})
        self.assertEqual(shared.inherit_tool(csw, {"tool_name": "Agent", "tool_input": {
            "prompt": "fixture", "model": "haiku", "effort": "low"}}), {})

    def test_install_is_idempotent_and_keeps_permission_flow(self):
        settings = {"env": {}, "hooks": {}}
        shared.install(csw, settings)
        shared.install(csw, settings)
        rows = [row for row in settings["hooks"]["PreToolUse"]
                if any(h.get("command", "").endswith("csw inherit-hook") for h in row.get("hooks", []))]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["matcher"], "^(Agent|Workflow)$")

    def test_inline_workflow_drops_agent_effort_only(self):
        source = """// effort: 'high' is documentation
const metadata={effort:'metadata'}
const result=await agent(`p, ${metadata}`, {label:'x', model:'small', effort:'low', schema:{properties:{effort:{type:'string'}}}})
return result
"""
        output = shared.inherit_workflow_effort(source)
        self.assertNotIn("effort:'low'", output)
        self.assertIn("effort:'metadata'", output)
        self.assertIn("properties:{effort:{type:'string'}}", output)
        self.assertIn("model:'small'", output)  # Native FORCE owns model routing.

    def test_tool_rewrite_does_not_auto_approve(self):
        payload = {"tool_name": "Workflow", "tool_input": {
            "script": "const x=await agent('x',{effort:'max',label:'x'});return x"}}
        result = shared.inherit_tool(csw, payload)
        decision = result["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", decision)
        self.assertNotIn("effort:'max'", decision["updatedInput"]["script"])

    def test_direct_agent_removes_explicit_model_and_effort(self):
        result = shared.inherit_tool(csw, {"tool_name": "Agent", "tool_input": {
            "prompt": "fixture", "subagent_type": "general-purpose",
            "model": "haiku", "effort": "low"}})
        self.assertEqual(result["hookSpecificOutput"]["updatedInput"], {
            "prompt": "fixture", "subagent_type": "general-purpose"})

    def test_statusline_tracks_live_effort_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            old_root = csw.ROOT
            csw.ROOT = Path(temp)
            try:
                directory = csw.ROOT / "shared-sessions";directory.mkdir()
                state = {"owner_pid": os.getpid(), "owner_start": csw.hot_module().process_identity(os.getpid()),
                         "status": "running", "model": None, "busy": False, "agents": []}
                csw.write_json(directory / "fixture.json", state)
                original_parent = shared.claude_parent
                shared.claude_parent = lambda: os.getpid()
                try:
                    shared.observe_model(csw, {"session_id": "fixture", "effort": {"level": "max"}}, {})
                    self.assertEqual(csw.read_json(directory / "fixture.json")["effort"], "max")
                    shared.observe_model(csw, {"session_id": "fixture", "effort": {"level": "high"}}, {})
                    self.assertEqual(csw.read_json(directory / "fixture.json")["effort"], "high")
                finally:
                    shared.claude_parent = original_parent
            finally:
                csw.ROOT = old_root


if __name__ == "__main__":
    unittest.main()
