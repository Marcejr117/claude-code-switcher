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
  offers (queried from each provider's API, cached for an hour). Selecting a
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

Optional pieces in `contrib/`:

- `csw.sh` — a `claude()` shell wrapper that routes every launch through
  `csw launch` (reads the shared live settings).
- `statusline.sh` — minimal status line showing provider · profile · model · cwd.
- `systemd/` — user units for the subscription proxy (see below).

## Quickstart

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

## Subscription proxy (Codex / Cursor, optional)

Codex and Cursor accounts are routed through
[claude-code-proxy](https://github.com/raine/claude-code-proxy) (MIT, third
party — not part of this project). `install.sh --with-proxy` clones, builds and
installs it with the systemd user units from `contrib/systemd/`. Each proxy
account runs as its own systemd unit with an isolated config directory.

**Note:** routing a ChatGPT or Cursor subscription through an unofficial proxy
may violate those providers' terms of service. Use it at your own risk.

## Tests

209 unit tests, all synthetic (no network, no real credentials):

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
