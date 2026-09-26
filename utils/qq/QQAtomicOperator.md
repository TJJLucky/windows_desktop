# QQAtomicOperator

> 基于线程队列串行封装的 QQ UI 自动化操作器。解决 GUI 自动化并发冲突：**所有界面操作排队串行执行，同一时刻只运行一个 UI 任务，避免同时截图、点击、输入相互打架**。

## 特性

1. **单 Worker 串行执行**：全部 UI 任务进入队列，后台单个工作线程依次执行，杜绝 GUI 并发竞争。
2. **统一同步调用接口**：上层调用是阻塞同步，每个接口返回统一格式 `(result, err)`，`err` 为异常对象或 None。
3. **每个任务独立结果队列**：多调用方同时调用不会结果串扰；支持任务超时控制。
4. **队列状态观测**：`pending_task_count` 属性可查看当前等待执行的任务数量，方便调试、监控积压。
5. **优雅停止**：支持主动停止后台工作线程，不依赖 daemon 强制回收。
6. **异常自动捕获**：任务内部异常会被捕获，通过返回值传回，不会直接崩掉 worker 线程。

## 模块依赖结构

```bash
utils/qq/
├── __init__.py
├── userList.py          # UserList 用户/会话列表管理
├── input.py             # InputBox 输入框发送文本
├── message.py           # MessageList 消息读取
└── QQAtomicOperator.py  # QQAtomicOperator 本文件
```

## 导入

```python
# 双路径均可：相对导入（原生源）或 utils 顶层导入（打包后 embed 环境）
from agent_runtime.integrations.windows_desktop.utils.qq.QQAtomicOperator import QQAtomicOperator
# 或者
from utils.qq.QQAtomicOperator import QQAtomicOperator
```

## API 接口说明

### 初始化

```python
op = QQAtomicOperator()   # 单例，重复实例化不会重启 worker
```

- 初始化内部组件（`UserList`/`InputBox`/`MessageList` 单例），自动启动后台 worker 常驻线程。
- worker 线程会持续轮询任务队列，等待任务投递。

### 1. get_contact_list

```python
contacts, err = op.get_contact_list(timeout=60.0)
```

- **功能**：获取会话/联系人列表（会触发截图解析，缓存过期时执行 UI 操作）
- 参数
  - `timeout`：任务整体最大等待时间（排队 + 执行）
- 返回：`(dict[str, dict], Optional[Exception])`
  - 成功：`contacts` 为联系人字典，`err=None`
  - 超时：返回 `{}, TimeoutError(...)`
  - 业务异常：返回 `{}, 实际异常对象`

### 2. send_message

```python
ok, err = op.send_message(contact_name="张三", text="你好", timeout=60.0)
```

- **功能**：根据名字激活指定会话（包含匹配，自动移除符号），然后发送文本消息
- 参数
  - `contact_name`：联系人/会话名称
  - `text`：要发送的文本
  - `timeout`：超时时间
- 返回：`(bool, Optional[Exception])`
  - 成功：`True, None`
  - 超时：`False, TimeoutError(...)`

### 3. read_message_list

```python
msgs, err = op.read_message_list(contact_name="张三", timeout=60.0)
```

- **功能**：根据名字激活会话，读取当前会话消息列表（并清除用户列表缓存，使后续列表反映新状态）
- 返回：`(list[dict], Optional[Exception])`
  - 成功：消息列表，`err=None`
  - 超时：`[], TimeoutError(...)`

### 4. pending_task_count

```python
count = op.pending_task_count
print(count)   # 当前队列等待任务数
```

> 调试监控接口，查看当前队列等待执行的任务数量。

### 5. stop

```python
op.stop()
```

> **程序退出必须调用**

- 设置停止事件，等待 worker 线程退出（最多等待 3 秒）
- 不调用 stop，daemon 线程会被解释器强制杀死，可能 UI 操作中途中断。

## 标准调用示例

```python
from agent_runtime.integrations.windows_desktop.utils.qq.QQAtomicOperator import QQAtomicOperator

def main():
    op = QQAtomicOperator()

    # 获取联系人
    contacts, err = op.get_contact_list(timeout=60)
    if err:
        print("获取联系人失败：", err)
    else:
        print("联系人列表：", contacts)

    # 发送消息
    ok, err = op.send_message("张三", "测试消息", timeout=120)
    if err:
        print("发送失败：", err)

    # 读取消息
    msgs, err = op.read_message_list("张三", timeout=120)
    if err:
        print("读取消息失败：", err)
    else:
        print("收到消息：", msgs)

    # 查看队列状态
    print("队列状态：", op.pending_task_count)

    # 程序结束，关闭线程
    op.stop()

if __name__ == "__main__":
    main()
```

## 返回值约定（非常重要）

所有对外 API 统一返回二元组 `(result, err)`

1. `err is None`：任务正常执行完毕，`result` 为业务返回结果；
2. `err 是 TimeoutError`：**调度超时**，任务可能还在排队，不保证未被 worker 执行（超时后任务可能仍会执行）；
3. `err 是其他异常（ValueError/OCR 异常等）`：任务已被 worker 执行，业务逻辑抛出异常。

业务判断示例：

```python
ok, err = op.send_message("张三", "hi", timeout=60)
if err is not None:
    if isinstance(err, TimeoutError):
        print("任务调度超时，队列可能积压或 worker 卡死")
    else:
        print(f"执行业务异常：{err}")
else:
    print("执行成功", ok)
```

## 内部原理简述

1. **task_queue**：全局任务队列，主线程把任务投递进去。
2. **worker_thread**：后台常驻线程，循环消费 `task_queue`，所有 UI 自动化逻辑只在该线程运行。
3. **res_q（per-task result queue）**：每个 API 调用创建独立小队列，作为“回信通道”；worker 执行完成后把 `(result, err)` put 进该队列；主线程阻塞等待该队列返回结果。
4. **pending_task_count**：返回 `task_queue.qsize()`，用于观测队列积压。

> ⚠️注意：`timeout` 计时起点是**调用 API 那一刻**，排队耗时也计入超时时间。如果队列积压任务过多，即使任务还没开始执行，也可能触发超时。

## 注意事项 & 坑点

1. ❗**程序退出务必调用 `op.stop()`**，不要完全依赖 `daemon=True`。
2. 不要在 `impl` 内部做无限阻塞死循环，会卡住整个 worker 线程，导致后续全部任务超时。
3. 高频循环调用接口会造成任务队列积压，可以通过 `pending_task_count` 监控。
4. 所有 UI 操作全部走 API 入队，不要直接调用 `xxx_impl` 内部方法，内部方法只能 worker 线程调用。
5. 超时不等于业务报错，要区分 `TimeoutError` 和业务异常。
6. `timeout` 默认 10 秒较短，一次刷新可能 3~7 秒；实际调用建议传 60~120 秒。

## 异常场景

1. 联系人不存在：`active_user_by_name` 抛出 `ValueError`（`find_user` 包含匹配不到），会被捕获放到返回的 `err`，不会卡死 worker。
2. worker 卡死/死循环：后续所有任务都会报 `TimeoutError`，`pending_task_count` 可以看到任务持续积压。
3. 任务排队太多：`pending_task_count` 持续上涨，接口大概率超时，需要控制调用频率。

## 简单故障排查

- 大量任务返回 `TimeoutError`：看 `pending_task_count` 是否持续上涨；上涨说明 worker 卡死或者任务处理速度跟不上调用速度。
- 同一个会话发送后读不到最新状态：`send_message_impl` / `read_message_list_impl` 中已调用 `self.userList.clear_user_list_cache()`，清除会话列表缓存，不要移除该调用。
