"""RapidOCR 文字识别模块：预处理 + 离线识别，适配 QQ 双底色场景。"""

import os
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


class OCREngine:
    """RapidOCR 引擎单例（基于 ONNX Runtime，无需 PaddlePaddle）。"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_ready"):
            return
        self._ready = True
        from rapidocr_onnxruntime import RapidOCR
        model_dir = self._resolve_model_dir()
        det = os.path.join(model_dir, "ch_PP-OCRv4_det_infer.onnx")
        rec = os.path.join(model_dir, "ch_PP-OCRv4_rec_infer.onnx")
        cls = os.path.join(model_dir, "ch_ppocr_mobile_v2.0_cls_infer.onnx")
        for path in (det, rec, cls):
            if not os.path.exists(path):
                raise FileNotFoundError("模型缺失: {path}（请检查 RAPIDOCR_MODEL_PATH={model_dir}）".format(path=path, model_dir=model_dir))
        self._engine = RapidOCR(det_model_path=det, rec_model_path=rec, cls_model_path=cls)

    @staticmethod
    def _resolve_model_dir() -> str:
        """解析 OCR 模型目录：优先 RAPIDOCR_MODEL_PATH 环境变量，
        其次自动探测安装目录 <root>/current/ocr-models，
        最后回落 rapidocr 包内 models 兜底。
        """
        env = os.environ.get("RAPIDOCR_MODEL_PATH")
        if env:
            return env
        # 打包后安装结构：<root>/current/python-runtime/python.exe，
        # 模型放在 <root>/current/ocr-models
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (exe_dir.parent / "ocr-models", exe_dir / "ocr-models"):
            if (candidate / "ch_PP-OCRv4_det_infer.onnx").exists():
                return str(candidate)
        # 本地开发兜底：使用 rapidocr 包内默认 models 目录
        import rapidocr_onnxruntime as _rapidocr
        return os.path.join(os.path.dirname(_rapidocr.__file__), "models")

    def ocr(self, img: np.ndarray) -> list:
        """返回 [[bbox, text, confidence], ...]"""
        result, _ = self._engine(img)
        return result or []

    @classmethod
    def warm_up(cls):
        """预热引擎。"""
        cls()


# ── 模块级便捷函数 ────────────────────────────────────────────────

def ocr_warm_up():
    """预热引擎，在线程池中调用避免阻塞。"""
    OCREngine.warm_up()
