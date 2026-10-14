# grqc-daily · arXiv 论文日报

自动抓取 arXiv 指定分类的**当日新增论文**（仅首次公开的 v1 版本），用大模型生成中文摘要与分类综述，
输出排版精美的 **PDF 日报**；每月再做一次更精简的**分类月度综述**。

> 默认订阅 `gr-qc`（广义相对论与量子宇宙学），可在 Web 界面里换成 arXiv 的全部 **155 个分类**的任意组合。

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()
[![Fonts](https://img.shields.io/badge/fonts-OFL%201.1-orange)]()

---

## 特性

| 能力 | 说明 |
|---|---|
| **分类订阅** | 155 个 arXiv 分类任意多选，与 `gr-qc` 同级；一次运行同步更新全部报告 |
| **多分类去重** | 按 arXiv ID 全局去重：同一篇论文只调用一次模型，只出现在一份报告里 |
| **仅统计新投稿** | 只收 v1 首次公开，忽略版本更新（可配置） |
| **北京时间自然日** | 报告日按 `Asia/Shanghai` 自然日切分；自动回溯 7 天，周末/节假日延迟公告也能补录 |
| **PDF 报告** | 页码、页眉、PDF 书签、可点击链接；中文用**思源黑体**，字体随仓库分发，目标机器无需安装任何字体 |
| **公式排版** | MathJax 预渲染成 SVG（与 LaTeX 输出同一套 Computer Modern 字形），按公式哈希缓存 |
| **参考文献** | 作者、标题、arXiv 号、链接，全部由代码从元数据生成，模型不参与，杜绝编造 |
| **月度综述** | 按主题 map-reduce 汇总，含统计、环比、分主题综述与代表性论文 |
| **Web 配置界面** | 模型/调度/路径/语言/分类订阅全部可视化配置，改完即时生效 |
| **多模型支持** | DeepSeek、Kimi(Moonshot)、OpenAI、Claude(Anthropic)，以及任意**本地 OpenAI 兼容服务** |
| **可迁移** | 全部路径相对项目根，支持 `GRQC_HOME` 重定位；无绝对路径硬编码 |

---

## 快速开始

```bash
git clone <你的仓库地址> grqc-daily
cd grqc-daily

./install.sh                 # 创建 .venv 并安装依赖
# ./install.sh --with-math   # 可选：同时安装 MathJax（公式与 LaTeX 一致）

export DEEPSEEK_API_KEY=sk-...      # 或稍后在 Web 界面里填

.venv/bin/python run.py daily       # 生成昨天（北京时间）的日报
.venv/bin/python run.py serve       # 打开 Web 配置界面
```

产物：

```
reports/
├── gr-qc/2026-10-13.pdf      # 每个分类一份日报
├── gr-qc/latest.pdf          # 最新一期（固定路径，便于反复打开）
├── hep-th/2026-10-13.pdf
├── monthly/2026-10.pdf       # 月度综述
└── index.md                  # 全部报告的索引
```

---

## 四种部署方式

| 方式 | 适用 | 命令 |
|---|---|---|
| **本地 CLI** | 手动或配合自己的定时器 | `.venv/bin/python run.py daily` |
| **常驻守护 + Web 界面** | 一台长期开机的机器 | `.venv/bin/python run.py daemon`（定时逻辑在进程内，Web 改时间即时生效） |
| **systemd 用户服务** | Linux 桌面/服务器，开机自启 | `packaging/systemd/install-service.sh` |
| **GitHub Actions** | 不想本地安装 | 仓库 Secrets 填 `DEEPSEEK_API_KEY`，`.github/workflows/daily.yml` 每天 11:00 UTC（北京 19:00）自动跑 |
| **Docker** | 全平台一致 | `docker build -t grqc-daily . && docker run ...`（见 `Dockerfile`） |

> **Apple Silicon / Windows 提示**：WeasyPrint 依赖 pango。macOS 用 `brew install pango`；
> Windows 建议用 Docker 或 WSL，也可直接依赖 Chromium 兜底后端（会自动降级，代价是无页码）。

---

## 命令行

```bash
python run.py collect  [--day YYYY-MM-DD] [--category gr-qc]   # 只抓取入库
python run.py daily    [--day YYYY-MM-DD] [--no-llm]           # 抓取并生成日报
python run.py monthly  [--month YYYY-MM]                       # 生成月报
python run.py serve    [--port 8765] [--no-open]               # Web 配置界面
python run.py daemon                                           # 常驻定时
python run.py status                                           # 运行状态
```

---

## 配置

配置在项目根的 `config.json`（首次运行由 `config.example.json` 复制而来），
也可以用 Web 界面修改。关键项：

```jsonc
{
  "report_language": "zh",              // zh | en | zh-en
  "subscriptions": [                    // 订阅哪些分类
    { "category": "gr-qc", "enabled": true, "report_name": "", "out_dir": "" }
  ],
  "output_mode": "per_category",        // per_category | combined | both
  "crosslist_policy": "primary",        // primary | all | combined_dedup
  "schedule": {
    "mode": "daily", "time": "19:00", "timezone": "Asia/Shanghai",
    "report_day_offset": 1,             // 报告日 = 运行日 - 1
    "catch_up_days": 7                  // 每次回溯天数
  },
  "llm": {
    "provider": "deepseek",             // deepseek | moonshot | openai | anthropic | custom
    "model": "deepseek-flash",
    "disable_thinking": true            // 结构化摘要关闭思考模式，避免推理 token 挤占输出
  },
  "report": { "font_profile": "source-han", "math_backend": "auto" }
}
```

**密钥存放**（优先级从高到低）：

1. 环境变量：`DEEPSEEK_API_KEY` / `MOONSHOT_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `CUSTOM_API_KEY`
2. 项目根 `secrets.json`（权限 600，已在 `.gitignore` 中）

Web 界面里只会回显掩码（`sk-2431••••••••abcd`），永不返回明文。

---

## 自定义模型

| 厂商 | Wire 协议 | 备注 |
|---|---|---|
| DeepSeek / Kimi / OpenAI / 本地服务 | OpenAI Chat Completions | 填 Base URL 与模型名即可 |
| Claude (Anthropic) | Anthropic Messages | 协议不同，已内置适配器（`system` 独立字段、`content[0].text`、`x-api-key`、无 `response_format`） |

本地服务（Ollama / vLLM / LM Studio）在 Web 界面选「自定义（OpenAI 兼容）」，
Base URL 填例如 `http://127.0.0.1:11434/v1`，密钥可留空。

---

## 依赖

| 层级 | 内容 | 必需性 |
|---|---|---|
| Python | **3.11+**，核心流程只用标准库 | 必需 |
| Python 包 | WeasyPrint（+ 传递依赖），装在项目内 `.venv` | 必需 |
| 系统库 | `libpango` / `libcairo`（桌面 Linux 一般已装） | 必需 |
| 字体 | **思源黑体 SC + DejaVu Sans，随仓库分发** | 已内置 |
| 可选 | Node + `mathjax-full`（公式与 LaTeX 一致） | 可选，缺失自动降级为 Unicode |
| 可选 | Chromium（WeasyPrint 不可用时的 PDF 兜底） | 可选 |

仓库自带字体意味着：**目标机器不装任何中文字体也能正确出 PDF**（含《通用规范汉字表》全部 8105 字，
其中 196 个位于 CJK 扩展 B）。运行时会做字符覆盖自检，出现无法渲染的字符会在日志中明确告警。

---

## 数据来源与合规

- 论文元数据来自 **arXiv API**。元数据以 **CC0 1.0** 发布，可自由存储、转换与分享。
- 遵守 arXiv 使用条款：**每 3 秒至多 1 次请求**、单连接、携带可识别 User-Agent；
  本项目**不下载也不转存 PDF 全文**，报告中一律链接到 arXiv 摘要页。
- 本项目与 arXiv.org **无任何官方关联**。
- 详见 [arXiv API Terms of Use](https://info.arxiv.org/help/api/tou.html)。

---

## 目录结构

```
grqc-daily/
├── run.py                  CLI 入口
├── install.sh              安装脚本
├── grqc/                   核心包
│   ├── arxiv_client.py     抓取（限速/重试/分页）
│   ├── collect.py          采集编排与分类归属
│   ├── store.py            sqlite 状态（去重/索引/公式缓存/用量）
│   ├── summarize.py        摘要提示词与校验（防幻觉）
│   ├── render.py           HTML 版式与字体嵌入
│   ├── pdf.py              PDF 后端（WeasyPrint → Chromium 降级）
│   ├── report_daily.py     日报编排
│   ├── report_monthly.py   月报 map-reduce
│   ├── server.py           Web 后端（标准库 http.server）
│   └── llm/                多厂商模型适配
├── web/                    纯静态配置界面
├── assets/fonts/           思源黑体 + DejaVu
├── assets/mathjax/         MathJax 预渲染脚本
├── data/                   arXiv 分类表、主题分类表
├── packaging/systemd/      systemd 用户服务模板
├── .github/workflows/      GitHub Actions 定时任务
├── Dockerfile
└── reports/                产出的 PDF（已 gitignore）
```

---

## English

**grqc-daily** fetches newly announced arXiv papers (first-release `v1` only) for any combination of
arXiv's 155 categories, summarises them with an LLM, and renders a typeset **PDF daily digest**
plus a **monthly thematic review**.

Highlights:

- Multi-category subscriptions with global de-duplication by arXiv ID (a paper is summarised once
  and appears in exactly one report).
- Beijing-time calendar day with a 7-day catch-up window, so delayed weekend/holiday announcements
  are never lost.
- PDF with page numbers, running headers, bookmarks and clickable links. **Fonts ship with the
  repository** (Source Han Sans SC + DejaVu Sans), so no system font installation is required —
  including full coverage of the 8,105-character *Table of General Standard Chinese Characters*.
- Formula typesetting via MathJax pre-rendered to SVG (same Computer Modern outlines as LaTeX),
  cached by formula hash.
- References (authors, title, arXiv ID, URL) are generated from metadata by code, never by the model.
- Providers: DeepSeek, Kimi, OpenAI, Claude (native Anthropic Messages protocol), and any local
  OpenAI-compatible endpoint.
- Portable: all paths are relative to the project root; `GRQC_HOME` relocates mutable state.

```bash
./install.sh && export DEEPSEEK_API_KEY=sk-... && .venv/bin/python run.py daily
```

Metadata comes from the arXiv API (CC0) under its
[Terms of Use](https://info.arxiv.org/help/api/tou.html); this project is not affiliated with arXiv.org.

---

## License

代码以 **MIT** 授权；内置字体为 **SIL OFL 1.1**（思源黑体）与 Bitstream Vera 系许可（DejaVu）。
详见 [LICENSE](LICENSE)。
