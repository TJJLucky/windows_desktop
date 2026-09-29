"""仅在构建 wheel 时运行：声明该发行包携带 x64 Windows DLL。"""

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name == "wheel":
            # wheel 中含 input_event.dll / wgc_capture.dll，不能伪装成“任意系统可用”的纯 Python 包。
            build_data["pure_python"] = False
            # DLL 是 Windows x64 二进制：该标签阻止 pip 把包安装到 macOS、Linux、ARM Windows 等环境。
            build_data["tag"] = "py3-none-win_amd64"
