# 无障碍树读取（会话列表与聊天区）

QQ NT 基于 Chromium，默认向 Windows 发布 UI Automation 无障碍树（本意是给读屏软件用）。
它把「界面上有什么」直接报出来，因此比截图 + OCR 快一到两个数量级，也不受字体、主题、
DPI 缩放影响。实现见 `utils/qq/accessibility.py`（2026-10-13 新增，提交 `5205116`）。

## 为什么用它

| 能力 | 无障碍树 | 截图链路（现网） |
| --- | --- | --- |
| 会话列表一屏 | **35 ms**（热读；26 行已渲染 / 15 行完整可见，冷读 65 ms） | 逐行右键「复制 QQ 号」约 480 ms/行 |
| 读当前会话可见消息 | **74 ms**（热读；21 条已渲染 / 7 条可见，冷读 90~134 ms） | 框选复制 **1770 ms**（同一会话同一内容，且只给可见的 7 条） |

另外三项附带收益：

- 行/消息元素自带 `CurrentBoundingRectangle`（**屏幕物理像素**），不必再用「窗口左上角 +
  截图内像素」换算，绕开了跨电脑 DPI 的偏移风险；
- 当前会话名（`chat-header__contact-name`）直读，不需要对会话列表做 OCR 复核；
- 滚动是否到位可由「行 rect 是否落在列表区内」判断，不必再猜坐标。

## 已实现接口

```python
from utils.qq.window_ops import ensure_qq_window_with_retry
from utils.qq import accessibility as A

hwnd = ensure_qq_window_with_retry()._hWnd
pane = A.accessibility_pane(hwnd)   # 会话列表
chat = A.accessibility_chat(hwnd)   # 当前会话的消息
```

- `ContactListAccessibility.pane(hwnd) -> AccessibilityPane`
  `rect` 列表区矩形；`rows` 全部已渲染行（含滚出视口的）；`visible_rows()` 只留完整可见的；
  `selected_rows()` 当前打开的会话；`rows_named(name)` 按昵称精确匹配。
- `ContactListAccessibility.chat(hwnd) -> AccessibilityChat`
  `contact_name` 当前会话名；`area_rect` 消息区视口；`messages` 全部已渲染消息；
  `visible_messages()` 只留与视口相交的。
- 每行 `AccessibilityRow`：`name` / `texts` / `rect` / `selected` / `pinned` / `copyable` /
  `unread` / `unread_count`（后三个见「未读红点」与「不可复制会话」两节）。
- 每条 `AccessibilityMessage`：`sender` / `timestamp` / `text` / `texts` / `rect`。
- 不可用时统一抛 `AccessibilityUnavailableError`（`code = "QQ_ACCESSIBILITY_UNAVAILABLE"`），
  调用方据此回退到截图链路，不应把它当业务失败。

读取本身是**只读**的：不点击、不发送键、不改剪贴板，也不要求前台窗口。

## 无障碍树的结构

会话列表（`ClassName` 带 `recent-contact-item` 的是行；`--selected` 是当前会话，
`--top` 是置顶；右击菜单条目同名，必须按 `menu` 排除）：

```
q-scroll-view ... recent-contact-list      （列表区，Name="会话列表"）
└─ recent-contact-item ...                 row rect / --selected / --top
   └─ Group item__content ...              行内容块（头像/文本/角标都挂在它下面）
      ├─ Text 昵称 / Text 时间 / Text 预览
      ├─ Group "N条未读"                   未读角标（有未读时才有）
      └─ Image（无类名、无名字）            服务/系统会话的 <img> 头像 = 不支持复制
```

单条消息：

```
message
├─ message__timestamp no-copy        → 子 Text = "昨天 12:42"（仅每个时间分组的首条）
├─ message-container ...
│  ├─ avatar-span                    → 子 Text = 发送人昵称
│  └─ msg-content-container ...      → Text 正文（@ 原样保留）
│                                     图片/表情的占位文字挂在 Group/Image 的 Name 上
└─ gray-tip-message / gray-tip-action（入群提示等灰条，人名挂在非 Text 元素的 Name 上）
```

两个解析上的坑（已在实现里处理）：

- QQ 会在父 `Group` 上重复子 `Text` 的同一句话（`avatar-span` 就是），只按「全部名称」
  收正文会读到两份；正文的文字部分因此只收 `Text` 控件，媒体占位才收元素名；
- comtypes 在消息元素上会把 `tagRECT` 解成裸 4 元 tuple，直接取 `.left` 会抛
  `AttributeError`，统一走 `_element_rect()` 归一化。

## 真机实测样例（2026-10-13，1280x1390 窗口）

会话列表：

```
pane rect (l,t,r,b) = (70, 115, 383, 1385)      行高 80px
树内行数 26，完整可见 15，枚举 64.8 ms（热读 35.0 ms）

#    name                     rect(l,t,r,b)         sel    pin   vis
0    2026-计算机系统3-周四        (70,  115, 383, 195)  False  True  True
2    汉艺材质包售后群            (70,  275, 383, 355)  True   False True   ← 当前会话
14   开心家群                   (70, 1235, 383,1315)  False  False True
15   Noita魔法群NG+            (70, 1315, 383,1395)  False  False False  ← 只露出半行
25   计算机游戏开发-储颖老师       (70, 2115, 383,2195)  False  False False  ← 视口外
```

聊天区（同一时刻）：

```
当前会话名 = '汉艺材质包售后群'
消息区视口 = (382, 115, 1050, 1127)
21 条已渲染，视口内 7 条，读取 90 ms（热读 74 ms）

#    vis    timestamp     sender              text
14   True   昨天 12:42     伟大的工人阶级万岁     @听风言
15   True                 伟大的工人阶级万岁     谢谢你
16   True                 （..）              不用谢
17   True                 伟大的工人阶级万岁     对的，跟我们客气什么
18   True                 伟大的工人阶级万岁     图片
19   True                 （..）              图片
20   True   昨天 22:47     明麓                加入了群聊。
```

对照：同一会话走现网 `copy_visible_selection()` 耗时 1770 ms，复制文本内容与上表
14~19 行一致（第 20 行的入群提示不在其可见范围内）。

## 已知边界

- **只给名字，不含 QQ 号**：号码只留一个诊断入口（`POST /v1/contacts/current:identify`,
  一次右键「复制 QQ 号」），业务链路不再碰号码（见
  [04-user-list-and-activation.md](04-user-list-and-activation.md)）；
- 长列表仍要滚动（`/v1/contacts:query {"all":true}` 会扫掠整张名单，见
  [04-user-list-and-activation.md](04-user-list-and-activation.md)）：树里只保留
  「视口附近约一屏半」的行，更远的行不在树里；
  且名单**不足一屏时容器根本不发布 ScrollPattern**（真机实测 `IsScrollPatternAvailable=False`），
  百分比滚动定位不可用，此时自动回退滚轮扫掠；
- 图片消息只有「图片」占位文字，拿不到本地文件路径（现网框选复制能给出 `<img src=...>`）；
- 时间戳是 QQ 的显示格式（`昨天 12:42` / `星期日` / `10/01`），不是绝对时间；
- 依赖 QQ 暴露无障碍层：QQ 大版本更新可能改结构，窗口最小化时读不到 —— 两种情况统一抛
  `AccessibilityUnavailableError` 让调用方回退。

## 契约口径：统一为 contactName（2026-10-13 已实施）

`service/contracts.py` 与命令账本的目标口径已从 `qqNumber` 改成 `contactName`。
改之前客户端一次都调不通（真实载荷离线打服务端）：

```
POST /v1/contacts:query  {"contactName":"TJJ"}   -> 422 extra_forbidden
POST /v1/commands/read   {"contactName":"TJJ"}   -> 422 missing qqNumber + extra_forbidden
POST /v1/commands/send   {"commandId":...,"contactName":"TJJ","text":"hi"} -> 422 同上
```

原因是旧契约只收 `qqNumber`（`extra="forbid"`），而客户端
`price-agent-next/apps/desktop/electron/main/qq/connector.ts` 全链路只发 `contactName`，
`contacts` 响应又以号码为 key、客户端却当昵称用。无障碍树只给名字、客户端也只用名字，
因此口径统一到 `contactName`；QQ 号降级为诊断（仅 `contacts/current:identify`）。

## 未读红点（新消息判断，2026-10-13 实施）

判断某个会话有没有新消息，直接用树里的**未读角标**，不必再截图找红点 RGB：

- 有未读时，行的内容块（`item__content`）下会多一个**名字形如「N条未读」**的子元素；
  无未读则没有该元素。真机实测样例：置顶会话 "！" 的内容块下有
  `Group name="1条未读"`（rect `(435,160,455,180)`，行右侧）。
- 条数取**角标内层的数字文本**，退回角标自身名字里的数字。
  坑：外层名字会滞后 —— 真机实测出现过外层 `1条未读` 而内层文本 `3`
  （消息一直进、名字没刷），所以**条数以内层文本为准**；`99+` 只取第一段数字。
- 对外字段：`/v1/contacts:query` 每行新增 `unread`（bool）与 `unreadCount`（int 或 null），
  实现在 `utils/qq/contact_lookup.py:_assemble`（字段名沿用响应里的 camelCase 口径）。

真机实测（2026-10-13，`！` 会话 3 条未读）：

```
AccessibilityRow: ！  unread=True   unread_count=3   pinned=True  copyable=True
/v1/contacts:query -> "！": { ..., "unread": true, "unreadCount": 3 }
```

与旧的图像红点（`utils/qq/user_list.py:check_new_msg`，检测 RGB(247,76,48)、轮廓 200~400px²）
相比：树里读未读是**同一次列表枚举顺带完成**（真机 10 行枚举约 177 ms，加读未读无可测回归），
不额外截图、不受主题/DPI 影响；图像红点仍是 `check_new_msg` 的保留能力，仅作兜底/诊断。

## 进程级崩溃 0xC0000005：comtypes 指针被多释放一次（2026-10-13 修复）

**现象**：`POST /v1/contacts:query`（带 `contactName`）调几次之后整个服务进程退出，
退出码 `-1073741819 (0xC0000005)`。日志末尾固定长这样：

```
accessibility.enumerate rows=10 ... elapsed_ms=202.11
accessibility.enumerate rows=10 ... elapsed_ms=39.49
WARNING accessibility.scroll_state.retry attempt=1 reason=NULL COM pointer access
accessibility.connect hwnd=722622 previous=None
WARNING accessibility.scroll_state.retry attempt=2 reason=NULL COM pointer access
...
accessibility.connect hwnd=722622 previous=None      <- 这行之后进程就没了
```

**定位**（`python -X faulthandler` 直接跑 `contact_lookup.query_contacts(name)`，可 100% 复现）：
faulthandler 打出的原生栈是

```
Garbage-collecting
comtypes/_post_coinit/unknwn.py:420 in Release
comtypes/_post_coinit/unknwn.py:288 in __del__
pywinauto/findwindows.py:181 in find_elements
... application.py -> wait_until_passes -> __resolve_control
utils/qq/accessibility.py:437 in _connect
```

即 **GC 恰好在 `_connect` 的 `find_elements` 执行期间触发，去 `Release()` 一个已经释放过的
COM 指针** → 访问违例。不是 QQ 窗口的问题，是本进程把指针释放多了。

**根因**：`_connect` 里原本写的是

```python
element = app.window(handle=hwnd).element_info.element
window = cast(element, POINTER(UIA.IUIAutomationElement))   # ← 崩溃源
```

`comtypes.cast is ctypes.cast`（就是同一个函数，实测为 True）。它只是把**同一个地址再包一层**：
新包装对象**没有 AddRef**，但 comtypes 的指针包装在 `__del__` 里照样 `Release()`。
于是同一个 UIA 元素被释放两次 —— 先到的那个把元素销毁，另一个包装就成了悬垂指针，
之后任何一次 GC 落到它上面就是 `0xC0000005`。重连次数越多、GC 越容易撞上（
`scroll_state` 每次白重连 3 次，一次查找白连 6 次，正好是放大器）。

**A/B 实测**：只把 `cast` 换成恒等映射（`lambda obj, typ: obj`），同一段压测立刻从「必崩」
变成「全过」。**修复**就是删掉这次 cast，改成直接持有 `element_info.element` 本身
（同一个对象，引用计数平衡：`ElementFromHandle` +1 ↔ 丢弃时 -1），见 `accessibility.py:_connect`。

**顺带修掉「白重连」**：会话名单不足一屏时，QQ（Chromium）**根本不发布 ScrollPattern**
（`GetCurrentPattern(UIA_ScrollPatternId)` 返回空指针，`IsScrollPatternAvailable=False`）。
空指针再 `.QueryInterface(...)` 会抛裸 `ValueError: NULL COM pointer access`，被
`is_transient_error` 误判成「元素树瞬时重建」，于是每读一次滚动状态就白重连 3 次。
现在 `_scroll_pattern` 先判空，抛新增的 `ScrollPatternUnsupported`（继承
`AccessibilityUnavailableError` 以复用调用方的回退口径，但被 `is_transient_error`
**明确排除**，不再重试）：调用方照旧 `except AccessibilityUnavailableError` → 回退滚轮扫掠。

真机验证（2026-10-13，hwnd=722622，10 行名单）：

| 项目 | 修前 | 修后 |
| --- | --- | --- |
| `query_contacts("不存在的名字")` ×3 | 第 3 轮进程 0xC0000005 退出 | 3 轮全部正常抛 `ContactNotFoundError` |
| 80 轮 `_assemble`（原 `snapshot`）+ `_reset()` + 强制 GC | — | 无崩溃 |
| `scroll_state`（名单不足一屏） | 6 次重连后报业务错误 | **10 ms** 内直接抛 `ScrollPatternUnsupported` |
| 临时服务 `POST /v1/contacts:query` ×12 | 崩溃 | 12/12 存活（6 次空查询 200、6 次未知名 503 业务错误） |
| `POST /v1/contacts/current:identify` ×3 | — | 3/3 200，返回 `3442859534`（717~1136 ms） |

## 落地现状（2026-10-13）

| 环节 | 当前实现 | 提交 |
| --- | --- | --- |
| 会话名单枚举 / 滚动定位 / 点击激活 | 无障碍树（`utils/qq/contact_lookup.py`） | `6f0fdb7` |
| 聊天区会话名、消息枚举 | 无障碍树（`utils/qq/accessibility.py`） | `5205116` |
| 读消息（对外 `/v1/commands/read`） | 仍走框选复制（`utils/qq/message.read_messages`），空会话与不可复制会话提前判掉 | `a0e8ec5` |
| 不可复制会话过滤 | 行结构判据 + 本机学习记录（弱证据两次才生效） | `6f0fdb7` + 本次 |
| 号码识别 | 仅诊断（`/v1/contacts/current:identify`） | — |
| 未读/新消息判断 | 无障碍树未读角标（`/v1/contacts:query` 的 `unread`/`unreadCount`） | 本次 |
| UIA 指针生命周期 / 滚动状态 | 单指针持有（不再 `ctypes.cast` 二次包装）；`ScrollPatternUnsupported` 不重试 | 本次 |

## 本次探索的思路取舍（复盘用，避免重走）

- **逐行右键「复制 QQ 号」当主查找手段**：约 480 ms/行，
  长列表一整屏就要十几秒；且询价对象多为陌生群，前提是能先定位会话、而不是先拿到号码。
  现仅保留为「需要复核号码时」的一次性动作。
- **截图 + 昵称 OCR 找联系人**：受字体、主题、DPI 影响，滚动后还要重新取帧换算坐标；
  同一会话靠框选复制读消息要 1770 ms，而且只给可见的 7 条。
- **用 OCR 把「名字 ↔ 号码」建映射来兼容号码口径**：列表过长时目标会被挤出视口，
  OCR 只能覆盖当前屏，映射不完整，不能当主查找路径。
- **调整框选起止点来绕开「起点要压在气泡上」**：实测从中间向下拉、从左下往右上拉都能触发选中，
  但**起点横向必须落在气泡上**、**终点那一侧没有消息也不会框选**，
  因此仍以「左上 → 右下」为准；不套用 QQPilot 的范围/选择方案，
  沿用现有框选范围（已按 DPI 缩放，提交 `6487d69` / `85f217e`）。
- **操控鼠标时加蒙版提醒用户不要动鼠标**：仍是候选，本次未实现；
  应用到新链路时再评估。

## 后续计划（按此顺序）

1. 读消息也改走无障碍树 `chat()`（热读 74 ms vs 框选 1770 ms）：先做「树里消息 ↔ 框选文本」
   的对照验证，再切主路径，`AccessibilityUnavailableError` 时回退框选。
2. 框选复制继续留在两条链路（空会话判定、无障碍树不可用时的回退），
   继续压缩它在真机上的 COM 崩溃面（底层 `_ctypes.pyd 0xc0000005` 未根治，
   已确认普通扫名单不再触发）。
   注：无障碍树侧那次 `0xC0000005` 不是同一类，已按上文定位并修复（多释放一次指针）。
3. 清理只剩诊断用途的号码链路：`utils/qq/contact_number.py`、`utils/qq/contact_scan.py`（已删）、
   `templates/copy_qq_number.png`，按「是否还保留 `contacts/current:identify`」决定去留。

## 如何复现（只读，不点击）

```python
import sys; sys.path.insert(0, r"E:\Project\agent\windows_desktop")
from utils.qq.window_ops import ensure_qq_window_with_retry
from utils.qq import accessibility as A

hwnd = ensure_qq_window_with_retry()._hWnd
pane = A.accessibility_pane(hwnd)
print(pane.rect, len(pane.rows), len(pane.visible_rows()), pane.elapsed_ms)
for row in pane.rows:
    print(row.name, row.rect, row.selected, row.pinned, row.texts)

chat = A.accessibility_chat(hwnd)
print(chat.contact_name, chat.area_rect, chat.elapsed_ms)
for message in chat.messages:
    print(message.visible_in(chat.area_rect), message.timestamp, message.sender, message.text)
```

要求：QQ 主窗口可见（未最小化）。运行环境用 `D:\anaconda3\envs\qq-desktop-service\python.exe`。
