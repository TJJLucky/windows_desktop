"""计时装饰器：函数耗时输出。零依赖其他 utils 子包。"""

import time


def timer(func):
    def wrapper(*args, **kwargs):
        t1 = time.time()
        res = func(*args, **kwargs)
        t2 = time.time()
        print(f"【{func.__name__}】耗时：{(t2 - t1) * 1000:.2f} ms")
        return res

    return wrapper
