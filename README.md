# cczh — Claude Code 显示层翻译

**模型全程用英文思考与回复（省 token），你在屏幕上直接读中文。**

> **English**: cczh keeps Claude Code's model thinking and writing in English
> (which costs fewer tokens), then translates the streamed output into Chinese
> on the display layer only — via the official `MessageDisplay` / `PreToolUse`
> hooks and a tiny local daemon. Context, transcripts, and code stay in English;
> only what's painted on your screen becomes Chinese. Falls back to English
> automatically whenever translation is unavailable.

纯钩子实现：不碰 API 链路、不改 Claude Code 本体。模型的上下文、会话存档、代码全部保持英文原样，翻译只发生在"渲染到屏幕"这最后一层。

```
你输入中文 ──────────────────────► Claude Code（上下文全程英文）
                                   │ ~/.claude/CLAUDE.md 约定: 英文回复 → 省 token
Claude 的英文回复（流式，按批）
      │
      ▼ MessageDisplay HTTP 钩子
本地服务 127.0.0.1:8795（DeepSeek，~1s/批）
      │
      ▼
屏幕上你看到中文
```

## 为什么能省 token

模型按 token 计费，而**同一个意思，中文在分词器里通常比英文贵 1.5~2 倍**。

| 层 | 内容 | 本工具的作用 |
|---|---|---|
| 模型输出 | 回复、thinking | **大头**：输出 token 单价最高、永不缓存 → 约定英文回复 |
| 历史回放 | 每轮重发的对话 | 英文回复攒的历史小 ~40%，会话越长省越多 |
| 屏幕显示 | 渲染文字 | 随便翻——不耗模型任何 token，只耗翻译 API（每天几厘钱） |

省 token 幅度取决于模型的分词器：对中文低效的分词器（如 Claude 系）收益明显；对中文优化过的分词器收益较小，但英文回复通常仍略省，且显示层翻译对任何后端都有效。

## 功能

- **回复翻译**：助手正文流式翻译成中文，带上下文保证术语前后一致
- **代码绝对安全**：``` / ~~~ 围栏整行放行，从不送给翻译模型
- **提问弹窗翻译**：Claude 用 AskQuestion 弹选择题时，问题/标题/选项全部翻成中文（PreToolUse 钩子在弹窗前改写参数）
- **`/btw` 旁路提问**：浮层不走显示钩子，通过 CLAUDE.md 指令让模型直接用中文回答
- **历史会话补翻**：`cczh zh` 把任意旧会话存档翻成一份中文 Markdown
- **全程安全回退**：翻译服务挂掉、超时、API 报错 → 自动显示英文原文，会话零影响
- **开机自启**：launchd 管理，登录即启动、崩溃自动拉起

## 安装

前提：macOS + Python 3（仅标准库）+ Claude Code + 一个翻译 API key。

```bash
git clone https://github.com/RuifengFu/cczh ~/project/cczh && cd ~/project/cczh
./cczh install
```

`install` 做的事（全部可被 `uninstall` 撤销）：

1. 找 API key（环境变量 `$DEEPSEEK_API_KEY` → `~/.zshrc` 的 export 行），写入 `~/.config/cczh/env`（权限 600）
2. `~/.claude/settings.json` 合并写入两个 HTTP 钩子（改动前自动备份为 `settings.json.bak-cczh`；**已有钩子绝不动**）
3. `~/.claude/CLAUDE.md` 追加英文回复约定块（带 `cczh:begin/end` 标记）
4. 写 launchd 配置 `~/Library/LaunchAgents/com.cczh.translator.plist` 并启动
5. 链接 `cczh` 命令到 `~/.local/bin/`
6. 跑一遍自检（含真实翻译）

装完后**新开的 Claude Code 会话**即生效（钩子在会话启动时加载，旧会话需重启）。

## 配置

`~/.config/cczh/env`（KEY=VALUE 格式）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `CCZH_API_KEY` / `DEEPSEEK_API_KEY` | 翻译 API key（前者优先） | 无 |
| `CCZH_BASE_URL` | OpenAI 兼容端点 | `https://api.deepseek.com` |
| `CCZH_MODEL` | 翻译模型 | `deepseek-chat` |
| `CCZH_PORT` | 本地服务端口 | `8795` |

翻译后端使用 DeepSeek。也可以在 `~/.config/cczh/env` 里手动改成任何 OpenAI 兼容端点（`CCZH_BASE_URL` + `CCZH_MODEL` + `CCZH_API_KEY`），改完 `cczh restart` 生效；切回 DeepSeek 用：

```bash
cczh backend deepseek
```

## 日常使用

装完之后正常用 `claude` 即可，无需任何操作。管理命令：

```bash
cczh status      # 服务在线？翻译了多少批、平均耗时、最近错误
cczh logs        # 服务日志（带时间戳）
cczh restart     # 改了配置/代码后重启
cczh stop        # 停止（claude 回到英文显示，其他不受影响）
cczh start       # 启动
cczh test        # 自检：本地分类逻辑 + 真实翻译 + AskQuestion 样例
cczh zh          # 补翻历史会话 → 中文 Markdown（默认当前目录最近一次，最近 40 轮）
cczh zh --all    # 全部轮次；cczh zh <存档.jsonl> 指定文件
cczh uninstall   # 撤销全部改动（只删自己的钩子，别人的配置不动）
```

## 实现方式

**文件**：

| 文件 | 职责 |
|---|---|
| `server.py` | 翻译守护进程（仅 Python 标准库）：HTTP 服务、分词分类、翻译、状态管理 |
| `cczh` | 管理命令：install / uninstall / backend / status / test / zh |

**两个钩子**（`~/.claude/settings.json`，均为官方 hooks API）：

| 事件 | matcher | 作用 |
|---|---|---|
| `MessageDisplay` | * | 助手正文渲染前 POST 到 `/hooks/message-display`，返回 `displayContent` 替换显示。**只改屏幕，不改存档和模型上下文** |
| `PreToolUse` | `AskUserQuestion` | 弹窗提问前 POST 到 `/hooks/ask-question`，返回 `updatedInput` 把问题/标题/选项翻成中文 |

**翻译流水线**（`server.py`）：

- 逐行分类：围栏行 / 代码行 / ASCII 图放行 / prose 翻译；围栏状态按 `message_id` 跨批次跟踪
- 段落级处理：空行是天然边界、不送翻、位置原样回填——模型吞行也只影响单段，段落结构永不丢
- 中文检测：已是中文的批次直接放行，不调 API
- 上下文一致性：每条消息维护"英文尾/中文尾"（各 280 字符）随请求送给译者
- 超长批次（>2000 字符）按行切块、最多 4 块**并行**翻译（连接池 4 条）
- **硬预算 17s**（钩子上限 20s 留余量），超时立即放弃 → Claude Code 自动回退英文
- 4xx 不重试（402/429 重试无意义）；网络错误/5xx 重试一次
- 每批要求译者保持行数一致；不一致时保底拼接、不丢内容

**英文回复约定**（`~/.claude/CLAUDE.md` 的 `cczh` 标记块）：

- 正文（含 thinking）用英文——这是省 token 的核心开关
- 例外 1：`/btw` 旁路提问直接用中文（浮层不走 MessageDisplay，翻译不到，只能让模型源头说中文）
- 例外 2：用户明确要中文产出时（写中文文案、翻译成中文）该部分用中文

## 已知限制

- 每批显示多 ~1s（等翻译），长回复"一段一段"出现
- 灰色斜体的 thinking 块**不经过 MessageDisplay**，保持英文（钩子事件覆盖范围所限）
- 工具输出、文件内容、slash 命令结果本来就是英文/代码，不翻译
- 历史会话回看不会重新翻译（屏幕画过不重画），要读旧会话用 `cczh zh`
- AskQuestion 弹窗翻译后，你选的中文选项会以中文回到模型上下文（极小的上下文混入，无实际影响）
- 表格、跨批列表的翻译偶尔生硬（逐批独立翻译的代价）

## 故障排查

| 症状 | 处理 |
|---|---|
| 显示变回英文 | `cczh status` 看是否在线 → `cczh restart` → `cczh logs` 看错误 |
| 翻译腔重/术语错 | 属于模型发挥；可换后端 `cczh backend ...` 对比 |
| 402 余额不足 | 翻译 API 账户欠费，充值即自动恢复 |
| 某会话完全不翻 | 该会话启动早于安装/改配置——重启会话（钩子在会话启动时加载） |
| `/btw` 还是英文 | 该会话缓存了旧 CLAUDE.md，重启会话 |
| 翻译越来越慢 | `cczh status` 看平均耗时，`cczh restart` 重置连接池 |

## 卸载

```bash
cczh uninstall
```

移除：两个钩子（只删 cczh 的 URL，其他钩子保留）、CLAUDE.md 约定块、launchd 服务、`~/.config/cczh/env`、命令链接。进行中的会话重启后完全恢复原状。

## FAQ

**问：API 能察觉/这合法吗？**
答：全部改动都在官方公开的扩展点上——hooks 的设计用途就包括"转换显示文本"（官方示例有脱敏、去 markdown）；CLAUDE.md 是普通配置。API 只看到它本来就该看到的请求和它自己的英文产出；翻译发生在响应到达之后的本机。CLAUDE.md 里的说明是明着写给模型看的，透明而非隐藏。

**问：我输入的中文也翻成英文了吗？**
答：没有（输入翻译需要 API 代理，收益小、风险大，刻意不做）。你的输入仍按原样计费——但它通常是最小的一块，大头在输出和历史回放。

**问：为什么 /btw 和 AskQuestion 要特殊处理？**
答：它们是独立于主渲染路径的 UI。AskQuestion 可以在弹窗前用 PreToolUse 改写参数（已支持）；/btw 浮层没有对应的改写点，只能通过 CLAUDE.md 让模型直接说中文。

**问：翻译质量不稳？**
答：本工具把"绝不能翻错"的部分（代码、命令、路径）硬性隔离在翻译模型之外，只把自然语言送翻。语序和术语偏好问题属于翻译模型发挥，换后端（`cczh backend`）可对比。
