# systemd 用户服务

定时逻辑在 `arxiv-daily-report daemon` 进程内部，systemd 只负责把进程拉起来并保活。
因此**用 Web 界面改运行时间可以立即生效，不需要 reload 服务**。

## 为什么用用户级服务（`systemctl --user`）

**不要用系统级服务。** 程序要读写你自己的 `~/.config`、`~/.local/share`、`~/.cache`：

- 以你的身份运行天然没有权限问题；
- 系统级服务默认以 root 运行，会把报告与数据库写成 root 所有，之后你自己反而改不动；
- 模型密钥是你个人的，放在用户目录里更合适。

## 两种安装方式

**发行版安装包**：单元已随包放在 `/usr/lib/systemd/user/arxiv-daily-report.service`，直接用即可：

```bash
systemctl --user enable --now arxiv-daily-report
```

**源码检出**：用下面的脚本生成本机路径的单元：

```bash
packaging/systemd/install-service.sh          # 安装并启动
packaging/systemd/install-service.sh --uninstall
```

它会：

1. 把 `arxiv-daily-report.service.tmpl` 中的 `@PROJECT_DIR@` / `@VENV_PY@` 替换成本机真实路径；
2. 写入 `~/.config/systemd/user/arxiv-daily-report.service`；
3. 执行 `systemctl --user daemon-reload && enable --now`。

若当前 shell 连不上用户级 systemd（例如在容器或非图形会话里），脚本会提示你
在桌面终端里手动执行那两条命令。

## 常用操作

```bash
systemctl --user status  arxiv-daily-report      # 状态
systemctl --user restart arxiv-daily-report      # 重启（例如改了 config.json 后强制重载）
journalctl --user -u arxiv-daily-report -f       # 实时日志
systemctl --user disable --now arxiv-daily-report # 停用
```

## 密钥

推荐用 `EnvironmentFile`：在项目根建一个 `.env`（已在 `.gitignore` 中排除）：

```
DEEPSEEK_API_KEY=sk-...
```

模板里的 `EnvironmentFile=-@PROJECT_DIR@/.env` 前缀 `-` 表示文件不存在也不报错。
也可以直接把密钥写进 Web 界面（存到 `secrets.json`，权限 600）。

## 开机自启

用户级服务默认在登录后启动。若希望**未登录也运行**（服务器场景），执行一次：

```bash
sudo loginctl enable-linger "$USER"
```
