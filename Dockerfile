FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# 安装系统依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# ── 第 1 层：单独安装 CPU-only torch（体积 ~250MB vs CUDA 版 ~2GB）──────────
# 独立一层的好处：只要这行不变，无论 requirements.txt 怎么改都不会重新下载 torch
# --mount=type=cache 将 pip wheel 缓存持久化到 BuildKit 存储，
#   即使层缓存失效（如基础镜像更新），wheel 文件也不用重新从网络下载
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch --index-url https://download.pytorch.org/whl/cpu

# ── 第 2 层：安装其余依赖 ──────────────────────────────────────────────────
# requirements.txt 不变时此层直接命中缓存；变化时借助 pip wheel 缓存也很快
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# ── 第 3 层：复制项目代码 ──────────────────────────────────────────────────
# 代码频繁变动，但放在最后一层，不会触发 pip 重装
COPY . .

# 暴露端口
EXPOSE 18000

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18000"]
