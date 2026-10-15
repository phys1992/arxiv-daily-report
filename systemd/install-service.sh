#!/usr/bin/env bash
# 把 systemd 用户单元安装到 ~/.config/systemd/user/ 并启用。
#
#   ./install-service.sh            # 安装并启动
#   ./install-service.sh --uninstall
#
# 说明：定时（例如每天 19:00）由进程内的调度器完成，可用 Web 界面随时修改；
#       因此这里只需要一个常驻服务，不需要 timer 单元。
set -euo pipefail

# 本脚本位于 <项目根>/systemd/，所以往上一级就是项目根
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_NAME="arxiv-daily-report.service"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT_NAME"
  systemctl --user daemon-reload || true
  say "已卸载 $UNIT_NAME"
  exit 0
fi

command -v systemctl >/dev/null || die "本机没有 systemd，请改用 run.py daemon 或 KDE 自启动"

VENV_PY="$PROJECT_DIR/.venv/bin/python"
[[ -x "$VENV_PY" ]] || die "未找到 .venv，请先执行 ./install.sh"

say "写入 $UNIT_DIR/$UNIT_NAME"
# 先写临时文件再移动：重定向失败时 bash 自己会往 stderr 甩一行 errno，
# 这里连同它一起压掉，只留下面这条可操作的提示。
if ! { sed -e "s#@PROJECT_DIR@#$PROJECT_DIR#g" -e "s#@VENV_PY@#$VENV_PY#g" \
        "$PROJECT_DIR/systemd/arxiv-daily-report.service.tmpl" > "$UNIT_DIR/$UNIT_NAME"; } 2>/dev/null; then
  die "写入失败：$UNIT_DIR 不可写。
  常见原因：当前处在容器/沙箱里（文件系统只读），或该目录权限受限。
  请在正常的桌面终端里重新执行：
    $PROJECT_DIR/systemd/install-service.sh"
fi

if ! systemctl --user daemon-reload 2>/dev/null; then
  die "无法连接用户级 systemd（bus 不可达）。文件已写入，请在桌面终端里手动执行：
    systemctl --user daemon-reload && systemctl --user enable --now $UNIT_NAME"
fi

systemctl --user enable --now "$UNIT_NAME"
sleep 2
systemctl --user --no-pager status "$UNIT_NAME" || true
say "完成。查看日志：journalctl --user -u $UNIT_NAME -f"
