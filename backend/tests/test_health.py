"""健康检查与错误格式测试。

运行::

    cd backend
    pytest -v
    pytest -v -m "not db"     # 只跑不依赖数据库的
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings

PREFIX = settings.API_V1_PREFIX


class TestRoot:
    def test_root_returns_service_info(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["app"] == settings.APP_NAME
        assert body["health"] == f"{PREFIX}/health"


class TestHealth:
    """存活检查：不碰数据库，任何环境都必须通过。"""

    @pytest.mark.no_db
    def test_health_returns_ok(self, client: TestClient) -> None:
        resp = client.get(f"{PREFIX}/health")
        assert resp.status_code == 200

        body = resp.json()
        assert body["status"] == "ok"
        assert body["app"] == settings.APP_NAME
        assert body["version"] == settings.APP_VERSION
        assert body["env"] == settings.APP_ENV
        # 时间字段必须是合法 ISO 8601，前端直接 new Date() 能解析
        assert "T" in body["time"]

    @pytest.mark.no_db
    def test_health_needs_no_database(self, client: TestClient) -> None:
        """数据库挂了也不该影响存活检查 —— 这正是拆两个端点的意义。

        这里无法真的把数据库停掉，退而求其次：断言响应里
        不包含任何数据库相关信息，说明它确实没依赖数据库。
        """
        body = client.get(f"{PREFIX}/health").json()
        assert "database" not in body
        assert "pool" not in body


class TestHealthDb:
    """数据库检查：需要真库，连不上自动 skip。"""

    def test_health_db_returns_version_and_latency(
        self, client: TestClient, require_db: None
    ) -> None:
        resp = client.get(f"{PREFIX}/health/db")
        assert resp.status_code == 200

        body = resp.json()
        assert body["status"] == "ok"
        assert body["database"] == "mysql"
        assert body["server_version"], "应返回数据库版本号"
        assert body["latency_ms"] >= 0

    def test_health_db_reports_pool_status(
        self, client: TestClient, require_db: None
    ) -> None:
        pool = client.get(f"{PREFIX}/health/db").json()["pool"]
        assert pool is not None, "MySQL 下应返回连接池状态"
        assert set(pool) == {"size", "checked_in", "checked_out", "overflow"}
        # 池容量应与 .env 里的 DB_POOL_SIZE 一致
        assert pool["size"] == settings.DB_POOL_SIZE


class TestErrorFormat:
    """所有错误响应必须是统一的 {code, message, detail} 结构。"""

    @pytest.mark.no_db
    def test_unknown_route_uses_unified_format(self, client: TestClient) -> None:
        resp = client.get(f"{PREFIX}/this-route-does-not-exist")
        assert resp.status_code == 404

        body = resp.json()
        # 关键断言：不是 FastAPI 默认的 {"detail": "Not Found"}
        assert set(body) == {"code", "message", "detail"}
        assert body["code"] == "ROUTE_NOT_FOUND"

    @pytest.mark.no_db
    def test_method_not_allowed_uses_unified_format(self, client: TestClient) -> None:
        resp = client.post(f"{PREFIX}/health")
        assert resp.status_code == 405

        body = resp.json()
        assert set(body) == {"code", "message", "detail"}
        assert body["code"] == "METHOD_NOT_ALLOWED"

    @pytest.mark.no_db
    def test_docs_available(self, client: TestClient) -> None:
        """接口文档可访问，且 OpenAPI 里能看到健康检查两个端点。"""
        assert client.get("/docs").status_code == 200

        schema = client.get(f"{PREFIX}/openapi.json").json()
        paths = schema["paths"]
        assert f"{PREFIX}/health" in paths
        assert f"{PREFIX}/health/db" in paths
