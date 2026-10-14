"""数学公式渲染。

优先使用 MathJax（Node）把 arXiv 标题里的 $...$ 预渲染成 SVG —— 与 LaTeX 输出
在视觉上一致（同一套 Computer Modern 字形轮廓）。渲染结果按公式源码哈希缓存，
日常运行几乎全部命中缓存。

降级链（永不失败）：
  1. 缓存命中 -> 直接用 SVG
  2. MathJax 可用 -> 渲染并写回缓存
  3. 都不可用 -> Unicode 数学转换（确定性、无依赖）
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .store import Store

log = logging.getLogger(__name__)

MATH_RE = re.compile(r"\$([^$]+)\$")

# Unicode 数学符号映射（降级路径 + 无 MathJax 时使用）
_SYMBOLS = {
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ", r"\epsilon": "ε",
    r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η", r"\theta": "θ", r"\vartheta": "ϑ",
    r"\iota": "ι", r"\kappa": "κ", r"\lambda": "λ", r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ",
    r"\pi": "π", r"\rho": "ρ", r"\sigma": "σ", r"\tau": "τ", r"\upsilon": "υ", r"\phi": "φ",
    r"\varphi": "φ", r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ", r"\Lambda": "Λ", r"\Xi": "Ξ",
    r"\Pi": "Π", r"\Sigma": "Σ", r"\Upsilon": "Υ", r"\Phi": "Φ", r"\Psi": "Ψ", r"\Omega": "Ω",
    r"\infty": "∞", r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\neq": "≠",
    r"\ne": "≠", r"\times": "×", r"\cdot": "·", r"\pm": "±", r"\mp": "∓", r"\to": "→",
    r"\rightarrow": "→", r"\leftarrow": "←", r"\sim": "∼", r"\simeq": "≃", r"\approx": "≈",
    r"\propto": "∝", r"\nabla": "∇", r"\partial": "∂", r"\sum": "∑", r"\prod": "∏",
    r"\int": "∫", r"\sqrt": "√", r"\equiv": "≡", r"\in": "∈", r"\notin": "∉",
    r"\subset": "⊂", r"\supset": "⊃", r"\cup": "∪", r"\cap": "∩", r"\oplus": "⊕",
    r"\otimes": "⊗", r"\dagger": "†", r"\ell": "ℓ", r"\hbar": "ℏ", r"\perp": "⊥",
    r"\parallel": "∥", r"\angle": "∠", r"\circ": "∘", r"\star": "⋆", r"\ast": "∗",
    r"\mathcal{N}": "𝒩", r"\mathcal{O}": "𝒪", r"\mathcal{L}": "ℒ", r"\mathcal{H}": "ℋ",
    r"\mathrm{d}": "d", r"\text{d}": "d", r"\,": " ", r"\;": " ", r"\ ": " ", r"\!": "",
    r"\quad": " ", r"\qquad": "  ", r"\{": "{", r"\}": "}", r"\%": "%", r"\&": "&",
}
_SUP = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUB = str.maketrans("0123456789+-=()aeoxhklmnpst", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ")


def unicode_math(tex: str) -> str:
    """把一段 LaTeX 数学源码转成 Unicode 近似文本（确定性）。"""
    s = tex
    for k in sorted(_SYMBOLS, key=len, reverse=True):
        s = s.replace(k, _SYMBOLS[k])
    s = re.sub(r"\\text\{([^}]*)\}", r"\1", s)
    s = re.sub(r"\\mathrm\{([^}]*)\}", r"\1", s)
    s = re.sub(r"\\mathcal\{([^}]*)\}", r"\1", s)
    s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: m.group(1).translate(_SUP), s)
    s = re.sub(r"\^(\w)", lambda m: m.group(1).translate(_SUP), s)
    s = re.sub(r"_\{([^{}]*)\}", lambda m: m.group(1).translate(_SUB), s)
    s = re.sub(r"_(\w)", lambda m: m.group(1).translate(_SUB), s)
    s = re.sub(r"\\[a-zA-Z]+", "", s)
    return s.replace("{", "").replace("}", "").replace("$", "").strip()


def _fix_svg(svg: str, tex: str = "") -> str:
    """MathJax 用 ex 单位标注尺寸；换算成 em 以便与周围文字同缩放。

    另外把 MathJax 生成的元素 id 改成按公式哈希唯一的前缀：多个 SVG 出现在同一
    文档里时，MathJax 的顺序编号会重复（WeasyPrint 会报 "Anchor defined twice"）。
    """
    svg = re.sub(r'width="([\d.]+)ex"', lambda m: 'width="%.3fem"' % (float(m.group(1)) * 0.5), svg)
    svg = re.sub(r'height="([\d.]+)ex"', lambda m: 'height="%.3fem"' % (float(m.group(1)) * 0.5), svg)
    svg = re.sub(r"vertical-align:\s*(-?[\d.]+)ex", lambda m: "vertical-align: %.3fem" % (float(m.group(1)) * 0.5), svg)
    prefix = "M" + _hash(tex)[:10]
    svg = re.sub(r'MJX-\d+-', prefix + "-", svg)
    return svg


class MathService:
    """公式渲染服务（缓存 + MathJax + Unicode 降级）。"""

    def __init__(self, store: Store, *, assets_dir: Path, backend: str = "auto", node: str | None = None):
        self.store = store
        self.assets = Path(assets_dir)
        self.backend = backend          # auto | mathjax | unicode
        self.node = node or shutil.which("node")
        self._script = self.assets / "mathjax" / "render.js"
        self._modules = self.assets / "mathjax"
        self._stats = {"hit": 0, "rendered": 0, "unicode": 0}

    # -- 能力探测 ----------------------------------------------------------
    @property
    def mathjax_available(self) -> bool:
        if self.backend == "unicode":
            return False
        if not self.node or not self._script.exists():
            return False
        return (self._modules / "node_modules" / "mathjax-full").exists()

    # -- 批量准备 ----------------------------------------------------------
    @staticmethod
    def extract(text: str) -> list[str]:
        return [m.group(1).strip() for m in MATH_RE.finditer(text or "") if m.group(1).strip()]

    def prepare(self, texts: list[str]) -> None:
        """把一批文本里出现的公式补进缓存（一次 Node 调用批量渲染）。"""
        formulas: list[str] = []
        for t in texts:
            formulas.extend(self.extract(t))
        if not formulas:
            return
        missing: list[str] = []
        seen: set[str] = set()
        for f in formulas:
            h = _hash(f)
            if h in seen:
                continue
            seen.add(h)
            if self.store.math_get(h):
                self._stats["hit"] += 1
            else:
                missing.append(f)
        if not missing:
            return
        if self.mathjax_available:
            rendered = self._render_mathjax(missing)
            for tex, svg in rendered.items():
                self.store.math_put(_hash(tex), tex, svg)
                self._stats["rendered"] += 1
            if len(rendered) == len(missing):
                return
            log.warning("MathJax 只渲染出 %d/%d 个公式，其余走 Unicode 降级", len(rendered), len(missing))
        self._stats["unicode"] += len(missing)

    def _render_mathjax(self, formulas: list[str]) -> dict[str, str]:
        items = [{"id": str(i), "tex": f, "display": False} for i, f in enumerate(formulas)]
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "in.json"
            out = Path(td) / "out.json"
            inp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            try:
                proc = subprocess.run(
                    [self.node, str(self._script), str(inp), "local", str(out)],
                    cwd=str(self._modules), capture_output=True, text=True, timeout=180,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("MathJax 调用失败：%s", exc)
                return {}
            if proc.returncode != 0 or not out.exists():
                log.warning("MathJax 退出码 %s：%s", proc.returncode, (proc.stderr or "")[-300:])
                return {}
            try:
                data = json.loads(out.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.warning("MathJax 输出解析失败：%s", exc)
                return {}
            return {item["tex"]: _fix_svg(item["svg"], item["tex"]) for item in data if item.get("svg")}

    # -- 行内渲染 ----------------------------------------------------------
    def to_html(self, text: str) -> str:
        """把含 $...$ 的文本转成 HTML 片段（数学部分内联 SVG 或 Unicode）。"""
        if not text:
            return ""
        out: list[str] = []
        pos = 0
        for m in MATH_RE.finditer(text):
            out.append(html.escape(text[pos:m.start()]))
            tex = m.group(1).strip()
            svg = self.store.math_get(_hash(tex))
            if svg:
                out.append(f'<span class="math">{svg}</span>')
            else:
                out.append(f'<span class="math-unicode">{html.escape(unicode_math(tex))}</span>')
            pos = m.end()
        out.append(html.escape(text[pos:]))
        return "".join(out)

    def plain(self, text: str) -> str:
        """纯文本化（用于分类、检索等场景）。"""
        return MATH_RE.sub(lambda m: unicode_math(m.group(1)), text or "")

    def report_stats(self) -> dict[str, int]:
        return dict(self._stats)


def _hash(tex: str) -> str:
    return hashlib.sha256(tex.encode("utf-8")).hexdigest()[:32]
