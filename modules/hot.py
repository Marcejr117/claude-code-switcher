"""Opt-in, session-local provider changes through Claude's native settings reload.

No request proxy, injected keystrokes, or Claude restarts. The user selects the
new model with /model; hooks block prompts until that selection is compatible.
Claude access tokens are read from their owning account, never refresh tokens.
"""
import contextlib
import copy
from datetime import datetime
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portable  # noqa: E402

EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "SubagentStart",
          "SubagentStop", "PreModelSwitch", "PostModelSwitch")
LABELS = {"Codex": "OpenAI / Codex", "Claude sub": "Claude / Max", "Cursor": "Cursor",
          "GLM": "GLM", "DeepSeek": "DeepSeek", "OrcaRouter": "OrcaRouter"}
ORDER = list(LABELS)


def load_api():
    loader = importlib.machinery.SourceFileLoader("csw_hot_api", str(portable.CSW_BIN))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    api = importlib.util.module_from_spec(spec)
    loader.exec_module(api)
    return api


def process_identity(pid):
    return portable.process_start(pid)


def running(state):
    owner_alive = (state.get("owner_start") is not None
                   and process_identity(state.get("owner_pid")) == state.get("owner_start"))
    child_alive = (state.get("claude_start") is not None
                   and process_identity(state.get("claude_pid")) == state.get("claude_start"))
    return state.get("status") == "running" and (owner_alive or child_alive)


def session_path(api, identifier):
    if not re.fullmatch(r"[0-9a-f]{12}", identifier):
        raise ValueError("Identificador de sesión inválido; consulta csw hot list")
    path = api.ROOT / "hot-sessions" / identifier
    if path.is_symlink() or not path.is_dir():
        raise ValueError("No existe esa sesión en caliente")
    return path


@contextlib.contextmanager
def session_lock(directory):
    with (directory / ".lock").open("a") as lock:
        portable.flock(lock)
        yield


def sessions(api):
    rows = []
    for directory in sorted((api.ROOT / "hot-sessions").glob("*")):
        try:
            directory = session_path(api, directory.name)
            state = api.read_json(directory / "state.json")
            if running(state):
                rows.append((directory, state))
        except (OSError, ValueError):
            continue
    return rows


def profile_in_use(api, filename):
    return any(state.get("profile") == filename for _, state in sessions(api))


def goal_active(state):
    """Read native goal lifecycle receipts, not the per-turn Stop/busy flag.

    A met=true sentinel is a clear (including clear-after-error), not necessarily
    achievement. Sidechain receipts and goal-like text in messages do not count.
    Called only when changing provider/model, not for each model request.
    """
    filename = state.get("transcript_path")
    if not filename:
        if state.get("prompt_seen"):
            raise ValueError("No se puede comprobar el goal de esta sesión; cambio bloqueado")
        return False
    transcript = Path(filename)
    if transcript.suffix != ".jsonl":
        raise ValueError("Registro de sesión no reconocido; no se puede comprobar el goal")
    active = False
    try:
        with transcript.open("rb") as stream:
            for line in stream:
                if not line.strip():
                    continue
                if not line.endswith(b"\n"):
                    raise ValueError("El registro del goal se está actualizando; espera y vuelve a intentarlo")
                if b'"goal_status"' not in line:
                    continue
                record = json.loads(line)
                if record.get("type") != "attachment" or record.get("isSidechain"):
                    continue
                status = record.get("attachment", {})
                if status.get("type") != "goal_status":
                    continue
                if not isinstance(status.get("met"), bool):
                    raise ValueError("Estado del goal no reconocido; cambio bloqueado")
                active = not status["met"] and not status.get("failed", False)
    except FileNotFoundError:
        if state.get("prompt_seen") is False:
            return False  # A newly opened session can have no transcript yet.
        raise ValueError("Falta el registro para comprobar el goal; cambio bloqueado") from None
    except OSError:
        raise ValueError("No se puede leer el registro del goal; cambio bloqueado") from None
    return active


def _clear_interrupted_turn(state):
    """Cancellation can skip Stop; trust only the latest native turn receipt."""
    filename = state.get("transcript_path")
    if not state.get("busy") or not isinstance(filename, str) or not filename.endswith(".jsonl"):
        return False
    try:
        with Path(filename).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            offset = max(0, stream.tell() - 1024 * 1024)
            stream.seek(offset)
            if offset:
                stream.readline()  # The bounded tail can start inside a record.
            tail = stream.read()
        if not tail.endswith(b"\n"):
            return False  # A partial append cannot establish an idle session.
        for line in reversed(tail.splitlines()):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                return False
            if record.get("isSidechain") or record.get("isMeta") or record.get("agentId"):
                continue
            if record.get("type") not in ("user", "assistant"):
                continue
            message = record.get("message", {})
            if record["type"] != "user" or not isinstance(message, dict) or message.get("role") != "user":
                return False
            content = message.get("content")
            if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
                content = content[0].get("text") if content[0].get("type") == "text" else None
            if content not in ("[Request interrupted by user]", "[Request interrupted by user for tool use]"):
                return False
            receipt = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            if receipt.tzinfo is None:
                return False
            started = state.get("busy_since")
            if started is not None and (not isinstance(started, (int, float)) or not receipt.timestamp() >= started):
                return False  # UserPromptSubmit runs before its transcript append.
            state["busy"] = False
            state.pop("busy_since", None)
            return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False
    return False


def _workflow_agent_states(state, pending):
    """Workflow has its own journal; failed workers can skip SubagentStop."""
    transcript = Path(state["transcript_path"])
    directory = transcript.parent / transcript.stem / "subagents/workflows"
    states = {}
    try:
        for journal in directory.glob("*/journal.jsonl"):
            latest = {}
            with journal.open("rb") as stream:
                for line in stream:
                    if not line.endswith(b"\n"):
                        return {agent: ("unknown", None) for agent in pending}
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        return {agent: ("unknown", None) for agent in pending}
                    agent = row.get("agentId")
                    if agent in pending:
                        latest[agent] = {"started": "running", "result": "completed",
                                         "failed": "failed"}.get(row.get("type"), "unknown")
            for agent, status in latest.items():
                ended = None
                if state.get("agents_started", {}).get(agent) is not None:
                    sidechain = journal.parent / f"agent-{agent}.jsonl"
                    try:
                        with sidechain.open("rb") as stream:
                            stream.seek(0, os.SEEK_END)
                            offset = max(0, stream.tell() - 1024 * 1024)
                            stream.seek(offset)
                            if offset:
                                stream.readline()
                            tail = stream.read()
                        if tail.endswith(b"\n"):
                            for line in reversed(tail.splitlines()):
                                row = json.loads(line)
                                if row.get("type") not in ("user", "assistant"):
                                    continue
                                receipt = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                                ended = receipt.timestamp() if receipt.tzinfo else None
                                break
                    except (OSError, ValueError, TypeError, KeyError, AttributeError):
                        pass
                # Conflicting journals cannot establish that a worker is idle.
                if agent in states and states[agent][0] not in ("completed", "failed"):
                    continue
                states[agent] = (status, ended)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {agent: ("unknown", None) for agent in pending}
    return states


def _clear_finished_agents(state):
    """Reconcile missing SubagentStop from native task lifecycle receipts."""
    filename = state.get("transcript_path")
    agents = state.get("agents", [])
    starts = state.get("agents_started", {})
    if (not agents or not isinstance(agents, list) or not all(isinstance(agent, str) for agent in agents)
            or not isinstance(starts, dict) or not isinstance(filename, str) or not filename.endswith(".jsonl")):
        return False
    pending = set(agents)
    needles = [agent.encode() for agent in pending]
    latest = {}
    try:
        # An agent can finish hours before /model; a bounded tail would miss it.
        with Path(filename).open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    return False
                if not any(needle in line for needle in needles):
                    continue
                record = json.loads(line)
                if (not isinstance(record, dict) or record.get("type") != "user"
                        or record.get("isSidechain") or record.get("agentId")):
                    continue
                message = record.get("message", {})
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                result = record.get("toolUseResult")
                if isinstance(result, dict) and result.get("agentId") in pending:
                    agent, status = result["agentId"], result.get("status")
                elif (isinstance(result, dict) and isinstance(result.get("task"), dict)
                      and result["task"].get("task_id") in pending
                      and result.get("retrieval_status") in ("success", "timeout", "not_ready")):
                    agent = result["task"]["task_id"]
                    status = result["task"].get("status") if result["retrieval_status"] == "success" else "running"
                elif (record.get("origin") == {"kind": "task-notification"}
                      and record.get("promptSource") == "system"):
                    content = message.get("content")
                    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
                        content = content[0].get("text") if content[0].get("type") == "text" else None
                    if not isinstance(content, str) or not content.lstrip().startswith("<task-notification>"):
                        continue
                    identifier = re.search(r"<task-id>([A-Za-z0-9_-]{1,100})</task-id>", content)
                    outcome = re.search(r"<status>([a-z_]+)</status>", content)
                    if not identifier or identifier[1] not in pending or not outcome:
                        continue
                    agent, status = identifier[1], outcome[1]
                else:
                    continue
                receipt = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
                if receipt.tzinfo is None:
                    return False
                latest[agent] = (status, receipt.timestamp())
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False
    for agent, (status, ended) in _workflow_agent_states(state, pending).items():
        if (status not in ("completed", "failed") or agent not in latest
                or ended is not None and ended >= latest[agent][1]):
            latest[agent] = (status, ended)
    finished = set()
    for agent, (status, ended) in latest.items():
        started = starts.get(agent)
        if (status in ("completed", "failed", "killed")
                and (started is None or isinstance(started, (int, float))
                     and isinstance(ended, (int, float)) and ended >= started)):
            finished.add(agent)
    if not finished:
        return False
    state["agents"] = [agent for agent in agents if agent not in finished]
    for agent in finished:
        starts.pop(agent, None)
    if not starts:
        state.pop("agents_started", None)
    return True


def ensure_model_switch_idle(state):
    _clear_interrupted_turn(state)
    if not state.get("busy"):
        _clear_finished_agents(state)
    if state.get("busy") or state.get("agents"):
        raise ValueError("Claude o sus subagentes están trabajando; espera a que terminen")


def ensure_switch_idle(state):
    ensure_model_switch_idle(state)
    if goal_active(state):
        raise ValueError("Hay un goal activo. Puedes cambiar de modelo con /model entre turnos; para cambiar de proveedor o cuenta, espera a que finalice o usa /goal clear")


def choose_number(title, rows, back=False, cancel_label="Cancelar"):
    if not rows:
        print("No hay opciones disponibles.")
        return None
    while True:
        print("\n" + title)
        for index, (label, _) in enumerate(rows, 1):
            print(f"  {index}. {label}")
        print(f"  0. {cancel_label}" + (" · b. Volver a proveedores" if back else ""))
        choice = input("Elige un número (Enter = cancelar): ").strip().lower()
        if choice in ("", "q", "0"):
            return None
        if back and choice == "b":
            return "back"
        if choice.isdecimal() and 0 < int(choice) <= len(rows):
            return rows[int(choice) - 1][1]
        print("Elige un número de la lista.")


def choose_profile(api, allowed=None):
    groups = {}
    for filename in api.profiles():
        path = api.ROOT / filename
        target = api.selected_data(path)
        backend = api.provider(target)
        if backend in LABELS and (allowed is None or backend in allowed):
            groups.setdefault(backend, []).append(path)
    if not groups:
        print("No hay cuentas de esos proveedores. Añade una desde csw → Mis cuentas.")
        return None
    labels = api.names()
    while True:
        backend = choose_number("PASO 1/2 · ¿Qué proveedor quieres usar?", [
            (f"{LABELS[key]} ({len(groups[key])} cuenta{'s' if len(groups[key]) != 1 else ''})", key) for key in ORDER if key in groups
        ])
        if backend is None:
            return None
        profile = choose_number(f"PASO 2/2 · {LABELS[backend]} · ¿Qué cuenta?", [
            (f"{labels.get(path.name, api.profile_label(path))} [{api.profile_label(path)}]"
             f" · modelo guardado: {api.effective_model(api.selected_data(path))}", path)
            for path in groups[backend]
        ], back=True)
        if profile != "back":
            return profile


def choose_session(api, identifier=None):
    if identifier:
        directory = session_path(api, identifier)
        if not running(api.read_json(directory / "state.json")):
            raise ValueError("Esa sesión ya no está abierta")
        return directory
    rows = sessions(api)
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        raise ValueError("No hay sesiones en caliente. Inicia una con csw hot start")
    if not sys.stdin.isatty():
        raise ValueError("Hay varias sesiones; indica su ID de csw hot list")
    return choose_number("Sesión que quieres cambiar", [
        (f"{p.name} · {s['label']} · {s.get('model', '?')} · {s.get('cwd', '')}", p)
        for p, s in rows
    ])


def oauth_store(api, profile):
    return api.read_json(api.claude_store(profile) / ".credentials.json").get("claudeAiOauth", {})


def oauth_scopes(api, profile):
    # Claude treats an env-var token as user:inference only, which turns off
    # scope-gated features such as Claude in Chrome. Declare the login's real scopes.
    try:
        scopes = oauth_store(api, profile).get("scopes")
    except (OSError, ValueError):
        return ""
    return " ".join(s for s in scopes if isinstance(s, str)) if isinstance(scopes, list) else ""


def oauth_access(api, profile):
    try:
        data = oauth_store(api, profile)
        token, expires = data.get("accessToken"), data.get("expiresAt")
        if not isinstance(token, str) or not token or not isinstance(expires, (int, float)):
            raise ValueError("Falta una sesión OAuth válida")
        if expires <= (time.time() + 60) * 1000:
            raise ValueError("La sesión OAuth ha caducado o está a punto de caducar")
        return token
    except (OSError, ValueError) as error:
        raise ValueError(f"{error}. Renueva con csw login {api.profile_label(profile)}; no cierres Claude") from None


def credentials(api, profile, target):
    result = api.provider_transport(target)
    if api.provider(target) == "Claude sub":
        env = result.setdefault("env", {})
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_access(api, profile)
        env["CLAUDE_CODE_OAUTH_SCOPES"] = oauth_scopes(api, profile)
    return result


def model_allowed(api, model, target, catalog):
    if not isinstance(model, str) or not model:
        return False
    try:
        normalized = api.normalize_model(model, target)
    except ValueError:
        return False
    if api.provider(target) == "Claude sub":
        return True  # Native Claude applies account/model availability checks.
    normalized = re.sub(r"\[1m\]$", "", normalized)
    normalized = re.sub(r"^cursor-(plan|ask):", "cursor:", normalized)
    return normalized in {row["model"] for row in catalog}


def configured(api, directory, current, target, state):
    result = api.merge_provider(current, target)
    # Explicit empty values remove credentials and settings from the old route.
    env = result.setdefault("env", {})
    keys = set(api.PROVIDER_ENV)
    keys.update(k for k in current.get("env", {}) if api.provider_key(k))
    keys.update(("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"))
    for key in keys:
        env[key] = ""
    env.update(target.get("env", {}))
    if api.provider(target) == "Claude sub":
        env["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
    else:
        for family in ("OPUS", "SONNET", "HAIKU", "FABLE"):
            key = f"ANTHROPIC_DEFAULT_{family}_MODEL"
            env[key] = env.get(key) or api.effective_model(target)
    # /model owns the active model; never pin ANTHROPIC_MODEL in a hot session.
    env["ANTHROPIC_MODEL"] = ""
    env.update(CSW_PROFILE=state["label"], CSW_PROVIDER=state["provider"],
               CSW_HOT_REVISION=str(state["revision"]))
    result["apiKeyHelper"] = target.get("apiKeyHelper", "")
    result["modelPicker"] = {"replaceBuiltInOptions": True, "options": [
        {"model": api.preferred_context_model(row["model"], target),
         "label": f"{state['label']} · {row['label']}",
         "description": f"{row['model']} · contexto {api.context_label(row['model'], target)}"}
        for row in state["catalog"]
    ]}
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} hook {directory.name}"
    hooks = result.setdefault("hooks", {})
    for event in EVENTS:
        previous = hooks.setdefault(event, [])
        if not any(any(h.get("command") == command for h in entry.get("hooks", [])) for entry in previous):
            previous.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    return result


def prepare_target(api, profile, model=None):
    target = api.selected_data(profile)
    if api.provider(target) not in LABELS:
        raise ValueError("Proveedor no compatible con el modo en caliente")
    api.check_proxy(target)
    target = credentials(api, profile, target)
    if model is not None:
        target["model"] = model
        target.setdefault("env", {})["ANTHROPIC_MODEL"] = model
    api.prefer_supported_context(target, model)
    return target, api.model_catalog(profile, target)["models"]


def use_profile(api, directory, profile):
    # Resolve login/catalog before taking locks or changing the live connection.
    target, catalog = prepare_target(api, profile)
    with api.locked(), session_lock(directory):
        state = api.read_json(directory / "state.json")
        if not running(state):
            raise ValueError("La sesión ha terminado")
        ensure_switch_idle(state)
        if not profile.exists():
            raise ValueError("La cuenta se retiró durante la selección")
        current = api.read_json(directory / "settings.json")
        state.update(profile=profile.name, label=api.profile_label(profile), provider=api.provider(target),
                     catalog=catalog, revision=state["revision"] + 1, auth_error=None)
        compatible = model_allowed(api, state.get("model"), target, catalog)
        state["phase"] = "ready" if compatible else "choose-model"
        if compatible:
            target["model"] = state["model"]
            target.setdefault("env", {})["ANTHROPIC_MODEL"] = state["model"]
            api.prefer_supported_context(target, state["model"])
        settings = configured(api, directory, current, target, state)
        # A failed settings write cannot leave the previous state claiming success.
        old_state = api.read_json(directory / "state.json")
        api.write_json(directory / "state.json", state)
        try:
            api.write_json(directory / "settings.json", settings)
        except Exception:
            api.write_json(directory / "state.json", old_state)
            raise
    print(f"Cambio preparado en {directory.name}: {state['label']} ({LABELS[state['provider']]}).")
    print("Espera un par de segundos a la recarga; en la MISMA sesión de Claude abre /model. No reinicies.")
    print("La próxima petición llevará el historial al proveedor/cuenta seleccionado.")
    if state["provider"] == "Cursor":
        print("Aviso: Cursor responde texto, pero sus herramientas aún no están validadas en este proxy.")


def refresh_access(api, directory):
    with session_lock(directory):
        state = api.read_json(directory / "state.json")
        if state.get("provider") != "Claude sub":
            return
        try:
            token = oauth_access(api, api.profile_path(state["profile"]))
        except ValueError as error:
            if state.get("auth_error") != str(error):
                state["auth_error"] = str(error)
                api.write_json(directory / "state.json", state)
            return
        settings = api.read_json(directory / "settings.json")
        changed = settings["env"].get("CLAUDE_CODE_OAUTH_TOKEN") != token
        if changed:
            state["revision"] += 1
            settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] = token
            settings["env"]["CLAUDE_CODE_OAUTH_SCOPES"] = oauth_scopes(api, api.profile_path(state["profile"]))
            settings["env"]["CSW_HOT_REVISION"] = str(state["revision"])
            api.write_json(directory / "settings.json", settings)
        if state.get("auth_error") or changed:
            state["auth_error"] = None
            api.write_json(directory / "state.json", state)


def hook(api, identifier, payload, environment=None):
    environment = os.environ if environment is None else environment
    directory = session_path(api, identifier)
    event = payload.get("hook_event_name")
    with session_lock(directory):
        state = api.read_json(directory / "state.json")
        profile = api.profile_path(state["profile"])
        target = api.selected_data(profile)
        agent = payload.get("agent_id")
        if not agent and isinstance(payload.get("transcript_path"), str):
            state["transcript_path"] = payload["transcript_path"]
        if event in ("UserPromptSubmit", "PreModelSwitch"):
            if state.get("auth_error"):
                raise ValueError(state["auth_error"])
            settings = api.read_json(directory / "settings.json")
            native_oauth = state["provider"] == "Claude sub"
            if native_oauth:
                access = oauth_access(api, profile)
                if settings["env"].get("CLAUDE_CODE_OAUTH_TOKEN") != access:
                    raise ValueError("El acceso OAuth de la cuenta ha cambiado; espera a que se recargue o vuelve a seleccionarla con csw hot menu")
            for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CSW_HOT_REVISION"):
                if key == "CLAUDE_CODE_OAUTH_TOKEN" and native_oauth and key not in environment:
                    # Claude deliberately withholds real OAuth tokens from hooks.
                    # Validate the owning account above, and retain endpoint and
                    # revision checks; never require exposing the secret to hooks.
                    continue
                if str(environment.get(key, "")) != str(settings["env"].get(key, "")):
                    raise ValueError(f"Claude aún no ha recargado {key}, o el proyecto lo sobrescribe. Espera y vuelve a intentarlo; tu mensaje queda bloqueado")
            if event == "UserPromptSubmit" and state["phase"] != "ready":
                raise ValueError(f"Has elegido {state['label']}. Usa /model para elegir un modelo de esa cuenta antes de continuar")
            if event == "PreModelSwitch":
                if not agent:
                    ensure_model_switch_idle(state)
                if not model_allowed(api, payload.get("to_model"), target, state["catalog"]):
                    raise ValueError("Ese modelo no pertenece al proveedor seleccionado. Usa csw hot menu para cambiar de cuenta")
        if event == "SessionStart":
            if payload.get("session_id") != state.get("session_id"):
                state["prompt_seen"] = False
            state.update(session_id=payload.get("session_id"), transcript_path=payload.get("transcript_path"))
            if isinstance(payload.get("model"), str):
                state["model"] = payload["model"]
                state["phase"] = "ready" if model_allowed(api, state["model"], target, state["catalog"]) else "choose-model"
        elif event == "UserPromptSubmit" and not agent:
            state["prompt_seen"] = True
            state["busy"] = True
            state["busy_since"] = time.time()
        elif event in ("Stop", "StopFailure") and not agent:
            state["busy"] = False
            state.pop("busy_since", None)
        elif event == "SubagentStart" and agent:
            state["agents"] = sorted(set(state.get("agents", [])) | {agent})
            state.setdefault("agents_started", {})[agent] = time.time()
        elif event == "SubagentStop" and agent:
            state["agents"] = [x for x in state.get("agents", []) if x != agent]
            state.get("agents_started", {}).pop(agent, None)
        elif event == "PostModelSwitch" and not agent:
            model = payload.get("to_model")
            state["model"] = model
            if model_allowed(api, model, target, state["catalog"]):
                target = credentials(api, profile, target)
                target["model"] = model
                target.setdefault("env", {})["ANTHROPIC_MODEL"] = model
                api.prefer_supported_context(target, model)
                state.update(phase="ready", revision=state["revision"] + 1)
                current = api.read_json(directory / "settings.json")
                api.write_json(directory / "settings.json", configured(api, directory, current, target, state))
            else:
                state["phase"] = "choose-model"
        api.write_json(directory / "state.json", state)
    return {}


def start(api, profile, forwarded):
    if any(x in ("--settings", "--setting-sources") or x.startswith(("--settings=", "--setting-sources=")) for x in forwarded):
        raise ValueError("El modo hot necesita recargar sus ajustes; no admite --settings ni --setting-sources")
    if any(x in ("--disable-hooks", "--bare", "--safe-mode") for x in forwarded):
        raise ValueError("El modo hot necesita sus hooks de protección")
    if "--no-session-persistence" in forwarded:
        raise ValueError("El modo hot necesita el registro de sesión para proteger los goals. Sin persistencia usa csw run")
    target, catalog = prepare_target(api, profile)
    for index, argument in enumerate(forwarded):
        if argument == "--model" and index + 1 < len(forwarded):
            model = api.normalize_model(forwarded[index + 1], target)
            if not model_allowed(api, model, target, catalog):
                raise ValueError("El modelo solicitado no pertenece a este perfil")
            forwarded[index + 1] = model
            target, catalog = prepare_target(api, profile, model)
        elif argument.startswith("--model="):
            model = api.normalize_model(argument.split("=", 1)[1], target)
            if not model_allowed(api, model, target, catalog):
                raise ValueError("El modelo solicitado no pertenece a este perfil")
            forwarded[index] = "--model=" + model
            target, catalog = prepare_target(api, profile, model)
    identifier = uuid.uuid4().hex[:12]
    directory = api.ROOT / "hot-sessions" / identifier
    directory.mkdir(mode=0o700, parents=True)
    current = api.read_json(api.LIVE)
    if current.get("disableAllHooks"):
        raise ValueError("El modo hot requiere hooks activos; disableAllHooks está habilitado")
    state = {"version": 1, "id": identifier, "owner_pid": os.getpid(), "owner_start": process_identity(os.getpid()),
             "status": "running", "profile": profile.name, "label": api.profile_label(profile),
             "provider": api.provider(target), "catalog": catalog, "model": api.effective_model(target),
             "phase": "ready", "revision": 1, "busy": False, "agents": [], "cwd": os.getcwd(), "auth_error": None, "prompt_seen": False}
    api.write_json(directory / "state.json", state)
    api.write_json(directory / "settings.json", configured(api, directory, current, target, state))
    api.ensure_native_links(directory)
    # Definitions, not account metadata or refresh tokens. As with named accounts,
    # per-server OAuth grants may require login in this isolated configuration root.
    global_config = api.ROOT.parent / ".claude.json"
    if global_config.is_file():
        native = api.read_json(global_config)
        shared = {key: native[key] for key in ("mcpServers", "projects", "hasCompletedOnboarding", "lastOnboardingVersion") if key in native}
        api.write_json(directory / ".claude.json", shared)
    environment = {k: v for k, v in os.environ.items() if not api.provider_key(k)}
    environment.update(CLAUDE_CONFIG_DIR=str(directory), CSW_CONFIG_DIR=str(api.ROOT), CSW_HOT_ID=identifier)
    environment.pop("CSW_PROFILE", None)
    environment.pop("CSW_PROVIDER", None)
    environment.pop("CSW_HOT_REVISION", None)
    print(f"Sesión en caliente: {identifier} · {state['label']}", flush=True)
    print("Para cambiar: en otra terminal, csw hot menu; después /model aquí. El historial se conserva.", flush=True)
    child = None
    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    try:
        # Prevent --resume from restoring another provider's previous model.
        # Native /model remains higher-priority and can change it in-process.
        startup_model = [] if any(x == "--model" or x.startswith("--model=") for x in forwarded) else ["--model", state["model"]]
        child = subprocess.Popen([api.CLAUDE_BIN, "--setting-sources", "user,project,local", *startup_model, *forwarded], env=environment)
        # Ctrl-C belongs to Claude (cancel turn), not its supervising launcher.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        with session_lock(directory):
            state = api.read_json(directory / "state.json")
            state["claude_pid"] = child.pid
            state["claude_start"] = process_identity(child.pid)
            api.write_json(directory / "state.json", state)
        while child.poll() is None:
            try:
                refresh_access(api, directory)
            except (OSError, ValueError):
                pass  # Hooks fail closed if the store is unreadable.
            time.sleep(1)
        return child.returncode
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        with session_lock(directory):
            state = api.read_json(directory / "state.json")
            state.update(status="closed", busy=False, agents=[])
            api.write_json(directory / "state.json", state)
            settings = api.read_json(directory / "settings.json")
            for key in list(settings.get("env", {})):
                if api.provider_key(key):
                    settings["env"][key] = ""
            settings["apiKeyHelper"] = ""
            api.write_json(directory / "settings.json", settings)
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)


def main(api, args):
    command = args[0] if args else "start"
    rest = args[1:] if args else []
    if command in ("help", "-h", "--help"):
        print(api.ui_module().HOT_HELP)
        return 0
    if command == "list":
        rows = sessions(api)
        for directory, state in rows:
            if state.get("auth_error"):
                phase = "renovar login"
            elif state.get("busy") or state.get("agents"):
                phase = "trabajando"
            elif state["phase"] == "choose-model":
                phase = "elige modelo con /model"
            else:
                phase = "modelo seleccionado"
            print(f"{directory.name} · {state['label']} · {state['model']} · {phase} · {state['cwd']}")
        if not rows:
            print("No hay sesiones en caliente abiertas. Usa csw hot start")
        return 0
    if command in ("menu", "use"):
        identifier = rest[1] if command == "use" and len(rest) == 2 else rest[0] if command == "menu" and rest else None
        if (command == "use" and len(rest) not in (1, 2)) or (command == "menu" and len(rest) > 1):
            raise ValueError("Uso: csw hot menu [sesión] | csw hot use <perfil> [sesión]")
        directory = choose_session(api, identifier)
        if directory is None:
            return 0
        if command == "menu" and not sys.stdin.isatty():
            raise ValueError("El menú necesita una terminal; usa csw hot use <perfil> [sesión]")
        if sys.stdin.isatty():
            state = api.read_json(directory / "state.json")
            print(f"\nVas a cambiar la sesión {directory.name}")
            print(f"Ahora: {state['label']} · {state['model']} · {state.get('cwd', '')}")
        profile = api.resolve(rest[0]) if command == "use" else choose_profile(api)
        if profile is not None:
            if sys.stdin.isatty():
                print("Al continuar, la próxima petición enviará el historial de esta conversación a esa cuenta/proveedor.")
                if input(f"¿Cambiar a {api.profile_label(profile)}? [s/N]: ").strip().lower() not in ("s", "si", "sí"):
                    print("Cancelado: la sesión conserva su cuenta.")
                    return 0
            use_profile(api, directory, profile)
        return 0
    if command == "start":
        if rest and rest[0] != "--" and not rest[0].startswith("-"):
            profile, rest = api.resolve(rest[0]), rest[1:]
        elif sys.stdin.isatty():
            profile = choose_profile(api)
            if profile is None:
                return 0
        else:
            raise ValueError("Indica el perfil: csw hot start <perfil> [-- opciones de Claude]")
        if rest and rest[0] == "--":
            rest = rest[1:]
        return start(api, profile, rest)
    raise ValueError("Usa csw hot start [perfil], csw hot menu [sesión], csw hot use <perfil> [sesión] o csw hot list")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        api = load_api()
        if len(sys.argv) == 3 and sys.argv[1] == "hook":
            output = hook(api, sys.argv[2], json.load(sys.stdin))
            print(json.dumps(output))
        else:
            raise ValueError("Este módulo se usa mediante csw hot")
    except Exception as error:
        # All hook failures block instead of silently changing the wrong account.
        print(f"csw hot: {error}", file=sys.stderr)
        sys.exit(2)
