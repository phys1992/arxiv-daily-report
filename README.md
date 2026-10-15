# arxiv-daily-report · arXiv 论文日报

自动抓取 arXiv 指定分类的**当日新增论文**（仅首次公开的 v1 版本），用大模型生成中文摘要与分类综述，
输出排版精美的 **PDF 日报**；每月 1 号再依据当月的日报浓缩出一份**月度简报**。

日报是"清单 + 综述"，月报是"摘要"：月报不列文献、不列论文，只在正文里标注该内容出现在哪一天的日报上。

> 默认订阅 `gr-qc`（广义相对论与量子宇宙学），可在 Web 界面里换成 arXiv 全部 **155 个分类**的任意组合——
> 它不只为 gr-qc 服务，任何分类（或分类组合）都能生成日报。

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()
[![Packages](https://img.shields.io/badge/packages-deb%20%7C%20rpm%20%7C%20pkg.tar.zst-orange)]()
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)]()

---

## 特性

| 能力 | 说明 |
|---|---|
| **分类订阅** | 155 个 arXiv 分类任意多选；一次运行同步更新全部报告 |
| **多分类去重** | 按 arXiv ID 全局去重：同一篇论文只调用一次模型，只出现在一份报告里 |
| **仅统计新投稿** | 只收 v1 首次公开，忽略版本更新（可配置） |
| **北京时间自然日** | 报告日按 `Asia/Shanghai` 自然日切分；自动回溯 7 天，周末/节假日延迟公告也能补录 |
| **PDF 报告** | 页码、页眉、PDF 书签、可点击链接；**字体使用系统字体**——西文用系统西文字体，汉字优先思源黑体、否则用系统默认中文字体 |
| **公式排版** | MathJax 预渲染成 SVG（与 LaTeX 输出同一套 Computer Modern 字形），按公式哈希缓存 |
| **论文清单** | 每篇给出作者、标题、arXiv 号与可点击链接（由代码从元数据生成，模型不参与）；不再单列参考文献表 |
| **月度简报** | **每月 1 号**（北京时间）自动依据上一个自然月的日报浓缩：一段概览 + 主要进展 + 分主题要点；**不设字数上限**，力求准确的前提下简明扼要；正文只标注日期（如（10月13日）），不出现 arXiv 号 |
| **Web 配置界面** | 模型/调度/路径/语言/分类订阅全部可视化配置，改完即时生效 |
| **多模型支持** | DeepSeek、Kimi(Moonshot)、OpenAI、Claude(Anthropic)，以及任意**本地 OpenAI 兼容服务** |
| **systemd 用户服务** | `systemctl --user` 一键开机自启，**不需要 root**，也不会有权限问题 |
| **三种安装包** | Debian/Ubuntu 的 `.deb`、Fedora/RHEL 的 `.rpm`、Arch/Manjaro 的 `.pkg.tar.zst`，由 GitHub Actions 打 tag 自动构建发布 |
| **XDG 目录规范** | 配置进 `~/.config`、数据进 `~/.local/share`、缓存进 `~/.cache`；也支持 `ADR_HOME` 便携模式（整个目录拷走就能用） |
| **测试** | 63 项离线测试（含用本地 mock 验证的两种模型协议适配），不联网、不消耗模型额度 |

---

## 安装

支持 **Linux**（Debian 系 / Fedora 系 / Arch 系）。三选一：

### 方式一：发行版安装包（推荐）

从 [Releases](../../releases) 下载对应格式：

```bash
# Debian / Ubuntu
sudo apt install ./arxiv-daily-report_0.2.0-1_amd64.deb

# Fedora / RHEL
sudo dnf install ./arxiv-daily-report-0.2.0-1.x86_64.rpm

# Arch / Manjaro
sudo pacman -U ./arxiv-daily-report-0.2.0-1-x86_64.pkg.tar.zst
```

安装后命令是 `arxiv-daily-report`。三种包安装到相同位置，便于统一维护：

| 内容 | 路径 |
|---|---|
| 命令入口 | `/usr/bin/arxiv-daily-report` |
| Python 包 | `/usr/lib/arxiv-daily-report/` |
| 数据（分类表、主题表） | `/usr/share/arxiv-daily-report/data/` |
| Web 界面静态文件 | `/usr/share/arxiv-daily-report/web/` |
| systemd 用户单元 | `/usr/lib/systemd/user/arxiv-daily-report.service` |
| 文档与许可证 | `/usr/share/doc/arxiv-daily-report/`、`/usr/share/licenses/arxiv-daily-report/` |

### 方式二：源码检出（可迁移）

```bash
git clone <你的仓库地址> arxiv-daily-report
cd arxiv-daily-report
./install.sh                 # 创建 .venv 并安装 WeasyPrint
# ./install.sh --with-math   # 可选：同时安装 MathJax（公式与 LaTeX 一致）
```

这种方式下**所有状态都在项目目录内**（`config.json`、`data/`、`reports/`、`cache/`），
整个目录拷到另一台机器就能继续用。

### 方式三：Docker

```bash
docker build -t arxiv-daily-report .
docker run -d --name arxiv-daily -p 8765:8765 -e ADR_WEB_PASSWORD=你的口令 arxiv-daily-report
```

> 绑定非回环地址时必须设置 `ADR_WEB_PASSWORD`，否则程序拒绝启动。

### 依赖（方式一、二都需要）

**本项目不打包字体，也不打包 WeasyPrint**，需要由系统提供：

```bash
# 1) WeasyPrint（生成 PDF 必需）
#    Arch 已由安装包自动带上；Debian/Fedora 官方源没有这个包，用 pip 装：
pip install --user weasyprint
#    想隔离环境就用 pipx：pipx install weasyprint

# 2) 系统库（桌面版通常已有；最小化服务器/容器需要补）
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0   # Debian/Ubuntu
sudo dnf install pango cairo gdk-pixbuf2                                           # Fedora
sudo pacman -S pango cairo gdk-pixbuf2                                             # Arch

# 3) 中文字体（否则报告里的汉字是方块）
sudo apt install fonts-noto-cjk                  # Debian/Ubuntu
sudo dnf install google-noto-sans-cjk-fonts      # Fedora
sudo pacman -S noto-fonts-cjk                    # Arch
```

| 发行版 | WeasyPrint 来源 | 包依赖 |
|---|---|---|
| Debian / Ubuntu | `pip install --user weasyprint` | `python3`，建议 `python3-pip` |
| Fedora / RHEL | `pip install --user weasyprint` | `python3`，建议 `python3-pip` |
| Arch / Manjaro | `extra/python-weasyprint` | `python`、`python-weasyprint`（自动装上） |

> 若 WeasyPrint 或 pango 缺失，程序会**自动降级到 Chromium 后端**（若已安装 Chromium），
> 仍能出 PDF，但会失去页码与 PDF 书签，字体也会变成 Type 3。日志里会明确告警。
>
> 字体选用规则（无需配置）：西文走系统的西文字体（DejaVu Sans / Liberation Sans / Noto Sans 等逐个回退），
> 汉字**优先思源黑体**（`Source Han Sans SC` / `Noto Sans CJK SC`），其次文泉驿微米黑、微软雅黑、苹方，
> 最后落到系统默认中文字体。启动时会用 `fc-list` 检查并在缺失时给出上面的安装命令。
> 想换字体就设 `report.font_family`（任意 CSS 字体列表），或用 `report.font_file` 内联一个字体文件。

---

## 快速开始

```bash
# 安装包方式
arxiv-daily-report serve            # 打开 Web 配置界面，填模型密钥
arxiv-daily-report daily            # 生成昨天（北京时间）的日报

# 源码方式
.venv/bin/python run.py serve
.venv/bin/python run.py daily

# 也可以直接用环境变量给密钥
export DEEPSEEK_API_KEY=sk-...
```

**推荐用 systemd 用户服务常驻自启**（见下节）：

```bash
systemctl --user enable --now arxiv-daily-report
journalctl --user -u arxiv-daily-report -f
```

产物（安装包方式在 `~/.local/share/arxiv-daily-report/reports/`，源码方式在项目内 `reports/`）：

```
reports/
├── gr-qc/2026-10-13.pdf      # 每个分类一份日报
├── gr-qc/latest.pdf          # 最新一期（固定路径，便于反复打开）
├── hep-th/2026-10-13.pdf
├── monthly/2026-10.pdf       # 月度简报（依据该月日报浓缩）
└── index.md                  # 全部报告的索引
```

---

## 开机自启：用 systemd 用户服务

**请用用户级服务（`systemctl --user`），不要用系统级服务。** 原因：

- 程序要读写你自己的 `~/.config`、`~/.local/share`、`~/.cache`——以你的身份运行天然没有权限问题；
- 系统级服务默认以 root 运行，会把报告和数据库写成 root 所有，之后你自己反而改不动；
- 模型密钥是你个人的，放在用户目录里更合适。

### 安装包方式（单元已随包安装）

```bash
systemctl --user enable --now arxiv-daily-report     # 启用并立即启动
systemctl --user status arxiv-daily-report           # 查看状态
journalctl --user -u arxiv-daily-report -f           # 跟踪日志
systemctl --user disable --now arxiv-daily-report    # 停用
```

若希望**未登录时也保持运行**（例如服务器上），开启 lingering：

```bash
sudo loginctl enable-linger "$USER"
```

### 源码方式

```bash
./packaging/systemd/install-service.sh              # 自动生成并启用单元
./packaging/systemd/install-service.sh --uninstall  # 卸载
```

定时（默认每天 19:00，`Asia/Shanghai`）由**进程内部的调度器**完成，systemd 只负责把进程拉起来并保活。
因此用 Web 界面改运行时间会立即生效，不需要 `daemon-reload`，也就不需要 timer 单元。

---

## 命令行

安装包方式用 `arxiv-daily-report`，源码方式用 `python run.py`，参数完全一致：

```bash
arxiv-daily-report collect  [--day YYYY-MM-DD] [--category gr-qc]   # 只抓取入库
arxiv-daily-report daily    [--day YYYY-MM-DD] [--no-llm]           # 抓取并生成日报
arxiv-daily-report monthly  [--month YYYY-MM] [--no-llm]            # 生成月报（默认上一个自然月）
arxiv-daily-report serve    [--port 8765] [--no-open]               # Web 配置界面
arxiv-daily-report daemon                                           # 常驻定时（systemd 就是跑它）
arxiv-daily-report status                                           # 运行状态与目录位置
```

`--no-llm` 只出 PDF 不调用模型，适合先验证抓取与排版，或节省额度。

---

## 日报与月报的分工

| | 日报 | 月度简报 |
|---|---|---|
| 何时生成 | 每天（默认北京时间 19:00） | **每月 1 号**，覆盖上一个自然月 |
| 数据来源 | arXiv API 的当日新增论文 | **只读当月日报的要点存档**，不回头翻原始论文 |
| 结构 | 今日要点 + 分类速览 + 论文清单（含作者/标题/arXiv 号/链接） | 本月概览（一段） + 主要进展 + 分主题要点 |
| 引用方式 | 论文清单里的编号 `[3]` | **只标日期**，如（10月13日）；不出现 arXiv 号 |
| 参考文献表 | 无（清单已含全部信息） | 无 |
| 篇幅 | 约 5–15 页（随当日论文数） | 不设字数上限；准确优先，简明其次 |

月报的输入是每天日报存档下来的「今日要点 + 各主题综述 + 各主题篇数」。
因此**先要有日报，才会有月报**；某个月没有生成过日报时，月报会提示并跳过。

术语与表述统一要求（日报与月报都适用）：**术语用领域标准译名、表述与原文一致、逻辑完整、语法从简**。
月报额外要求：**力求准确是第一位的**——宁可写长一点把事情说清楚，也不为了短而牺牲准确性；
在准确的前提下不重复、不铺陈、不写套话。

---

## 配置与目录

配置文件名是 `config.json`，位置随安装方式而变：

| | 源码检出 / `ADR_HOME` 便携模式 | 安装包方式（XDG） |
|---|---|---|
| 配置与密钥 | `<项目根>/config.json`、`secrets.json` | `~/.config/arxiv-daily-report/` |
| 数据库、原始 XML、报告 | `<项目根>/data/`、`<项目根>/reports/` | `~/.local/share/arxiv-daily-report/` |
| 缓存（可随时删） | `<项目根>/cache/` | `~/.cache/arxiv-daily-report/` |
| 随程序分发的资产 | `<项目根>/data/`、`assets/` | `/usr/share/arxiv-daily-report/` |

用 `arxiv-daily-report status` 可以看到本机实际使用的路径。关键配置项：

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
  "report": {
    "font_family": "",                  // 空 = 内置字体栈（西文优先 + 中文回退）
    "font_file": "",                    // 可选：内联一个字体文件
    "math_backend": "auto"              // auto | mathjax | unicode
  },
  "paths": {
    "reports": "",                      // 空 = 自动推导
    "data": "",
    "cache": "",
    "publish_dir": ""                   // 例如同步盘目录；空则不发布
  }
}
```

**环境变量覆盖**（优先级高于配置文件，适合容器与脚本）：

| 变量 | 作用 |
|---|---|
| `ADR_HOME` | 便携模式总目录：配置/数据/缓存/报告全部收进这里 |
| `ADR_CONFIG_HOME` / `ADR_DATA_HOME` / `ADR_CACHE_HOME` | 分别指定三个目录 |
| `ADR_PREFIX` | 系统安装前缀，默认 `/usr`（用于 `/usr/local`、Nix 等布局） |
| `ADR_ASSETS` | 静态资产目录（MathJax 等） |
| `ADR_WEB_PASSWORD` / `ADR_WEB_HOST` / `ADR_WEB_PORT` | Web 服务 |
| `ADR_PROVIDER` / `ADR_MODEL` / `ADR_BASE_URL` | 模型 |
| `ADR_REPORT_LANGUAGE` / `ADR_UI_LANGUAGE` | 语言 |

**密钥存放**（优先级从高到低）：

1. 环境变量：`DEEPSEEK_API_KEY` / `MOONSHOT_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `CUSTOM_API_KEY`
2. `secrets.json`（权限 600，`chmod 600` 自动设置，已在 `.gitignore` 中）

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

## 打包与发布

安装包由 `nfpm` 从同一份模板生成，三种格式共用完全相同的安装路径
（因此不依赖各发行版的 `python3.X/site-packages`，一个包可在三家通用）：

```bash
# 本地构建（需要 nfpm：https://github.com/goreleaser/nfpm/releases）
./packaging/build-packages.sh                    # 版本号取自 pyproject.toml
VERSION=0.2.0 ./packaging/build-packages.sh      # 或显式指定
```

产出 `dist/arxiv-daily-report_<版本>-1_amd64.deb`、`*.x86_64.rpm`、`*.pkg.tar.zst`。
脚本内置自检：**安装包里若混入运行期数据（`state.sqlite3`、`data/raw/`、`reports/`）会直接报错中止**。

发布走 GitHub Actions：

```bash
git tag v0.2.0 && git push origin v0.2.0
```

`.github/workflows/release.yml` 会自动跑测试 → 构建三种包 → 附上 `SHA256SUMS` → 发布到 Release。
`.github/workflows/ci.yml` 则在每次 push/PR 上跑测试并验证三种包的布局是否正确。

---

## 数据来源与合规

- 论文元数据来自 **arXiv API**。元数据以 **CC0 1.0** 发布，可自由存储、转换与分享。
- 遵守 arXiv 使用条款：**每 3 秒至多 1 次请求**、单连接、携带可识别 User-Agent；
  本项目**不下载也不转存 PDF 全文**，报告中一律链接到 arXiv 摘要页。
- **建议在配置里填写 `fetch.contact`**（你的邮箱或仓库地址），它会出现在 User-Agent 中，
  便于 arXiv 在流量异常时联系你：`arxiv-daily-report/0.2.0 (arXiv API client; you@example.com)`。
- 本项目与 arXiv.org **无任何官方关联**。
- 详见 [arXiv API Terms of Use](https://info.arxiv.org/help/api/tou.html)。

---

## 目录结构

```
arxiv-daily-report/
├── run.py                  CLI 入口（源码方式）
├── install.sh              源码安装脚本
├── arxiv_daily_report/     核心包
│   ├── config.py           配置 + XDG/便携目录解析
│   ├── arxiv_client.py     抓取（限速/重试/分页）
│   ├── collect.py          采集编排与分类归属
│   ├── store.py            sqlite 状态（去重/索引/公式缓存/用量）
│   ├── summarize.py        摘要提示词与校验（防幻觉）
│   ├── render.py           HTML 版式与字体栈
│   ├── pdf.py              PDF 后端（WeasyPrint → Chromium 降级）
│   ├── report_daily.py     日报编排
│   ├── report_monthly.py   月报 map-reduce
│   ├── server.py           Web 后端（标准库 http.server）
│   └── llm/                多厂商模型适配
├── web/                    纯静态配置界面
├── data/                   arXiv 分类表、主题表（随程序分发）
├── assets/mathjax/         MathJax 预渲染脚本（可选依赖）
├── packaging/
│   ├── nfpm.yaml.tmpl      deb/rpm/pkg.tar.zst 打包模板
│   ├── build-packages.sh   构建三种安装包
│   ├── launcher.py         安装包里的 /usr/bin 入口
│   └── systemd/            用户级服务单元与安装脚本
├── .github/workflows/      ci.yml（测试+打包校验）、release.yml（发布）、daily.yml（云端生成）
├── Dockerfile
└── reports/                产出的 PDF（已 gitignore）
```

---

## English

**arxiv-daily-report** fetches newly announced arXiv papers (first-release `v1` only) for any
combination of arXiv's 155 categories, summarises them with an LLM, and renders a typeset
**PDF daily digest** plus a **monthly thematic review**.

Highlights:

- Multi-category subscriptions with global de-duplication by arXiv ID (a paper is summarised once
  and appears in exactly one report).
- Beijing-time calendar day with a 7-day catch-up window, so delayed weekend/holiday announcements
  are never lost.
- PDF with page numbers, running headers, bookmarks and clickable links. **Fonts come from the
  system**: Latin text uses the system's Latin font, CJK text prefers Source Han Sans (falling back
  to the system's default Chinese font).
- Formula typesetting via MathJax pre-rendered to SVG (same Computer Modern outlines as LaTeX),
  cached by formula hash.
- Paper lists (authors, title, arXiv ID, URL) are generated from metadata by code, never by the model.
- Providers: DeepSeek, Kimi, OpenAI, Claude (native Anthropic Messages protocol), and any local
  OpenAI-compatible endpoint.
- **Linux-only**, shipped as `.deb`, `.rpm` and `.pkg.tar.zst`, all installing to the same FHS
  paths (`/usr/lib/arxiv-daily-report`, `/usr/share/arxiv-daily-report`).
- Follows the XDG base directory spec for user state (`~/.config`, `~/.local/share`, `~/.cache`),
  with `ADR_HOME` for a fully portable layout.

Install:

```bash
sudo apt install ./arxiv-daily-report_0.2.0-1_amd64.deb   # or dnf / pacman -U
pip install --user weasyprint          # PDF backend (packaged on Arch as python-weasyprint)
arxiv-daily-report serve               # configure the LLM provider in the web UI
systemctl --user enable --now arxiv-daily-report   # auto-start, no root required
```

Metadata comes from the arXiv API (CC0) under its
[Terms of Use](https://info.arxiv.org/help/api/tou.html); this project is not affiliated with arXiv.org.

---

## License

代码以 **MIT** 授权。本项目**不分发字体文件**，PDF 使用系统字体；详见 [LICENSE](LICENSE)。
