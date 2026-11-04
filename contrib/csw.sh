# Read the shared settings.json live; explicit csw run still isolates an account.
# Source this from ~/.zshrc or ~/.bashrc.
claude() {
    "$(command -v csw)" launch -- "$@"
}
