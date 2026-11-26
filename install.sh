#!/usr/bin/env bash
set -euo pipefail

for dependency in git uv; do
    command -v "$dependency" >/dev/null 2>&1 || {
        printf 'Required command missing: %s\n' "$dependency" >&2
        exit 1
    }
done

install_tmp="$(mktemp -d)"
trap 'rm -rf -- "$install_tmp"' EXIT

# 可选分支或标签用于固定源码版本，默认使用仓库默认分支。
ref="${MYJIRA_CLI_REF:-}"
clone_args=(--depth 1)
if [[ -n "$ref" ]]; then
    clone_args+=(--branch "$ref")
fi
git clone "${clone_args[@]}" -- "${MYJIRA_CLI_REPO:-https://github.com/CStyle1024/myjira-cli.git}" "$install_tmp/myjira-cli"
uv tool install --reinstall "$install_tmp/myjira-cli"

uv_bin="$(uv tool dir --bin)"
"$uv_bin/myjira-cli" --tool-info
printf 'Installed: %s/myjira-cli\n' "$uv_bin"
printf 'If the command is missing from PATH, run uv tool update-shell.\n'
