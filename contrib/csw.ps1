# Read the shared settings.json live; explicit csw run still isolates an account.
# Dot-source this from your PowerShell $PROFILE:  . "$HOME\.local\share\csw\csw.ps1"
function claude { csw launch -- @args }
