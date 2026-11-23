---
name: session-task-playbook
description: "会话间任务委派（Session2Session）的操作手册：什么时候把任务交给另一个会话、接到任务后怎么开工与汇报、任务卡长什么样、摘要与产物怎么写、失败了怎么收尾与催办。当你要 delegate/委派/派活/交接任务，或收到任务卡需要 accept/report/完成/卡住/催办，或要查任务进度（task_list/task_get）时使用。"
metadata:
  {
    "copaw": {
      "emoji": "🔀",
      "requires": {}
    }
  }
---

# session-task 手册

六个工具就是全部接口：`task_add` / `task_list` / `task_get` / `task_report` /
`task_pulse` / `task_cancel`。任务状态只有五个：`submitted` → `working` →
`completed` / `failed` / `canceled`，中间可停一次 `input-required`（等发起方补信息）。
事件流是唯一事实来源，写在 `<workspace>/session-tasks/events/<task_id>.jsonl`。

## 一、什么时候委派（发起方）

**该委派**：这件事需要另一个会话的上下文或专长（那边的历史、那边的文件、
那边的模型）；这件事要跑很久，不该堵住当前对话；这件事本来就属于另一个频道
（比如微信群里的需求交回给主会话处理）。

**别委派**：一句话就能答的；你自己三步内能做完的；只是"想让别人看一眼"而
没有明确验收标准的 —— 那会变成一条永远停在 `submitted` 的僵尸任务。

委派前先 `task_list(role="from")` 看一眼，别对同一目标重复派同一件事。

## 二、怎么委派

```
task_add(
    to_session="sub-1a2b3c4d",     # 必须是真实存在的会话 ID
    title="把 11 月周报合成一页",   # 一句话，≤200 字符
    goal="读 11 月四份周报，输出一页 A4 HTML，含 3 张趋势图 …",  # ≤4000 字符
    artifacts=["<path-to>/weekly.md"],  # 可选：要读或要产出的路径，≤50 项
)
```

- **`goal` 就是验收标准**：写清"输入在哪、产出是什么格式、什么样算做完"。
  接收方看不到你脑子里的上下文，`goal` 是它唯一的信息来源。
- 返回值是 `task_id`，把它记在回复里；后面所有操作都用它。
- 成功后插件会 best-effort 把**任务卡**推到 `to_session`。推不出去不影响任务
  成立（事件已经落盘），但接收方就等着被动发现 —— 那种情况请口头再提醒一次。

任务卡的形状（插件生成，最多 300 字符，接收方看到的就是这个）：

```
【任务 id=t-20260101-0000-a1b2c3d4】把 11 月周报合成一页
来自 wechat:<user>｜目标 读四份周报输出一页 A4
1) task_get({task_id}) 看详情
2) task_pulse({task_id}, '收到') 接单
3) 边做边 task_report({task_id}, 'working', '进展')
4) 完成 task_report({task_id}, 'completed', '结果', ['产物'])
5) 缺信息 task_report({task_id}, 'input-required', '你要什么')
做完必须 report，不能静默结束。
```

（真实的卡片里 `{task_id}` 是实际 id。看到卡片就当"已接单"：先 `task_get`，
再 `task_pulse`，别静默。）

## 三、接到任务怎么做（接收方）

1. **先 `task_get(task_id)`**，把 `goal` 与 `artifacts` 读完再动手；不读就开工
   是最常见的返工原因。
2. **开工先报到**：`task_report(task_id, "working", "我先读那四份周报")`。
   发起方看到的是 `▶ 进行中`，比沉默强得多。
3. **长任务打心跳**：超过几分钟没进展就 `task_pulse(task_id, "在画第三张图")`。
   同一条 note 重复打只会记一条，不用怕刷。
4. **缺信息就停**：`task_report(task_id, "input-required", "缺 10 月数据，路径？")`。
   这条会推到发起方，并且状态停在等你答复，不会被当成做完。
5. **做完必须 report**：`task_report(task_id, "completed", 摘要, artifacts=[...])`。
   **不 report 的任务，发起方永远只看到 `submitted`** —— 这是本插件唯一的
   "静默失败"模式。

## 四、摘要与产物怎么写

- **摘要是一句话，不是交付物**。写"做了什么 + 结论是什么"：
  `"四份周报已合并，产出 1 页 A4（含 3 张趋势图），待你确认口径"`。
  ≤8000 字符硬上限，但真正被推到频道的只有一行（截到 120 字符），
  所以第一句就要能独立读懂。
- **产物写完整路径列表，且是"全量"而不是增量**：再次 report 会**整体替换**
  旧列表。漏写一条 = 发起方看不到它。
- 状态行是自动生成的：`✅ 完成：…（N 个产物）`、`❌ 失败：…`、
  `⏸ 需要你：…`、`▶ 进行中`、`🚫 已取消`。你只负责把摘要写对。

## 五、失败、卡住、催办

| 情况 | 动作 |
|---|---|
| 做不完 / 做错了 | `task_report(id, "failed", 原因 + 已做到哪一步)` |
| 缺信息 | `task_report(id, "input-required", 具体要什么)` |
| 任务不该做 | `task_cancel(id, 原因)` |
| 发起方想知道进度 | `task_get(id)`；需要催就用普通消息提醒接收方，别重复 `task_add` |
| 想重复派同一件事 | 先 `task_list(role="to")`，已有就 `task_pulse` 或续报，不要新建 |

终态（`completed` / `failed` / `canceled`）**不可改动**：同一个"内容完全相同"
的重试会返回 `ok`（这样丢了响应也能安全重试），改一个字都会被拒绝。要续做，
就新开一条任务并在 `goal` 里引用旧 `task_id`。

## 六、边界

- **只有发起方与接收方能读写这条任务**，第三方调用得到 `{"ok": false}`。
- 工具永不抛异常：出错一律返回 `{"ok": false, "error": "..."}`，把 `error`
  原样读出来再决定下一步。
- 投递是"尽力而为"：频道挂了、没配 transport、dry-run 打开时，事件照样写，
  只是消息没发出去。写周报这类**实质动作**永远以事件流为准，不以"发出去了"为准。
