#!/usr/bin/env bash
# grqc-daily 安装脚本：创建虚拟环境、安装依赖、可选安装 MathJax、生成初始配置。
#
#   ./install.sh              # 默认：仅安装 Python 依赖
#   ./install.sh --with-math  # 额外安装 MathJax（公式排版与 LaTeX 一致，需要 node/npm）
#   ./install.sh --index-url https://mirrors.aliyun.com/pypi/simple/
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

WITH_MATH=0
INDEX_URL=""
PY="${PYTHON:-python3}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-math) WITH_MATH=1; shift ;;
    --index-url) INDEX_URL="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
done

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 运行环境
say "检查 Python"
command -v "$PY" >/dev/null || die "未找到 python3，请先安装 Python 3.11+"
"$PY" - <<'PY' || die "Python 版本过低，需要 3.11+"
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
"$PY" --version

say "创建虚拟环境 .venv"
[[ -d .venv ]] || "$PY" -m venv .venv
VENV_PY="$PROJECT_DIR/.venv/bin/python"
[[ -x "$VENV_PY" ]] || VENV_PY="$PROJECT_DIR/.venv/Scripts/python.exe"   # Windows(Git Bash)

say "安装 Python 依赖（WeasyPrint 及字体处理）"
PIP_ARGS=(install -q --disable-pip-version-check -r requirements.txt)
if [[ -n "$INDEX_URL" ]]; then
  HOST="$(printf '%s' "$INDEX_URL" | sed -E 's#https?://([^/]+)/.*#\1#')"
  "$VENV_PY" -m pip "${PIP_ARGS[@]}" --index-url "$INDEX_URL" --trusted-host "$HOST" \
    || die "从指定镜像安装失败：$INDEX_URL"
else
  # 官方源优先，失败则依次尝试常见镜像
  ok=0
  for idx in "https://pypi.org/simple/" \
             "https://mirrors.aliyun.com/pypi/simple/" \
             "https://pypi.tuna.tsinghua.edu.cn/simple/"; do
    HOST="$(printf '%s' "$idx" | sed -E 's#https?://([^/]+)/.*#\1#')"
    say "  尝试镜像：$idx"
    if "$VENV_PY" -m pip "${PIP_ARGS[@]}" --index-url "$idx" --trusted-host "$HOST" 2>/dev/null; then
      ok=1; break
    fi
  done
  [[ $ok -eq 1 ]] || die "依赖安装失败。可手动指定镜像：./install.sh --index-url <URL>"
fi

# ---------------------------------------------------------------- 可选 MathJax
if [[ $WITH_MATH -eq 1 ]]; then
  if command -v npm >/dev/null; then
    say "安装 MathJax（公式预渲染，与 LaTeX 输出一致）"
    ( cd assets/mathjax && npm install --no-audit --no-fund --loglevel=error ) \
      || warn "MathJax 安装失败：公式将自动降级为 Unicode 转换，不影响出报告"
  else
    warn "未找到 npm，跳过 MathJax；公式将降级为 Unicode 转换"
  fi
else
  say "跳过 MathJax（如需 LaTeX 级公式排版，请重新执行：./install.sh --with-math）"
fi

# ---------------------------------------------------------------- 配置
if [[ ! -f config.json ]]; then
  cp config.example.json config.json
  say "已生成 config.json（可编辑，也可用 Web 界面配置）"
else
  say "config.json 已存在，保留不动"
fi

# ---------------------------------------------------------------- 自检
say "环境自检"
"$VENV_PY" - <<'PY'
import importlib, shutil, sys
ok = True
try:
    import weasyprint
    print(f"  ✓ WeasyPrint {weasyprint.__version__}")
except Exception as exc:
    ok = False
    print(f"  ✗ WeasyPrint 不可用：{exc}")
    print("    Linux: 需要 libpango-1.0-0 libpangoft2-1.0-0 libcairo2")
    print("    macOS: brew install pango")
    print("    Windows: 建议使用 Docker 或 WSL")
exe = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
print(f"  {'✓' if exe else '·'} Chromium 兜底后端：{exe or '未安装（可选）'}")
node = shutil.which("node")
mj = (__import__('pathlib').Path('assets/mathjax/node_modules/mathjax-full')).exists()
print(f"  {'✓' if mj else '·'} MathJax 公式渲染：{'已安装' if mj else f'未安装（node={node or \"无\"}）'}")
import shutil as _sh, subprocess as _sp
if _sh.which("fc-list"):
    zh = _sp.run(["fc-list", ":lang=zh-cn", "family"], capture_output=True, text=True).stdout.strip()
    if not zh:
        print("  ✗ 系统中文字体：未检测到（报告里的汉字会显示为方块）")
        print("      Debian/Ubuntu: sudo apt install fonts-noto-cjk")
        print("      Fedora:        sudo dnf install google-noto-sans-cjk-fonts")
        print("      Arch:          sudo pacman -S noto-fonts-cjk")
    elif "source han" in zh.lower() or "\u601d\u6e90" in zh:
        print("  ✓ 系统中文字体：思源黑体")
    else:
        print("  · 系统中文字体：有（未检测到思源黑体，将使用系统默认中文字体）")
else:
    print("  · 未找到 fc-list，跳过中文字体检查")
sys.exit(0 if ok else 1)
PY
SELFCHECK=$?

echo
say "安装完成"
cat <<EOF

下一步：
  1) 配置模型密钥（任选其一）
       export DEEPSEEK_API_KEY=sk-...          # 环境变量优先
       或启动 Web 界面后在「大模型设置」里填写
  2) 生成一期日报
       .venv/bin/python run.py daily
  3) 打开配置界面
       .venv/bin/python run.py serve
  4) 常驻定时（每工作日 19:00 自动生成）
       .venv/bin/python run.py daemon
     或安装用户级 systemd（见 packaging/systemd/README.md）

EOF
exit $SELFCHECK
