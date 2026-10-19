"""状态存储（sqlite3，标准库）。

用途：
  * 论文去重（键 = arxiv_id + version，全局唯一，跨分类共享）；
  * 记录每篇论文命中了哪些订阅分类；
  * 已生成的报告索引（便于补录时重排）；
  * 运行日志、公式 SVG 缓存、token 账本。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .models import Paper, day_bounds_epoch, month_bounds_epoch

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    uid            TEXT PRIMARY KEY,
    arxiv_id       TEXT NOT NULL,
    version        INTEGER NOT NULL,
    primary_category TEXT,
    published      TEXT,
    updated        TEXT,
    day_bj         TEXT,
    day_et         TEXT,
    ts_utc         INTEGER,
    payload        TEXT NOT NULL,
    summarized     INTEGER NOT NULL DEFAULT 0,
    first_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_papers_day_bj ON papers(day_bj);
CREATE INDEX IF NOT EXISTS idx_papers_arxiv ON papers(arxiv_id);

CREATE TABLE IF NOT EXISTS paper_categories (
    uid      TEXT NOT NULL,
    category TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (uid, category)
);
CREATE INDEX IF NOT EXISTS idx_pc_cat ON paper_categories(category);

CREATE TABLE IF NOT EXISTS reports (
    day        TEXT NOT NULL,
    category   TEXT NOT NULL,
    path       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'daily',
    count      INTEGER NOT NULL DEFAULT 0,
    generated_at TEXT NOT NULL,
    revision   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (day, category, kind)
);

CREATE TABLE IF NOT EXISTS day_digest (
    day        TEXT NOT NULL,
    category   TEXT NOT NULL,
    highlights TEXT NOT NULL DEFAULT '',
    themes     TEXT NOT NULL DEFAULT '{}',
    theme_counts TEXT NOT NULL DEFAULT '{}',
    -- 当日论文清单（JSON: [{"id":..., "title":...}]）。月报据此为重要文献附 arXiv 号。
    papers     TEXT NOT NULL DEFAULT '[]',
    model      TEXT NOT NULL DEFAULT '',
    count      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (day, category)
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    status     TEXT NOT NULL DEFAULT 'running',
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 全文抓取结果缓存：同一篇论文只抓一次 arXiv HTML
CREATE TABLE IF NOT EXISTS fulltext (
    arxiv_id   TEXT PRIMARY KEY,
    source     TEXT NOT NULL DEFAULT '',
    abstract   TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '{}',
    error      TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS math_cache (
    tex_hash TEXT PRIMARY KEY,
    tex      TEXT NOT NULL,
    svg      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    provider     TEXT,
    model        TEXT,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    calls        INTEGER DEFAULT 0,
    note         TEXT
);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """老版本数据库的轻量迁移（幂等）。"""
        # 时区无关的日期查询：按 UTC 时间戳建索引，任意时区都能精确按日取
        try:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(papers)")}
            if "ts_utc" not in cols:
                self.conn.execute("ALTER TABLE papers ADD COLUMN ts_utc INTEGER")
                self._backfill_ts_utc()
            # 索引必须等列存在之后再建：老库在 SCHEMA 阶段还没有这一列
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_ts ON papers(ts_utc)")
            self.conn.commit()
        except Exception:  # noqa: BLE001
            pass
        try:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(day_digest)")}
            if "theme_counts" not in cols:
                self.conn.execute(
                    "ALTER TABLE day_digest ADD COLUMN theme_counts TEXT NOT NULL DEFAULT '{}'"
                )
            if "papers" not in cols:
                self.conn.execute(
                    "ALTER TABLE day_digest ADD COLUMN papers TEXT NOT NULL DEFAULT '[]'"
                )
        except Exception:  # noqa: BLE001
            pass

    def _backfill_ts_utc(self) -> None:
        """把已有记录的 updated 时间换算成 UTC 秒回填。"""
        from .models import _parse_iso

        rows = self.conn.execute(
            "SELECT uid, payload FROM papers WHERE ts_utc IS NULL").fetchall()
        for r in rows:
            try:
                updated = json.loads(r["payload"]).get("updated") or ""
            except Exception:  # noqa: BLE001
                updated = ""
            ts = int(_parse_iso(updated).timestamp())
            self.conn.execute("UPDATE papers SET ts_utc=? WHERE uid=?", (ts, r["uid"]))
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # -- 运行记录 ----------------------------------------------------------
    def start_run(self, detail: str = "") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO runs(started_at, detail) VALUES(?,?)", (_now(), detail)
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, detail: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET ended_at=?, status=?, detail=? WHERE id=?",
                (_now(), status, detail, run_id),
            )
            self.conn.commit()

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- kv ---------------------------------------------------------------
    def kv_get(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()

    # -- 论文 --------------------------------------------------------------
    def has_paper(self, uid: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM papers WHERE uid=?", (uid,)).fetchone()
            is not None
        )

    def add_paper(self, paper: Paper, assignments: dict[str, bool]) -> bool:
        """登记论文。

        assignments: {分类: 该论文是否被分配到该分类的报告}
        返回 True 表示这是新论文（此前未见）；已存在则只补充/更新归属。
        """
        bj = paper.day_str("Asia/Shanghai")
        et = paper.day_str("America/New_York")
        ts = int(paper.dt_updated("UTC").timestamp())
        new = not self.has_paper(paper.uid)
        with self._lock:
            if new:
                self.conn.execute(
                    "INSERT OR IGNORE INTO papers"
                    "(uid,arxiv_id,version,primary_category,published,updated,day_bj,day_et,"
                    " ts_utc,payload,summarized,first_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        paper.uid, paper.arxiv_id, paper.version, paper.primary_category,
                        paper.published, paper.updated, bj, et, ts,
                        json.dumps(paper.to_dict(), ensure_ascii=False),
                        1 if paper.summary else 0, _now(),
                    ),
                )
            for c, is_primary in assignments.items():
                self.conn.execute(
                    "INSERT INTO paper_categories(uid,category,is_primary) VALUES(?,?,?) "
                    "ON CONFLICT(uid,category) DO UPDATE SET is_primary=excluded.is_primary",
                    (paper.uid, c, 1 if is_primary else 0),
                )
            self.conn.commit()
        return new

    def save_paper_summary(self, paper: Paper) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE papers SET payload=?, summarized=1 WHERE uid=?",
                (json.dumps(paper.to_dict(), ensure_ascii=False), paper.uid),
            )
            self.conn.commit()

    def papers_for(self, category: str, tz: str, day: str, *, policy: str = "primary") -> list[Paper]:
        """取某分类某日的论文。

        按 UTC 时间戳的 [起, 止) 区间筛选 —— 时区只影响区间的换算，
        因此配置成任何时区都精确，不再依赖预先算好的 day_bj / day_et 列。

        policy = primary : 只取被分配到该分类报告的论文（默认，跨分类不重复）
        policy = all     : 凡带该分类标签的都取（可能跨报告重复）
        """
        start, end = day_bounds_epoch(day, tz)
        extra = "AND c.is_primary=1" if policy != "all" else ""
        sql = (
            "SELECT p.payload FROM papers p JOIN paper_categories c ON c.uid=p.uid "
            f"WHERE c.category=? AND p.ts_utc>=? AND p.ts_utc<? {extra} "
            "ORDER BY p.primary_category, p.arxiv_id"
        )
        rows = self.conn.execute(sql, (category, start, end)).fetchall()
        return [Paper.from_dict(json.loads(r["payload"])) for r in rows]

    def all_papers_on(self, tz: str, day: str) -> list[Paper]:
        start, end = day_bounds_epoch(day, tz)
        rows = self.conn.execute(
            "SELECT payload FROM papers WHERE ts_utc>=? AND ts_utc<? "
            "ORDER BY primary_category, arxiv_id",
            (start, end),
        ).fetchall()
        return [Paper.from_dict(json.loads(r["payload"])) for r in rows]

    def papers_in_month(self, tz: str, month: str) -> list[Paper]:
        """某自然月的论文（按配置时区划分月份边界）。"""
        start, end = month_bounds_epoch(month, tz)
        rows = self.conn.execute(
            "SELECT payload FROM papers WHERE ts_utc>=? AND ts_utc<? AND version=1 "
            "ORDER BY primary_category, arxiv_id",
            (start, end),
        ).fetchall()
        return [Paper.from_dict(json.loads(r["payload"])) for r in rows]

    def categories_of(self, uid: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT category FROM paper_categories WHERE uid=?", (uid,)
        ).fetchall()
        return [r["category"] for r in rows]

    def day_counts(self, tz: str, day: str) -> dict[str, int]:
        start, end = day_bounds_epoch(day, tz)
        rows = self.conn.execute(
            "SELECT primary_category, COUNT(*) n FROM papers WHERE "
            "ts_utc>=? AND ts_utc<? AND version=1 GROUP BY primary_category ORDER BY n DESC",
            (start, end),
        ).fetchall()
        return {r["primary_category"]: r["n"] for r in rows}

    # -- 日报摘要存档（月报的输入）------------------------------------------
    def save_day_digest(self, day: str, category: str, *, highlights: str = "",
                        themes: dict | None = None, theme_counts: dict | None = None,
                        papers: list[dict] | None = None,
                        model: str = "", count: int = 0) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO day_digest(day,category,highlights,themes,theme_counts,papers,model,"
                "count,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(day,category) DO UPDATE SET "
                "highlights=excluded.highlights, themes=excluded.themes, "
                "theme_counts=excluded.theme_counts, papers=excluded.papers, model=excluded.model, "
                "count=excluded.count, created_at=excluded.created_at",
                (day, category, highlights or "",
                 json.dumps(themes or {}, ensure_ascii=False),
                 json.dumps(theme_counts or {}, ensure_ascii=False),
                 json.dumps(papers or [], ensure_ascii=False),
                 model, count, _now()),
            )
            self.conn.commit()

    def get_day_digest(self, day: str, category: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM day_digest WHERE day=? AND category=?", (day, category)
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        for key in ("themes", "theme_counts", "papers"):
            try:
                out[key] = json.loads(out.get(key) or "{}")
            except Exception:
                out[key] = {} if key != "papers" else []
        if not isinstance(out.get("papers"), list):
            out["papers"] = []
        return out

    def month_digests(self, start_day: str, end_day: str, categories: list[str] | None = None) -> list[dict[str, Any]]:
        """取某个月区间内所有日报摘要，按日期升序。"""
        sql = "SELECT * FROM day_digest WHERE day>=? AND day<?"
        args: list[Any] = [start_day, end_day]
        if categories:
            sql += " AND category IN (%s)" % ",".join("?" * len(categories))
            args.extend(categories)
        sql += " ORDER BY day, category"
        rows = self.conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for key in ("themes", "theme_counts", "papers"):
                try:
                    d[key] = json.loads(d.get(key) or "{}")
                except Exception:
                    d[key] = {} if key != "papers" else []
            if not isinstance(d.get("papers"), list):
                d["papers"] = []
            out.append(d)
        return out

    # -- 全文缓存 ----------------------------------------------------------
    def get_fulltext(self, arxiv_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM fulltext WHERE arxiv_id=?", (arxiv_id,)
        ).fetchone()

    def save_fulltext(self, arxiv_id: str, *, source: str = "", abstract: str = "",
                      payload: str = "{}", error: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO fulltext(arxiv_id,source,abstract,payload,error,fetched_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(arxiv_id) DO UPDATE SET "
                "source=excluded.source, abstract=excluded.abstract, payload=excluded.payload, "
                "error=excluded.error, fetched_at=excluded.fetched_at",
                (arxiv_id, source or "", abstract or "", payload or "{}", error or "", _now()),
            )
            self.conn.commit()

    def fulltext_stats(self) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN error='' THEN 1 ELSE 0 END) AS ok FROM fulltext"
        ).fetchone()
        return {"total": row["total"] or 0, "with_fulltext": row["ok"] or 0}

    # -- 报告索引 ----------------------------------------------------------
    def record_report(self, day: str, category: str, path: str, count: int, kind: str = "daily") -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT revision FROM reports WHERE day=? AND category=? AND kind=?",
                (day, category, kind),
            ).fetchone()
            rev = (row["revision"] + 1) if row else 1
            self.conn.execute(
                "INSERT INTO reports(day,category,path,kind,count,generated_at,revision) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(day,category,kind) DO UPDATE SET "
                "path=excluded.path, count=excluded.count, generated_at=excluded.generated_at, "
                "revision=excluded.revision",
                (day, category, path, kind, count, _now(), rev),
            )
            self.conn.commit()
            return rev

    def report_exists(self, day: str, category: str, kind: str = "daily") -> bool:
        """该日该分类是否已经生成过报告（用于补缺失的日期）。"""
        return self.conn.execute(
            "SELECT 1 FROM reports WHERE day=? AND category=? AND kind=?",
            (day, category, kind),
        ).fetchone() is not None

    def reported_days(self, category: str, start: str, end: str,
                      kind: str = "daily") -> set[str]:
        """[start, end) 区间内已生成报告的日期集合。"""
        rows = self.conn.execute(
            "SELECT day FROM reports WHERE category=? AND kind=? AND day>=? AND day<?",
            (category, kind, start, end),
        ).fetchall()
        return {r["day"] for r in rows}

    def earliest_report_day(self, kind: str = "daily") -> str:
        """已生成报告的最早日期；没有则返回空串。"""
        row = self.conn.execute(
            "SELECT MIN(day) d FROM reports WHERE kind=?", (kind,)).fetchone()
        return (row["d"] or "") if row else ""

    def list_reports(self, kind: str = "daily", limit: int = 60) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM reports WHERE kind=? ORDER BY day DESC, category LIMIT ?",
            (kind, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- 公式缓存 ----------------------------------------------------------
    def math_get(self, tex_hash: str) -> str | None:
        row = self.conn.execute(
            "SELECT svg FROM math_cache WHERE tex_hash=?", (tex_hash,)
        ).fetchone()
        return row["svg"] if row else None

    def math_put(self, tex_hash: str, tex: str, svg: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO math_cache(tex_hash,tex,svg,created_at) VALUES(?,?,?,?)",
                (tex_hash, tex, svg, _now()),
            )
            self.conn.commit()

    def math_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) c FROM math_cache").fetchone()["c"])

    # -- 用量记账 ----------------------------------------------------------
    def add_usage(self, provider: str, model: str, prompt_tokens: int, completion_tokens: int, calls: int = 1, note: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO token_ledger(ts,provider,model,prompt_tokens,completion_tokens,calls,note)"
                " VALUES(?,?,?,?,?,?,?)",
                (_now(), provider, model, prompt_tokens, completion_tokens, calls, note),
            )
            self.conn.commit()

    def usage_since(self, since_iso: str) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c,"
            " COALESCE(SUM(calls),0) n FROM token_ledger WHERE ts>=?",
            (since_iso,),
        ).fetchone()
        return {"prompt_tokens": row["p"], "completion_tokens": row["c"], "calls": row["n"]}
