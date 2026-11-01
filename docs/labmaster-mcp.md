# labMaster 功能 → Kelvin MCP 对照表（v2-C）

日期：2026-10-30。范围：labMaster v0.1.7 的**全部功能面**（`panel_server.py` 的 HTTP 路由、`panel_mcp.py` 的 16 个
`panel_*` 工具、UI 的 Home / Sequence / History / Settings / Notes / Chat 页面），逐条对照 Kelvin 主 AI（in-app chat 与
外部 MCP client 共用的工具表）。

结论先说：

- **序列、函数库、AI 写脚本、History、分析脚本、数据索引、日志、主页**这些 labMaster 能由人/AI 做的核心工作，Kelvin
  的 AI 现在都有对应 MCP 工具（v1 的 12 个 + v2-C 新增 22 个 = **34 个 `labmaster_*` 工具**）。
- **协作 Chat / @agent / 通知 / Notes / 远程访问 / 多语言**由 Kelvin 原生能力覆盖（v1 已把 labMaster 式团队聊天并进来），
  不再重复做一套工具。
- **明确不暴露给 AI 的**只有：人自己的配置与 UI 操作（Settings/paths、open folder、函数库整库整写、History 清空/导出/
  manual groups、插件与自定义页面、语言与手机听写）。理由逐条写在表里：它们是"人在他那台电脑上的一次操作"，给 AI
  做权限只会绕过人，而且 Kelvin 原生已在页面上提供。
- 序列的 **start 仍受人的 arm 开关**（`AI may start runs`，只有人能在 Sequence 页打开）；会丢弃/替换东西的动作
  （清空序列、覆盖已存序列、删已存序列、删除函数、覆盖脚本/分析脚本、取消分析）都要求 `confirm=true`，且工具在不带
  confirm 时**不会碰引擎**、只回一句"先问用户"。

---

## 一、Home 主页

| labMaster 功能 | Kelvin 对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| Overview 页：5 个 tile（测量进度/History/已存序列/函数库/面板） | `labmaster_home`（README）+ `labmaster_status` | 🆕 | 返回 greeting、tile 值/细节/tone、AI 卡片数；`status` 给序列细节 |
| AI/人往主页加卡片 | `labmaster_home_add_card` / `labmaster_home_remove_card` / `labmaster_home_cards` | ✅ v0 | 卡片存 `~/Kelvin/home.json`，人刷新 Home 立即可见 |
| Notes tile（打开 Markdown 笔记页） | Kelvin lab memory（`shared_memory` / `remember_shared`）+ `read_file`/`list_folder` | 🔁 | 笔记的内容归"实验室记忆"更合适；文件读写 Kelvin 原生已有 |
| 版本号显示 | `labmaster_status`（引擎版本 v0.1.7） | ✅ | |

## 二、Sequence 测量序列

| labMaster 功能 | Kelvin 对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| 状态/进度/每块状态 | `labmaster_status` | ✅ v0 | 每块带 uid、状态、当前/下一块 |
| 函数库 → 添加块（可带参数、index） | `labmaster_add_block(block, params, index)` | ✅ v0 | 加完返回新块 uid |
| 改块参数/名字、重跑 FAILED（reset） | `labmaster_update_block(uid, params, name)` | ✅ v0 | FAILED 的 Retry/Skip 语义仍建议人去面板点（见"已知缺口"） |
| 删块（FAILED 块需确认） | `labmaster_delete_block(uid, confirm)` | ✅ v0 | FAILED 块无 confirm 时只回确认文本 |
| 拖拽排序 | `labmaster_move_block(uid, index)` | 🆕 | |
| 复制块（Duplicate） | `labmaster_duplicate_block(uid)` | 🆕 | 返回副本 uid |
| Clear（可 keep_done） | `labmaster_clear_sequence(keep_done, confirm)` | 🆕 | 破坏性 → confirm |
| 序列改名 | `labmaster_rename_sequence(name)` | ✅ v1 | AI 测量也会自动给默认日期名命名 |
| Start / Pause / Pause-rep / Next-rep / Skip / Stop / Reset / End-wait / Set-wait / Safe stop | `labmaster_control(action, confirm, minutes, uid)` | ✅ v0/v1 | **start 受 arm 开关**：arm 关时出审批卡；stop/safe_stop 免确认；skip/reset/end_wait/set_wait/next_rep 需 confirm |
| arm 开关（AI 能否直接 start） | 只有人在 Sequence 页能开关；工具只读（`status` 里显示 on/off） | 🔁 | 刻意不做 MCP 写：AI 不能给自己授权 |
| 已存序列：列表 | `labmaster_saved` | ✅ v0 | |
| 保存当前序列 / 覆盖 | `labmaster_saved_save(name, overwrite, confirm)` | 🆕 | overwrite 需 confirm |
| 加载已存序列（append/replace） | `labmaster_saved_load(id, name, append, replace, confirm)` | 🆕 | replace 丢弃现有队列 → confirm |
| 删除已存序列 | `labmaster_saved_delete(id, name, confirm)` | 🆕 | confirm |
| 序列 reorder（整表拖拽一次提交） | `labmaster_move_block` 逐块移动 | 🔁 | 效果相同，无需整表接口 |
| 序列名前默认日期名 | 自动（AI 测量时 `ensure_sequence_name`） | ✅ v1 | |

## 三、函数库（palette）

| labMaster 功能 | Kelvin 对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| 函数库列表（name/kind/params/target/note） | `labmaster_functions` | ✅ v0 | 也列出可导入的 .py |
| 导入已有 .py 成函数 | `labmaster_import_script(path, name)` | ✅ v0 | 整库回写，带 base_rev |
| **AI 写新测量脚本**（原 labMaster 没有） | `labmaster_write_script(name, code, overwrite, confirm)` | 🆕 | 写 `~/Kelvin/sequence/scripts/`；先 parse，再用 vendored `scan.scan()` 返回参数/组/注释；拒绝路径逃逸；覆盖需 confirm |
| 读取脚本源码/参数 | `labmaster_read_script(path)` | 🆕 | 返回代码 + 自动发现的参数 |
| 参数自动发现（注释/分组） | `scan.scan()`（写脚本时/读脚本时返回） | 🆕 | 与面板 "+ New function" 同一套解析器 |
| 函数改名/note/默认参数/颜色 | `labmaster_function_update(id, name, note, params, color)` | 🆕 | |
| 复制函数 | `labmaster_function_duplicate(id)` | 🆕 | |
| 删除函数 | `labmaster_function_remove(id, confirm)` | 🆕 | confirm；已用的块保留自己的记录 |
| Duplicate / New function 的 UI 表单 | 由写脚本 + import 组合覆盖 | 🔁 | |
| 插件管理 / 自定义页面（开发者功能） | 不适用 | ⛔ | 这是改 labMaster 自身代码的开发功能，Kelvin 里没有对应运行面；理由：不是测量能力，且需要改文件系统里的后端代码，不应由 AI 在会话里做 |

## 四、History / 数据

| labMaster 功能 | Kelvin 对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| History 运行列表（run/blocks/params/datasets/status/时间） | `labmaster_history(limit, query)` | 🆕 | 每 run 多行紧凑输出，给"下次测什么"做推荐用 |
| 单次 run 详情 | `labmaster_run_detail(run_id)` | 🆕 | 引擎 run（blocks/params/datasets）或 Kelvin 自己记录的测量 |
| 数据文件夹索引（数据集路径/大小/时间） | `labmaster_datasets(limit)` | 🆕 | 用 vendored `dataindex.read_folder`，不依赖索引线程 |
| 数据集目录内容（只读） | `labmaster_dataset_files(path)` | 🆕 | 只允许数据根目录之内 |
| History 清空 | — | ⛔ | 删人的实验记录，属于人自己的操作；Kelvin History 页有，不给 AI 一键清空 |
| manual groups / handoff token / 导出 | — | ⛔ | 这是 History 页给人做的分组与导出工作流；AI 需要数据时直接给 `datasets` 路径即可，绕开 token |
| 分析出的图/文件 | `labmaster_analysis_status`（out/tail/n_figures/records）+ Kelvin `list_folder`/`read_file` | 🔁 | 图在数据目录里，Kelvin 原生文件工具能读 |

## 五、分析脚本（History 的 Scripts tab / my_analysis）

| labMaster 功能 | Kelvin 对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| 分析脚本列表（id/params/类/问题） | `labmaster_analysis_scripts` | 🆕 | id 形如 `my:0:count_rows.py` |
| 新建分析脚本（"New script"，模板） | `labmaster_write_analysis(name, code, overwrite, confirm)` | 🆕 | AI 直接写 my_analysis.dirs[0]；拒绝重名隐藏模块/覆盖需 confirm；模板列表在 `analysis_scripts` |
| 运行分析（选 dataset/groups，PARAMS） | `labmaster_run_analysis(script, dataset, run, params)` | 🆕 | dataset 可传一个路径/列表/`;` 分隔；也可传 panel run id |
| 任务状态/结果 | `labmaster_analysis_status(job_id)` | 🆕 | 引擎 `analysis_status()`：state/done/total/items/out/tail/figures |
| 取消分析 | `labmaster_analysis_cancel(job_id, confirm)` | 🆕 | confirm；不碰测量 |
| 分析结果比较/卡片/History 记录 | 引擎自己记录（`/api/datafolder/analyses`），Kelvin History 页可见 | 🔁 | 状态里已带 records/n_figures |
| 分析 job 的历史卡片（analyses_get/figures 路由） | 不单独暴露 | ⛔ | 纯 UI 展示；结果文件在数据目录里，原生文件工具可读 |

## 六、日志 / 运行记录

| labMaster 功能 | Kelvin 对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| Log 面板（引擎日志） | `labmaster_log(lines)` | 🆕 | 时间/级别/块 uid/文本 |
| 测量 stdout（runner 输出） | 同 `labmaster_log`（session.log 同一来源） | 🆕 | 引擎自己的 log buffer 已含 runner 行 |
| 事件记录（approvals/commands） | Kelvin lab record：`recent_events` / `activity` / `run_provenance` | 🔁 | Kelvin 原生更全（gate 的审批与命令记录） |

## 七、协作 / AI / 通知 / 其他

| labMaster 功能 | Kelvin 对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| 多人共享 Chat（显示名/颜色/回复引用/链接） | Kelvin Team 协作聊天（v1 已换成 labMaster 式）+ `team_chat` | 🔁 | 原生覆盖，数据在 `~/Kelvin/team/chat.json` |
| `@agent` 唤醒 AI | Kelvin 团队聊天 `@agent`（v1 接线 `web/team_agent.py`） | 🔁 | 与 labMaster 行为一致 |
| `@notify` / ntfy 推送 | Kelvin 自己的通知（run 完成/失败/watch 报警，`watch`） | 🔁 | labMaster 的 ntfy 是它那套通知；Kelvin 用自己的 |
| Agent box（面板自带 agent，`panel_agent.py`） | Kelvin 主 AI 本身 | 🔁 | 这就是本轮融合的目的：AI 由 Kelvin 提供 |
| Settings：paths、sim/real、密码、语言、安全 | Kelvin Settings 页（人）+ `lab_config`（读实验配置） | ⛔ / 🔁 | 配置是人的：AI 可读（`lab_config`、`labmaster_datasets` 给出 data root），不给写 |
| open folder（Explorer 打开） | 人在 Kelvin 页面点击 | ⛔ | 打开资源管理器是人的桌面操作，不是测量能力 |
| 远程访问（tailnet / 手机浏览器） | Kelvin 自己的门户（局域网/手机/QR，`web/qr.py`） | 🔁 | 两套都是"用手机看实验室"，Kelvin 用它自己的 |
| 多语言（EN/中文/越南语/罗马尼亚语） | Kelvin 门户自己的语言设置 | ⛔ N/A | UI 层功能，与 MCP 无关 |
| 手机听写 | 浏览器/系统功能 | ⛔ N/A | 与 MCP 无关 |
| 安全 notes / Safe stop 脚本 | `labmaster_control("safe_stop", confirm)`；safe-stop 脚本配置是人的 Settings | ✅ v0/🔁 | 停机动作能由 AI 触发（需 confirm）；脚本内容归人 |
| 版本 / 系统自检 | `labmaster_status`（引擎版本）+ Kelvin `kelvin_help` | ✅ | |
| 扫描/驱动仪器（PyVISA 直连脚本） | 脚本由 Sequence engine 的 runner 跑；v2-A（engine 队友）把 runner 的仪器命令接进 Kelvin SafetyGate | 🔁 | MCP 侧只负责"写/加/跑"；安全门在 runner 链路上，跨模块 |

---

## 八、v2-C 新增的 22 个 MCP 工具（清单）

签名与 READ/ACT、confirm 规则（全部 `structured_output=False`、返回可读 str、失败不抛）：

| 工具 | 注解 | 说明 |
| --- | --- | --- |
| `labmaster_move_block(uid, index)` | ACT | 移动块 |
| `labmaster_duplicate_block(uid)` | ACT | 复制块，返回副本 uid |
| `labmaster_clear_sequence(keep_done=False, confirm=False)` | ACT | 清空队列（confirm） |
| `labmaster_saved_save(name, overwrite=False, confirm=False)` | ACT | 保存当前序列（覆盖 confirm） |
| `labmaster_saved_load(id="", name="", append=False, replace=False, confirm=False)` | ACT | 加载已存序列（replace confirm） |
| `labmaster_saved_delete(id="", name="", confirm=False)` | ACT | 删已存序列（confirm） |
| `labmaster_write_script(name, code, overwrite=False, confirm=False)` | ACT | AI 写测量脚本到 scripts/，返回自动发现参数（覆盖 confirm） |
| `labmaster_read_script(path)` | READ | 读脚本源码 + 参数 |
| `labmaster_function_update(id, name="", note=None, params=None, color="")` | ACT | 改函数名/note/默认参数/颜色 |
| `labmaster_function_duplicate(id)` | ACT | 复制函数 |
| `labmaster_function_remove(id, confirm=False)` | ACT | 删函数（confirm） |
| `labmaster_history(limit=20, query="")` | READ | 引擎 History（blocks/params/datasets） |
| `labmaster_run_detail(run_id)` | READ | 单 run 详情（引擎或 Kelvin 记录） |
| `labmaster_analysis_scripts()` | READ | 分析脚本 + 模板 + 目录 |
| `labmaster_write_analysis(name, code, overwrite=False, confirm=False)` | ACT | AI 写分析脚本（覆盖 confirm） |
| `labmaster_run_analysis(script, dataset=None, run="", params=None)` | ACT | 跑分析（引擎 analysis_run） |
| `labmaster_analysis_status(job_id="")` | READ | 分析任务状态/结果摘要 |
| `labmaster_analysis_cancel(job_id="", confirm=False)` | ACT | 取消分析（confirm） |
| `labmaster_datasets(limit=20)` | READ | 数据文件夹索引 |
| `labmaster_dataset_files(path)` | READ | 数据集目录内容（只读） |
| `labmaster_log(lines=50)` | READ | 引擎日志 |
| `labmaster_home()` | READ | Home 页 tile + AI 卡片数 |

另：这 16 个"不碰仪器"的新工具已加入 `web/agent.py` 的 `OFF_LAB`（不吃 lab 锁：脚本/函数库/History/分析/数据/日志/Home）；
序列自身的编辑（add/update/delete/move/duplicate/clear/control/import/saved_*）仍吃 lab 锁，因为它们改变"要跑什么"。

**上下文安排（最终）**：34 个 `labmaster_*` 里 **11 个常在**（每个 chat 每步都在工具表里）：`labmaster_status`、
`labmaster_functions`、`labmaster_history`、`labmaster_add_block`、`labmaster_update_block`、`labmaster_import_script`、
`labmaster_write_script`、`labmaster_control`、`labmaster_rename_sequence`、`labmaster_home_add_card`、
`labmaster_home_remove_card`；**其余 23 个进 `DEFERRED`**（`web/agent.py` 的 purpose 组 "labMaster's saved sequences,
functions, analysis and data"），用 `find_tools` 或**直接按名字调用**时自动加载（`web/agent.py:1290` 的既有逻辑），加载后
在同一 chat 里一直可用。AI 仍然"看得见/做得到"全部功能，只是不必每步都背着它们的 schema。

另外 `labmaster_control` 补齐了 FAILED 块后的 Start 选择：`failed="retry"|"skip"` + `failed_uids=[...]`（与 Sequence
页问人的 Retry/Skip 同一个语义，透传给 `ENGINE.start(failed=..., failed_uids=...)`）。

## 九、已知缺口（v0.1 不改，记录在此）

1. **History 清空 / manual groups / 导出**：刻意不暴露（人的记录与 UI 工作流，见上表理由）。
2. **函数库"整库整写"的直接接口**：AI 用 update/duplicate/remove/import 单条改，不提供整表回写（防误删）。
3. **分析结果图片的专门读取工具**：状态里有 out/tail/n_figures/records；图片文件本身走 Kelvin 原生 `list_folder`/`read_file`。
4. **labMaster 的 panel_agent（自带 agent）** 不再需要：AI 由 Kelvin 提供。

## 十、工具表体积（最终实测）

按 chat 每步实际发给模型的 `{name, description, schema}` 序列化（`ChatAgent` 的 `_lab_tools`，v2-C + 延迟加载后实测；
tokens 按 4 字符/token 估）：

| 项目 | 数值 |
| --- | --- |
| MCP server 注册的工具总数 | **118 个**（chat 的 `agent.tools` 为 117：`MCP_ONLY` 的 `remember_shared` 只在 agent 侧） |
| chat 首步工具表（未 `find_tools`） | **57 个工具 / 45,657 字符 ≈ 11.4k tokens/步** |
| 其中 `labmaster_*` 常在 | **11 个 / 8,731 字符 ≈ 2.2k tokens** |
| 不在表里的 `labmaster_*` | **23 个 / 13,448 字符**（`find_tools` 或按名字调用时加载，之后仍在） |
| 若 34 个全常在（改造前） | 80 个工具 / 59,105 字符 ≈ 14.8k tokens/步 |
| **本改造每步省** | **13,448 字符 ≈ 3.4k tokens**（系统提示的 "# More tools" 为这组多一行 602 字符 ≈ 150 tokens，净省 ≈ 3.2k tokens/步，约 22%） |

若之后还要省：`labmaster_*` 的 description 占 11.7k 字符，精简 docstring 是下一个杠杆；schema 本身（9.2k 字符）多是
参数名/类型，砍不动。改动分布见 `web/agent.py` 的 `DEFERRED` 字典（新增一组）与 `tests/test_labmaster_mcp.py` 里
`ALWAYS_ON_LABMASTER` / `DEFERRED_LABMASTER` 的断言。


