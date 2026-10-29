#!/bin/sh
# HTTPS downloads only. Python validates files and performs atomic installation.
set -eu
ref=${SHELLCLAUDE_REF:-feature/chatgpt-sign-in}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destination=${1:-$script_dir}
python_bin=${PYTHON:-python3}
base="https://raw.githubusercontent.com/xyxyx718/shellclaude/$ref"
stage=$(mktemp -d "${TMPDIR:-/tmp}/shellclaude-download.XXXXXX")
trap 'rm -rf "$stage"' EXIT HUP INT TERM
curl -fsSL "$base/release-files.sha256" -o "$stage/release-files.sha256"
curl -fsSL "$base/install.py" -o "$stage/install.py"
"$python_bin" "$stage/install.py" --list "$stage/release-files.sha256" > "$stage/paths"
while IFS= read -r path; do
    mkdir -p "$stage/$(dirname -- "$path")"
    curl -fsSL "$base/$path" -o "$stage/$path"
done < "$stage/paths"
"$python_bin" "$stage/install.py" --source "$stage" --dir "$destination" --ref "$ref"
