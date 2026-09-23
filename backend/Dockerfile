# ============================================================
# OpsPilot 后端镜像
#
# 构建：docker build -t opspilot-backend ./backend
# ============================================================

# ★ 基础镜像做成可覆盖的参数。
#
# 为什么？国内直接拉 docker.io 经常不通（被墙或代理问题）。
# 有了这个参数，不改 Docker 配置也能走镜像源构建：
#
#   docker build \
#     --build-arg BASE_IMAGE=dockerproxy.net/library/python:3.11-slim \
#     -t opspilot-backend ./backend
#
# 不传就默认用官方地址，在能正常拉镜像的环境里行为不变。
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

# ── 环境变量 ───────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    # 不写 .pyc 文件：容器里不需要，还能让镜像小一点
    PYTHONDONTWRITEBYTECODE=1 \
    # pip 不缓存下载的包：构建完就删，不留在镜像层里
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# pip 源。
#
# ⚠️ 这里默认用【官方源】而不是国内镜像，原因有点反直觉：
#
#   在 Docker 容器里实测发现，官方源能通，国内镜像源反而报
#   SSLError(SSLEOFError)。原因是宿主机上的代理软件（Clash 之类）
#   按规则分流：官方域名走代理（通），国内域名走直连
#   （而容器的直连出口是断的）。
#
#   所以"国内用镜像更快"这条经验在【容器构建】场景下不一定成立 ——
#   要按实际网络测出来，不能照搬。
#
# 如果你的环境里镜像源确实更快，构建时覆盖即可：
#   --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX=https://pypi.org/simple

# ★ 代理开关，默认关闭。
#
# 为什么必须显式处理这件事？
#
# Docker Desktop 会把【系统代理】自动注入到构建环境里
# （HTTP_PROXY=http://http.docker.internal:3128）。
# 如果机器上开着 Clash 这类工具而后又关掉了，或者代理只监听
# 127.0.0.1（Docker 虚拟机访问不到），容器里的 pip 就会带着一个
# 连不通的代理去请求 —— 报错是看不懂的：
#     SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]'))
#
# 而国内镜像源本来就是直连的，压根不需要代理。
# 所以默认 unset 掉，链路反而通。
#
# 如果你的网络【必须】通过代理才能访问外网，构建时传：
#   --build-arg PIP_USE_PROXY=http://your-proxy:8080
ARG PIP_USE_PROXY=""

WORKDIR /app

# ── 依赖 ───────────────────────────────────────────────────
#
# ★ 先单独 COPY requirements.txt 再装依赖，最后才 COPY 源码。
#
# 这不是为了好看，是 Docker 分层缓存的关键：
#   只要 requirements.txt 没变，这一层就直接用缓存，
#   改业务代码重新构建时【不会重装依赖】—— 从几分钟变成几秒。
#
# 反过来写（先 COPY . 再装依赖）的话，改一行代码就会触发完整重装。
COPY requirements.txt .

# 代理处理 + 安装。写成一条 RUN，因为每个 RUN 是独立的 shell，
# export 的效果不会跨 RUN 保留。
RUN if [ -n "$PIP_USE_PROXY" ]; then \
        export HTTP_PROXY="$PIP_USE_PROXY" HTTPS_PROXY="$PIP_USE_PROXY"; \
        echo "使用代理: $PIP_USE_PROXY"; \
    else \
        unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; \
        echo "已清除构建环境注入的代理，直连镜像源"; \
    fi \
    && pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt

# ── 源码 ───────────────────────────────────────────────────
COPY . .

# ── 非 root 用户 ───────────────────────────────────────────
#
# 容器默认以 root 运行。万一应用被攻破，攻击者直接拿到 root。
# 建一个普通用户来跑，是容器安全最基本的实践之一。
RUN useradd --create-home --uid 1000 opspilot \
    && chown -R opspilot:opspilot /app
USER opspilot

EXPOSE 8000

# ── 健康检查 ───────────────────────────────────────────────
#
# 用 python 而不是 curl：slim 镜像里没有 curl，
# 为了健康检查去装一个纯属浪费。
# urllib 是标准库，一定有。
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status==200 else 1)"

# ── 启动 ───────────────────────────────────────────────────
#
# ★ 启动前先跑数据库迁移。
#
# 为什么放在 CMD 里而不是构建时？
#   构建镜像时数据库还没起，迁移必然失败。
#   放在启动时，能配合 docker-compose 的 depends_on 等数据库就绪。
#
# 注意：多副本部署时多个容器会同时跑迁移，可能冲突。
# 正确做法是单独一个 init 容器跑迁移，或者用分布式锁。
# 本项目单副本，先用最简单的写法。
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
