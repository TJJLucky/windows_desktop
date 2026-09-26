"""FastAPI 自带 Swagger /docs 可用（默认调试页）的回归测试。

验证两个对外能力：
1. `/docs`（Swagger UI）可访问 —— 最终用户/调试者的浏览器调试入口；
2. `/openapi.json` 机器可读合同包含全部 v1 接口 —— 调用方可据此生成客户端。
"""

# Path：临时目录路径类型标注（pytest fixture 类型）
from pathlib import Path

# pytest：测试框架
import pytest
# TestClient：不真正起服务器的 FastAPI 测试客户端（httpx 驱动）
from fastapi.testclient import TestClient

# create_app：被测应用的工厂函数
from service.app import create_app


@pytest.fixture
def client(tmp_path: Path):
    # 每个测试都用独立临时目录放账本库，避免测试间相互污染
    app = create_app(ledger_path=tmp_path / "ledger.sqlite3")
    # 返回一个不发真实网络请求的测试客户端（直接调用 ASGI 应用）
    return TestClient(app)


def test_docs_available(client):
    # 场景：GET /docs 应返回 Swagger UI 页面
    resp = client.get("/docs")
    # 断言：200 且页面包含 swagger 字样（说明返回的是 Swagger 资源页而非 404）
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower()


def test_openapi_lists_v1_apis(client):
    # 场景：GET /openapi.json 应返回完整机器可读合同
    resp = client.get("/openapi.json")
    # 断言：200 可访问
    assert resp.status_code == 200
    # 取出 OpenAPI 契约中的路径表
    paths = resp.json()["paths"]
    # 断言：六个 v1 业务接口全部登记在案（任何一个被误删/改名都会在此暴露）
    for path in ("/v1/health", "/v1/capture:check", "/v1/contacts:query", "/v1/commands/read", "/v1/commands/send", "/v1/chat/history"):
        assert path in paths


def test_openapi_uses_chinese_descriptions(client):
    body = client.get("/openapi.json").json()
    assert body["info"]["title"] == "QQ 桌面自动化服务"
    assert body["paths"]["/v1/commands/send"]["post"]["summary"] == "发送消息"
    send_schema = body["components"]["schemas"]["SendMessageRequest"]
    assert send_schema["properties"]["commandId"]["title"] == "命令 ID"
    assert "幂等键" in send_schema["properties"]["commandId"]["description"]