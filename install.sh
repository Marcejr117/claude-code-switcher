#!/usr/bin/env bash
# csw installer: copies the binary and support modules to the user's local prefix.
# Optional: --with-proxy builds raine/claude-code-proxy for Codex/Cursor accounts.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin_dir="${HOME}/.local/bin"
data_dir="${HOME}/.local/share/csw"
with_proxy=0
[ "${1:-}" = "--with-proxy" ] && with_proxy=1

if ! command -v python3 >/dev/null; then
    echo "error: python3 is required" >&2; exit 1
fi
if ! python3 -c 'import tomllib' 2>/dev/null; then
    echo "error: csw needs python3 >= 3.11 (tomllib)" >&2; exit 1
fi

install -Dm 755 "$here/csw" "$bin_dir/csw"
install -d "$data_dir"
install -m 644 "$here/modules"/*.py "$data_dir/"
echo "installed: $bin_dir/csw (+ $(ls "$here/modules" | wc -l) modules in $data_dir)"

if [ "$with_proxy" = 1 ]; then
    if ! command -v cargo >/dev/null; then
        echo "error: --with-proxy needs cargo (https://rustup.rs)" >&2; exit 1
    fi
    src="${TMPDIR:-/tmp}/claude-code-proxy"
    rm -rf "$src"
    git clone --depth 1 https://github.com/raine/claude-code-proxy.git "$src"
    (cd "$src" && cargo build --release)
    install -Dm 755 "$src/target/release/claude-code-proxy" "$bin_dir/claude-code-proxy"
    install -Dm 644 "$here/contrib/systemd/claude-code-proxy.service" \
        "${HOME}/.config/systemd/user/claude-code-proxy.service"
    install -Dm 644 "$here/contrib/systemd/claude-code-proxy@.service" \
        "${HOME}/.config/systemd/user/claude-code-proxy@.service"
    systemctl --user daemon-reload
    rm -rf "$src"
    echo "installed: claude-code-proxy + systemd units"
fi

echo
echo "next steps:"
echo "  1. add a provider:      csw add glm|deepseek|orcarouter|claude <name>"
echo "  2. list profiles:       csw list"
echo "  3. switch profile:      csw <profile>     (or: csw run <profile> to keep others open)"
echo "  4. optional shell wrapper and statusline: see contrib/"
