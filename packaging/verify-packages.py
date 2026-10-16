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

import bz2
import glob
import gzip
import io
import lzma
import struct
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


# 载荷压缩算法 -> 开头magic（用来定位载荷真实起点）
_COMPRESSOR_MAGIC = {
    "gzip": b"\x1f\x8b",
    "bzip2": b"BZh",
    "xz": b"\xfd7zXZ\x00",
    "lzma": b"\x5d\x00\x00",
    "zstd": b"\x28\xb5\x2f\xfd",
    "none": b"",
}


def _rpm_header(blob: bytes, pos: int):
    """解析一个 rpm header，返回（标签表, 数据段, 结束位置）。"""
    if blob[pos:pos + 3] != b"\x8e\xad\xe8":
        raise ValueError(f"rpm header 魔数不对：{blob[pos:pos + 3].hex()}")
    nindex, hsize = struct.unpack(">II", blob[pos + 8:pos + 16])
    start = pos + 16
    entries = [struct.unpack(">iiii", blob[start + i * 16:start + (i + 1) * 16])
               for i in range(nindex)]
    data = blob[start + nindex * 16:start + nindex * 16 + hsize]
    return entries, data, start + nindex * 16 + hsize


def _rpm_str(data: bytes, off: int) -> str:
    end = data.find(b"\x00", off)
    return data[off:end if end != -1 else None].decode("latin-1")


def _decompress(blob: bytes, algo: str) -> bytes:
    if algo == "gzip":
        return gzip.decompress(blob)
    if algo == "bzip2":
        return bz2.decompress(blob)
    if algo in ("xz", "lzma"):
        return lzma.decompress(blob)
    if algo == "zstd":
        try:                                    # Python 3.14+
            from compression import zstd as _zstd       # type: ignore[import-not-found]
            return _zstd.decompress(blob)
        except ImportError:
            pass
        try:
            import zstandard                            # 可选第三方
            return zstandard.ZstdDecompressor().decompress(blob)
        except ImportError:
            pass
        r = subprocess.run(["zstd", "-d", "-c"], input=blob, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("载荷是 zstd 压缩，需要一个能解 zstd 的解压器")
        return r.stdout
    raise RuntimeError(f"不认识的 rpm 载荷压缩算法：{algo}")


def _cpio_names(data: bytes) -> list[str]:
    """列出 cpio（newc 格式）里的文件名。"""
    names: list[str] = []
    pos = 0
    while pos + 110 <= len(data):
        if data[pos:pos + 6] not in (b"070701", b"070702"):
            break
        fields = [int(data[pos + 6 + i * 8:pos + 6 + (i + 1) * 8], 16) for i in range(13)]
        filesize, namesize = fields[6], fields[11]
        name_start = pos + 110
        name = data[name_start:name_start + namesize - 1].decode("utf-8", "replace")
        if name == "TRAILER!!!":
            break
        names.append(name)
        pos = name_start + namesize
        pos += (4 - pos % 4) % 4           # cpio 的对齐都是相对归档开头的
        pos += filesize
        pos += (4 - pos % 4) % 4
    return names


def list_rpm(path: Path) -> list[str]:
    """纯 Python 解析 rpm，不依赖 bsdtar（发行版镜像上未必有）。"""
    blob = path.read_bytes()
    if blob[:4] != b"\xed\xab\xee\xdb":
        raise ValueError("不是 rpm 文件")
    pos = 96                                    # 固定长度的 lead
    _sig, _sigdata, end = _rpm_header(blob, pos)
    pos = end + (8 - end % 8) % 8               # 签名头对齐到 8 字节
    entries, data, end = _rpm_header(blob, pos)

    fmt = comp = ""
    for tag, _typ, off, _cnt in entries:
        if tag == 1124:                         # PAYLOADFORMAT
            fmt = _rpm_str(data, off)
        elif tag == 1125:                       # PAYLOADCOMPRESSOR
            comp = _rpm_str(data, off)
    if fmt and fmt != "cpio":
        raise RuntimeError(f"不支持的 rpm 载荷格式：{fmt}")

    # 主头之后不一定补齐到 8 字节（实测 nfpm 产出的包就没补），
    # 所以按压缩算法的 magic 在几个候选偏移里定位载荷起点。
    magic = _COMPRESSOR_MAGIC.get(comp or "gzip", b"")
    start = None
    for delta in range(9):
        cand = end + delta
        if magic and blob[cand:cand + len(magic)] == magic:
            start = cand
            break
    if start is None:
        start = end
    payload = _decompress(blob[start:], comp or "gzip")
    names = _cpio_names(payload)
    if not names:
        raise RuntimeError("rpm 载荷里没解析出任何文件")
    return names


def list_zst(path: Path) -> list[str]:
    # 优先纯 Python（tarfile 从 3.14 起原生支持 zstd）
    try:
        with tarfile.open(str(path), "r:*") as tf:
            return tf.getnames()
    except Exception:  # noqa: BLE001 - 老版本 Python 上退回到命令行
        pass
    for cmd in (["tar", "--zstd", "-tf"], ["bsdtar", "tf"]):
        try:
            r = subprocess.run([*cmd, str(path)], capture_output=True, text=True)
        except FileNotFoundError:
            continue
        if r.returncode == 0:
            return r.stdout.splitlines()
    raise RuntimeError("读取 pkg.tar.zst 失败：需要 zstd（或装了 libarchive-tools 的 bsdtar）")


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
