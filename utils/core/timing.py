"""计时装饰器与阶段计时工具。零依赖其他 utils 子包。"""

from contextlib import contextmanager
from functools import wraps
import time


def timer(func):
    """打印函数总耗时；即使函数抛异常也会输出耗时。"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            print(f"【{func.__name__}】耗时：{elapsed_ms:.2f} ms", flush=True)

    return wrapper


@contextmanager
def timed_stage(label: str):
    """打印一段内部阶段耗时；异常时也会输出。"""
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"【{label}】耗时：{elapsed_ms:.2f} ms", flush=True)
