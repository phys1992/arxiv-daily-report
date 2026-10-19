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
| **按自然日切分** | 报告日按 `Asia/Shanghai`（可配置成任意时区）自然日切分；自动回溯 7 天，周末/节假日延迟公告也能补录 |
| **结合全文** | 抓取 arXiv 官方 HTML，读取论文的**结论/讨论/总结**小节原文，与摘要一起交给模型；抓不到全文（老论文等）时如实降级为仅摘要 |
| **PDF 报告** | 页码、页眉、PDF 书签、可点击链接；默认按 arXiv 预印本排版——西文与公式用 **Computer Modern**，汉字用 **思源宋体**；缺字体时自动回退并在日志里提示 |
| **公式排版** | MathJax 预渲染成 SVG（与 LaTeX 输出同一套 Computer Modern 字形），按公式哈希缓存 |
| **论文清单** | 每篇给出作者、标题、arXiv 号与可点击链接（由代码从元数据生成，模型不参与） |
| **月度简报** | **每月 1 号**自动依据上一个自然月的日报浓缩：一段概览 + 主要进展 + 分主题要点；**重要文献在对应日期后附 arXiv 号**，号逐条核对确属该日 |
| **Web 配置界面** | 模型/调度/路径/字体/语言/分类订阅全部可视化配置，改完即时生效；本机直接打开即可，无需令牌 |
| **多模型支持** | DeepSeek、Kimi(Moonshot)、OpenAI、Claude(Anthropic)，以及任意**本地 OpenAI 兼容服务** |
| **systemd 用户服务** | `systemctl --user` 一键开机自启，**不需要 root**，也不会有权限问题 |
| **三种安装包** | Debian/Ubuntu 的 `.deb`、Fedora/RHEL 的 `.rpm`、Arch/Manjaro 的 `.pkg.tar.zst` |
| **XDG 目录规范** | 报告进「文档」目录，配置/状态/缓存各归其位；也支持 `ADR_HOME` 便携模式（整个目录拷走就能用） |
| **离线测试** | 118 项，不联网、不消耗模型额度 |

---

## 工作原理

### 日报

每天定时（默认北京时间 19:00）跑一次：

1. **抓取** 通过 arXiv API 拉取订阅分类在目标日（默认昨天）的论文。报告日按配置时区的**自然日**
   切分，并向前**回溯若干天**（默认 7 天），这样周末与节假日延迟公告的论文也不会漏；只收首次公开的 v1。
   > arXiv 的时间戳是 **UTC**（形如 `2026-10-16T17:30:38Z`）。程序内部一律按 UTC 比较，
   > 只在划分「哪一天」时换算成配置的时区——所以把时区改成东京、纽约都不影响正确性，
   > 同一天凌晨/深夜的论文不会被算错日子。
2. **去重** 按 arXiv ID 全局去重。一篇论文只调用一次模型，也只出现在一份报告里
   （跨分类的交叉列表论文按 `crosslist_policy` 决定归属）。
3. **读全文** 每篇抓一次 arXiv 官方 HTML，提取**结论 / 讨论 / 总结**类小节的原文，
   与摘要一起作为模型输入。摘要交代问题与方法，结论给出最终结果、适用条件与定量数值。
   拿不到 HTML 的（老论文等）如实降级为仅摘要，不会假装读过。
4. **生成要点** 把当天的论文一次性交给大模型，产出三样东西：**今日要点**、
   **各主题的当日综述**、**每篇论文的一句话要点**。提示词要求：只用给定材料、
   术语用领域标准译名、数值与成立条件不得改动、语法从简。
5. **排版** 用 WeasyPrint 渲染成 PDF：公式由 MathJax 预渲染成 SVG（Computer Modern 字形）；
   论文清单里的作者、标题、arXiv 号与链接由代码从元数据生成，模型不参与。
6. **存档** 当天的要点与主题综述存进数据库，供月报使用。

### 月报

每月 1 号执行，覆盖上一个自然月：

1. **只读当月日报的存档**（今日要点 + 各主题综述 + 各主题篇数），不再回头翻原始论文，
   也不再联网抓全文。
2. **两步浓缩**：先按主题汇总（内容多时自动分批再合并），再生成一段总览与主要进展。
3. **附 arXiv 号**：重要文献在日期后附号，如（10月13日，arXiv:2610.17034）。
   号只能从"可引用清单"（当天论文的号 + 标题）里挑；生成后还会逐条核对是否确属该日，
   编造或张冠李戴的一律剔除，只保留日期。

因此**先有日报才会有月报**；某个月没有日报时，月报会提示并跳过。

---

## 安装

支持 **Linux**（Debian 系 / Fedora 系 / Arch 系）。三选一：

### 方式一：发行版安装包（推荐）

从 [Releases](../../releases) 下载对应格式：

```bash
# Debian / Ubuntu
sudo apt install ./arxiv-daily-report_0.1.2-1_amd64.deb

# Fedora / RHEL
sudo dnf install ./arxiv-daily-report-0.1.2-1.x86_64.rpm

# Arch / Manjaro
sudo pacman -U ./arxiv-daily-report-0.1.2-1-x86_64.pkg.tar.zst
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
git clone git@github.com:phys1992/arxiv-daily-report.git
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

### 依赖

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

# 3) 字体（推荐装这两套，缺了会自动回退）
#    西文/公式：Computer Modern（arXiv 预印本字体）
sudo apt install fonts-lmodern        # Debian/Ubuntu；Arch: sudo pacman -S otf-latin-modern
#    中文：思源宋体
sudo apt install fonts-noto-cjk       # Debian/Ubuntu；Fedora: sudo dnf install google-noto-serif-cjk-fonts
```

### 字体：推荐 Computer Modern + 思源宋体

报告默认按 **arXiv 预印本**的样子排版：

| 用途 | 字体 | 说明 |
|---|---|---|
| **西文 + 数学** | **Computer Modern**（Latin Modern / CMU） | LaTeX 默认字体，也就是 arXiv 上绝大多数 PDF 的正文与公式 |
| **中文** | **思源宋体**（Source Han Serif SC / Noto Serif CJK SC） | 宋体是中文科技论文的正文惯例 |

**强烈建议装上这两套**，否则会退到备选字体，观感会变。程序启动时会自动检测
（`arxiv-daily-report status` 也能看到实际用的是哪套）：

| 情况 | 行为 |
|---|---|
| 系统注册了 Latin Modern / CMU | 直接用 |
| **只装了 TeX Live** | **自动使用 TeX Live 里的 Latin Modern 文件**，不必再装字体包 |
| 两者都没有 | 西文退到 Times 系（Nimbus Roman → Liberation Serif → …），日志给出安装命令 |
| 没有思源宋体 | 中文退到 `Noto Serif CJK SC` → `SimSun` → 系统默认，日志给出安装命令 |
| 完全没有中文字体 | 汉字会显示为方块，日志明确告警 |

安装命令：

```bash
# Computer Modern
sudo apt install fonts-lmodern        # Debian / Ubuntu
sudo pacman -S otf-latin-modern       # Arch / Manjaro
sudo dnf install texlive-collection-fontsrecommended   # Fedora（含 Latin Modern）

# 思源宋体
sudo apt install fonts-noto-cjk        # Debian / Ubuntu
sudo pacman -S noto-fonts-cjk          # Arch
sudo dnf install google-noto-serif-cjk-fonts          # Fedora
```

### 换字体

三种预设，在 Web 界面「报告内容 → 字体方案」里选，或改配置：

| `report.font_preset` | 西文 | 中文 | 适用 |
|---|---|---|---|
| `arxiv`（默认） | Computer Modern | 思源宋体 | 对齐 arXiv 预印本 |
| `journal` | Times 系（Nimbus Roman） | 思源宋体 | 对齐 PRD 等**正式发表版** |
| `sans` | DejaVu Sans | 思源黑体 | 无衬线，偏文档/幻灯片观感 |

也可以完全自定义：`report.font_family` 填任意 CSS 字体列表（会覆盖预设），
或用 `report.font_file` 内联一个字体文件。

> **数学字体**：公式由 MathJax 预渲染成 SVG，用的是 **Computer Modern 字形轮廓**——
> 与 LaTeX 输出一致，无需额外配置。

---

## 快速开始

### 源码方式

clone 下来就能跑——**不装任何 Python 包也能出 PDF**（会自动选用 Chromium 后端）：

```bash
git clone git@github.com:phys1992/arxiv-daily-report.git
cd arxiv-daily-report

python3 run.py                    # 不带参数：显示用法、界面地址与当前状态
python3 run.py serve              # 启动 Web 配置界面（填模型密钥）
python3 run.py daily              # 抓取并生成昨天的日报 PDF
python3 run.py daily --no-llm     # 只出 PDF、不调模型（不花钱，适合先验证）
```

不带第三方包时 PDF 会**降级**：没有页码、字体嵌入为 Type 3。想要完整排版（页码、书签、
标准字体子集）就装上 WeasyPrint：

```bash
./install.sh                      # 建 .venv + 装 WeasyPrint（自动多镜像回退）
# ./install.sh --with-math        # 可选：同时装 MathJax，公式与 LaTeX 一致
.venv/bin/python run.py daily     # 之后用 venv 里的 python 跑
```

### 安装包方式

```bash
sudo apt install ./arxiv-daily-report_<版本>-1_amd64.deb    # 或 dnf / pacman -U
pip install --user weasyprint        # Debian/Fedora 需自己装（Arch 已随包）

arxiv-daily-report serve             # 打开 Web 配置界面，填模型密钥
arxiv-daily-report daily             # 生成昨天（北京时间）的日报
```

两种方式命令完全对应：源码方式把 `arxiv-daily-report` 换成 `python3 run.py` 即可。

### 模型密钥

任选一种：

```bash
export DEEPSEEK_API_KEY=sk-...       # 环境变量（优先级最高）
```

或在 Web 界面「大模型设置」里填写（存进 `secrets.json`，权限 600）。

### 常驻自启

```bash
systemctl --user enable --now arxiv-daily-report     # 安装包方式，单元已随包安装
# 源码方式：./systemd/install-service.sh
journalctl --user -u arxiv-daily-report -f
```

---

## 怎么打开 Web 配置界面

**直接打开就行，不需要令牌也不需要密码：**

```
http://127.0.0.1:8765
```

界面默认只监听回环地址（`127.0.0.1`），采用「本机即可信」的模型。查当前地址：

```bash
arxiv-daily-report status        # 输出里有「配置界面」一行
arxiv-daily-report serve         # 临时起一个前台实例，会自动打开浏览器
```

常驻服务在后台跑着时，地址同样是这一行：

```bash
journalctl --user -u arxiv-daily-report | grep 在浏览器里打开
```

### 三种访问模式

| 模式 | 触发条件 | 怎么进 |
|---|---|---|
| **免认证**（默认） | 未设密码、`require_token=false` | 直接开 `http://127.0.0.1:8765` |
| **令牌** | `web.require_token = true` | 用 `status` 打印的 `?token=...` 地址 |
| **密码** | 设了 `web.password`（对外监听时强制） | 直接开，输入密码登录 |

### 安全边界

- 对外监听（`web.host` 设为 `0.0.0.0` 等）时**强制要求密码**，否则拒绝启动——同网段任何人都能改你的模型密钥。
- 免认证模式下会校验 `Host` 头，挡掉 **DNS rebinding**（恶意网页把域名解析到 127.0.0.1 再借浏览器打本机服务）。
- 免认证只适用于「本机是可信的」这一前提。多人共用的机器建议设 `web.password` 或打开 `web.require_token`。

常用参数：

```bash
arxiv-daily-report serve --port 8766        # 换端口（8765 被占用时）
arxiv-daily-report serve --no-open          # 不自动打开浏览器
arxiv-daily-report daemon --no-web          # 常驻但不开界面
```

---

## 开机自启：用 systemd 用户服务

**请用用户级服务（`systemctl --user`），不要用系统级服务。** 原因：

- 程序要读写你自己的 `~/.config`、`~/.local/state`、`~/.cache`——以你的身份运行天然没有权限问题；
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

### 源码方式（脚本安装单元）

```bash
./systemd/install-service.sh              # 自动生成并启用单元
./systemd/install-service.sh --uninstall  # 卸载
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
arxiv-daily-report daemon                                           # 常驻定时（同时提供配置界面）
arxiv-daily-report status                                           # 运行状态与目录位置
arxiv-daily-report cleanup  [--dry-run]                             # 清理缓存（中间数据）
```

`--no-llm` 只出 PDF 不调用模型，适合先验证抓取与排版，或节省额度。

---

## 日报与月报的分工

| | 日报 | 月度简报 |
|---|---|---|
| 何时生成 | 每天（默认北京时间 19:00） | **每月 1 号**，覆盖上一个自然月 |
| 数据来源 | arXiv API 的当日新增论文 + 每篇的 arXiv HTML 结论节 | **只读当月日报的要点存档** |
| 结构 | 今日要点 + 分类速览 + 论文清单（含作者/标题/arXiv 号/链接） | 本月概览（一段） + 主要进展 + 分主题要点 |
| 引用方式 | 论文清单里的编号 `[3]` | 标日期；**重要文献**再附 arXiv 号，如（10月13日，arXiv:2610.17034），可点击 |
| 参考文献表 | 无（清单已含全部信息） | 无 |
| 篇幅 | 约 5–15 页（随当日论文数） | 不设字数上限；准确优先，简明其次 |

术语与表述统一要求（日报与月报都适用）：**术语用领域标准译名、表述与原文一致、逻辑完整、语法从简**。
月报额外要求：**力求准确是第一位的**——宁可写长一点把事情说清楚，也不为了短而牺牲准确性。

---

## 配置与目录

配置文件名是 `config.json`，位置随安装方式而变：

| | 源码检出 / `ADR_HOME` 便携模式 | 安装包方式（XDG） |
|---|---|---|
| **最终报告** | `<项目根>/reports/` | **`~/Documents/arxiv-daily-report/`** |
| 配置与密钥 | `<项目根>/config.json`、`secrets.json` | `~/.config/arxiv-daily-report/` |
| 状态（数据库） | `<项目根>/data/` | `~/.local/state/arxiv-daily-report/` |
| 缓存（可随时清） | `<项目根>/cache/` | `~/.cache/arxiv-daily-report/` |
| 随程序分发的资产 | `<项目根>/data/`、`assets/` | `/usr/share/arxiv-daily-report/` |

**安装包方式下你只需要记住一件事：报告在 `~/Documents/arxiv-daily-report/`。**
其余都是程序自己管理的标准位置，不用管。想换地方就设 `paths.reports`
（Web 界面「存储与路径 → 报告目录」），中间数据的位置不需要你选。

它遵守 XDG 规范：配置进 `XDG_CONFIG_HOME`、状态进 `XDG_STATE_HOME`、
缓存进 `XDG_CACHE_HOME`，最终产物进用户的「文档」目录
（用 `xdg-user-dir DOCUMENTS` 解析，中文系统会落到 `~/文档`）。

### 报告的目录结构

```
reports/
├── gr-qc/
│   ├── 2026-10/                  # 日报按月份分一层：长期运行也不会堆成几百个文件
│   │   ├── 2026-10-07.pdf
│   │   └── 2026-10-13.pdf
│   └── latest.pdf                # 最新一期，路径固定不变，便于反复打开
├── combined/                     # output_mode 为 combined/both 时的合并日报
│   └── 2026-10/2026-10-13.pdf
├── monthly/
│   ├── gr-qc/2026-10.pdf         # 月报按分类分目录
│   └── arXiv/2026-10.pdf         # 订阅多个分类时，合并简报放在 arXiv/
└── index.md                      # 全部报告的索引（Markdown 表格，链接可用）
```

层级规则：**日报 `<分类>/<年-月>/`，月报 `monthly/<分类>/`**。
分类的 `out_dir` 可覆盖日报的根目录；`paths.reports` 决定整个 `reports/` 的位置。

### 缓存清理

中间数据（arXiv 原始响应、公式 SVG、论文全文摘录）会随时间累积——
原始响应约 1–2 MB/天，一年下来几百 MB。程序会**定期自动清理**：

```bash
arxiv-daily-report cleanup --dry-run    # 先看看会清什么
arxiv-daily-report cleanup              # 直接清理
```

默认保留 **3 个月**，在 Web 界面「存储与路径 → 缓存清理周期」可选：

| 选项 | `cache.cleanup` | 保留天数 |
|---|---|---|
| 每周（最短） | `1w` | 7 |
| 每 2 周 | `2w` | 14 |
| 每月 | `1m` | 30 |
| 每 2 个月 | `2m` | 60 |
| **每 3 个月（默认）** | `3m` | 90 |
| 每半年 | `6m` | 180 |
| 每年 | `1y` | 365 |
| 永久保留 | `never` | 不清理 |

清理**只动缓存**，最终报告不会被动。常驻服务每次启动时会自动清一次，
`status` 里能看到当前占用与周期。

### 数据长期保留策略

按每天 30 篇论文估算增长量：

| 内容 | 年增长 | 处理 |
|---|---|---|
| arXiv 原始响应 | ~600 MB | 缓存，按上面的周期自动清理 |
| 论文全文摘录 | ~60 MB | 缓存，同上 |
| 论文要点 + 元数据 | ~11 MB | 长期保留 |
| 日报摘要存档 | ~2 MB | 长期保留 |

长期保留的三样东西各有用途：

- **论文要点与元数据** —— 重跑某天的日报时直接复用，不再调用模型。
- **日报摘要存档** —— 重新生成月报（换语言、换字体、提示词升级）时要用。
- **摘要原文** —— 只用于「生成要点」那一步，渲染 PDF 并不需要，而且随时能从 arXiv 重新取回。
  因此当某一天的论文**都已经有当前版本的要点**、且当天**存有日报摘要**时，
  程序会释放这些摘要原文（通常可省下数据库约七成空间）。
  条件不满足的一律保留，保证任何时候重跑都不会因为缺输入而产出劣质要点。

用 `arxiv-daily-report status` 可以看到本机实际使用的路径与占用。

### 关键配置项

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
  "cache": {
    "cleanup": "3m"                     // 1w | 2w | 1m | 2m | 3m | 6m | 1y | never
  },
  "fulltext": {
    "enabled": true,                    // 日报摘要是否结合全文的结论/讨论部分
    "source": "auto",                   // auto | arxiv-html | ar5iv
    "max_chars": 4000,                  // 每篇结论原文送入模型的上限（字符）
    "throttle_seconds": 1.0,            // 抓取间隔，别给 arXiv 添压力
    "timeout_seconds": 30
  },
  "report": {
    "font_preset": "arxiv",             // arxiv（CM + 思源宋体）| journal（Times 系）| sans
    "font_family": "",                  // 非空则完全覆盖预设（任意 CSS 字体列表）
    "font_file": "",                    // 可选：内联一个字体文件
    "math_backend": "auto"              // auto | mathjax | unicode
  },
  "paths": {
    "reports": "",                      // 空 = 安装版用「文档」目录，源码版用项目内 reports/
    "data": "",
    "cache": "",
    "publish_dir": ""                   // 例如同步盘目录；空则不发布
  }
}
```

**环境变量覆盖**（优先级高于配置文件，适合容器与脚本）：

| 变量 | 作用 |
|---|---|
| `ADR_HOME` | 便携模式总目录：配置/状态/缓存/报告全部收进这里 |
| `ADR_CONFIG_HOME` / `ADR_DATA_HOME` / `ADR_CACHE_HOME` | 分别指定三个目录 |
| `ADR_PREFIX` | 系统安装前缀，默认 `/usr`（用于 `/usr/local`、Nix 等布局） |
| `ADR_ASSETS` | 静态资产目录（MathJax 等） |
| `ADR_WEB_PASSWORD` / `ADR_WEB_HOST` / `ADR_WEB_PORT` | Web 服务 |
| `ADR_PROVIDER` / `ADR_MODEL` / `ADR_BASE_URL` | 模型 |
| `ADR_REPORT_LANGUAGE` / `ADR_UI_LANGUAGE` | 语言 |

**密钥存放**（优先级从高到低）：

1. 环境变量：`DEEPSEEK_API_KEY` / `MOONSHOT_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `CUSTOM_API_KEY`
2. `secrets.json`（权限 600，已在 `.gitignore` 中）

Web 界面里只会回显掩码（`sk-2431••••••••abcd`），永不返回明文。

---

## 自定义模型

| 厂商 | Wire 协议 | 备注 |
|---|---|---|
| DeepSeek / Kimi / OpenAI / 本地服务 | OpenAI Chat Completions | 填 Base URL 与模型名即可 |
| Claude (Anthropic) | Anthropic Messages | 协议不同，已内置适配器 |

本地服务（Ollama / vLLM / LM Studio）在 Web 界面选「自定义（OpenAI 兼容）」，
Base URL 填例如 `http://127.0.0.1:11434/v1`，密钥可留空。

---

## 数据来源与合规

- 论文元数据来自 **arXiv API**。元数据以 **CC0 1.0** 发布，可自由存储、转换与分享。
- 遵守 arXiv 使用条款：**请求间隔可配置（默认 3 秒，即官方要求的「每 3 秒最多 1 次」）**、携带可识别 User-Agent；
  本项目**不下载 PDF 全文**，读全文时抓的是 arXiv 自己生成的 HTML 页面，
  且每篇只抓一次、结果缓存在本地，重复生成报告不会重复请求。
  报告中一律链接到 arXiv 摘要页。
- **建议在配置里填写 `fetch.contact`**（你的邮箱或仓库地址），它会出现在 User-Agent 中，
  便于 arXiv 在流量异常时联系你：`arxiv-daily-report/0.1.2 (arXiv API client; you@example.com)`。
- 本项目与 arXiv.org **无任何官方关联**。

### 遇到 `HTTP 429 Too Many Requests`

首次运行要回溯 7 天，请求较集中，**arXiv 限流是正常现象，不是程序故障**。
程序会按 30 秒 / 1 分钟 / 2 分钟 / 4 分钟 / 5 分钟自动退避重试（合计约 12 分钟），
并优先遵守服务端返回的 `Retry-After`。常驻服务在失败后也会在 5 / 15 / 30 分钟后自动重试，
不会干等到第二天。

仍然失败时：

```bash
arxiv-daily-report daily          # 过几分钟再跑一次即可
```

反复出现就把请求放慢一些：

```jsonc
{
  "fetch": {
    "request_interval_sec": 5,      // 默认 3；放慢到 5 秒
    "contact": "you@example.com"    // 建议填写，便于 arXiv 在流量异常时联系你
  },
  "schedule": { "catch_up_days": 3 } // 首次运行可把回溯天数调小（默认 7）
}
```
- 详见 [arXiv API Terms of Use](https://info.arxiv.org/help/api/tou.html)。

---

## 目录结构

```
arxiv-daily-report/
├── run.py                  CLI 入口（源码方式）
├── install.sh              源码安装脚本
├── arxiv_daily_report/     核心包
│   ├── config.py           配置与目录解析（XDG / 便携）
│   ├── arxiv_client.py     抓取（限速/重试/分页）
│   ├── collect.py          采集编排与分类归属
│   ├── fulltext.py         抓 arXiv HTML、提取结论/讨论小节
│   ├── store.py            sqlite 状态（去重/存档/公式缓存/用量）
│   ├── summarize.py        日报提示词与校验
│   ├── report_daily.py     日报编排
│   ├── report_monthly.py   月报编排
│   ├── render.py           HTML 版式与字体
│   ├── pdf.py              PDF 后端（WeasyPrint → Chromium 降级）
│   ├── mathrender.py       公式预渲染（MathJax → SVG）
│   ├── cleanup.py          缓存清理与数据保留
│   ├── scheduler.py        常驻调度
│   ├── server.py           Web 后端（标准库 http.server）
│   └── llm/                多厂商模型适配
├── web/                    纯静态配置界面
├── data/                   arXiv 分类表、主题表
├── systemd/                用户级服务单元与安装脚本
├── packaging/              安装包构建脚本
└── reports/                产出的 PDF（源码方式；已 gitignore）
```

---

## English

**arxiv-daily-report** fetches newly announced arXiv papers (first-release `v1` only) for any
combination of arXiv's 155 categories, summarises them with an LLM, and renders a typeset
**PDF daily digest** plus a **monthly thematic review**.

How it works:

1. **Fetch** the subscribed categories for the target day (Beijing calendar day, default:
   yesterday) with a configurable catch-up window, so delayed weekend/holiday announcements
   are not lost.
2. **De-duplicate** globally by arXiv ID — a paper is summarised once and appears in exactly
   one report.
3. **Read the full text**: fetch arXiv's own HTML and extract the Conclusion/Discussion section,
   giving the model both the abstract (problem and method) and the conclusion (final results,
   validity conditions, numbers). Falls back to abstract-only when no HTML exists.
4. **Summarise** the day in one model call, producing daily highlights, per-theme notes and a
   one-line note per paper.
5. **Render** to PDF with WeasyPrint; formulas are pre-rendered by MathJax to SVG using
   Computer Modern outlines, and the paper list is generated from metadata by code.
6. **Archive** the day's notes so the monthly brief can be built from them.

The **monthly brief** runs on the 1st, covers the previous calendar month, reads only the stored
daily notes, and attaches arXiv IDs to important work right after its digest date — every ID is
verified against that day's paper list.

Install (Linux only):

```bash
sudo apt install ./arxiv-daily-report_0.1.2-1_amd64.deb   # or dnf / pacman -U
pip install --user weasyprint          # PDF backend (packaged on Arch as python-weasyprint)
arxiv-daily-report serve               # configure the LLM provider in the web UI
systemctl --user enable --now arxiv-daily-report   # auto-start, no root required
```

Final reports land in `~/Documents/arxiv-daily-report/`; configuration, state and cache follow
the XDG base directory spec. Metadata comes from the arXiv API (CC0) under its
[Terms of Use](https://info.arxiv.org/help/api/tou.html); this project is not affiliated with arXiv.org.

---

## License

代码以 **MIT** 授权。本项目**不分发字体文件**，PDF 使用系统字体；详见 [LICENSE](LICENSE)。
