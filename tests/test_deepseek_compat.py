"""Wire compatibility tests use synthetic messages and a local fake upstream."""
import copy
import importlib.util
import io
import json
import threading
import unittest
import urllib.request
from unittest.mock import patch

import os
from pathlib import Path

import test_csw

csw = test_csw.csw
COMPAT_PATH = os.environ.get(
    "CSW_COMPAT", str(Path(__file__).resolve().parent.parent / "modules" / "deepseek_compat.py"))
spec = importlib.util.spec_from_file_location("deepseek_compat", COMPAT_PATH)
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


def image_history(kind="server_tool_use"):
    return {"model": "deepseek-flash", "messages": [
        {"role": "user", "content": "Describe a blue square."},
        {"role": "assistant", "content": [
            {"type": kind, "id": "old_image", "name": "analyze_image", "input": {}},
            {"type": "tool_result", "tool_use_id": "old_image", "content": "A blue square."},
            {"type": "text", "text": "Prior explanation."}]},
        {"role": "user", "content": "Reply OK."}]}


class HistoryTest(unittest.TestCase):
    def test_glm_server_image_pair_becomes_text_without_losing_result(self):
        payload = image_history()
        before = copy.deepcopy(payload)
        fixed, count = compat.normalize_history(payload)
        self.assertEqual(count, 1)
        self.assertEqual(payload, before)
        self.assertEqual([block["type"] for block in fixed["messages"][1]["content"]], ["text"] * 3)
        self.assertIn("A blue square.", fixed["messages"][1]["content"][1]["text"])
        self.assertEqual(compat.normalize_history(fixed), (fixed, 0))

    def test_native_client_relabelled_image_history_is_also_compatible(self):
        fixed, count = compat.normalize_history(image_history("tool_use"))
        self.assertEqual(count, 1)
        self.assertTrue(all(block["type"] == "text" for block in fixed["messages"][1]["content"]))

    def test_declared_image_tool_keeps_its_client_calls(self):
        payload = image_history("tool_use")
        payload["tools"] = [{"name": "analyze_image", "input_schema": {"type": "object"}}]
        self.assertEqual(compat.normalize_history(payload), (payload, 0))

    def test_normal_tools_thinking_and_search_blocks_are_unchanged(self):
        payload = {"tools": [{"name": "noop"}], "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Synthetic reasoning.", "signature": "keep"},
                {"type": "tool_use", "id": "normal", "name": "noop", "input": {}},
                {"type": "server_tool_use", "id": "search", "name": "web_search", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "normal", "content": "OK"}]}]}
        self.assertEqual(compat.normalize_history(payload), (payload, 0))

    def test_image_blocks_without_ids_do_not_match_unrelated_blocks(self):
        payload = image_history()
        payload["messages"][1]["content"][0].pop("id")
        self.assertEqual(compat.normalize_history(payload), (payload, 0))

    def test_glm_webreader_server_and_relabelled_client_history_become_text(self):
        for kind in ("server_tool_use", "tool_use"):
            with self.subTest(kind=kind):
                payload = image_history(kind)
                payload["messages"][1]["content"][0]["name"] = "webReader"
                fixed, count = compat.normalize_history(payload)
                self.assertEqual(count, 1)
                self.assertTrue(all(block["type"] == "text" for block in fixed["messages"][1]["content"]))
                self.assertIn("A blue square.", fixed["messages"][1]["content"][1]["text"])

    def test_generic_server_tool_with_assistant_result_is_preserved_as_text(self):
        payload = image_history()
        payload["messages"][1]["content"][0]["name"] = "legacy_provider_tool"
        fixed, count = compat.normalize_history(payload)
        self.assertEqual(count, 1)
        self.assertTrue(all(block["type"] == "text" for block in fixed["messages"][1]["content"]))

    def test_declared_client_webreader_is_not_rewritten(self):
        payload = image_history("tool_use")
        payload["messages"][1]["content"][0]["name"] = "webReader"
        payload["tools"] = [{"name": "webReader"}]
        self.assertEqual(compat.normalize_history(payload), (payload, 0))

    def test_historical_call_without_result_keeps_an_explicit_missing_result_record(self):
        payload = {"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "old", "name": "Read", "input": {"file_path": "fixture.txt"}}]},
            {"role": "user", "content": "Continue"},
            {"role": "assistant", "content": "A later answer."},
            {"role": "user", "content": "Reply OK"}]}
        fixed, count = compat.normalize_history(payload)
        self.assertEqual(count, 1)
        block = fixed["messages"][0]["content"][0]
        self.assertEqual(block["type"], "text")
        self.assertIn("No execution result was recorded", block["text"])
        self.assertIn("fixture.txt", block["text"])
        self.assertFalse(any(b.get("type") == "tool_result" for m in fixed["messages"]
                             if isinstance(m.get("content"), list) for b in m["content"]))

    def test_current_partial_parallel_batch_is_not_fabricated_or_removed(self):
        payload = {"messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "b", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "OK"}]}]}
        self.assertEqual(compat.normalize_history(payload), (payload, 0))

    def test_interrupted_latest_call_is_historical_without_a_fabricated_result(self):
        payload = {"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "old", "name": "Read", "input": {}}]},
            {"role": "user", "content": "[Request interrupted by user]"},
            {"role": "user", "content": "Reply OK"}]}
        fixed, count = compat.normalize_history(payload)
        self.assertEqual(count, 1)
        self.assertEqual(fixed["messages"][0]["content"][0]["type"], "text")

    def test_streamed_assistant_fragments_are_one_current_batch(self):
        payload = {"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "pending", "name": "Read", "input": {}}]},
            {"role": "assistant", "content": "More content in the same batch"},
            {"role": "user", "content": "Context"}]}
        self.assertEqual(compat.normalize_history(payload), (payload, 0))


class RouteTest(unittest.TestCase):
    def setUp(self):
        test_csw.ProfilesTest.setUp(self)

    def test_compatibility_endpoint_keeps_deepseek_identity_and_status_line(self):
        target = {"model": "deepseek-flash", "env": {"ANTHROPIC_BASE_URL": compat.BASE_URL, "CSW_PROFILE": "deepseek"}}
        self.assertEqual(csw.provider(target), "DeepSeek")
        self.assertTrue(csw.status_model({"model": {"id": "deepseek-flash"}}, target["env"]).startswith("DeepSeek ·"))

    def test_opt_in_routes_deepseek_without_modifying_saved_credentials(self):
        target = copy.deepcopy(self.direct)
        self.assertEqual(csw.provider_transport(target), target)
        csw.atomic_write(csw.ROOT / ".deepseek-compat-enabled", "enabled\n")
        adapted = csw.provider_transport(target)
        self.assertEqual(adapted["env"]["ANTHROPIC_BASE_URL"], compat.BASE_URL)
        self.assertEqual(adapted["env"]["ANTHROPIC_AUTH_TOKEN"], target["env"]["ANTHROPIC_AUTH_TOKEN"])
        self.assertEqual(target, self.direct)
        self.assertEqual(csw.provider_transport(self.native), self.native)
        self.assertEqual(csw.provider_transport(self.proxy), self.proxy)

    def test_compatibility_service_does_not_use_codex_authentication(self):
        target = {"env": {"ANTHROPIC_BASE_URL": compat.BASE_URL}}
        with patch.object(csw, "authenticated", side_effect=AssertionError("wrong provider")), \
                patch.object(csw, "proxy_ready", return_value=True):
            csw.check_proxy(target)

    def test_shared_reload_routes_through_compatibility_when_enabled(self):
        csw.atomic_write(csw.ROOT / ".deepseek-compat-enabled", "enabled\n")
        with patch.object(csw, "proxy_ready", return_value=True):
            csw.switch("deepseek")
        current = csw.read_json(csw.LIVE)
        self.assertEqual(current["env"]["ANTHROPIC_BASE_URL"], compat.BASE_URL)
        self.assertEqual(current["env"]["CSW_PROVIDER"], "DeepSeek")


class WireTest(unittest.TestCase):
    def test_sse_errors_are_detected_even_when_http_status_is_200(self):
        frame = b'event: error\ndata: {"type":"error","error":{"type":"invalid_request_error","message":"missing tool_result"}}'
        self.assertEqual(compat.stream_error_detail(frame), "missing tool_result")
        self.assertIsNone(compat.stream_error_detail(b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"not logged"}}'))
        self.assertIsNone(compat.stream_error_detail(b'event: error\ndata: invalid'))

    def test_upstream_auth_and_stream_are_preserved(self):
        forwarded = []
        timeouts = []
        stream_bytes = b'event: message_start\ndata: {"type":"message_start"}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n'

        class Upstream:
            def open(self, request, timeout):
                forwarded.append(request)
                timeouts.append(timeout)
                response = io.BytesIO(stream_bytes)
                response.code = 200
                response.headers = {"Content-Type": "text/event-stream", "x-request-id": "synthetic"}
                return response

        server = compat.http.server.ThreadingHTTPServer(("127.0.0.1", 0), compat.CompatHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            payload = image_history()
            payload["stream"] = True
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/deepseek/v1/messages?beta=true",
                data=json.dumps(payload).encode(),
                headers={"x-api-key": "synthetic-key", "Content-Type": "application/json", "x-stainless-timeout": "3000"})
            client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with patch.object(compat.urllib.request, "build_opener", return_value=Upstream()), \
                    patch("builtins.print"):
                with client.open(request, timeout=5) as response:
                    self.assertEqual(response.read(), stream_bytes)
                    self.assertEqual(response.headers["x-request-id"], "synthetic")
            self.assertEqual(forwarded[0].full_url, compat.UPSTREAM + "/v1/messages?beta=true")
            self.assertEqual(forwarded[0].get_header("X-api-key"), "synthetic-key")
            self.assertEqual(timeouts, [3000.0])
            fixed = json.loads(forwarded[0].data)
            self.assertEqual([block["type"] for block in fixed["messages"][1]["content"]], ["text"] * 3)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
