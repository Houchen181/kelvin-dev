# 把 labMaster 并进 Kelvin：这次做了什么、踩了哪些坑、后面怎么走

写于 2026-10-30。分支 `integration/labmaster`，三个提交（v0 桥接 → v1 进程内融合 → v2 安全门/打包/全功能 MCP）。
这份文件是给人看的，不是宣传稿：做成的、没做成的、有代价的，都写在里面。

---

## 一句话

labMaster 的引擎现在跑在 Kelvin 进程里，8770 端口没了。Sequence 页是唯一的队列，History 独立成一页，
团队聊天换成了 labMaster 那种多人协作聊天；Kelvin 的 AI 有 34 个工具能看、改 labMaster 的序列、函数、
脚本、History、分析、数据和主页；任何人启动的序列，脚本里的仪器命令都要过 Kelvin 的安全门；
Windows 打包能跑起来，安装包还没做。

---

## 一、融合的基本原则

后面继续揉的时候也按这几条来，不然会越做越乱。

1. **Kelvin 是主线，labMaster 是能力。**
   不是两个 App 拼窗口，是把 labMaster 的东西变成 Kelvin 的一个页面、一组工具。反过来做（在 labMaster 里嵌 Kelvin）
   会同时失去两边的长处。

2. **一个概念只留一份。**
   队列、History、聊天、安全模型，各只有一个。曾经的"两套队列 + 两套聊天 + 两套历史"看着能跑，
   用起来会分不清哪条记录才算数，AI 也会问"我该操作哪个"。

3. **AI 是一等公民，但人的决定它不能替。**
   AI 可以读、可以改、可以写代码、可以加块；"让 AI 能不能直接跑"这个开关只有人能在 Sequence 页打开；
   会丢东西的操作（清空、覆盖、删除）AI 必须先问人；驱动样品变大的命令还是要人批。

4. **安全是执行路径的属性，不是界面的属性。**
   不管命令是 AI 发的、人点的、还是 labMaster 那块脚本自己发的，都走同一个 gate。
   只给"AI 的按钮"加确认、给"人的按钮"免检，等于没做安全。

5. **能原生就原生。**
   不用 iframe、不另起端口、不做第二个进程当中间人。前端直接进 Kelvin 的静态页，后端直接进同一个 Python 进程。

6. **AI 写的代码要放进实验室的目录里。**
   写进 `~/Kelvin/sequence/scripts/`（测量）或 `my_analysis/`（分析），能被参数自动发现、能被人打开看、
   能被人删掉。不要藏在会话缓存里。

7. **数据留在本地，动作留痕。**
   run、审批、命令、AI 改了什么，都在本机、都有记录。删除和覆盖要能说清"删了哪个、在哪"。

8. **一次只动一条主线，用测试兜底。**
   这次按"进程内 → 安全门 → 打包 → MCP 覆盖"分段做，每段都有测试；没有一次性大爆炸式重构。

---

## 二、这次做了什么

### 1) 架构：端口没了
- labMaster 0.1.7 的引擎（`panel_engine.py` 等 14 个文件）用 `tools/vendor_sequence.py` 复制进
  `src/labharness/sequence/`。脚本可重复执行：`--check` 会核对 sha256，重跑不会覆盖 Kelvin 的定制。
- 引擎在 Kelvin 进程里创建（`SequenceService` 单例）；数据在 `~/Kelvin/sequence/`（`KELVIN_SEQUENCE_DIR` 可改）。
- 原来的 HTTP 桥接 `web/labmaster.py` 换了实现：同名函数、同样返回形状，但内部直接调引擎，不再走网络。

### 2) 界面：4 个 tab，队列只有一处
- 最左 rail：Chat / Home / Sequence / History。
- Sequence 页：函数库、序列（可改名、arm 开关、块状态/uid/上下移/删除）、已存序列、导入 Python、
  紧凑 History（带 "View all →"）。聊天右侧那个窄的 queue 视图退役了（后端还在，只是不再展示）。
- History 页：全部 run（Kelvin 的 run 文件夹 + 引擎自己的 history + AI 测量记录），可按来源/状态/关键词筛，
  点开能看 plan、自动分析、`report.pdf`/`report.pptx` 下载和 "Open in Analysis"。

### 3) AI 的工具：从 11 个到 34 个
新增的覆盖了 labMaster 原来只有"人"能做的事：

- **序列**：加块/改参/删块/移动/复制/清空/改名/已存序列保存·加载·删除/10 种运行控制/失败块后 Retry·Skip。
- **函数库与代码生成**：`labmaster_write_script`（AI 直接写测量脚本，自动发现参数）、`labmaster_read_script`、
  导入已有 `.py`、函数改名/改参/复制/删除。
- **History**：`labmaster_history`（给"下一步测什么"做推荐）、`labmaster_run_detail`。
- **分析**：分析脚本列表、`labmaster_write_analysis`（AI 写分析代码）、跑分析、查状态、取消。
- **数据与日志**：数据集索引、目录内容、引擎日志。
- **主页**：`labmaster_home`（tile + 卡片）以及原有的加/删卡片。

工具很多会吃上下文，所以做了取舍：**11 个核心工具常驻**（status/functions/history/add_block/update_block/
import_script/write_script/control/rename_sequence/home 加删卡片），其余 23 个放在 `find_tools` 的延迟加载里
（直接按名字调用也会自动加载）。实测首步工具表从 59,105 字符降到 45,657 字符，**每步省约 3.2k tokens（≈22%）**。
完整的"功能 → 工具/替代 → 状态"对照表在 `docs/labmaster-mcp.md`。

### 4) 安全门（phase B）
这是这轮技术上最要紧的一块。

- Kelvin 本来就有一套给"实验室自己的脚本"用的机制：子进程带一个替身库（`kelvin_shim.py`），
  pyvisa/pyserial/socket 的仪器访问变成发给 Kelvin 的请求，由 `SafetyGate` 逐条检查/审批/回答。
- 这次把它接到了 labMaster 的 runner 上：只给**测量 runner**注入这套环境（`_spawn` 子类覆盖，没改 vendored 引擎），
  **safe-stop 脚本不注入**——停机必须随时能关输出，不能卡在审批上。
- 每个 block 运行首次要驱动仪器时弹一张审批卡，写清脚本名、仪器、限值，并说明"批准一次覆盖本 block run，
  后续命令仍逐条查限值/互锁/警戒，只是不再问你"。拒绝则 block 立刻失败；超限命令不经审批直接拒绝。
- 人按 Stop / 序列被停时，shim 会收到停止：`time.sleep` 提前返回、脚本收到一次 `KeyboardInterrupt`；
  失败或停止时会把这次碰过的 drive 拉回安全值，并写实验室记录（`sequence_block_start/end`、每条命令、每次审批）。
- 没有 gate 的旧路径零变化（有测试锁定）。打包版里也验过握手：子进程日志
  `KELVIN_IPC=True frozen=True mode=real`，实验室记录 `"gate": "in-process"`。

### 5) 协作聊天：换掉，不是加一套
- Kelvin 原来的 Team Chat 只有"名字 + 文本"，换成了 labMaster 的 peoplechat：显示名和颜色、回复引用、
  Markdown/链接、`@agent` 唤醒 Kelvin 主 AI（回答以 Kelvin 的身份、引用问句）、限流、归档、未读。
- 旧的 `~/Kelvin/team-chat.jsonl` 第一次打开时自动迁移，原文件改名 `.migrated`。
- 数据在 `~/Kelvin/team/chat.json`；`teamchat.py` 变成兼容层，旧调用方不用改。

### 6) 测量记录与命名
- AI 每次 `run_plan`/`run_script`/`run_recipe`/`queue_add` 成功，都会进 History（source=kelvin），
  并把默认的日期序列名改成 `<测量名>--YYYY-MM-DD`；AI 也可以 `labmaster_rename_sequence("laser check")`。
- 人手动在 Sequence 页跑的 block，由引擎自己的 history 记录，也在同一个 History 页里（source=panel）。

### 7) 打包
- `pyinstaller packaging/kelvin.spec --noconfirm`：`dist/Kelvin`，110.3 MB / 570 文件，
  `Kelvin.exe` 24.7 MB + 新增的 `KelvinRunner.exe` 6.6 MB。
- 为什么有 Runner：打包后 `sys.executable` 是 `Kelvin.exe`，引擎拿它去跑 `run_block.py` 会变成重启 App。
  所以加了第二个 exe，专门按 `-u <script> ...` 的方式跑 vendored runner（包括 `-u -c` 预检、执行 PYTHONPATH 里的
  `sitecustomize`，这是 shim 在 frozen 下能工作的关键）。`sequence/paths.py` 在 frozen 时自动指向它。
- 自检：`Kelvin.exe --self-test` exit 0（8.2 s），`KelvinRunner.exe --self-test` exit 0；起包 HTTP 200；
  frozen 下 SIM/real block、安全门握手、分析 runner 都跑过。构建/验证细节在 `PACKAGING.md`。

---

## 三、重要冲突，以及怎么解决的

| 冲突 | 当时的状况 | 怎么解决的 | 代价/遗留 |
|---|---|---|---|
| 端口撞车 | Kelvin 默认就是 8770，和 labMaster 一样 | 引擎进进程，Kelvin 独占端口；数据搬到 `~/Kelvin/sequence` | 旧的独立 labMaster 不能同时跑（一台机器一个 Kelvin） |
| 两套执行/安全模型 | labMaster 用子进程直接开仪器，没有 gate；Kelvin 有 gate | 保留 labMaster 的块和 runner，只把 Kelvin 的 shim/IPC 注入测量 runner；safe-stop 不注入 | 每个 block 首次驱动要一张卡；真实仪器上还没实测 |
| 两套队列 | Kelvin 队列（plan/step）和 labMaster 队列（script/wait 块）并存 | UI 只留 Sequence 页；聊天右侧队列退役；后端保留 | Kelvin 原队列后端与 Sequence 的语义仍需一次统一（见下一步） |
| 两套聊天/两个 agent | Kelvin Team Chat + labMaster Chat/`@agent` | 用 labMaster 的 peoplechat 替换 Team Chat，`@agent` 接到 Kelvin 主 AI | 手动实验事件"回传"给哪个 AI 还没定（见讨论区） |
| History 两个来源 | 引擎 history 和 Kelvin run 文件夹各一份 | 完整 History 页合并；AI 记录按 run_id 去重，run 文件夹优先 | 序列页紧凑 History 与完整页数据源仍略有差异 |
| 打包后的 Python | frozen 后 `sys.executable` 是 Kelvin.exe | 第二个 exe `KelvinRunner.exe` + `paths.frozen_runner()`；vendor 脚本把这段定制也生成出来，重跑 vendor 不会丢 | 厂商库/科学计算包多时需要改配实验室 Python（文档已写） |
| 工具太多吃上下文 | 34 个 labmaster 工具全常在，每步 +5.4k tokens | 11 常驻 + 23 延迟加载；实测省 3.2k tokens/步 | 第一次用延迟工具多一次加载（或直接按名字调用） |
| 测试污染用户目录 | 跑测试会写真实的 `~/Kelvin/sequence/state` | `tests/conftest.py` autouse 把引擎目录指到 tmp | 无 |

---

## 四、对两边核心功能的改动

**Kelvin**
- 新增：rail 4 tab、完整的 Sequence/History 页面、34 个 labMaster 工具、`/api/history/all`、
  `/api/labmaster/armed`、`SequenceService`、`scriptrun.ShimServer`、`sequence/gate.py`、`peoplechat.py`、
  `web/team_agent.py`、`packaging/kelvin_runner.py`。
- 改动：`web/labmaster.py` 从 HTTP 桥变进程内门面；`web/app.py` 建服务并挂 gate；`teamchat.py` 变兼容层；
  `web/agent.py` 的 `OFF_LAB`/`DEFERRED` 加了新工具；`packaging/kelvin.spec` 双 exe；
  `tests/conftest.py` 隔离引擎目录。
- 没有动：gate 本体、run engine、仪器卡、审批卡语义、既有 MCP 工具的行为。

**labMaster**
- 源码一行没改。它的引擎/runner/UI 代码被复制进 Kelvin 的 `sequence/`（sha256 清单在 `VENDORED.json`，
  重新生成只能走 `tools/vendor_sequence.py`）。
- 在融合版里：**不再需要它的端口、页面、自带 agent、ntfy 通知**；它的 HTML/CSS/JS 只在移植团队聊天时参考过。
- 数据迁移：旧 `state/history.json`、`blocks.json`、`saved_sequences.json`、`scripts/` 已复制进 `~/Kelvin/sequence/`；
  旧团队聊天自动迁移。

---

## 五、验证（都是真跑出来的）

- 测试：**230 passed**（sequence service、安全门、桥接/门面、MCP 工具、web/webapi、peoplechat、
  team/analysis、agent、tool_loading、lab_setup、script_runs）。
- 安全门：9 个用例（一次审批覆盖 block、拒绝、超限无卡直接拒、Stop 在 0.3 s 内生效、safe-stop 不进 gate、
  旧路径不变、真实 `create_app` 端到端）。
- UI：CDP 真点击 — Sequence 27/27、History 22/22、Team 29/29。
- MCP：真实引擎上跑通"AI 写脚本 → 自动发现 4 个参数 → 导入成函数 → 加块 → 移动/复制/删除 →
  保存/加载/删除已存序列"；以及"AI 写分析脚本 → 在 dataset 上跑 → 轮询到 analysis done"。
- 打包：110.3 MB，`--self-test` exit 0，HTTP 200；冻结产物 PYZ 里核对到全部 34 个工具与门面函数；
  frozen 下 SIM/real block、安全门握手、分析 runner 都验过。

---

## 六、建议下一步（按优先级）

1. **上真仪器验证安全门**：限值、互锁、警戒读数、斜坡、拒绝、Stop、失败后回安全。现在是 virtual lab 证明的逻辑，
   真实仪器上还没跑过——这条没做完之前，不应该拿来跑真样品。
2. **安装器**：本机没有 Inno Setup，装好 ISCC 后 `kelvin.iss` 不用改就能连 `KelvinRunner.exe` 一起打进去；
   之后是签名、WebView2 依赖检查、卸载流程。
3. **事件回传 / AI 可见性**（你提的那条，见讨论区）：人手动启动/停止序列、block 完成/失败，
   要不要作为事件让 Kelvin 的 AI 看到；如果看，放在哪里看。
4. **失败块 Retry/Skip 的端到端**：工具参数已经透传，但"真的失败一个块、再让 AI 带 retry 启动"还没在真机上跑过。
5. **工具表继续瘦身**：description 现在 11.7k 字符，砍一半能再省 ~1.5k tokens/步；分析类工具可再分组。
6. **History 数据源统一**：Sequence 里的紧凑 History 和完整页的数据源还不完全一样，适合并成一套查询。
7. **跨进程锁**：独立 `kelvin-mcp` CLI 和 Kelvin 门户都指向同一个 `~/Kelvin/sequence` 时，现在靠"一台电脑一个 Kelvin"
   的约定挡着，新引擎上没有自己的锁。要么加锁，要么在 CLI 里直接拒绝。
8. **macOS 打包**：spec 结构支持，但没构建验证过。

---

## 七、留给后面讨论的问题（你提的 + 我们遇到的）

**A. 手动操作和实验事件，给哪个 AI 看？**
- 选 1：只给 Kelvin 主 AI 的会话上下文里加一段"最近发生了什么"摘要（不打扰，AI 下次回答时知道）。
- 选 2：作为系统消息发到团队协作框（所有人能看到，但会刷屏，而且不一定是给 AI 的）。
- 选 3：两者都做——协作框里只放"人需要知道"的（开始/结束/失败），AI 的上下文里放更细的（每块）。
- 还可以考虑像 Claude Desktop 的子任务完成那样：只在"AI 关心过这件事"时才回传（例如它刚建议过这个测量）。

**B. 消息归属**：团队聊天里的消息，是所有人共用的；AI 的回答是"Kelvin"身份的。当人在 Sequence 页手动点了
Start，算不算"Kelvin 的动作"？记录上应写人还是平台？这会影响 History 里的归因和以后的审计。

**C. 两个 AI 的边界**：Kelvin 主 AI（in-app chat）和外部 MCP client（Claude Code/Codex）都能操作同一个 lab。
现在的约定是"一个 AI 一个审批边界"，但同一个 lab 里同时被两个 AI 操作时，谁先谁后、谁看得见谁的动作，还没规则。

**D. 工具可见性的度**：34 个工具里 11 个常驻、23 个延迟。如果你希望 AI"什么都能直接看到"，就全常驻
（每步多 3.2k tokens）；如果你更在意长对话和小模型，就保持现状。可以做成设置项，让每个实验室自己选。

---

## 附：怎么跑 / 文件在哪

```powershell
# 起 Kelvin（labMaster 引擎在里面，不用再起 8770）
cd D:\Codex\Kelvin\kelvin-dev
.\.venv\Scripts\kelvin --port 8791 --no-browser --no-setup --no-accounts
# 打开 http://127.0.0.1:8791/#<access_key>（key 在 ~/Kelvin/settings.json）
# 深链：?view=home / ?view=seq / ?view=history

# 测试
.\.venv\Scripts\python -m pytest tests/test_sequence_gate.py tests/test_sequence_service.py `
  tests/test_labmaster_mcp.py tests/test_peoplechat.py tests/test_web.py -q

# 打包
.\.venv\Scripts\pyinstaller packaging\kelvin.spec --noconfirm
dist\Kelvin\Kelvin.exe --self-test
```

- 引擎与数据：`~/Kelvin/sequence/`（state、data、scripts、my_analysis、notes）
- Kelvin 自己的 run：`~/Kelvin/labs/<lab>/runs/`
- 团队聊天：`~/Kelvin/team/chat.json`
- 设计/规格：`V1_SPEC.md`、`V2_SPEC.md`、`packaging` 说明见 `PACKAGING.md`
- labMaster 功能对照：`docs/labmaster-mcp.md`
- 代码地图：`sequence/`（引擎+安全门）、`web/labmaster.py`（门面）、`agent/server.py`（工具）、
  `web/app.py`（路由）、`static/`（前端）、`peoplechat.py` + `web/team_agent.py`（聊天）
