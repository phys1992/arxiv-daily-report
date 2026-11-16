#!/usr/bin/env bash
# 构建 Debian / Fedora / Arch 三种安装包（deb、rpm、pkg.tar.zst）。
#
#   ./packaging/build-packages.sh              # 版本号从包元数据读取
#   VERSION=0.2.2 ./packaging/build-packages.sh
#
# 依赖 nfpm（单文件二进制）：https://github.com/goreleaser/nfpm/releases
#   CI 里由 .github/workflows/release.yml 负责安装；本地可放到 PATH 或用 NFP M 变量指定。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NFP="${NFPM:-nfpm}"
command -v "$NFP" >/dev/null || { echo "找不到 nfpm，请先安装或设置 NFPM=/path/to/nfpm" >&2; exit 1; }

# 版本号：优先环境变量，其次 pyproject.toml
VERSION="${VERSION:-$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)}"
[[ -n "$VERSION" ]] || { echo "无法确定版本号" >&2; exit 1; }
echo "==> 版本 $VERSION"

# ---------------------------------------------------------------- 打包根目录
echo "==> 准备 dist/pkgroot"
rm -rf dist/pkgroot
mkdir -p dist/pkgroot
# Python 包（去掉缓存与 egg-info）
cp -r arxiv_daily_report dist/pkgroot/
find dist/pkgroot -type d \( -name __pycache__ -o -name '*.egg-info' \) -prune -exec rm -rf {} + 2>/dev/null || true
# 命令入口：发行版布局下从 /usr/lib/arxiv-daily-report 导入
install -m 0755 packaging/launcher.py dist/pkgroot/arxiv-daily-report

# ---------------------------------------------------------------- 打三种包
# 三种发行版的依赖不同（Debian/Fedora 是 python3 且没有 weasyprint 包；
# Arch 是 python 且 extra/python-weasyprint 存在），因此按格式渲染模板。
echo "==> nfpm 打包"
mkdir -p dist
for fmt in deb rpm archlinux; do
  echo "    - $fmt"
  VERSION="$VERSION" FORMAT="$fmt" python3 - "$ROOT" <<'PYEOF'
import os, pathlib, sys
root = pathlib.Path(sys.argv[1])
fmt = os.environ["FORMAT"]
# matplotlib 用于把公式排成 SVG：安装包里没有 MathJax 的 node_modules
deps = {
    "archlinux": ["python", "python-weasyprint", "python-matplotlib"],
    "deb": ["python3"],
    "rpm": ["python3"],
}[fmt]
# Arch 已硬依赖 weasyprint；Debian/Fedora 只能建议装 pip 后自行 pip install weasyprint
recs = [] if fmt == "archlinux" else ["python3-pip", "python3-matplotlib"]
tpl = (root / "packaging/nfpm.yaml.tmpl").read_text(encoding="utf-8")
tpl = tpl.replace("@DEPENDS@", "\n".join("  - " + d for d in deps))
tpl = tpl.replace("@RECOMMENDS@", "\n".join("  - " + r for r in recs) if recs else "  []")
out = root / "dist" / ".nfpm" / f"nfpm-{fmt}.yaml"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(tpl, encoding="utf-8")
PYEOF
  VERSION="$VERSION" "$NFP" package -f "dist/.nfpm/nfpm-$fmt.yaml" -p "$fmt" -t dist/ 2>&1 | sed 's/^/      /'
done

# ---------------------------------------------------------------- 安装包自检
# 注意：不能用 `bsdtar tf x.deb`——它只列出 deb 的 3 个 ar 成员，
# 不会递归进内层 data.tar，对 deb 等于没检查。用专门写的校验脚本。
echo "==> 校验安装包内容"
python3 packaging/verify-packages.py "dist/*.deb" "dist/*.rpm" "dist/*.pkg.tar.zst"

echo
echo "==> 产物"
ls -la dist/*.deb dist/*.rpm dist/*.pkg.tar.zst 2>/dev/null | awk '{printf "  %8.1f KB  %s\n",$5/1024,$9}'
