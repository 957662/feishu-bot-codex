#!/usr/bin/env bash
# setup.sh — install/upgrade/uninstall/doctor for feishu-bot-codex.
# 全自动:检测缺失依赖 → 经你同意后用 brew / apt / npm 装好 → 注册 daemon
#   ./setup.sh              交互式询问每个依赖
#   ./setup.sh install -y   非交互式,直接装所有缺的
#   ./setup.sh doctor       只检测,不装
#   ./setup.sh uninstall    卸 daemon + 全局软链,保留 bindings
#   ./setup.sh update       拉代码 + 重装依赖
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ACTION="${1:-install}"
AUTO_YES="${YES:-0}"
for arg in "$@"; do
    case "$arg" in
        -y|--yes) AUTO_YES=1 ;;
    esac
done

# -----------------------------------------------------------------------------
# Helpers — detection / consent / auto-install
# -----------------------------------------------------------------------------

detect_os() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux)  echo "linux" ;;
        *)      echo "unsupported" ;;
    esac
}

confirm() {
    local msg="$1"
    if [ "$AUTO_YES" = "1" ]; then
        echo "[auto-yes] $msg"
        return 0
    fi
    # Non-interactive shell (CI, docker, curl|bash) → no tty to prompt on;
    # auto-confirm so the script doesn't hang waiting for input on a closed fd.
    if [ ! -t 0 ] || [ ! -r /dev/tty ]; then
        echo "[no-tty, auto-yes] $msg"
        return 0
    fi
    read -rp "$msg [Y/n] " ans </dev/tty || ans="y"
    case "${ans:-y}" in
        n|N|no|No|NO) return 1 ;;
        *) return 0 ;;
    esac
}

# Re-source brew shellenv so newly-installed binaries land on PATH for the
# rest of this script (otherwise we'd need to tell the user to restart shell)
refresh_path() {
    if [ -x /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -x /usr/local/bin/brew ]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
    # npm global bin (in case node was just installed)
    if command -v npm >/dev/null 2>&1; then
        local npm_prefix
        npm_prefix="$(npm prefix -g 2>/dev/null || echo)"
        [ -n "$npm_prefix" ] && export PATH="$npm_prefix/bin:$PATH"
    fi
}

ensure_brew() {
    if command -v brew >/dev/null 2>&1; then
        return 0
    fi
    if ! confirm "Homebrew 没装。它是 macOS 上自动装 python/tmux/node 的入口,装吗?"; then
        echo "ERROR: 没 brew 就没法自动装其他依赖。手动装好 python3/tmux/node 后重新跑。" >&2
        return 1
    fi
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    refresh_path
    if ! command -v brew >/dev/null 2>&1; then
        echo "ERROR: brew 装完仍然找不到。可能需要手动 source ~/.zshrc 后重跑。" >&2
        return 1
    fi
}

# ensure_pkg <command-to-check> [brew-pkg-name] [apt-pkg-name]
ensure_pkg() {
    local cmd="$1"
    local brew_pkg="${2:-$1}"
    local apt_pkg="${3:-$1}"
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "[ok] $cmd: $(command -v "$cmd")"
        return 0
    fi
    local os; os="$(detect_os)"
    case "$os" in
        macos)
            ensure_brew || return 1
            if confirm "缺 $cmd,用 brew install $brew_pkg 自动装吗?"; then
                brew install "$brew_pkg"
                refresh_path
            else
                echo "ERROR: 没 $cmd 没法继续。" >&2; return 1
            fi
            ;;
        linux)
            if confirm "缺 $cmd,用 sudo apt-get install -y $apt_pkg 装吗?"; then
                sudo apt-get update -qq
                sudo apt-get install -y "$apt_pkg"
            else
                echo "ERROR: 没 $cmd 没法继续。" >&2; return 1
            fi
            ;;
        *)
            echo "ERROR: 不识别的系统,手动装 $cmd 后重跑。" >&2; return 1
            ;;
    esac
}

ensure_npm_global() {
    local pkg="$1"
    local bin="$2"
    if command -v "$bin" >/dev/null 2>&1; then
        echo "[ok] $bin: $(command -v "$bin")"
        return 0
    fi
    if confirm "缺 $bin,用 npm i -g $pkg 装吗?"; then
        npm install -g "$pkg"
        refresh_path
    fi
}

# -----------------------------------------------------------------------------
# Install pieces (unchanged from before; just called from action_install)
# -----------------------------------------------------------------------------

install_python_pkg() {
    if [ ! -d .venv ]; then
        echo "[install] creating .venv..."
        python3 -m venv .venv
    fi
    # shellcheck source=/dev/null
    source .venv/bin/activate
    echo "[install] pip install -e .[dev]"
    pip install --quiet --upgrade pip
    pip install --quiet -e ".[dev]"
}

global_bin_dir() {
    for d in /opt/homebrew/bin /usr/local/bin; do
        if [ -d "$d" ] && [ -w "$d" ]; then
            echo "$d"; return 0
        fi
    done
    mkdir -p "$HOME/.local/bin"
    echo "$HOME/.local/bin"
}

install_global_symlink() {
    local target="$SCRIPT_DIR/.venv/bin/feishu-bot-codex"
    local bin_dir; bin_dir="$(global_bin_dir)"
    local link="$bin_dir/feishu-bot-codex"
    if [ ! -x "$target" ]; then
        echo "WARN: $target not found; skipping symlink" >&2
        return
    fi
    ln -sf "$target" "$link"
    echo "[ok] symlink: $link → $target"
    if ! command -v feishu-bot-codex >/dev/null 2>&1; then
        echo "WARN: $link 不在 PATH 上,把 $bin_dir 加到你 shell rc 里。" >&2
    fi
}

install_slash_commands() {
    bash scripts/install-commands.sh
}

install_launchd() {
    local plist_src="scripts/launchd.plist"
    local plist_dst="$HOME/Library/LaunchAgents/com.qingyun.feishu-bot-codex.plist"
    local python_bin="$SCRIPT_DIR/.venv/bin/python"
    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$HOME/.feishu-bot-codex/logs"
    sed -e "s|__PYTHON__|$python_bin|g" -e "s|__HOME__|$HOME|g" "$plist_src" > "$plist_dst"
    launchctl unload "$plist_dst" 2>/dev/null || true
    launchctl load "$plist_dst"
    echo "[ok] launchd service loaded: $plist_dst"
}

install_systemd() {
    local svc_src="scripts/systemd.service"
    local svc_dst="$HOME/.config/systemd/user/feishu-bot-codex.service"
    local python_bin="$SCRIPT_DIR/.venv/bin/python"
    mkdir -p "$HOME/.config/systemd/user"
    mkdir -p "$HOME/.feishu-bot-codex/logs"
    sed -e "s|__PYTHON__|$python_bin|g" -e "s|__HOME__|$HOME|g" "$svc_src" > "$svc_dst"
    systemctl --user daemon-reload
    systemctl --user enable --now feishu-bot-codex.service
    echo "[ok] systemd user service enabled: $svc_dst"
}

install_service() {
    local os; os="$(detect_os)"
    case "$os" in
        macos) install_launchd ;;
        linux) install_systemd ;;
        *)     echo "WARN: 不识别的系统 $os; daemon 需手动启动。" ;;
    esac
}

wait_for_socket() {
    local sock="$HOME/.feishu-bot-codex/control.sock"
    local deadline=$((SECONDS + 10))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if [ -S "$sock" ]; then
            echo "[ok] socket appeared: $sock"
            return 0
        fi
        sleep 0.5
    done
    echo "WARN: socket 10s 内没出现。看日志: $HOME/.feishu-bot-codex/logs/" >&2
    return 1
}

# -----------------------------------------------------------------------------
# Actions
# -----------------------------------------------------------------------------

action_install() {
    echo "==> 1. 检查 + 自动装系统依赖"
    ensure_pkg python3 python@3.12 python3
    ensure_pkg tmux    tmux         tmux
    ensure_pkg node    node         nodejs

    echo ""
    echo "==> 2. 检查 + 自动装 npm 全局工具"
    ensure_npm_global "@larksuite/cli"           lark-cli
    ensure_npm_global "@openai/codex" codex
    if confirm "可选:也装 Claude Code (codex 机器人也能驱动 Claude,加 --agent claude)?"; then
        ensure_npm_global "@anthropic-ai/claude-code" claude
    fi
    if confirm "可选:装 mermaid-cli (让机器人自动把 \`\`\`mermaid 代码块渲染成图)?"; then
        ensure_npm_global "@mermaid-js/mermaid-cli" mmdc
    fi

    echo ""
    echo "==> 3. Python venv + 项目本体"
    install_python_pkg

    echo ""
    echo "==> 4. CLI 软链 + slash 命令 + 系统服务"
    install_global_symlink
    install_slash_commands
    install_service
    wait_for_socket || true

    echo ""
    echo "✅ feishu-bot-codex installed."
    echo "Next:"
    echo "  cd <your-project>"
    echo "  feishu-bot-codex shell    # tmux + Codex (or --agent claude)"
    echo "  /bot-new <name>            # inside Claude TUI"
}

action_uninstall() {
    local os; os="$(detect_os)"
    case "$os" in
        macos)
            launchctl unload "$HOME/Library/LaunchAgents/com.qingyun.feishu-bot-codex.plist" 2>/dev/null || true
            rm -f "$HOME/Library/LaunchAgents/com.qingyun.feishu-bot-codex.plist"
            ;;
        linux)
            systemctl --user disable --now feishu-bot-codex.service 2>/dev/null || true
            rm -f "$HOME/.config/systemd/user/feishu-bot-codex.service"
            systemctl --user daemon-reload 2>/dev/null || true
            ;;
    esac
    for d in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
        local link="$d/feishu-bot-codex"
        if [ -L "$link" ] && [[ "$(readlink "$link")" == *"feishu-bot-codex/.venv/bin/feishu-bot-codex"* ]]; then
            rm -f "$link"
            echo "[ok] removed symlink: $link"
        fi
    done
    rm -f "$HOME/.feishu-bot-codex/control.sock"
    echo "✅ Daemon stopped + service files removed."
    echo "Bindings preserved at: $HOME/.feishu-bot-codex/bindings.toml"
    echo "Slash commands kept at: $HOME/.claude/commands/bot-*.md"
}

action_update() {
    git pull --ff-only
    install_python_pkg
    install_slash_commands
    action_doctor
    echo "✅ Update complete. Daemon will auto-restart on next file change."
}

action_doctor() {
    echo "[check] python3:           $(command -v python3 || echo MISSING)"
    echo "[check] tmux:              $(command -v tmux || echo MISSING)"
    echo "[check] node:              $(command -v node || echo MISSING)"
    echo "[check] npm:               $(command -v npm || echo MISSING)"
    echo "[check] lark-cli:          $(command -v lark-cli || echo MISSING)"
    echo "[check] codex:             $(command -v codex || echo MISSING)"
    echo "[check] claude (optional): $(command -v claude || echo 'not installed (--agent claude needs it)')"
    echo "[check] mmdc (optional):   $(command -v mmdc || echo 'not installed (optional)')"
    echo "[check] feishu-bot-codex: $(command -v feishu-bot-codex || echo 'MISSING (run ./setup.sh install)')"
    echo "[check] socket:            $([ -S "$HOME/.feishu-bot-codex/control.sock" ] && echo OK || echo MISSING)"
    echo "[check] bindings.toml:     $([ -f "$HOME/.feishu-bot-codex/bindings.toml" ] && echo OK || echo NONE)"
    local plist="$HOME/Library/LaunchAgents/com.qingyun.feishu-bot-codex.plist"
    if [ -f "$plist" ]; then
        if grep -q "EnvironmentVariables" "$plist"; then
            echo "[check] launchd PATH env:  OK"
        else
            echo "[check] launchd PATH env:  MISSING (重跑 ./setup.sh install 修)"
        fi
    fi
    if [ -S "$HOME/.feishu-bot-codex/control.sock" ]; then
        # shellcheck source=/dev/null
        [ -f .venv/bin/activate ] && source .venv/bin/activate
        feishu-bot-codex ping || echo "WARN: socket 在但 ping 失败"
    fi
}

case "$ACTION" in
    install)               action_install ;;
    uninstall|--uninstall) action_uninstall ;;
    update|--update)       action_update ;;
    doctor|--doctor)       action_doctor ;;
    -y|--yes)              action_install ;;
    *) echo "Usage: $0 [install|uninstall|update|doctor] [-y]" >&2; exit 1 ;;
esac
