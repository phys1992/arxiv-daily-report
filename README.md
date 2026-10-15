# grqc-daily · arXiv 论文日报

自动抓取 arXiv 指定分类的**当日新增论文**（仅首次公开的 v1 版本），用大模型生成中文摘要与分类综述，
输出排版精美的 **PDF 日报**；每月 1 号再依据当月的日报浓缩出一份**月度简报**。

日报是"清单 + 综述"，月报是"摘要"：月报不列文献、不列论文，只在正文里标注该内容出现在哪一天的日报上。

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
| **PDF 报告** | 页码、页眉、PDF 书签、可点击链接；**字体使用系统字体**——西文用系统西文字体，汉字优先思源黑体、否则用系统默认中文字体 |
| **公式排版** | MathJax 预渲染成 SVG（与 LaTeX 输出同一套 Computer Modern 字形），按公式哈希缓存 |
| **论文清单** | 每篇给出作者、标题、arXiv 号与可点击链接（由代码从元数据生成，模型不参与）；不再单列参考文献表 |
| **月度简报** | **每月 1 号**（北京时间）自动依据上一个自然月的日报浓缩：一段概览 + 主要进展 + 分主题要点；**不设字数上限**，力求准确的前提下简明扼要；正文只标注日期（如（10月13日）），不出现 arXiv 号 |
| **Web 配置界面** | 模型/调度/路径/语言/分类订阅全部可视化配置，改完即时生效 |
| **多模型支持** | DeepSeek、Kimi(Moonshot)、OpenAI、Claude(Anthropic)，以及任意**本地 OpenAI 兼容服务** |
| **可迁移** | 全部路径相对项目根，支持 `GRQC_HOME` 重定位；不打包字体与第三方库，仓库仅 1 MB 级 |
| **测试** | 43 项离线测试（含用本地 mock 验证的两种模型协议适配） |
| **安全** | 默认仅监听回环地址；对外绑定时强制密码；密钥掩码回显、`secrets.json` 权限 600 |

---

## 快速开始

> 前置条件：Python 3.11+、WeasyPrint（用 `install.sh` 自动装）、
> 系统库 pango/cairo、以及系统中文字体。详见下方[依赖与安装](#依赖与安装)。

```bash
git clone <你的仓库地址> grqc-daily
cd grqc-daily

./install.sh                 # 创建 .venv 并安装 WeasyPrint
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
├── monthly/2026-10.pdf       # 月度简报（依据该月日报浓缩）
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
| **Docker + 密码** | 部署到服务器 | `-e GRQC_WEB_PASSWORD=...`（绑定 `0.0.0.0` 时必需，否则拒绝启动） |
| **Docker** | 全平台一致 | `docker build -t grqc-daily . && docker run ...`（见 `Dockerfile`） |

> **Apple Silicon / Windows 提示**：WeasyPrint 依赖 pango。macOS 用 `brew install pango`；
> Windows 建议用 Docker 或 WSL，也可直接依赖 Chromium 兜底后端（会自动降级，代价是无页码）。

---

## 命令行

```bash
python run.py collect  [--day YYYY-MM-DD] [--category gr-qc]   # 只抓取入库
python run.py daily    [--day YYYY-MM-DD] [--no-llm]           # 抓取并生成日报
python run.py monthly  [--month YYYY-MM] [--no-llm]            # 生成月报（默认上一个自然月）
python run.py serve    [--port 8765] [--no-open]               # Web 配置界面
python run.py daemon                                           # 常驻定时
python run.py status                                           # 运行状态
```

---

## 日报与月报的分工

| | 日报 | 月度简报 |
|---|---|---|
| 何时生成 | 每天（默认北京时间 19:00） | **每月 1 号**，覆盖上一个自然月 |
| 数据来源 | arXiv API 的当日新增论文 | **只读当月日报的要点存档**，不回头翻原始论文 |
| 结构 | 今日要点 + 分类速览 + 论文清单（含作者/标题/arXiv 号/链接） | 本月概览（一段） + 主要进展（4–6 条） + 分主题要点 |
| 引用方式 | 论文清单里的编号 `[3]` | **只标日期**，如（10月13日）；不出现 arXiv 号 |
| 参考文献表 | 无（清单已含全部信息） | 无 |
| 篇幅 | 约 5–15 页（随当日论文数） | 不设字数上限；准确优先，简明其次 |

月报的输入是每天日报存档下来的「今日要点 + 各主题综述 + 各主题篇数」。
因此**先要有日报，才会有月报**；某个月没有生成过日报时，月报会提示并跳过。

术语与表述统一要求（日报与月报都适用）：**术语用领域标准译名、表述与原文一致、逻辑完整、语法从简**。
月报额外要求：**力求准确是第一位的**——宁可写长一点把事情说清楚，也不为了短而牺牲准确性；
在准确的前提下不重复、不铺陈、不写套话。

---

## 配置

配置在项目根的 `config.json`（首次运行由 `config.example.json` 复制而来），
也可以用 Web 界面修改。关键项：

```jsonc
{
  "report_language": "zh",              // zh | en | zh-en（中英对照会各跑一遍模型）
  "subscriptions": [                    // 订阅哪些分类
    { "category": "gr-qc", "enabled": true, "report_name": "", "out_dir": "" }
  ],
  "output_mode": "per_category",        // per_category | combined | both
  "crosslist_policy": "primary",        // primary | all | combined_dedup
  "schedule": {
    "mode": "daily", "time": "19:00", "timezone": "Asia/Shanghai",
    "report_day_offset": 1,             // 报告日 = 运行日 - 1
    "catch_up_days": 7,                 // 每次回溯天数
    "auto_monthly": true                // 每月 1 号自动生成上月简报
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

## 依赖与安装

### 1. Python 与 WeasyPrint（必需）

**本项目依赖 WeasyPrint 生成 PDF，不随仓库打包**，需要自行安装：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # 即 weasyprint
```

`install.sh` 会自动完成这一步（官方 PyPI 失败时依次尝试阿里云、清华镜像）：

```bash
./install.sh
```

### 2. 系统库（WeasyPrint 需要）

WeasyPrint 通过 pango 排版，需要以下系统库。**桌面版 Linux 通常已经装了**
（GTK/Qt 应用的公共依赖），只有最小化服务器/容器需要手动补：

| 发行版 | 安装命令 |
|---|---|
| Debian / Ubuntu | `sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0` |
| Fedora / RHEL | `sudo dnf install pango cairo gdk-pixbuf2` |
| Arch / Manjaro | `sudo pacman -S pango cairo gdk-pixbuf2` |
| openSUSE | `sudo zypper install pango cairo gdk-pixbuf` |

> 若这些库缺失，程序会**自动降级到 Chromium 后端**（若已安装 Chromium），
> 仍能出 PDF，但会失去页码与 PDF 书签，字体也会变成 Type 3。日志里会明确告警。

### 3. 中文字体（必需）

PDF 里的汉字由系统字体渲染。**请确保系统装了中文字体**，否则汉字会显示为方块：

```bash
sudo apt install fonts-noto-cjk        # Debian/Ubuntu：思源黑体的发行版打包
sudo dnf install google-noto-sans-cjk-fonts   # Fedora
sudo pacman -S noto-fonts-cjk          # Arch
```

字体选用规则（无需配置）：

1. 西文（英文、数字、符号）→ 系统的西文字体（DejaVu Sans / Liberation Sans / Noto Sans 等，逐个回退）
2. 汉字 → **优先思源黑体**（`Source Han Sans SC` / `Noto Sans CJK SC`），没有则依次回退到
   文泉驿微米黑、微软雅黑、苹方等，最后落到系统默认中文字体

程序启动时会用 `fc-list` 检查中文字体是否存在，缺失时在日志里给出上面这些安装提示。

想换字体就设 `report.font_family`（任意 CSS 字体列表），例如：

```jsonc
"report": {
  "font_family": "\"Noto Serif CJK SC\", serif",   // 改用思源宋体
  "font_file": ""                                 // 也可指定一个字体文件内联进来
}
```

### 4. 可选组件

| 组件 | 用途 | 安装 | 缺失时 |
|---|---|---|---|
| Node + MathJax | 公式排版与 LaTeX 一致 | `./install.sh --with-math` | 自动降级为 Unicode 公式表示 |
| Chromium | WeasyPrint 不可用时的 PDF 兜底 | 系统包管理器 | 只能出 HTML/纯元数据 |

## 数据来源与合规

- 论文元数据来自 **arXiv API**。元数据以 **CC0 1.0** 发布，可自由存储、转换与分享。
- 遵守 arXiv 使用条款：**每 3 秒至多 1 次请求**、单连接、携带可识别 User-Agent；
  本项目**不下载也不转存 PDF 全文**，报告中一律链接到 arXiv 摘要页。
- **建议在配置里填写 `fetch.contact`**（你的邮箱或仓库地址），它会出现在 User-Agent 中，
  便于 arXiv 在流量异常时联系你：`grqc-daily/0.1.0 (arXiv API client; you@example.com)`。
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
├── assets/mathjax/         MathJax 预渲染脚本（可选依赖）
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
- PDF with page numbers, running headers, bookmarks and clickable links. **Fonts come from the
  system**: Latin text uses the system's Latin font, CJK text prefers Source Han Sans (falling back
  to the system's default Chinese font). See *Dependencies* above for what to install.
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

代码以 **MIT** 授权。本项目**不分发字体文件**，PDF 使用系统字体；详见 [LICENSE](LICENSE)。
