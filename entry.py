"""PyInstaller 打包入口：以包方式加载 service.__main__，保持其包内相对导入有效。

仅用于 exe 构建（见 build_exe 脚本）；源码开发/安装模式仍走 console script
price-agent-qq-service（service.__main__:main），无需此文件。
"""

from service.__main__ import main

if __name__ == "__main__":
    main()
