"""Loopback-only Anthropic pass-through for cross-provider tool history."""
import copy
import http.server
import json
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "http://127.0.0.1:18768/deepseek"
UPSTREAM = "https://api.deepseek.com/anthropic"
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}


def normalize_history(payload):
    """Preserve incompatible historical records as text without inventing results."""
    result = copy.deepcopy(payload)
    messages = result.get("messages", [])
    declared = {tool.get("name") for tool in result.get("tools", []) if isinstance(tool, dict)}
    assistant_results = {block.get("tool_use_id") for message in messages
                         if message.get("role") == "assistant" and isinstance(message.get("content"), list)
                         for block in message["content"] if block.get("type") == "tool_result"}
    legacy = set()
    for message in messages:
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            historical_server = block.get("name") in ("analyze_image", "webReader") or block.get("id") in assistant_results
            if (isinstance(block.get("id"), str) and block["id"] and historical_server
                    and (block.get("type") == "server_tool_use"
                         or block.get("type") == "tool_use" and block.get("name") not in declared)):
                legacy.add(block.get("id"))

    groups = []
    for message in messages:
        content = message.get("content", [])
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        if groups and groups[-1][0] == message.get("role"):
            groups[-1][1].extend(blocks)
        else:
            groups.append((message.get("role"), list(blocks)))
    recorded_results = {block.get("tool_use_id") for _role, blocks in groups
                        for block in blocks if block.get("type") == "tool_result"}
    last_assistant = max((index for index, (role, _blocks) in enumerate(groups) if role == "assistant"), default=-1)
    orphaned = set()
    for index, (role, blocks) in enumerate(groups):
        if role != "assistant":
            continue
        following = groups[index + 1][1] if index + 1 < len(groups) and groups[index + 1][0] == "user" else []
        interrupted = any(block.get("type") == "text" and block.get("text") in
                          ("[Request interrupted by user]", "[Request interrupted by user for tool use]")
                          for block in following)
        if index < last_assistant or interrupted:
            orphaned.update(block.get("id") for block in blocks if block.get("type") == "tool_use"
                            and isinstance(block.get("id"), str) and block["id"] not in recorded_results
                            and block["id"] not in legacy)

    for message in messages:
        blocks = message.get("content")
        if not isinstance(blocks, list):
            continue
        normalized = []
        for block in blocks:
            if block.get("type") in ("server_tool_use", "tool_use") and block.get("id") in legacy:
                normalized.append({"type": "text", "text": "[Historical GLM server tool: " + str(block.get("name")) + "]"})
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in legacy:
                content = block.get("content", "")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                normalized.append({"type": "text", "text": "[Historical server-tool result]\n" + text})
            elif block.get("type") == "tool_use" and block.get("id") in orphaned:
                normalized.append({"type": "text", "text":
                                   "[Historical tool call. No execution result was recorded.]\n" +
                                   json.dumps(block, ensure_ascii=False)})
            else:
                normalized.append(block)
        message["content"] = normalized
    return result, len(legacy) + len(orphaned)


def stream_error_detail(frame):
    """An SSE error can arrive inside HTTP 200; inspect only error events."""
    try:
        data = b"\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith(b"data:"))
        event = json.loads(data)
        if isinstance(event, dict) and event.get("type") == "error" and isinstance(event.get("error"), dict):
            return event["error"].get("message", "Upstream streaming error")
    except (ValueError, TypeError):
        pass
    return None


class CompatHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass  # Do not log credentials, prompts, paths, or request bodies.

    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path == "/deepseek/healthz":
            data = b'{"ok":true,"service":"csw-deepseek-compat"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.forward()

    def do_POST(self):
        self.forward()

    def report_error(self, detail, status, repairs, streaming=False):
        for name in ("x-api-key", "authorization"):
            credential = self.headers.get(name)
            if credential:
                detail = detail.replace(credential, "[redacted]")
                detail = detail.replace(credential.split()[-1], "[redacted]")
        print(json.dumps({"http_status": status, "history_repairs": repairs,
                          "stream_error": streaming, "error": detail[:1200]}), flush=True)

    def forward(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path.removeprefix("/deepseek")
        if (not parsed.path.startswith("/deepseek/") or route not in
                ("/v1/messages", "/v1/messages/count_tokens", "/v1/models")):
            self.send_error(404)
            return
        repairs = 0
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 128 * 1024 * 1024:
                self.send_error(413)
                return
            body = self.rfile.read(length) if length else None
            if self.command == "POST" and route == "/v1/messages":
                payload, repairs = normalize_history(json.loads(body))
                body = json.dumps(payload, ensure_ascii=False).encode()
            headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP_HEADERS}
            url = UPSTREAM + route + ("?" + parsed.query if parsed.query else "")
            request = urllib.request.Request(url, data=body, headers=headers, method=self.command)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            timeout = float(self.headers.get("x-stainless-timeout", "3000"))
            if not 0 < timeout <= 86400:
                raise ValueError("Invalid client timeout")
            try:
                response = opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error  # Preserve the upstream status and error body.
            with response:
                status = response.code
                self.send_response(status)
                for key, value in response.headers.items():
                    if key.lower() not in HOP_HEADERS:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if status >= 400:
                    data = response.read()
                    try:
                        detail = json.loads(data).get("error", {}).get("message", "")
                    except (ValueError, AttributeError):
                        detail = "Upstream rejected request"
                    self.report_error(detail, status, repairs)
                    self.wfile.write(data)
                else:
                    pending = b""
                    inspect_stream = ("text/event-stream" in response.headers.get("Content-Type", "")
                                      and response.headers.get("Content-Encoding", "identity") == "identity")
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        if inspect_stream:
                            pending += chunk
                            while b"\n\n" in pending:
                                frame, pending = pending.split(b"\n\n", 1)
                                detail = stream_error_detail(frame)
                                if detail:
                                    self.report_error(detail, status, repairs, streaming=True)
                            if len(pending) > 1024 * 1024:
                                pending = b""  # Diagnostic buffering must stay bounded.
                    print(json.dumps({"http_status": status, "history_repairs": repairs}), flush=True)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (OSError, ValueError, TypeError, AttributeError) as error:
            self.close_connection = True
            self.send_error(502 if isinstance(error, OSError) else 400, "Compatibility transport failed")


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 18768), CompatHandler)
    server.daemon_threads = True
    server.serve_forever()
