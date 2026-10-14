# grqc-daily 容器镜像
#
# 构建：
#   docker build -t grqc-daily .
#
# 生成日报（一次性）：
#   docker run --rm -v "$PWD/reports:/app/reports" -v "$PWD/data:/app/data" \
#     -e DEEPSEEK_API_KEY=sk-... grqc-daily daily
#
# 常驻 + Web 配置界面（对外暴露端口时必须设置密码）：
#   docker run -d --name grqc -p 8765:8765 \
#     -v "$PWD/reports:/app/reports" -v "$PWD/data:/app/data" \
#     -e DEEPSEEK_API_KEY=sk-... -e GRQC_WEB_PASSWORD=换成你的密码 \
#     grqc-daily serve --host 0.0.0.0 --port 8765 --no-open
#
FROM python:3.12-slim

# WeasyPrint 运行所需的系统库 + 拉丁兜底字体
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        shared-mime-info \
        fonts-dejavu-core \
        nodejs \
        npm \
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

ENV GRQC_HOME=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8765
VOLUME ["/app/reports", "/app/data"]

ENTRYPOINT ["python", "run.py"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765", "--no-open"]
