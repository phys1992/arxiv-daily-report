# arxiv-daily-report 容器镜像
#
# 构建：
#   docker build -t arxiv-daily-report .
#
# 生成日报（一次性）：
#   docker run --rm -v "$PWD/reports:/app/reports" -v "$PWD/data:/app/data" \
#     -e DEEPSEEK_API_KEY=sk-... arxiv-daily-report daily
#
# 常驻 + Web 配置界面（对外暴露端口时必须设置密码）：
#   docker run -d --name arxiv-daily -p 8765:8765 \
#     -v "$PWD/reports:/app/reports" -v "$PWD/data:/app/data" \
#     -e DEEPSEEK_API_KEY=sk-... -e ADR_WEB_PASSWORD=换成你的密码 \
#     arxiv-daily-report serve --host 0.0.0.0 --port 8765 --no-open
#
FROM python:3.12-slim

# WeasyPrint 运行所需的系统库，以及中英文字体。
# 本项目不分发字体，PDF 使用系统字体，所以镜像里必须装：
#   fonts-noto-cjk        中文（思源黑体的发行版打包，优先被字体栈选中）
#   fonts-dejavu-core     西文
# 缺了中文字体，报告里的汉字会渲染成方块。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        shared-mime-info \
        fontconfig \
        fonts-noto-cjk \
        fonts-dejavu-core \
        nodejs \
        npm \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 可选：把 MathJax 一起装进镜像，公式排版与 LaTeX 一致
RUN cd assets/mathjax && npm install --no-audit --no-fund --loglevel=error || true

# 容器内以非 root 运行
RUN useradd -m -u 10001 app && mkdir -p reports data && chown -R app:app /app
USER app

ENV ADR_HOME=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8765
VOLUME ["/app/reports", "/app/data"]

ENTRYPOINT ["python", "run.py"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765", "--no-open"]
