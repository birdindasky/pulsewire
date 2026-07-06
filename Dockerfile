# pulsewire app 容器:Python 3.12 + chromium(无头出图)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# uv(由官方镜像拷贝二进制)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 先装依赖(利用层缓存):需要 README + pyproject + src 才能 editable 安装
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system -e .

# 安装 chromium 及其系统依赖(出图用)。对 Debian 镜像源瞬时故障做重试。
RUN n=0; until [ "$n" -ge 3 ]; do \
      python -m playwright install --with-deps chromium && exit 0; \
      n=$((n+1)); echo "playwright install 失败,重试 $n/3"; apt-get update || true; sleep 8; \
    done; exit 1

# 其余项目文件(config.yaml / sources.yaml / web / prompts ...)
COPY . .

# 阶段 0:起来后做配置校验 + 连库自检即退出;后续阶段改为常驻 run
CMD ["pulsewire", "healthcheck"]
