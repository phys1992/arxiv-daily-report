#!/usr/bin/env python3
"""校验安装包的内容与洁净度。

用法：
    python3 packaging/verify-packages.py dist/*.deb dist/*.rpm dist/*.pkg.tar.zst

为什么需要单独一个脚本：`bsdtar tf x.deb` 只会列出 deb 的 3 个 ar 成员
（debian-binary、control.tar.*、data.tar.*），**不会递归进内层 tarball**。
如果直接用它做校验，对 .deb 就是空校验（假阴性）——安装包里的脏数据
和缺失文件都发现不了。这里对三种格式分别用正确的读取方式。

退出码 0 表示全部通过；否则打印问题并以 1 退出。
"""
from __future__ import annotations

import glob
import io
import subprocess
import sys
import tarfile
from pathlib import Path

# 三种发行版都必须存在的文件
REQUIRED = [
    "usr/bin/arxiv-daily-report",
    "usr/lib/systemd/user/arxiv-daily-report.service",
    "usr/share/arxiv-daily-report/data/arxiv_categories.json",
    "usr/share/arxiv-daily-report/data/theme_taxonomy.json",
    "usr/share/arxiv-daily-report/web/index.html",
    "usr/share/doc/arxiv-daily-report/README.md",
    "usr/share/licenses/arxiv-daily-report/LICENSE",
    "usr/lib/arxiv-daily-report/arxiv_daily_report/cli.py",
]

# 绝不能进安装包的运行期产物
FORBIDDEN = [
    "state.sqlite3",
    "/data/raw/",
    "/reports/",
    ".git/",
    "__pycache__/",
    "secrets.json",
    "config.json",
]


def _ar_members(blob: bytes):
    """解析 ar 归档，返回 [(名字, 内容)]。GNU/BSD 的扩展名表也一并处理。"""
    if not blob.startswith(b"!<arch>\n"):
        raise ValueError("不是 ar 归档")
    pos, out, longnames = 8, [], b""
    while pos + 60 <= len(blob):
        header = blob[pos:pos + 60]
        name = header[0:16].decode("ascii", "replace").strip()
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError:
            break
        data = blob[pos + 60:pos + 60 + size]
        pos += 60 + size + (size % 2)          # 2 字节对齐
        if name == "//":                       # GNU 长文件名表
            longnames = data
            continue
        if name.startswith("/") and name[1:].isdigit() and longnames:
            off = int(name[1:])
            end = longnames.find(b"/\n", off)
            name = longnames[off:end if end != -1 else None].decode("ascii", "replace")
        if name in ("/", "debian-binary"):
            continue
        out.append((name.rstrip("/"), data))
    return out


def list_deb(path: Path) -> list[str]:
    for name, data in _ar_members(path.read_bytes()):
        if not name.startswith("data.tar"):
            continue
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            return ["./" + m.name for m in tf.getmembers()]
    raise ValueError("deb 里找不到 data.tar")


def list_rpm(path: Path) -> list[str]:
    # libarchive 能直接读 rpm 载荷；rpm2cpio 未必装
    r = subprocess.run(["bsdtar", "tf", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"bsdtar 读取 rpm 失败：{r.stderr.strip()}")
    return r.stdout.splitlines()


def list_zst(path: Path) -> list[str]:
    for cmd in (["bsdtar", "tf"], ["tar", "--zstd", "-tf"]):
        try:
            r = subprocess.run([*cmd, str(path)], capture_output=True, text=True)
        except FileNotFoundError:
            continue
        if r.returncode == 0:
            return r.stdout.splitlines()
    raise RuntimeError("读取 pkg.tar.zst 失败：需要 bsdtar 或支持 zstd 的 tar")


def list_contents(path: Path) -> list[str]:
    n = path.name
    if n.endswith(".deb"):
        return list_deb(path)
    if n.endswith(".rpm"):
        return list_rpm(path)
    if n.endswith(".pkg.tar.zst"):
        return list_zst(path)
    raise ValueError(f"未知的包格式：{n}")


def check(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        items = list_contents(path)
    except Exception as exc:  # noqa: BLE001
        return [f"无法读取：{exc}"]
    if len(items) < 10:
        problems.append(f"只列出 {len(items)} 个条目，疑似未递归进内层归档")
    joined = "\n".join(items)
    for want in REQUIRED:
        # deb/rpm 的条目带 ./ 前缀，统一归一化后比较
        if not any(n.lstrip("./") == want for n in items):
            problems.append(f"缺少 {want}")
    for bad in FORBIDDEN:
        if bad in joined:
            problems.append(f"含运行期数据/无关文件：{bad}")
    return problems


def main(argv: list[str]) -> int:
    paths: list[Path] = []
    for arg in argv[1:]:
        paths.extend(Path(p) for p in glob.glob(arg))
    paths = [p for p in paths if p.suffix in (".deb", ".rpm", ".zst")]
    if not paths:
        print("没有找到安装包（需要 .deb / .rpm / .pkg.tar.zst）", file=sys.stderr)
        return 1

    failed = False
    for p in sorted(paths):
        problems = check(p)
        if problems:
            failed = True
            print(f"  ✗ {p.name}")
            for prob in problems:
                print(f"      - {prob}")
        else:
            print(f"  ✓ {p.name}")
    if failed:
        print("安装包校验未通过", file=sys.stderr)
        return 1
    print(f"  {len(paths)} 个安装包的布局与洁净度均通过校验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
