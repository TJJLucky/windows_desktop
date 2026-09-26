"""PyInstaller 打包入口：以包方式加载 service.__main__，保持其包内相对导入有效。

背景：PyInstaller 直接以 `service/__main__.py` 作为入口时，该模块会被当成顶层 `__main__`
执行，其中 `from .app import ...` 这类"包内相对导入"会报
`attempted relative import with no known parent package`。
解法：本文件位于包外（项目根），先 `import service`（确立包上下文），再取 `service.__main__.main`，
这样 `service.__main__` 在真正的包命名空间里执行，相对导入全部有效。

仅用于 exe 构建（见 qq-desktop-package skill 的 build_exe.py）；项目不提供 wheel 或
console script，源码调试可直接执行 `python -m service`。
"""

# 从 service 包导入 main 函数：
# import service.__main__ 会先加载 service/__init__.py 再加载 service/__main__.py，
# 从而让 __main__.py 里 `from .app import ...` 的相对导入有包上下文可用
from service.__main__ import main

# 标准入口保护：仅当本文件被直接执行（python entry.py / PyInstaller 入口）时才启动服务；
# 被其他模块 import 时不会触发副作用
if __name__ == "__main__":
    # 调用真正的服务入口：解析参数 → 绑定随机端口 → 发布 endpoint → 启动 uvicorn
    main()
