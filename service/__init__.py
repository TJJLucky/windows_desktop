"""QQ Desktop Service 的公开接口层。"""

from .app import create_app
from .facade import QqAutomationPort

__all__ = ["QqAutomationPort", "create_app"]
