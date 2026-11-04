#!/usr/bin/env bash
# Minimal statusline: provider/profile/model from csw + current directory.
# Hook it up in Claude Code with:
#   claude config set --global statusLine '{"type":"command","command":"bash ~/.local/share/csw/statusline.sh"}'
input=$(cat)
model=$(printf '%s' "$input" | "$(command -v csw)" statusline 2>/dev/null)
dir=$(printf '%s' "$input" | python3 -c 'import json,sys,os;print(os.path.basename(json.load(sys.stdin).get("workspace",{}).get("current_dir","")))' 2>/dev/null)
printf '\033[38;5;39m%s\033[0m \033[38;5;245m%s\033[0m' "$model" "$dir"
