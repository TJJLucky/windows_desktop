"""RapidOCR 文字识别模块：预处理 + 离线识别，适配 QQ 双底色场景。

- 使用 rapidocr_onnxruntime（基于 ONNX Runtime，无需 PaddlePaddle），完全离线；
- 单例模式：OCR 引擎初始化较重（加载三个模型），全进程只建一次；
- 模型目录三级解析：环境变量 RAPIDOCR_MODEL_PATH → 安装目录 ocr-models → 包内 models 兜底，
  覆盖"源码开发 / exe 打包"两种运行形态。
"""

# os：读取环境变量、拼接模型路径
import os
# sys：定位解释器目录（打包后安装结构探测）
import sys
# Path：路径对象
from pathlib import Path

# cv2：图像预处理（本模块当前直接交给 RapidOCR，保留导入便于后续灰度/二值化）
import cv2
# numpy：数组（OCR 输入格式）
import numpy as np
# PIL：图像对象（接口签名用）
from PIL import Image


class OCREngine:
    """RapidOCR 引擎单例（基于 ONNX Runtime，无需 PaddlePaddle）。"""
    # 类级单例实例
    _instance = None

    def __new__(cls):
        # 单例：只创建一次实例
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # 已初始化过（_ready 已设置）→ 跳过，避免重复加载模型
        if hasattr(self, "_ready"):
            return
        # 标记就绪（先置位再初始化，防重入）
        self._ready = True
        # 延迟 import：RapidOCR 及其依赖较重，首次真正需要时才加载
        from rapidocr_onnxruntime import RapidOCR
        # 解析模型目录（环境变量 → 安装目录 → 包内兜底）
        model_dir = self._resolve_model_dir()
        # 三个 ONNX 模型路径：检测 / 识别 / 方向分类
        det = os.path.join(model_dir, "ch_PP-OCRv4_det_infer.onnx")
        rec = os.path.join(model_dir, "ch_PP-OCRv4_rec_infer.onnx")
        cls = os.path.join(model_dir, "ch_ppocr_mobile_v2.0_cls_infer.onnx")
        # 逐个校验存在：任一缺失立即报错（给出模型目录线索，便于排查部署问题）
        for path in (det, rec, cls):
            if not os.path.exists(path):
                raise FileNotFoundError("模型缺失: {path}（请检查 RAPIDOCR_MODEL_PATH={model_dir}）".format(path=path, model_dir=model_dir))
        # 用显式模型路径创建引擎（不依赖包内默认路径，保证三种部署形态都能命中）
        self._engine = RapidOCR(det_model_path=det, rec_model_path=rec, cls_model_path=cls)

    @staticmethod
    def _resolve_model_dir() -> str:
        """解析 OCR 模型目录：优先 RAPIDOCR_MODEL_PATH 环境变量，
        其次自动探测安装目录 <root>/current/ocr-models，
        最后回落 rapidocr 包内 models 兜底。
        """
        # 优先级 1：用户/部署方显式指定（最可控）
        env = os.environ.get("RAPIDOCR_MODEL_PATH")
        if env:
            return env
        # 优先级 2：打包后安装结构 <root>/current/python-runtime/python.exe，
        # 模型放在 <root>/current/ocr-models（exe 的父目录或祖父目录）
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (exe_dir.parent / "ocr-models", exe_dir / "ocr-models"):
            # 以检测模型存在与否判断该目录是否有效
            if (candidate / "ch_PP-OCRv4_det_infer.onnx").exists():
                return str(candidate)
        # 优先级 3：本地开发兜底：使用 rapidocr 包内默认 models 目录
        import rapidocr_onnxruntime as _rapidocr
        return os.path.join(os.path.dirname(_rapidocr.__file__), "models")

    def ocr(self, img: np.ndarray) -> list:
        """执行 OCR，返回 [[bbox, text, confidence], ...]；无文本时返回空列表。"""
        # 引擎推理：返回 (结果列表, 剩余参数)；结果格式为 [框坐标, 文本, 置信度]
        result, _ = self._engine(img)
        # 空结果（None）归一化为空列表，调用方无需判空
        return result or []

    @classmethod
    def warm_up(cls):
        """预热引擎：提前触发初始化（加载模型），避免首次调用时卡顿。"""
        cls()


# ── 模块级便捷函数 ────────────────────────────────────────────────

def ocr_warm_up():
    """预热引擎，在线程池中调用避免阻塞。"""
    OCREngine.warm_up()
