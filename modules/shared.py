"""Guards for native Claude sessions following the shared settings.json."""
import copy
import hashlib
import os
from pathlib import Path
import re
import shlex
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portable  # noqa: E402

EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "StopFailure",
          "SubagentStart", "SubagentStop", "PreModelSwitch", "PostModelSwitch")


def install(api, settings):
    command = f"{shlex.quote(portable.script_path(sys.executable))} {shlex.quote(portable.script_path(portable.CSW_BIN))} shared-hook"
    settings.setdefault("env", {})["CSW_CONFIG_DIR"] = str(api.ROOT)
    for event in EVENTS:
        entries = settings.setdefault("hooks", {}).setdefault(event, [])
        if not any(isinstance(entry, dict) and any(h.get("command") == command for h in entry.get("hooks", [])) for entry in entries):
            entries.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    inherit = f"{shlex.quote(portable.script_path(sys.executable))} {shlex.quote(portable.script_path(portable.CSW_BIN))} inherit-hook"
    entries = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if not any(isinstance(entry, dict) and any(h.get("command") == inherit for h in entry.get("hooks", [])) for entry in entries):
        entries.append({"matcher": "^(Agent|Workflow)$",
                        "hooks": [{"type": "command", "command": inherit, "timeout": 10}]})


def claude_parent():
    pid = os.getppid()
    table = portable.process_table() if portable.WINDOWS else None
    for _ in range(12):
        try:
            parent, name = portable.process_parent(pid, table)
            if name.startswith("claude"):
                return pid
            pid = parent
            if pid <= 1:
                break
        except (OSError, ValueError, IndexError):
            break
    raise ValueError("No se identifica el proceso Claude; no se puede comprobar su estado")


def ensure_idle(api):
    hot = api.hot_module()
    for path in (api.ROOT / "shared-sessions").glob("*.json"):
        state = api.read_json(path)
        if hot.running(state):
            hot.ensure_switch_idle(state)


def catalog(settings):
    return [{"model": re.sub(r"\[1m\]$", "", row["model"])}
            for row in settings.get("modelPicker", {}).get("options", [])]


def process_model(api, pid, start, environment):
    # /clear creates another session in the same process without resetting /model.
    records = sorted((api.ROOT / "shared-sessions").glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in records:
        state = api.read_json(path)
        if state.get("owner_pid") == pid and state.get("owner_start") == start:
            return state.get("model")
    return environment.get("CSW_INITIAL_MODEL")


def observe_model(api, payload, environment=None):
    """Seed an unknown model or reconcile a stale previous-provider cache.

    Some older sessions do not deliver a model-switch hook after a picker change.
    Their native status line can repair a cached model from the previous route.
    Never replace a compatible model with a delayed, incompatible display frame.
    """
    environment = os.environ if environment is None else environment
    if environment.get("CSW_HOT_ID") or environment.get("CSW_SHARED_DISABLED") == "1":
        return
    identifier = payload.get("session_id", "")
    model = payload.get("model", {}).get("id")
    effort = payload.get("effort", {}).get("level")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
        return
    if not isinstance(model, str) or not model:
        model = None
    if effort not in ("low", "medium", "high", "xhigh", "max"):
        effort = None
    if model is None and effort is None:
        return
    path = api.ROOT / "shared-sessions" / f"{identifier}.json"
    if not path.is_file():
        return
    with api.locked():
        state = api.read_json(path)
        hot = api.hot_module()
        if not hot.running(state):
            return
        if state.get("owner_pid") != claude_parent():
            return
        changed = False
        if model is not None and state.get("model") != model and state.get("model"):
            settings = api.read_json(api.LIVE)
            models = catalog(settings)
            if hot.model_allowed(api, state["model"], settings, models):
                model = None  # Keep an already compatible lifecycle observation.
            elif not hot.model_allowed(api, model, settings, models):
                model = None  # Neither observation establishes a valid replacement.
        if model is not None and state.get("model") != model:
            state.update(model=model, model_source="native_statusline")
            changed = True
        if effort is not None and state.get("effort") != effort:
            state.update(effort=effort, effort_source="native_statusline")
            changed = True
        if changed:
            api.write_json(path, state)


def _skip_javascript_literal(source, index):
    quote = source[index]
    if source.startswith("//", index):
        end = source.find("\n", index + 2)
        return len(source) if end < 0 else end
    if source.startswith("/*", index):
        end = source.find("*/", index + 2)
        return len(source) if end < 0 else end + 2
    if quote not in ("'", '"', "`"):
        return index
    index += 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
        elif quote == "`" and source.startswith("${", index):
            index = _matching_delimiter(source, index + 1) + 1
        elif source[index] == quote:
            return index + 1
        else:
            index += 1
    return len(source)


def _top_level_segments(source):
    """Split object content on commas while respecting nested JS literals."""
    segments, start, stack, index = [], 0, [], 0
    pairs = {"(": ")", "[": "]", "{": "}"}
    while index < len(source):
        if source.startswith(("//", "/*"), index) or source[index] in ("'", '"', "`"):
            index = _skip_javascript_literal(source, index)
            continue
        char = source[index]
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char == "," and not stack:
            segments.append(source[start:index])
            start = index + 1
        index += 1
    segments.append(source[start:])
    return segments


def _matching_delimiter(source, start):
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack, index = [pairs[source[start]]], start + 1
    while index < len(source):
        if source.startswith(("//", "/*"), index) or source[index] in ("'", '"', "`"):
            index = _skip_javascript_literal(source, index)
            continue
        char = source[index]
        if char in pairs:
            stack.append(pairs[char])
        elif char == stack[-1]:
            stack.pop()
            if not stack:
                return index
        index += 1
    raise ValueError("script de workflow con delimitadores incompletos")


def _inherit_workflow_effort_structured(source):
    """Remove only top-level effort overrides from agent(..., {options})."""
    replacements, index = [], 0
    while index < len(source):
        if source.startswith(("//", "/*"), index) or source[index] in ("'", '"', "`"):
            index = _skip_javascript_literal(source, index)
            continue
        if (source.startswith("agent", index)
                and (index == 0 or source[index - 1] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$")):
            opening = index + 5
            while opening < len(source) and source[opening].isspace():
                opening += 1
            if opening >= len(source) or source[opening] != "(":
                index += 5
                continue
        else:
            index += 1
            continue
        closing = _matching_delimiter(source, opening)
        arguments = source[opening + 1:closing]
        parts = _top_level_segments(arguments)
        if len(parts) >= 2:
            second_start = opening + 1 + len(parts[0]) + 1
            options = parts[1]
            left = len(options) - len(options.lstrip())
            stripped = options.strip()
            if stripped.startswith("{"):
                obj_start = second_start + left
                obj_end = _matching_delimiter(source, obj_start)
                content = source[obj_start + 1:obj_end]
                fields = _top_level_segments(content)
                kept = [field for field in fields
                        if not re.match(r"^\s*effort\s*:", field)]
                if len(kept) != len(fields):
                    replacements.append((obj_start + 1, obj_end, ",".join(kept)))
        index = closing + 1
    for start, end, value in reversed(replacements):
        source = source[:start] + value + source[end:]
    return source


def inherit_workflow_effort(source):
    try:
        source = _inherit_workflow_effort_structured(source)
    except ValueError:
        # Older saved scripts can contain deeply nested template literals. Their
        # effort options are limited to the documented scalar levels below.
        pass
    value = r"(?:['\"](?:low|medium|high|xhigh|max)['\"]|LENS_EFFORT\s*\[[^\]\r\n]+\])"
    source = re.sub(r",\s*effort\s*:\s*" + value, "", source)
    source = re.sub(r"(\{\s*)effort\s*:\s*" + value + r"\s*,", r"\1", source)
    return re.sub(r"\beffort\s*:\s*" + value + r"\s*,?", "", source)


def _workflow_path(api, payload, value):
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    cwd = Path(payload.get("cwd") or os.getcwd())
    candidates = (cwd / ".claude/workflows" / path, api.ROOT / "workflows" / path)
    return next((candidate for candidate in candidates if candidate.is_file()), path)


def inherit_tool(api, payload):
    """Make future agents inherit live model and effort without approving tools."""
    name = payload.get("tool_name")
    if name not in ("Agent", "Workflow"):
        return {}
    original = payload.get("tool_input")
    if not isinstance(original, dict):
        raise ValueError("entrada de Agent/Workflow no reconocida")
    updated = copy.deepcopy(original)
    changed = False
    if name == "Agent":
        for key in ("model", "effort"):
            if key in updated:
                updated.pop(key)
                changed = True
    else:
        script = updated.get("script")
        if isinstance(script, str):
            inherited = inherit_workflow_effort(script)
            if inherited != script:
                updated["script"] = inherited
                changed = True
        elif isinstance(updated.get("scriptPath"), str) and not updated.get("resumeFromRunId"):
            source_path = _workflow_path(api, payload, updated["scriptPath"])
            if source_path.is_symlink() or not source_path.is_file():
                raise ValueError("scriptPath no es un archivo regular")
            source = source_path.read_text(encoding="utf-8")
            inherited = inherit_workflow_effort(source)
            if inherited != source:
                digest = hashlib.sha256((str(source_path.resolve()) + "\0" + inherited).encode()).hexdigest()[:20]
                directory = api.ROOT / "inherited-workflows"
                directory.mkdir(mode=0o700, exist_ok=True)
                destination = directory / f"{source_path.stem}-{digest}.js"
                api.atomic_write(destination, inherited)
                updated["scriptPath"] = str(destination)
                changed = True
        elif isinstance(updated.get("name"), str) and not updated.get("resumeFromRunId"):
            name_path = updated["name"] if updated["name"].endswith(".js") else updated["name"] + ".js"
            source_path = _workflow_path(api, payload, name_path)
            if source_path.is_file() and not source_path.is_symlink():
                source = source_path.read_text(encoding="utf-8")
                inherited = inherit_workflow_effort(source)
                if inherited != source:
                    digest = hashlib.sha256((str(source_path.resolve()) + "\0" + inherited).encode()).hexdigest()[:20]
                    directory = api.ROOT / "inherited-workflows"
                    directory.mkdir(mode=0o700, exist_ok=True)
                    destination = directory / f"{source_path.stem}-{digest}.js"
                    api.atomic_write(destination, inherited)
                    updated.pop("name")
                    updated["scriptPath"] = str(destination)
                    changed = True
    if not changed:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}


def hook(api, payload, environment=None):
    environment = os.environ if environment is None else environment
    if environment.get("CSW_HOT_ID") or environment.get("CSW_SHARED_DISABLED") == "1":
        return {}  # Explicitly isolated sessions retain their existing lifecycle.
    identifier = payload.get("session_id", "")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
        raise ValueError("Identificador de sesión Claude inválido")
    event = payload.get("hook_event_name")
    if event not in EVENTS:
        return {}
    directory = api.ROOT / "shared-sessions"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{identifier}.json"
    hot = api.hot_module()
    with api.locked():
        state = api.read_json(path) if path.exists() else {}
        if not state or event == "SessionStart" or not hot.running(state):
            pid = claude_parent()
            start = hot.process_identity(pid)
            state = {"owner_pid": pid, "owner_start": start, "status": "running",
                     "busy": False, "agents": [], "prompt_seen": False,
                     "model": payload.get("model") or process_model(api, pid, start, environment)}
        agent = payload.get("agent_id")
        if not agent and isinstance(payload.get("transcript_path"), str):
            state["transcript_path"] = payload["transcript_path"]
        settings = api.read_json(api.LIVE)
        profile = api.resolve(api.active())
        if event in ("UserPromptSubmit", "PreModelSwitch"):
            native = api.provider(settings) == "Claude sub"
            # Only extra Claude accounts pin a token; the primary login renews itself.
            pinned = settings.get("env", {}).get("CLAUDE_CODE_OAUTH_TOKEN")
            if native and pinned and pinned != hot.oauth_access(api, profile):
                raise ValueError(f"Renueva y selecciona la cuenta: csw login {api.profile_label(profile)}")
            for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CSW_PROFILE"):
                if key == "CLAUDE_CODE_OAUTH_TOKEN" and native and key not in environment:
                    continue
                if str(environment.get(key, "")) != str(settings.get("env", {}).get(key, "")):
                    raise ValueError(f"Claude aún no ha recargado {key} o un ajuste lo sobrescribe. Espera y vuelve a intentarlo")
            model = payload.get("to_model") if event == "PreModelSwitch" else state.get("model")
            # SessionStart.model is optional in native Claude. Not knowing it is
            # NOT evidence of a mismatched model. Keep route/auth checks above;
            # validate every explicit switch and any actually observed model.
            if (event == "PreModelSwitch" or model) and not hot.model_allowed(api, model, settings, catalog(settings)):
                raise ValueError(f"El modelo {model!r} no pertenece a {api.profile_label(profile)}. Abre /model y elige uno de su catálogo")
            if event == "PreModelSwitch" and not agent:
                hot.ensure_model_switch_idle(state)
        if event == "SessionStart" and not agent:
            state.update(model=payload.get("model") or state.get("model"), status="running")
        elif event == "SessionEnd" and not agent:
            state.update(status="closed", busy=False, agents=[])
        elif event == "UserPromptSubmit" and not agent:
            state.update(busy=True, prompt_seen=True, busy_since=time.time())
        elif event in ("Stop", "StopFailure") and not agent:
            state["busy"] = False
            state.pop("busy_since", None)
        elif event == "SubagentStart" and agent:
            state["agents"] = sorted(set(state.get("agents", [])) | {agent})
            state.setdefault("agents_started", {})[agent] = time.time()
        elif event == "SubagentStop" and agent:
            state["agents"] = [a for a in state.get("agents", []) if a != agent]
            state.get("agents_started", {}).pop(agent, None)
        elif event == "PostModelSwitch" and not agent:
            model = payload.get("to_model")
            state["model"] = model
            if hot.model_allowed(api, model, settings, catalog(settings)):
                updated = copy.deepcopy(settings)
                updated["model"] = api.normalize_model(model, settings)
                updated["env"]["ANTHROPIC_MODEL"] = ""
                api.prefer_supported_context(updated)
                if updated != settings:
                    api.write_json(api.LIVE, updated)
        api.write_json(path, state)
    return {}
