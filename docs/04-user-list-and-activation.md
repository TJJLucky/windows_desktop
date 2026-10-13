# 会话列表定位与激活（无障碍树）

## 会话身份

会话身份是会话行在 UI Automation 无障碍树里自报的名字（好友昵称/备注、群名、官方号名）。
HTTP 契约（`service/contracts.py`）与命令账本的目标口径统一为 `contactName`：
`/v1/contacts:query`、`/v1/commands/read`、`/v1/commands/send`、`/v1/commands/{id}`
的请求体字段与响应 key 都是会话名。

号码降级为诊断：只有 `POST /v1/contacts/current:identify` 会用一次右键「复制 QQ 号」
（`utils/qq/contact_number.py`），业务链路不再碰它。昵称 OCR 与其拼图链路整体移除
（`utils/vision/compose.py`、`utils/qq/contact_scan.py` 一并删除）。

## 名单读取

`UserList.get_user_list(contact_name=None)` → `contact_lookup.list_contacts`：
读无障碍树里的会话列表容器（`accessibility.pane`），返回

```json
{"汉艺材质包售后群": {"rect": [70, 275, 383, 355], "texts": [...],
                     "active": true, "pinned": false,
                     "copyable": true, "refused": false}}
```

含树里已渲染但滚出视口的行。两个注意点：

- 元素树重建期会瞬时返回空名单，`read_pane` 等到「连续两次读到同一份名单」或 1.5s 超时才返回；
- 默认筛掉两类读不了消息的会话（见下节的「不可复制会话过滤」），
  `include_non_copyable=True` 时全部保留并用 `copyable=False` 标出（诊断用）。

指定 `contact_name` 时先按名字滚动定位，找不到抛 `QQ_CONTACT_NOT_FOUND`。

## 按会话名定位与激活

`UserList.activate(contact_name)` → `contact_lookup.activate`：

1. `read_pane` 读当前一屏。命中且完整可见 → 直接用该行矩形中心作点击坐标
   （行自带**屏幕物理像素**矩形，跨电脑 DPI 不需要再换算）。
2. 已渲染但被视口边缘裁掉的行：按内容高度换算滚动百分比把它推进视口（`_reveal`），
   省掉整轮扫掠。
3. 仍找不到时扫掠：
   - 优先用列表容器的 UIA `ScrollPattern` 按百分比逐段下扫。真机实测
     `SetScrollPercent` 约 2~9 ms、重读一屏 40~65 ms；步长 = 树里已渲染行的跨度 × 0.8，
     因此相邻两段必然重叠、不会漏行。扫掠按钮上界为 40 步。
   - 容器不发布该 Pattern 时退回鼠标滚轮逐段滚动，按「名单不再变化」判到顶/到底
     （QQ 的滚轮滚动带动画，单次位移不稳定，不估算步长）。
   - 只有百分比扫掠没覆盖全表（`covered=False`）时才跑慢速滚轮回退；查找失败把列表滚回原位置。
4. 点击前先判断「是否已经是当前会话」：聊天区标题与激活行标记都等于目标名时直接返回，
   **不点击** —— 真机实测点击已激活的行反而会把激活高亮切掉（226 灰 → 245 灰）。
5. 点击命中行中心后，在 4 秒确认窗口内轮询两个**独立**信号：聊天区标题 == 目标名；
   或目标行带上 `--selected` 激活标记（官方号、临时会话的标题可能与行名不同）。
   两个信号每轮都判，不做「先只信标题、超时才补判标记」——真机实测后者会把一次瞬态
   COM 失败放大成 `ACTIVATION_FAILED` 假阴性。点击在 `WindowCaptureCtx` 中执行，
   临时置顶但不抢焦点。

失败语义：昵称为空/超长 `QQ_CONTACT_NAME_INVALID`；滚动查找后仍未找到
`QQ_CONTACT_NOT_FOUND`；同名多于一个 `QQ_CONTACT_AMBIGUOUS`；点击后当前会话仍不是目标
`QQ_CONTACT_ACTIVATION_FAILED`；目标不支持复制 `QQ_CONTACT_NOT_COPYABLE`；
无障碍树不可用 `QQ_ACCESSIBILITY_UNAVAILABLE`（调用方据此回退到截图链路）。

## 不可复制会话过滤

QQ 有一批会话没有框选复制消息的能力：服务/系统会话（「我的手机」「QQ邮箱提醒」）、
官方号（「QQ团队」）、临时会话里的邀请卡片、只含「暂不支持该消息类型」占位消息的会话。
留在名单里，每次读取都要白等十几秒才失败，而且是全链路 COM 崩溃风险最高的一段。
两层过滤：

- **行结构判据**（`accessibility._row_copyable`）：行内挂着 `<img>` 之类没有可复制文本的
  元素 → 判为不可复制，从名单里筛掉；对它们读消息毫秒级报 `QQ_CONTACT_NOT_COPYABLE`。
- **本机学习记录**（`utils/qq/contact_lookup.py`，state 目录下 `non-copyable-contacts.json`）：
  框选复制失败且失败码在 `REFUSAL_FAILURE_CODES` 内时按名字记一次，之后从名单里筛掉、
  直接读取毫秒级报同一个业务错误。记录只针对本机这个账号，随 state 目录走，最多 200 条。
  - **强弱两级**：进了多选态之后才失败的码是强证据（`QQ_COPY_CLIPBOARD_UNCHANGED`、
    `QQ_COPY_TEXT_EMPTY`、`QQ_COPY_ACTION_NOT_FOUND`），一次即生效；
    **没进多选态**（`QQ_SELECTION_NOT_ENTERED`）是弱证据，**连续两次**才生效。
    真机上没有消息的空会话也会报这个码（画布纯色，但像素判空没认出来），
    只记一次就筛会让真实供应商被静默丢出名单。
  - 开关 `QQ_CONTACT_REFUSAL_LEARNING=0` 关闭学习；删除记录文件即清空。

## 激活态与列表区域的图像判定（保留，供诊断）

会话定位已改走无障碍树，下面这些图像能力仍在，供诊断接口使用。
（未读/新消息判断 2026-10-13 起改走无障碍树的未读角标 —— 见
`docs/13-accessibility-tree.md` 的「未读红点」一节，图形红点仅作兜底。）

- `UserList.is_active_bg(img)`：按行背景**绝对色带**判激活，不读标题 OCR。
  浅色主题激活约 226 灰、未激活约 245 灰（悬停未激活约 235 灰），深色主题各降一档；
  非中性灰一律按未激活。不用「相对离群」兜底：常驻 235 灰的未读行会被误算成候选，
  在激活行滚出视区时报出假歧义。
- `UserList.refresh_image()`：确保 QQ 主窗口就绪 → WGC 整窗截图（会等待当前已渲染帧）→
  模板定位搜索栏/好友列表区域；行区域裁切仍在需要时用霍夫圆检测头像圆
  （`crop_user_row_by_avatar`，按 `cy` 排序），仅诊断用。
- 区域坐标缓存（`utils/qq/region_cache.py`）：按模板版本/存储实例、QQ 进程、窗口句柄、DPI、
  窗口矩形、最大化状态与截图尺寸生成签名；只缓存**区域位置与模板定位信息**，不缓存截图；
  窗口移动/缩放、进程或句柄、DPI/像素尺寸、模板版本变化时重新定位。
  `QQ_REGION_CACHE_TTL` 默认 `0`。右键菜单的「复制 QQ 号」与消息多选工具栏是动态位置，
  每次都在当前截图里重新匹配，不复用固定区域坐标缓存。
