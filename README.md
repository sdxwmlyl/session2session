<a id="top"></a>

**中文** ｜ [English](#english)

---

# Session Tasks（会话间任务委派）

同一个 agent 的不同会话之间互相派活：发起方建任务，接收方接单、报到、汇报，
结果只把**一行摘要 + 产物路径**回传到发起方的频道。任务状态持久化在追加式
事件日志里，进程重启不丢。

| | |
|---|---|
| 插件 id | `session-task` |
| 版本 | `0.1.0` |
| 类型 | `general`（普通插件：工具 + 提示词段 + 命令 + HTTP 路由 + 随包 skill） |
| 最低宿主 | QwenPaw `2.3.0` |

## 安装

**方式一：插件页**（推荐）
打开 QwenPaw → 设置 → 插件 → 安装本地插件，选择插件目录
`plugins_src/session-task`，安装后启用即可。

**方式二：接口**

```bash
curl -X POST http://127.0.0.1:<host-port>/api/plugins/install \
     -H "Content-Type: application/json" \
     -d "{\"source\": \"D:\\\\QwenPawWorkSpace\\\\default\\\\plugins_src\\\\session-task\", \"force\": true}"
```

确认状态与工具：

```bash
curl http://127.0.0.1:<host-port>/api/plugins/session-task/status
curl http://127.0.0.1:<host-port>/api/tools        # 应出现 6 个 task_* 工具
```

**方式三：命令行**

```bash
qwenpaw plugin install plugins_src/session-task --force
```

> 端口不是固定 8088：那是 CLI 的默认值。运行中的宿主会把自己的端口写进
> `<workspace>/config.json` 的 `last_api.port`（插件投递也按这个顺序找宿主）。

## 安装后你会得到什么

| 面 | 内容 |
|---|---|
| 工具 | `task_add`、`task_list`、`task_get`、`task_report`、`task_pulse`、`task_cancel` |
| 命令 | `/tasks`（我的任务）、`/task <id>`（单个任务详情）、`/nudge <id>`（催办） |
| 提示词段 | 当前会话的进行中任务摘要，动态注入系统提示词 |
| HTTP 只读接口 | `GET /api/session-task/tasks` 等，供前端/脚本查看 |
| Skill | `session-task-playbook`（随插件安装到工作区，默认启用） |

六个工具安装时**默认启用**：它们就是这个插件的全部功能，装完还要手动一个个
打开等于装了个空壳。

### `/tasks` 用法

```
/tasks               # 我发起的 + 派给我的，按更新时间倒序（默认摘要）
/task t-20261122-2223-4b506238
/nudge t-20261122-2223-4b506238
```

`/tasks` 只显示摘要（id / 标题 / 状态 / 双方会话 / 更新时间）。要看目标、摘要、
产物和全部历史，用 `task_get` 或 `/task <id>`。

## 状态机

```
submitted ──▶ working ──▶ completed
    │            │  ▲
    │            │  └── input-required ──┐
    │            └──▶ failed             │（补信息后回到 working）
    └────────────────▶ canceled
```

- 终态（`completed` / `failed` / `canceled`）不可改写；内容完全相同的重试返回 `ok`。
- 只有任务的发起方与接收方能读写这条任务。
- 任务 id 形如 `t-20261122-2223-4b506238`：分钟时间戳 + 8 位随机 hex，
  同一分钟建两个任务也不会撞车。

## 数据目录

一切都在当前工作区的 `session-tasks/` 下，纯文本、可直接查看/备份：

```
<workspace>/session-tasks/
├── events/<task_id>.jsonl     # 追加式事件流（唯一事实来源，带 seq 与时间戳）
├── index/                     # 预留：会话维度物化索引（当前视图按需合成，不落盘）
├── detail/                    # 预留：任务详情缓存（同上）
├── outbox/dry-run.jsonl       # 干跑模式下的"本应发出去"的消息
└── .locks/<task_id>.lock      # 写锁
```

任务视图（`task_get` 的返回）由事件流**按需合成**，不写第二份数据，所以
删除 `index/` 或 `detail/` 里的任何东西都不会丢信息。

- 事件流只追加、从不重写；`seq` 在锁内计算，单调递增。
- 一行坏数据只会被跳过并记日志，不会让整个任务不可读。
- **干跑**：`SESSION_TASK_DRY_RUN=1`（或 `QWENPAW_TASK_DRY_RUN=1`）时不发任何消息，
  只把待发文本追加到 `outbox/dry-run.jsonl`，用于验收与联调。

## 卸载

卸载插件会移除它注册的工具、命令、提示词段和路由，并清理由
`source=plugin:session-task` 标记的 skill 入口。

**`session-tasks/` 目录不会被删除**：任务历史属于用户数据，卸载一个插件不该
带走它。要清空请自己删目录（建议先备份）。

## 已知边界

- 提示词段挂载点：宿主只接受 `workspace` / `multimodal` / `env_context`，
  插件先试 `workspace` 前一个锚点，失败则回退 `workspace`（等价于紧跟 AGENTS.md）。
- 命令走宿主的 `register_control_command`；宿主没有该 API 时只有六个工具可用。
- 投递是尽力而为：没配 transport、频道掉线或干跑时，事件照写，消息不发，
  只记 warning。**任务成不成立只看事件流。**
- 老宿主缺某个可选 API 时，插件会降级加载（该面缺失 + 一条 warning），
  不会整体加载失败。

## 二期待做：stop handler / runtime hook（上游 API 未暴露）

US-010 要求的两件事——**每轮结束自动 `pulse`** 与 **结束前 stop guard**——
在 QwenPaw **2.3.0 上无法挂载**，因为宿主没有对应 API。已核对（2026-11-22，
`D:\QwenPaw\Lib\site-packages\qwenpaw\plugins\api.py`）：

| 需要的 API | 2.3.0 实况 |
|---|---|
| `register_runtime_hook` | 不存在（`PluginApi` 上没有任何 runtime/turn 级 hook） |
| `register_agent_stop_handler` | 不存在 |
| hook phase 枚举（`FINALLY`） | 不存在；包内 `FINALLY` / `HookPhase` 只出现在注释与 `finally:` 里 |

`PluginApi` 实际只有：`register_provider`（api.py:82）、`register_startup_hook`（:128）、
`register_shutdown_hook`（:160）、`register_uninstall_hook`（:192）、
`register_workspace_created_hook`（:235）、`register_http_router`（:271）、
`register_control_command`（:302）、`register_prompt_section`（:325）、
`register_tool`（:399）、`register_skill_provider`（:514）。四个 hook 都是**进程级**
（启动/关闭/卸载/工作区创建），不是「每轮结束」。

**因此本插件当前的行为**：`plugin.register()` 末尾调用 `reliability.install(api)`，
两条 API 都缺失时只写 warning 并返回
`{"installed": [], "missing": ["register_runtime_hook", "register_agent_stop_handler"]}`，
插件照常加载，六个工具与提示词段不受影响。该项**不阻塞一期验收**。

**已经写好的部分**（`reliability.py`，等上游暴露 API 即可生效）：

- `on_turn_finally` / `auto_pulse`：对当前会话名下 `working` 任务，若本轮没有
  report/pulse 就追加一条 `pulse`，`note` = 本轮最后一条 assistant 文本前 80 字
  （取不到则为 `""`）。同一 turn 对同一 task 只写一次（进程内 `(task_id, turn_key)`
  集合 + 磁盘上的 `idempotency_key=auto-pulse:<task>:<turn_key>`）。
- `on_agent_stop` / `stop_decision`：仍有 `working` 任务且本轮既无 report 也无 pulse
  时返回 `{"decision": "BLOCK", "blocked": true, "continuation": "任务 <id> 还在进行，
  请 task_report 或 task_pulse 后再结束", ...}`；每任务最多 BLOCK 2 次，第 3 次放行。
- **「本轮」怎么判定**：不用时钟，用**事件 seq 高水位**——每个任务记录上次 turn
  结束时的最大 `seq`，`seq` 更大的 `summary`/`pulse`（或带 `note` 的 `state`）就算
  本轮的活动。`seq` 由 `store.append_event` 在锁内分配、无空洞，比秒级时间戳可靠。
- 单测 `tests/test_reliability.py`（17 例）用 `FakeApi` 覆盖有/无两个注册函数、
  pulse 写入与 80 字截断、同 turn 只写一次、BLOCK 上限与跨任务计数独立。

**上游一旦提供 API**，只需确认签名后删除 `reliability.install` 里的两条
`getattr` 判断分支——插件侧的调用点已经在 `plugin.py` 里了。

## 相关文档

- 操作手册（给 agent 看）：`skills/session-task-playbook/SKILL.md`
- 需求与验收：仓库内 `plugins_src/session-task/` 同级的 PRD / 任务记录

---

<a id="english"></a>

# Session Tasks (Session2Session) — English

Delegate work between **sessions of the same agent**: the initiator creates a task, the
executor accepts, pulses, reports, and only **a one-line summary + artifact paths** travel
back to the initiator's channel. Task state lives in an append-only event log, so a process
restart never loses it.

| | |
|---|---|
| Plugin id | `session-task` |
| Version | `0.1.3` |
| Type | `general` (tools + prompt section + commands + HTTP router + bundled skill) |
| Min host | QwenPaw `2.3.0` |

## Install

1. **Plugin page (recommended)**: QwenPaw → Settings → Plugins → Install local plugin → pick the
   plugin directory, then enable it.
2. **HTTP**:
   ```bash
   curl -X POST http://127.0.0.1:<host-port>/api/plugins/install \
        -H "Content-Type: application/json" \
        -d "{\"source\": \"<path-to>/session-task\", \"force\": true}"
   ```
3. **CLI**: `qwenpaw plugin install <path>/session-task --force`

> The host port is not fixed: the running host writes its own port to `last_api.port` in
> `<workspace>/config.json`, and plugin delivery resolves it the same way.

## What you get

| Surface | Contents |
|---|---|
| Tools | `task_add`, `task_list`, `task_get`, `task_report`, `task_pulse`, `task_cancel` |
| Commands | `/tasks`, `/task <id>`, `/nudge <id>` |
| Prompt section | Active-task digest, injected dynamically (only when tasks are active) |
| HTTP | `GET /api/session-task/tasks` etc. |
| Skill | `session-task-playbook` (bundled, enabled by default) |

## State machine

```
submitted ──▶ working ──▶ completed
    │            │  ▲
    │            ▼  └── input-required (resume after supplying info)
    │         failed
    └────────────▶ canceled
```

* Terminal states (`completed` / `failed` / `canceled`) are immutable; an identical retry
  returns `ok`.
* Only the initiator and the executor of a task can read or write it.
* Task id format: `t-YYYYMMDD-HHMM-<8 hex>`, collision-safe within the same minute.

## Delivery and wake-up

`task_add` pushes the task card to the target session through the host chat API, which is
also what starts a turn in that session (observed: acceptance within ~3 s).

Since **2026-11-23** the prompt section additionally injects a four-rule **task protocol**
whenever the current session has active tasks:

1. on receiving a task card, call `task_report(state="working")` first;
2. before the turn ends, report a terminal state (`completed` with summary + artifact paths,
   or `failed` / `input-required` stating the blocker);
3. finishing while still `submitted` counts as a failed execution;
4. only report tasks you own.

## Data layout

Everything is plain text under `<workspace>/session-tasks/`:

```
events/<task_id>.jsonl     # append-only event log (single source of truth, has seq + ts)
outbox/dry-run.jsonl       # messages that *would* have been sent, in dry-run mode
.locks/<task_id>.lock      # write lock
```

Views are materialized from the event log on demand — no second copy of the data. A single
malformed line is skipped and logged; it never makes a task unreadable. Set
`SESSION_TASK_DRY_RUN=1` to record messages without sending.

## Known limits

* Prompt-section anchors: host accepts `workspace` / `multimodal` / `env_context`; the plugin
  tries the anchor before `workspace` and falls back to `workspace`.
* Commands go through `register_control_command`; when the host lacks it, only the six tools
  are available.
* Delivery is best-effort: no transport, a dead channel or dry-run means the event is written
  but the message is not sent — **task truth is always the event log**.
* QwenPaw **2.3.0** exposes neither `register_runtime_hook` nor `register_agent_stop_handler`,
  so the turn-end auto-pulse and stop-guard in `reliability.py` are implemented and unit-tested
  but not yet wired. Once the host ships those APIs, delete the two `getattr` branches in
  `reliability.install` and they activate.

## Tests

```bash
cd <path>/session-task
python -m pytest -q
```

Covers delivery/wake-up, HTTP router, prompt section, state machine, store, tools, views,
reliability (auto-pulse + stop guard) and wiring.

## Docs

* Playbook for agents: `skills/session-task-playbook/SKILL.md`
* Requirements / acceptance: PRD and task records shipped with the source.

## License

MIT (see `LICENSE`).

---

[↑ 中文](#top) ｜ [English](#english)
