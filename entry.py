"""PyInstaller 打包入口：以包方式加载 service.__main__，保持其包内相对导入有效。

背景：PyInstaller 直接以 `service/__main__.py` 作为入口时，该模块会被当成顶层 `__main__`
执行，其中 `from .app import ...` 这类"包内相对导入"会报
`attempted relative import with no known parent package`。
解法：本文件位于包外（项目根），先 `import service`（确立包上下文），再取 `service.__main__.main`，
这样 `service.__main__` 在真正的包命名空间里执行，相对导入全部有效。

仅用于 exe 构建（见 qq-desktop-package skill 的 build_exe.py）；项目不提供 wheel 或
console script，源码调试可直接执行 `python -m service`。
"""

# sys：读取仅供构建校验使用的轻量启动参数。
import sys


def _verify_runtime_resources() -> None:
    """验证 PyInstaller onefile 解压后的本地 DLL 均可被真实加载。

    此入口仅供 ``build_exe.py`` 调用：不启动 HTTP 服务、不连接 QQ、不移动鼠标。
    但会 import 鼠标层并实例化 WGC 捕获器，故能尽早发现 ``libs/input_event.dll`` 或
    ``libs/wgc_capture.dll`` 漏进 PyInstaller ``--add-data`` 清单的问题。
    """
    from utils.core import mouse
    from utils.core.screenshot import WGCCapture

    # mouse 模块导入时已用 ctypes.WinDLL 加载 InputEvent；这里实例化 WGC，确保第二个
    # DLL 也能从 onefile 的 _MEIPASS/libs 路径加载。
    WGCCapture()
    print(f"RUNTIME_RESOURCES_READY input_event={mouse._dll_path.name} wgc_capture=wgc_capture.dll")


# 从 service 包导入 main 函数：
# import service.__main__ 会先加载 service/__init__.py 再加载 service/__main__.py，
# 从而让 __main__.py 里 `from .app import ...` 的相对导入有包上下文可用
from service.__main__ import main

# 标准入口保护：仅当本文件被直接执行（python entry.py / PyInstaller 入口）时才启动服务；
# 被其他模块 import 时不会触发副作用
if __name__ == "__main__":
    if "--verify-runtime-resources" in sys.argv:
        _verify_runtime_resources()
    else:
        # 调用真正的服务入口：解析参数 → 绑定随机端口 → 发布 endpoint → 启动 uvicorn
        main()
