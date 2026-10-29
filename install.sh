#!/bin/sh
set -e
# Run in the installation directory. No subshell, file-list parsing or GitHub API.
mkdir -p scripts
curl -fL --connect-timeout 15 --max-time 60 https://raw.githubusercontent.com/xyxyx718/shellclaude/feature/chatgpt-sign-in/scripts/install.py -o scripts/install.py
"${PYTHON:-python3}" scripts/install.py "$@"
