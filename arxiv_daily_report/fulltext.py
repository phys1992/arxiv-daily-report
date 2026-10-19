"""抓取 arXiv 全文（HTML）并提取「摘要 + 结论/讨论」等总结性小节。

为什么用 HTML 而不是 PDF：
  * arXiv 会为投稿生成官方 HTML（LaTeXML 渲染），结构规整，体积只有 PDF 的一小部分；
  * 无需下载 PDF 全文，符合 arXiv 使用条款对批量下载 PDF 的限制。

提取策略：
  * 摘要取自 ``div.ltx_abstract``；
  * 「结论」类小节 = 顶层 ``h2`` 中标题命中 结论/讨论/总结/展望 等词的节
    （Conclusion(s)、Discussion、Summary、Concluding remarks、Outlook…）；
  * 公式用 ``<math alttext="...">`` 还原成 ``$...$``（arXiv 的每个 math 标签都带 alttext），
    这样后续 MathJax 流水线还能把它们排成正式公式。

拿不到 HTML 时返回 ``source="abstract-only"``，由调用方如实标注——
报告里会写清哪些论文只用了摘要，绝不假装读过全文。
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

__all__ = ["FullText", "FullTextService", "parse_html", "extract_text"]

ARXIV_HTML = "https://arxiv.org/html/{id}"
AR5IV_HTML = "https://ar5iv.labs.arxiv.org/html/{id}"

# 结论类小节的标题特征（arXiv 论文是英文，但标题风格不一，宁可多匹配几种）
CONCLUSION_RE = re.compile(
    r"\b(conclusions?|concluding(?:\s+remarks?)?|summary|summaries|discussion|"
    r"discussions|outlook|final\s+remarks?|future\s+(?:work|perspectives?)|"
    r"epilogue|closing\s+remarks?|results?\s+and\s+discussion)\b",
    re.I,
)
# 中文标题也认（少数论文的中文标题或作者自拟标题）
CONCLUSION_CJK = ("结论", "讨论", "总结", "展望", "结语")

# 标题前面的编号：I / II / 1 / 2.1 / A 之类
_TAG_SPAN = re.compile(r'<span[^>]*class="[^"]*ltx_tag[^"]*"[^>]*>.*?</span>', re.S | re.I)
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_MATH = re.compile(r"<math\b([^>]*)>(.*?)</math>", re.S | re.I)
_ALTTEXT = re.compile(r'alttext="([^"]*)"', re.I)
_H2 = re.compile(r"<h2\b[^>]*>.*?</h2>", re.S | re.I)
_TITLE_TAG = re.compile(r'<h([1-6])\b[^>]*class="[^"]*ltx_title[^"]*"[^>]*>(.*?)</h\1>', re.S | re.I)
_ABSTRACT_START = re.compile(r'<(div|blockquote)\b[^>]*class="[^"]*ltx_abstract[^"]*"[^>]*>', re.I)
_ABSTRACT_ALT = re.compile(
    r'<(div|blockquote)\b[^>]*class="[^"]*(?:ltx_abstract|abstract)[^"]*"[^>]*>', re.I)
_BIBLIO = re.compile(r'<(?:section|div)\b[^>]*class="[^"]*ltx_bibliography[^"]*"', re.I)
_MAX_TEXT = 4000          # 单个小节最多保留多少字符（够模型读，又不至于爆预算）


@dataclass
class FullText:
    """一篇论文的全文提取结果。"""

    arxiv_id: str
    source: str = "abstract-only"       # arxiv-html | ar5iv | abstract-only
    abstract: str = ""
    conclusions: list[tuple[str, str]] = field(default_factory=list)
    section_titles: list[str] = field(default_factory=list)
    error: str = ""
    weak_section: bool = False       # 命中的是「引言兼小结」这类替代小节，不是真正的结论

    @property
    def has_fulltext(self) -> bool:
        return bool(self.conclusions)

    def conclusion_text(self, limit: int = 6000) -> str:
        """把各结论类小节拼起来，控制在 limit 字符内。"""
        out: list[str] = []
        used = 0
        for title, body in self.conclusions:
            piece = f"【{title}】{body}".strip()
            if used + len(piece) > limit:
                piece = piece[: max(0, limit - used)]
            if piece:
                out.append(piece)
                used += len(piece)
            if used >= limit:
                break
        return "\n".join(out)

    def to_json(self) -> str:
        return json.dumps(
            {"conclusions": self.conclusions, "section_titles": self.section_titles,
             "weak_section": self.weak_section},
            ensure_ascii=False,
        )

    @classmethod
    def from_row(cls, row) -> "FullText":
        try:
            blob = json.loads(row["payload"] or "{}")
        except Exception:  # noqa: BLE001
            blob = {}
        return cls(
            arxiv_id=row["arxiv_id"],
            source=row["source"] or "abstract-only",
            abstract=row["abstract"] or "",
            conclusions=[tuple(x) for x in blob.get("conclusions", [])],
            section_titles=list(blob.get("section_titles", [])),
            error=row["error"] or "",
            weak_section=bool(blob.get("weak_section", False)),
        )


# ---------------------------------------------------------------------------
# HTML 解析
# ---------------------------------------------------------------------------
def extract_text(fragment: str, *, keep_math: bool = True) -> str:
    """把 HTML 片段转成纯文本。

    keep_math=True 时用 ``<math alttext="...">`` 还原成 ``$...$``，
    避免 MathML 被拍平成一堆孤立符号。

    实现要点：先把公式换成占位符，再去标签。否则公式里的 ``<``（例如 ``a<0.9``）
    会被后面的去标签正则当成 HTML 标签吃掉。
    """
    if not fragment:
        return ""
    s = _SCRIPT.sub(" ", fragment)

    math_tex: list[str] = []

    def _stash(m: re.Match) -> str:
        alt = _ALTTEXT.search(m.group(1))
        if keep_math and alt:
            tex = _html.unescape(alt.group(1)).strip()
            if tex:
                math_tex.append(tex)
                return f"\x00{len(math_tex) - 1}\x00"
        return " "

    s = _MATH.sub(_stash, s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s).replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s)

    def _unstash(m: re.Match) -> str:
        return f" ${math_tex[int(m.group(1))]}$ " if math_tex else " "

    s = re.sub(r"\x00(\d+)\x00", _unstash, s)
    return re.sub(r"\s+", " ", s).strip()


def _balanced_end(s: str, start: int, tag: str) -> int:
    """从 start（该标签的开始处）起找到匹配的结束位置，处理嵌套。"""
    depth = 0
    for m in re.finditer(rf"<{tag}\b[^>]*>|</{tag}>", s[start:], re.I):
        if m.group(0).lower().startswith(f"</{tag}"):
            depth -= 1
            if depth <= 0:
                return start + m.end()
        else:
            depth += 1
    return len(s)


def _abstract_of(s: str) -> str:
    m = _ABSTRACT_START.search(s) or _ABSTRACT_ALT.search(s)
    if not m:
        return ""
    # 摘要里的 <h6>Abstract</h6> 标题去掉
    end = _balanced_end(s, m.start(), m.group(1).lower())
    frag = s[m.end():end]
    frag = _TITLE_TAG.sub(" ", frag)
    text = extract_text(frag)
    return re.sub(r"^\s*Abstract[.:]?\s*", "", text, flags=re.I).strip()


def _title_of(raw: str) -> str:
    cleaned = _TAG_SPAN.sub(" ", raw)
    text = extract_text(cleaned)
    # 去掉残留的罗马数字/阿拉伯数字编号
    return re.sub(r"^(?:[IVXLCDM]+|\d+)(?:\.\d+)*\.?\s+", "", text).strip()


def _sections(s: str) -> tuple[list[str], list[tuple[str, str]]]:
    """返回（全部顶层小节标题, [(标题, 正文)]）。"""
    heads = [(m.start(), m.end(), m.group(0)) for m in _H2.finditer(s)]
    stop = len(s)
    biblio = _BIBLIO.search(s)
    if biblio:
        stop = min(stop, biblio.start())
    titles: list[str] = []
    bodies: list[tuple[str, str]] = []
    for i, (start, end, raw) in enumerate(heads):
        if start >= stop:              # 参考文献及其之后的标题不算正文小节
            break
        nxt = heads[i + 1][0] if i + 1 < len(heads) else stop
        nxt = min(nxt, stop)
        title = _title_of(raw)
        if not title:
            continue
        titles.append(title)
        body = extract_text(s[end:nxt])
        if body:
            bodies.append((title, body[:_MAX_TEXT]))
    return titles, bodies


def parse_html(arxiv_id: str, raw: str, source: str = "arxiv-html") -> FullText:
    """从 arXiv/ar5iv 的 HTML 里提取摘要与结论类小节。"""
    titles, bodies = _sections(raw)
    conclusions = _select_conclusions(bodies)
    return FullText(
        arxiv_id=arxiv_id,
        source=source if conclusions else "abstract-only",
        abstract=_abstract_of(raw),
        conclusions=conclusions,
        section_titles=titles,
        error="" if conclusions else "未找到结论/讨论类小节",
    weak_section=bool(conclusions) and _is_intro_like(conclusions[0][0]),
    )


def _is_conclusion(title: str) -> bool:
    if any(w in title for w in CONCLUSION_CJK):
        return True
    return bool(CONCLUSION_RE.search(title))


# "Introduction and Summary" 这类标题也含 summary，但它是引言，不是结论。
# 物理类论文常用"引言末尾给结果摘要"的写法，内容有用，但如果有真正的
# 结论/讨论节，应当优先用后者，别把预算花在引言上。
_INTRO_LIKE = re.compile(r"\b(introduction|motivation|overview|prologue)\b", re.I)


def _is_intro_like(title: str) -> bool:
    return bool(_INTRO_LIKE.search(title))


def _select_conclusions(bodies: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """优先取真正的结论/讨论节；没有时再退回「引言兼小结」这类小节。"""
    strong = [(t, b) for t, b in bodies if _is_conclusion(t) and not _is_intro_like(t)]
    if strong:
        return strong
    return [(t, b) for t, b in bodies if _is_conclusion(t)]


# ---------------------------------------------------------------------------
# 抓取 + 缓存
# ---------------------------------------------------------------------------
class FullTextService:
    """按需抓取全文并缓存到 sqlite，避免重复请求 arXiv。"""

    def __init__(self, store, cfg, *, progress=None):
        self.store = store
        self.cfg = cfg
        self.progress = progress or (lambda *a, **k: None)
        conf = cfg.get("fulltext", {}) or {}
        self.conf = conf
        self.enabled = bool(conf.get("enabled", True))
        self.source = str(conf.get("source", "auto") or "auto")
        self.timeout = float(conf.get("timeout_seconds", 30) or 30)
        # arxiv.org/robots.txt 要求 Crawl-delay: 15 —— 这里不允许配得更小
        wanted = float(conf.get("throttle_seconds", 15.0) or 15.0)
        from .arxiv_client import CRAWL_DELAY

        self.throttle = max(CRAWL_DELAY, wanted)
        if wanted < CRAWL_DELAY:
            log.warning("全文抓取间隔 %.1fs 低于 robots.txt 的 Crawl-delay %.0fs，已按 %.0fs 执行",
                        wanted, CRAWL_DELAY, self.throttle)
        self._last = 0.0
        self.stats = {"hit": 0, "fetched": 0, "failed": 0, "no_fulltext": 0}

    # -- 对外接口 ----------------------------------------------------------
    def get(self, arxiv_id: str, *, use_cache: bool = True) -> FullText:
        if not self.enabled:
            return FullText(arxiv_id=arxiv_id, error="全文抓取已关闭")
        if use_cache and self.store is not None:
            row = self.store.get_fulltext(arxiv_id)
            if row is not None:
                self.stats["hit"] += 1
                ft = FullText.from_row(row)
                if not ft.has_fulltext:
                    self.stats["no_fulltext"] += 1
                return ft
        ft = self._fetch(arxiv_id)
        if ft.has_fulltext:
            self.stats["fetched"] += 1
        else:
            self.stats["failed"] += 1
            self.stats["no_fulltext"] += 1
        if self.store is not None:
            try:
                self.store.save_fulltext(
                    arxiv_id, source=ft.source, abstract=ft.abstract,
                    payload=ft.to_json(), error=ft.error,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("全文缓存写入失败 %s：%s", arxiv_id, exc)
        return ft

    def prepare(self, arxiv_ids: list[str]) -> None:
        """批量预取（供日报在调用模型前一次性备好）。"""
        if not self.enabled:
            return
        pending = [a for a in arxiv_ids if self.store is None or self.store.get_fulltext(a) is None]
        if not pending:
            return
        note = self.progress
        note(f"  抓取全文：{len(pending)} 篇（arXiv HTML）")
        for i, aid in enumerate(pending, 1):
            self.get(aid, use_cache=False)
            if i % 10 == 0 or i == len(pending):
                note(f"    已处理 {i}/{len(pending)}，成功 {self.stats['fetched']}")

    # -- 抓取 --------------------------------------------------------------
    def _throttle(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.throttle:
            time.sleep(self.throttle - gap)
        self._last = time.monotonic()

    def _urls(self, arxiv_id: str) -> list[tuple[str, str]]:
        aid = arxiv_id.split("v")[0] if re.match(r"^[\w.]+v\d+$", arxiv_id) else arxiv_id
        out: list[tuple[str, str]] = []
        if self.source in ("auto", "arxiv-html"):
            out.append((ARXIV_HTML.format(id=aid), "arxiv-html"))
        if self.source in ("auto", "ar5iv"):
            out.append((AR5IV_HTML.format(id=aid), "ar5iv"))
        return out

    def _fetch(self, arxiv_id: str) -> FullText:
        from .arxiv_client import default_user_agent

        agent = default_user_agent(self.cfg.get("fetch", {}).get("contact", ""))
        last_err = ""
        for url, tag in self._urls(arxiv_id):
            self._throttle()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": agent})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code}"
                continue
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                continue
            if len(raw) < 2000:          # 太短多半是「尚无 HTML」的占位页
                last_err = "HTML 内容过短（可能尚无全文）"
                continue
            ft = parse_html(arxiv_id, raw, source=tag)
            if ft.has_fulltext:
                return ft
            last_err = ft.error or last_err
        return FullText(arxiv_id=arxiv_id, error=last_err or "抓取失败")
