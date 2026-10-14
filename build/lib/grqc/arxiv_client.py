"""arXiv API 客户端。

遵守 arXiv API Terms of Use：
  * 每 3 秒最多 1 次请求，单连接；
  * 只使用描述性元数据（CC0），不转存 PDF；
  * User-Agent 中带项目地址，便于对方联系。
https://info.arxiv.org/help/api/tou.html
"""
from __future__ import annotations

import datetime as dt
import gzip
import io
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Iterator

from .models import Paper, parse_arxiv_id

log = logging.getLogger(__name__)

API = "https://export.arxiv.org/api/query"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}
UA_NAME = "grqc-daily"


def default_user_agent(contact: str = "") -> str:
    """构造可识别的 User-Agent。

    arXiv 使用条款要求请求方可被识别，建议把自己的邮箱或仓库地址填到
    config 的 fetch.contact，方便对方在流量异常时联系你。
    """
    from . import __version__

    tail = f"; {contact.strip()}" if contact and contact.strip() else ""
    return f"{UA_NAME}/{__version__} (arXiv API client{tail})"


class ArxivError(RuntimeError):
    pass


class ArxivClient:
    def __init__(
        self,
        *,
        interval_sec: float = 3.0,
        timeout_sec: int = 60,
        page_size: int = 100,
        max_pages: int = 60,
        user_agent: str = "",
        retries: int = 3,
        raw_dir: Path | None = None,
    ):
        self.interval = max(0.0, float(interval_sec))
        self.timeout = int(timeout_sec)
        self.page_size = min(max(1, int(page_size)), 2000)
        self.max_pages = max(1, int(max_pages))
        self.ua = user_agent or API_VERSION
        self.retries = max(1, int(retries))
        self.raw_dir = raw_dir
        self._last_request = 0.0
        self.request_count = 0

    # -- HTTP --------------------------------------------------------------
    def _throttle(self) -> None:
        wait = self.interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _get(self, params: dict[str, str], tag: str = "") -> str:
        url = API + "?" + urllib.parse.urlencode(params, safe=":")
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.ua,
                    "Accept-Encoding": "gzip",
                    "Accept": "application/atom+xml",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                self._last_request = time.monotonic()
                self.request_count += 1
                text = raw.decode("utf-8", errors="replace")
                self._dump_raw(text, tag)
                return text
            except urllib.error.HTTPError as exc:  # 4xx 不重试
                self._last_request = time.monotonic()
                last = exc
                if 400 <= exc.code < 500 and exc.code not in (408, 429):
                    raise ArxivError(f"arXiv API 返回 {exc.code}: {exc.reason}") from exc
                log.warning("arXiv API %s（第 %d 次），退避重试", exc.code, attempt)
            except Exception as exc:  # noqa: BLE001
                self._last_request = time.monotonic()
                last = exc
                log.warning("arXiv 请求失败（第 %d 次）：%s", attempt, exc)
            if attempt < self.retries:
                time.sleep(min(60.0, 2.0 ** attempt * 3))
        raise ArxivError(f"arXiv 请求连续 {self.retries} 次失败：{last}")

    def _dump_raw(self, text: str, tag: str) -> None:
        if not self.raw_dir or not tag:
            return
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%H%M%S")
        (self.raw_dir / f"{tag}-{stamp}.xml").write_text(text, encoding="utf-8")

    # -- 解析 --------------------------------------------------------------
    @staticmethod
    def parse_entries(xml_text: str) -> list[Paper]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ArxivError(f"Atom 解析失败：{exc}") from exc
        papers: list[Paper] = []
        for e in root.findall("a:entry", NS):
            def txt(path: str, default: str = "") -> str:
                node = e.find(path, NS)
                return (node.text or "").strip() if node is not None else default

            aid_raw = txt("a:id")
            if not aid_raw:
                continue
            aid, ver = parse_arxiv_id(aid_raw)
            title = " ".join(txt("a:title").split())
            abstract = " ".join(txt("a:summary").split())
            authors = [
                (n.text or "").strip()
                for n in e.findall("a:author/a:name", NS)
                if (n.text or "").strip()
            ]
            prim = e.find("arxiv:primary_category", NS)
            cats = [c.get("term", "") for c in e.findall("a:category", NS)]
            papers.append(
                Paper(
                    arxiv_id=aid,
                    version=ver,
                    title=title,
                    authors=authors,
                    abstract=abstract,
                    published=txt("a:published"),
                    updated=txt("a:updated"),
                    primary_category=(prim.get("term", "") if prim is not None else ""),
                    categories=[c for c in cats if c],
                    comment=" ".join(txt("arxiv:comment").split()),
                    journal_ref=" ".join(txt("arxiv:journal_ref").split()),
                    doi=txt("arxiv:doi"),
                )
            )
        return papers

    @staticmethod
    def total_results(xml_text: str) -> int:
        m = re.search(r"<opensearch:totalResults[^>]*>(\d+)<", xml_text)
        return int(m.group(1)) if m else -1

    # -- 查询 --------------------------------------------------------------
    def query(
        self,
        search_query: str,
        *,
        start: int = 0,
        max_results: int | None = None,
        sort_by: str = "lastUpdatedDate",
    ) -> tuple[list[Paper], int]:
        params = {
            "search_query": search_query,
            "start": str(start),
            "max_results": str(max_results or self.page_size),
            "sortBy": sort_by,
            "sortOrder": "descending",
        }
        xml = self._get(params, tag="q")
        return self.parse_entries(xml), self.total_results(xml)

    def iter_window(
        self,
        category: str,
        start_utc: dt.datetime,
        end_utc: dt.datetime,
        *,
        tag: str = "",
    ) -> Iterator[Paper]:
        """按 lastUpdatedDate 窗口抓取某个分类的全部条目（分页）。"""
        # arXiv API 的日期过滤按 UTC，格式 YYYYMMDDHHMM
        expr = (
            f"cat:{category} AND lastUpdatedDate:"
            f"[{start_utc:%Y%m%d%H%M} TO {end_utc:%Y%m%d%H%M}]"
        )
        seen: set[str] = set()
        for page in range(self.max_pages):
            papers, total = self.query(
                expr, start=page * self.page_size, max_results=self.page_size
            )
            if page == 0:
                log.debug("分类 %s 窗口 %s~%s：totalResults=%s", category, start_utc, end_utc, total)
            if not papers:
                return
            fresh = 0
            for p in papers:
                if p.uid in seen:
                    continue
                seen.add(p.uid)
                fresh += 1
                ts = p.dt_updated("UTC")
                # 客户端精确过滤：API 的日期过滤语义偏宽松
                if start_utc <= ts < end_utc:
                    yield p
            if len(papers) < self.page_size or fresh == 0:
                return

    def fetch_by_ids(self, ids: Iterable[str], *, chunk: int = 50) -> list[Paper]:
        out: list[Paper] = []
        ids = [i for i in ids if i]
        for i in range(0, len(ids), chunk):
            part = ids[i : i + chunk]
            params = {
                "id_list": ",".join(part),
                "max_results": str(len(part)),
                "sortBy": "lastUpdatedDate",
                "sortOrder": "descending",
            }
            xml = self._get(params, tag="ids")
            out.extend(self.parse_entries(xml))
        return out


def beijing_day_window(day: str | dt.date, tz: str = "Asia/Shanghai") -> tuple[dt.datetime, dt.datetime]:
    """北京（或指定时区）自然日 -> UTC 半开区间 [start, end)。"""
    from zoneinfo import ZoneInfo

    if isinstance(day, str):
        d = dt.date.fromisoformat(day)
    else:
        d = day
    zone = ZoneInfo(tz)
    start = dt.datetime(d.year, d.month, d.day, tzinfo=zone).astimezone(dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    return start, end
