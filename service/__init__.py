"""QQ Desktop Service 的公开接口层。

对外（调用方/测试）只应通过本包导出入口使用：
- create_app：构造服务应用（测试注入替身、生产用真实门面）；
- QqAutomationPort：自动化实现抽象（测试可注入 fake）。
"""

# 导出应用工厂：构造受 token 保护的 FastAPI 应用（路由在 service/app.py 中注册）
from .app import create_app
# 导出自动化端口抽象：QQ 操作的接口定义（真实实现是 facade 中的门面）
from .facade import QqAutomationPort

# 声明包级公开 API：外部 `from service import create_app, QqAutomationPort` 只看到这两个名字
__all__ = ["QqAutomationPort", "create_app"]
