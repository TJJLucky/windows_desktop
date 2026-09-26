# utils 包结构调整方案（不改实现，只动结构）

> 项目：`E:\Project\agent\windows_desktop`
> 原则：**逻辑零改动** —— 不修改任何函数体、类行为、算法；只做：文件改名、文件移动、import 路径调整、调试代码抽离、文档归位。
> 依赖契约保持 `core → vision → qq` 单向。

---

## 1. 现状结构

```text
utils/
├── __init__.py              # 顶层重导出 + ensure_dpi_aware()
├── core/
│   ├── mouse.py             # 鼠标原语
│   ├── screenshot.py        # WGC 截图 + DPI
│   └── windows.py           # ⚠️ 混入了大量 QQ 专属逻辑
├── vision/
│   ├── matcher.py           # 模板匹配
│   └── ocr.py               # RapidOCR
├── qq/
│   ├── composites.py        # QQ 窗口唤起（命名模糊）
│   ├── input.py             # 输入框
│   ├── message.py           # 消息（模型+管理器+算法混在一个文件）
│   ├── regions.py           # 区域定位
│   ├── userList.py          # ⚠️ camelCase 命名 + 模型+管理器+算法混装
│   └── QQAtomicOperator.py  # ⚠️ CamelCase 命名；名字与实际职责不符
└── debug/                   # ⚠️ 调试图片混在包内
```

---

## 2. 问题诊断

### 2.1 命名规范不一致（最直观）
| 文件 | 问题 | 建议 |
|---|---|---|
| `userList.py` | camelCase，Python 惯例 snake_case | → `user_list.py` |
| `QQAtomicOperator.py` | CamelCase 文件名 | → `dispatcher.py`（见 2.4） |
| 其余文件 | snake_case ✅ | — |

### 2.2 core 层混入 QQ 专属逻辑（违背分层契约，最严重）
`utils/core/windows.py` 文件头声明"平台原子原语、零 utils 内部依赖"，但内容一半是 **QQ 专属**：

```python
# QQ 专属（应属 qq 层）：
start_qq()                # L26
get_qq_pids()             # L31
get_qq_windows()          # L40
close_all_qq_windows()    # L61
clickExpandBtn()          # L77
click_qq_tray_icon()      # L98
get_main_qq_windows()     # L239
get_overflow()            # L246
# 通用平台原语（应留 core）：
timer() / WindowCaptureCtx / set_window_z_pos / minimize_window / maximize_window / move_window / detach_from_edge
```

- 违背 README 自述的 `core → vision → qq` 单向依赖（core 不应知道 QQ）
- 连带污染：`utils/core/mouse.py` 的 `__main__` 调试块反向 import `qq.composites`（L66）

### 2.3 双路径 try/except 导入（脆弱 + 冗长）
每个文件顶部都有：
```python
try:
    from .userList import UserList
except ImportError:
    from utils.qq.userList import UserList
```
全项目 44 处跨模块导入，大部分是这种双路径。根源是"打包 embed 环境"与"源码环境"导入根不同。结构调整应**统一为单一导入规范**，消除 try/except。

### 2.4 文件职责过载 + 命名语义模糊
| 文件 | 实际职责 | 问题 |
|---|---|---|
| `userList.py` | `QQUser` 模型 + `UserList` 管理器 + 霍夫圆/OCR 预处理/红点算法 | 3 类职责混装 |
| `message.py` | `Message` 模型 + `MessageList` 管理器 + 气泡检测算法 | 3 类职责混装 |
| `composites.py` | QQ 窗口唤起/托盘/主窗口管理 | 名字"composites"含义模糊，实为"窗口操作" |
| `QQAtomicOperator.py` | 单消费者**串行队列调度器** | 名字像"原子操作器"，与职责不符；建议 `dispatcher.py` |

### 2.5 调试代码混入主模块
- 每个文件尾部 `if __name__ == "__main__"` 调试块（mouse/matcher/screenshot/windows/regions/userList/input/message）
- `utils/core/windows.py` 大量 `print` 噪音（get_qq_windows L55/L57 等）
- `utils/debug/big_image.png` 调试图片混在包内（项目根已有 `debug/` 目录可归并）

### 2.6 文档混在包内
- `utils/qq/QQAtomicOperator.md` 是设计文档，应移至 `docs/`

---

## 3. 目标结构

```text
utils/
├── __init__.py              # 仅 ensure_dpi_aware() + 兼容重导出
├── core/                    # 纯平台原语（零 QQ 感知）
│   ├── __init__.py
│   ├── screenshot.py        # WGC + DPI（不变）
│   ├── mouse.py             # 鼠标原语（不变）
│   └── window.py            # 通用窗口原语（原 windows.py 中通用部分）
├── vision/                  # 纯图像分析
│   ├── __init__.py
│   ├── matcher.py           # 模板匹配（不变）
│   ├── ocr.py               # RapidOCR（不变）
│   └── compose.py           # 【可选新增】拼图/坐标映射公共算法（承接 P0 拼图优化）
├── qq/                      # QQ 业务层
│   ├── __init__.py
│   ├── window_ops.py        # ← composites.py 改名 + 接收 core 迁来的 QQ 专属窗口函数
│   ├── regions.py           # 区域定位（不变）
│   ├── models.py            # 【新增】QQUser / Message 数据模型（从 userList/message 移出）
│   ├── user_list.py         # ← userList.py 改名，仅留 UserList 管理器
│   ├── input.py             # InputBox（不变）
│   ├── message.py           # MessageList（气泡检测，模型已移出）
│   └── dispatcher.py        # ← QQAtomicOperator.py 改名（串行调度器）
docs/
└── qq-atomic-operator.md    # ← QQAtomicOperator.md 移入
debug/                       # 项目根已有；utils/debug 的图片归并于此
```

依赖方向不变且更干净：`core → vision → qq`；core 不再反向依赖 qq。

---

## 4. 文件映射表（旧 → 新）

| 旧路径 | 新路径 | 动作 |
|---|---|---|
| `utils/core/windows.py` | `utils/core/window.py` | 改名 + 移出 QQ 专属函数（见 §5.3） |
| `utils/qq/composites.py` | `utils/qq/window_ops.py` | 改名 |
| `utils/qq/userList.py` | `utils/qq/user_list.py` | 改名 + 模型移出 |
| `utils/qq/QQAtomicOperator.py` | `utils/qq/dispatcher.py` | 改名 |
| （无） | `utils/qq/models.py` | 新建，承接 `QQUser`/`Message` |
| `utils/qq/QQAtomicOperator.md` | `docs/qq-atomic-operator.md` | 移动 |
| `utils/debug/big_image.png` | `debug/big_image.png` | 移动（项目根已有 debug/） |
| （无） | `utils/vision/compose.py` | 可选，P0 拼图算法落地处 |

**保留类名与函数名不变**（`UserList`/`MessageList`/`InputBox`/`QQAtomicOperator`→`QQAtomicOperator` 或 `Dispatcher`？见 §5.5 决策）。

---

## 5. 分阶段执行步骤

### Phase 1 —— 纯改名（最小风险，先跑通）
1. `userList.py` → `user_list.py`：只改文件名与 import 引用。
2. `QQAtomicOperator.py` → `dispatcher.py`：同上。
3. `composites.py` → `window_ops.py`：同上。
4. 同步更新 `utils/__init__.py` 顶层重导出、`service/facade.py` 的 `from utils.qq.QQAtomicOperator import ...`、各模块互引、README。
5. 立即验证：`pytest tests/` + `python -c "import utils.qq.dispatcher, utils.qq.user_list, utils.qq.window_ops"`。

### Phase 2 —— core 去 QQ 化
1. 从 `utils/core/windows.py` 移出 QQ 专属 8 个函数：
   `start_qq / get_qq_pids / get_qq_windows / close_all_qq_windows / clickExpandBtn / click_qq_tray_icon / get_main_qq_windows / get_overflow`
   → 移入 `utils/qq/window_ops.py`（它们与现有 composites 内容同属"QQ 窗口管理"）。
2. `windows.py` 保留通用原语并改名 `window.py`。
3. 同步更新引用：`regions.py`（`get_qq_windows`）、`composites.py`、`input.py`、`message.py`、`user_list.py` 中凡 `from utils.core.windows import get_qq_windows, ...` 的改为从 `qq.window_ops` 导入。
4. `utils/core/mouse.py` 的 `__main__` 调试块反向依赖 qq —— 随 Phase 4 调试块抽离一并解决。

### Phase 3 —— 职责拆分
1. 新建 `utils/qq/models.py`：
   - `QQUser`（从 userList.py 原样搬移，含全部 setter/to_dict）
   - `Message`（从 message.py 原样搬移，含全部 setter）
2. `user_list.py` 改为 `from .models import QQUser`（删除本文件内 QQUser 定义）
3. `message.py` 改为 `from .models import Message`（删除本文件内 Message 定义）
4. 【可选】`utils/vision/compose.py`：放 `merge_bubbles` / `map_ocr_to_*` 公共函数（为上一份计划的 P0 拼图 OCR 预留位置；本次若不做 P0，可仅建文件头注释占位）。

### Phase 4 —— 导入规范化 + 调试抽离
1. 统一导入规范：全包统一为**相对导入**（`from .xxx import ...` / `from ..yyy import ...`），删除所有 `try: ... except ImportError: from utils...` 双路径块。
   > 若打包环境确实依赖 `utils.*` 绝对导入，则反方向统一为绝对导入 —— 两者选一，执行前确认打包方式。
2. 调试块抽离：各文件 `if __name__ == "__main__"` 移到 `tools/`（新建）独立脚本：
   - `tools/debug_screenshot.py`、`tools/debug_matcher.py`、`tools/debug_regions.py`、`tools/debug_user_list.py`、`tools/debug_message.py`、`tools/debug_input.py`
3. `timer` 装饰器的 `print` 改为可选静默（保留计时，增加 `verbose=False` 参数，默认沿用现状）—— 若算"改实现"则跳过，仅记录在案。

### Phase 5 —— 兼容层与收尾
1. **兼容转发模块**（若存在外部引用风险，如 `agent_runtime.integrations.windows_desktop.utils.qq.QQAtomicOperator`）：
   旧文件位置放转发模块，例如 `utils/qq/QQAtomicOperator.py` 内容为：
   ```python
   from utils.qq.dispatcher import QQAtomicOperator  # 或 Dispatcher
   ```
   过渡期后删除。
2. 更新 `README.md` 目录结构与模块说明表。
3. `utils/__init__.py` 重导出同步：`screenshot / mouse / window / matcher / ocr / window_ops / regions / user_list / models / dispatcher`。

---

## 6. 引用更新清单（已 grep 全量，44 处）

| 引用方 | 现状引用 | 改后 |
|---|---|---|
| `service/facade.py:24` | `from utils.qq.QQAtomicOperator import QQAtomicOperator` | `from utils.qq.dispatcher import ...` |
| `utils/__init__.py` | 重导出 composites/regions/userList | 重导出 window_ops/regions/user_list/dispatcher |
| `utils/qq/input.py` | `.composites`、`utils.qq.userList`（调试块） | `.window_ops`、`utils.qq.user_list` |
| `utils/qq/message.py` | `.composites`、`..core.windows` | `.window_ops`、`..core.window` |
| `utils/qq/regions.py` | `.composites`、`..core.windows`（get_qq_windows） | `.window_ops`、`..core.window`（get_qq_windows 改从 window_ops 导入） |
| `utils/qq/user_list.py` | `.composites`、`..core.windows`、`..core.mouse` | `.window_ops`、`..core.window` |
| `utils/qq/dispatcher.py` | `utils.qq.userList/input/message` | `utils.qq.user_list/input/message` |
| `utils/qq/window_ops.py` | `..core.screenshot/windows/mouse`、`..vision.matcher` | `..core.screenshot/window/mouse`、`..vision.matcher` + 新增 QQ 窗口函数 |
| `utils/core/window.py` | （原 windows.py 内部） | 仅通用原语 |
| `utils/core/mouse.py` | 调试块 `..qq.composites` | 调试块移除（Phase 4） |
| `README.md` | 结构/模块表/示例 | 全部同步 |

---

## 7. 验证方案

| 阶段 | 验证 |
|---|---|
| 每个 Phase 后 | `python -m pytest tests/ -v` 全绿（现有 4 个测试不触碰图像层，纯服务合同） |
| Phase 1 后 | `python -c "import utils.qq.dispatcher, utils.qq.user_list, utils.qq.window_ops"` 无 ImportError |
| Phase 2 后 | `python -c "from utils.qq.window_ops import get_qq_windows, click_qq_tray_icon, get_overflow"`；`from utils.core.window import WindowCaptureCtx, timer` |
| Phase 3 后 | `from utils.qq.models import QQUser, Message`；`from utils.qq.user_list import UserList` |
| 全量后 | `python -c "import utils"`（顶层重导出）+ 小号冒烟 `get_contact_list` / `read_message_list` 各一次 |
| diff 审查 | `git diff --stat` 确认只有移动/改名/import，无函数体改动（先 `git add -A && git commit` 建基线） |

---

## 8. 明确不做的事（守住"不改实现"边界）

1. ❌ 不改任何函数体、算法、常量数值
2. ❌ 不改类名/方法签名/返回结构（`QQAtomicOperator` 类名保持不变，仅文件名改为 `dispatcher.py`；如你也想改类名，单独确认后作为独立事项）
3. ❌ 不做 P0 拼图 OCR / 归一化等性能优化（那是上一份《修改计划_定稿.md》的内容，本次只搭好 `vision/compose.py` 的壳）
4. ❌ 不引入第三方新依赖

---

## 9. 需要你确认的 2 个决策

1. **类名是否同步改**：`QQAtomicOperator` 文件名改 `dispatcher.py`，类名保持 `QQAtomicOperator`（推荐，零外部影响）还是改为 `Dispatcher`（更贴切，但 `facade.py` 与潜在外部引用需同步改）？
2. **导入规范**：统一为**相对导入**（`from .x import y`，源码风格更干净）还是**绝对导入**（`from utils.x import y`，打包 embed 环境兼容）？取决于你的打包方式。
