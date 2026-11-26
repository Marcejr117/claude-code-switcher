# csw — Claude Code profile & provider switcher

`csw` manages multiple Claude Code configurations as switchable profiles: Claude
subscriptions, direct Anthropic-compatible APIs (Z.ai/GLM, DeepSeek, OrcaRouter,
or any other), and proxied Codex/Cursor accounts. Each profile keeps its own
models, API keys, context windows, and subagent settings — one command swaps the
live `settings.json`, and running sessions pick up the change without a restart.

```
$ csw list
>  1  glm:main            GLM main            GLM glm-5.3[1m]
>  2  deepseek:main       DeepSeek main       DeepSeek deepseek-v4-pro
>  3  orcarouter:principal OrcaRouter principal OrcaRouter orcarouter/auto
>  4  codex:personal      Codex personal      Codex gpt-5.6-sol
$ csw orcarouter:principal
Cambio preparado... abre /model en la MISMA sesión. No reinicies.
```

## How it works

- Claude Code reads its configuration from `~/.claude/settings.json`. `csw`
  stores each profile as `settings.json.<BACKEND>.<name>.bak` and atomically
  swaps that file, marking the active one in `.settings-active`.
- **Hot switching**: hooks (`csw shared-hook`, `csw inherit-hook`) synchronize
  the live session — a model or account change applies to the running Claude
  Code instance on the next request, no restart needed.
- **Model catalogs**: `csw models <profile>` lists what the provider actually
  offers (queried from each provider's API, cached for an hour). Codex
  profiles mirror the Codex CLI `/model` list of the signed-in account
  (`~/.codex/models_cache.json`), falling back to the proxy's list. Selecting a
  model via `csw model` or the `/model` picker keeps profiles in sync.
- **Context windows**: known 1M-token models are tagged `[1m]`; smaller
  documented windows are enforced via `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, with
  auto-compaction set at 75% of the window. Unknown models stay on Claude
  Code's native defaults.
- **Subagent inheritance**: profiles default to `CLAUDE_CODE_SUBAGENT_MODEL=inherit`
  so subagents follow the session's live `/model` and `/effort`.

API keys live only in the profile files under `~/.claude`, created with mode
`0600` and prompted hidden. Nothing is sent anywhere except the provider you
configured.

## Install

Requirements: Linux, `python3 >= 3.11`, [Claude Code](https://claude.com/claude-code)
installed.

```sh
git clone https://github.com/Marcejr117/claude-code-switcher.git
cd claude-code-switcher
./install.sh
```

That installs `csw` to `~/.local/bin` and its support modules to
`~/.local/share/csw` (make sure `~/.local/bin` is on your `PATH`).

### Windows

Requirements: Windows 10/11, Python >= 3.11 (`winget install Python.Python.3.12`),
Claude Code with Git Bash (Claude Code runs the `csw` hooks through it).

```powershell
git clone https://github.com/Marcejr117/claude-code-switcher.git
cd claude-code-switcher
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Same layout as Linux (`%USERPROFILE%\.local\bin\csw` plus a `csw.cmd` launcher,
modules in `%USERPROFILE%\.local\share\csw`); the installer adds `.local\bin` to
the user `PATH`. Differences from Linux:

- Background services (DeepSeek adapter, Codex/Cursor proxy) run as detached
  processes instead of systemd units; pid and log files live in
  `~/.claude/services`. `enable` adds a per-user logon entry
  (`HKCU\...\Run`, started through `pythonw`, no admin, no console window).
- `install.ps1 -WithProxy` builds `claude-code-proxy.exe` from source. Use
  0.1.48 or newer (needs a recent stable Rust, e.g. `rustup update stable`):
  older builds only accept a fixed model list and reject newer Codex models.
- File modes (`0600`) do not apply; profiles rely on the user profile ACL.
- `csw rm` cannot see the environment of Claude sessions started without csw,
  so close those yourself before removing an account (sessions opened with
  `csw run` are still detected).
- Optional `claude` wrapper for PowerShell: add
  `. "$HOME\.local\share\csw\csw.ps1"` to your `$PROFILE`.

Before the first switch, save your current Claude subscription as the primary
profile so you can come back to it: `csw save CLAUDE.bak "Claude Max"`.

Optional pieces in `contrib/`:

- `csw.sh` — a `claude()` shell wrapper that routes every launch through
  `csw launch` (reads the shared live settings):

  ```sh
  echo 'source ~/.local/share/csw/csw.sh' >> ~/.zshrc   # or ~/.bashrc
  install -Dm 644 contrib/csw.sh ~/.local/share/csw/csw.sh
  ```

- `statusline.sh` — minimal status line showing provider · profile · model ·
  context window · cwd. Personal settings (this included) survive profile
  switches:

  ```sh
  install -Dm 755 contrib/statusline.sh ~/.local/share/csw/statusline.sh
  claude config set --global statusLine '{"type":"command","command":"bash ~/.local/share/csw/statusline.sh"}'
  ```

- `systemd/` — user units for the subscription proxy (see below).

## Quickstart

Each provider needs one account. On a fresh install, accounts are seeded from
built-in templates (base URL, sane default model, subagent inheritance); your
API key is prompted hidden and stored only in `~/.claude` with mode `0600`.

```sh
csw add glm main          # direct Z.ai API key, hidden prompt
csw add deepseek main     # direct DeepSeek API key
csw add orcarouter main   # any Anthropic-compatible router
csw add claude max2       # another Claude subscription (browser login)
csw add codex personal    # ChatGPT subscription via proxy (device-code login)
csw add cursor work

csw list                  # numbered profiles
csw 3                     # switch by number or name
csw run glm:main          # open a profile without closing other sessions

csw models glm:main       # provider's real catalog
csw model glm:main glm-5.3   # set the profile's model
csw active                # what the live settings point at
```

Inside Claude Code, `/model` shows the profile's own picker, built from the
provider catalog with context windows included.

## Several Claude accounts

`csw add claude <name>` logs the account in once, in the browser. From then on
`csw claude:<name>` (or its number) makes it *the* Claude Code login: its tokens
and account metadata are swapped into `~/.claude/.credentials.json` and
`.claude.json`, and the previous account's renewed login is put back in its own
directory. Nothing is pinned in `settings.json`, so Claude Code keeps renewing
the login itself and everything that needs a real claude.ai login keeps working:
the IDE extension, Claude in Chrome, claude.ai connectors. No second `/login`.

## The browser with other providers

Claude Code only turns Claude in Chrome on with a claude.ai login, so it is off
under GLM, DeepSeek, Codex and the rest. The bridge to the extension still works
as a plain MCP server, so csw registers it as `chrome` (tools `mcp__chrome__*`)
whenever the selected provider is not Claude, and removes it again for Claude,
which has the built-in integration. Navigation, page text and screenshots work;
what the model makes of a screenshot depends on its own image support. The
Claude extension must be installed in Chrome, same as for Claude itself.

## Subscription proxy (Codex / Cursor, optional)

Codex and Cursor accounts are routed through
[claude-code-proxy](https://github.com/raine/claude-code-proxy) (MIT, third
party — not part of this project). `install.sh --with-proxy` clones, builds and
installs it with the systemd user units from `contrib/systemd/`. Each proxy
account runs as its own systemd unit with an isolated config directory.

**Note:** routing a ChatGPT or Cursor subscription through an unofficial proxy
may violate those providers' terms of service. Use it at your own risk.

## Tests

211 unit tests, all synthetic (no network, no real credentials); they pass on
Linux and Windows (`python` instead of `python3` there):

```sh
cd tests && python3 -m unittest test_csw test_hot test_shared test_ui \
    test_deepseek_compat test_inherit test_cancellation
```

## Disclaimer

This is an independent tool, not affiliated with or endorsed by Anthropic.
Using Claude Code with third-party providers is subject to those providers'
terms and pricing.

## License

[MIT](LICENSE)
