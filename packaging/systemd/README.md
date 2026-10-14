# systemd 用户服务

定时逻辑在 `run.py daemon` 进程内部，systemd 只负责把进程拉起来并保活。
因此**用 Web 界面改运行时间可以立即生效，不需要 reload 服务**。

## 安装

```bash
packaging/systemd/install-service.sh          # 安装并启动
packaging/systemd/install-service.sh --uninstall
```

它会：

1. 把 `grqc-daily.service.tmpl` 中的 `@PROJECT_DIR@` / `@VENV_PY@` 替换成本机真实路径；
2. 写入 `~/.config/systemd/user/grqc-daily.service`；
3. 执行 `systemctl --user daemon-reload && enable --now`。

若当前 shell 连不上用户级 systemd（例如在容器或非图形会话里），脚本会提示你
在桌面终端里手动执行那两条命令。

## 常用操作

```bash
systemctl --user status  grqc-daily      # 状态
systemctl --user restart grqc-daily      # 重启（例如改了 config.json 后强制重载）
journalctl --user -u grqc-daily -f       # 实时日志
systemctl --user disable --now grqc-daily # 停用
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
