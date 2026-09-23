# ============================================================
# OpsPilot 前端镜像
#
# ★ 多阶段构建：builder 阶段编译，运行阶段只留静态文件。
#
# 为什么不用单阶段？
#   node_modules 有 200MB+，编译工具链更大。
#   但运行时根本不需要它们 —— 编译产物就是几个 js/css 文件。
#
#   单阶段镜像 ≈ 1GB，多阶段镜像 ≈ 50MB。
#   而且镜像小意味着攻击面小（没有 node、没有 npm、没有源码）。
#
# 构建：docker build -t opspilot-frontend ./frontend
# ============================================================

# ★ 基础镜像可覆盖，用法同 backend/Dockerfile：
#
#   docker build \
#     --build-arg NODE_IMAGE=dockerproxy.net/library/node:20-alpine \
#     --build-arg NGINX_IMAGE=dockerproxy.net/library/nginx:alpine \
#     -t opspilot-frontend ./frontend
#
# ★ 用 Node 22 而不是 20。
#
# 依赖树里的 @vueuse/core@15（Element Plus 间接引入的）声明了
# "node": ">=22"。Node 20 下虽然只是 warning，但加上其他问题
# 一起会让 npm ci 变得不稳定 —— 实测出现过 npm 自身崩溃
# （"Exit handler never called!"），导致 node_modules 装了一半，
# 后面 npm run build 找不到 vue-tsc。
#
# 构建环境用依赖要求的版本，省掉一整类难查的问题。
ARG NODE_IMAGE=node:22-alpine
ARG NGINX_IMAGE=nginx:alpine


# ══════════════════════════════════════════════════════════
# 阶段 1：编译
# ══════════════════════════════════════════════════════════
FROM ${NODE_IMAGE} AS builder

WORKDIR /app

# npm 源。默认官方源，原因同 backend/Dockerfile ——
# 容器里实测官方源通、国内镜像不通（代理分流导致的）。
# 需要换源时构建覆盖：--build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=https://registry.npmjs.org

# 代理开关，默认关闭。原因同 backend/Dockerfile 的说明 ——
# Docker Desktop 会把系统代理注入构建环境，而镜像源本来就是直连的，
# 带着一个连不通的代理反而会失败。
ARG PIP_USE_PROXY=""

# ★ 同样先 COPY 依赖清单再装 —— 利用分层缓存。
# package.json 没变就不会重装依赖。
COPY package*.json ./

# npm ci 而不是 npm install：
#   ci 严格按照 package-lock.json 安装，保证每次构建结果一致
#   install 可能更新 lock 文件，导致"本地能跑、CI 挂了"
RUN if [ -n "$PIP_USE_PROXY" ]; then \
        export HTTP_PROXY="$PIP_USE_PROXY" HTTPS_PROXY="$PIP_USE_PROXY"; \
    else \
        unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; \
    fi \
    && npm config set registry ${NPM_REGISTRY} \
    # 网络不稳时多试几次。npm 默认只重试 2 次、超时 5 分钟，
    # 在跨网络拉 200 多个包时容易失败。
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-maxtimeout 120000 \
    && npm config set fetch-timeout 300000 \
    && npm ci --no-audit --no-fund

# 再 COPY 源码
COPY . .

# 构建（内部会先跑 vue-tsc 类型检查，类型错误会让构建失败）
RUN npm run build


# ══════════════════════════════════════════════════════════
# 阶段 2：运行
# ══════════════════════════════════════════════════════════
FROM ${NGINX_IMAGE}

# 时区（日志时间戳用）
RUN apk add --no-cache tzdata \
    && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && apk del tzdata

# 删掉默认站点配置，换成我们的
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf

# ★ 只从 builder 阶段拷编译产物，其他什么都不带过来
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# 健康检查：用 wget（alpine 自带 busybox 的 wget）
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
