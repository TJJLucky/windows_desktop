"""计时装饰器：函数耗时输出。零依赖其他 utils 子包。"""

# time：获取单调/墙钟时间，计算耗时
import time


def timer(func):
    """装饰器：执行被装饰函数并打印其耗时（毫秒）。

    用于性能定位：在可疑的慢函数（如 OCR、模板匹配）上挂 @timer，
    每次调用都会在控制台输出耗时，便于对比优化前后差异。
    """

    def wrapper(*args, **kwargs):
        # 记录调用前时间戳（秒）
        t1 = time.time()
        # 原样执行被装饰函数并保留其返回值
        res = func(*args, **kwargs)
        # 记录调用后时间戳
        t2 = time.time()
        # 打印函数名与耗时（毫秒，保留两位小数）
        print(f"【{func.__name__}】耗时：{(t2 - t1) * 1000:.2f} ms")
        # 原样返回函数结果，不影响业务逻辑
        return res

    # 返回包装后的函数
    return wrapper
