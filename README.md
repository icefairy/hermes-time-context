# hermes-time-context

[![Hermes Plugin](https://img.shields.io/badge/Hermes-plugin-blue)](https://github.com/NousResearch/hermes-agent)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

> 让 Hermes 的模型知道**现在几点**，以及**你上次说话是多久以前**。

一个几十行的 Hermes 插件，解决多轮对话里最容易被忽略的失真：

```
你（10月1日 09:00）：帮我看下这个脚本为什么报错
AI：……可能是环境变量没设。
你（10月2日 15:30）：改好了，但还有个小问题
AI：好的，基于刚才的报错信息……          ← 它以为还是"刚才"
```

消息本身不带时间，模型只能把两轮当成连续对话。装了这个插件之后，模型看到的是：

```
【当前时间】2026-10-02 15:30 周五（Asia/Shanghai）｜距上一轮：1 天 6 小时
改好了，但还有个小问题
```

---

## 特性

- 🕐 **现实时间**：北京时间 / 任意 IANA 时区，可自定义格式
- ⏱️ **间隔感知**：超过阈值的断档自动标注「距上一轮：1 天 6 小时」
- 🧠 **缓存友好**：只改**最后一条** user 消息，历史前缀逐字节不变，上游前缀缓存继续命中
- 🔒 **零侵入**：不碰系统提示词、不改持久化历史、不动 assistant / tool 消息
- 📊 **自带观测**：`/time-context stats` 统计命中率，用数据证明缓存没被伤
- 🪶 **无依赖**：纯标准库，`zoneinfo` 缺失时自动回退 UTC+8

## 为什么不做在网关层

很多人第一反应是"在 API 网关里统一加个时间戳不就完了"。这行不通，原因有三：

| | 网关层 | 插件层（本方案） |
| --- | --- | --- |
| 知道"上次交互是什么时候"吗 | ❌ 只知道本次请求时间 | ✅ 按 session 记录真实间隔 |
| 能区分"用户发的话"和"工具返回的结果"吗 | ❌ 很多协议里它们都是 `role=user` | ✅ 能，结构化判断 |
| 会污染历史消息吗 | ⚠️ 若逐条加时间戳就会 | ✅ 只改最后一条，历史零改动 |

更关键的是：Hermes 的消息在数据库里**其实带 `timestamp` 字段**，但它在
`PERSISTENCE_ONLY_MESSAGE_FIELDS` 里，发往模型前会被剥掉。网关侧要拿到它，
只能去解析 Hermes 私有的会话库格式——耦合度一下就上去了。

所以正确的位置是客户端。这也是 Hermes 官方把注入点定在「当前轮 user 消息」、
而不是系统提示词的同一个理由：**system prompt 保持字节稳定，缓存才成立**。

## 缓存代价：≈ 0

上游前缀缓存按 token **逐字节**比对前缀，任何位置变了，该位置之后全部失效。

| 注入位置 | 缓存后果 |
| --- | --- |
| 系统提示词开头（"Current time: …"） | 💀 每次请求前缀全变，命中率归零 |
| **每条** user 消息前 | 💀 历史前缀作废，等于每轮重建 |
| `role=user` 的工具结果上 | 💀 污染工具输出语义 + 破坏断点 |
| **只加最后一条 user 消息** | ✅ 历史逐字节不变，只多几十 token 的 prefill |

还有一个容易被忽略的细节：Anthropic 系的缓存断点（`cache_control`）最多 4 个，
Hermes 默认放在「system 静态前缀 / system 末尾 / 最后 2~3 条消息」。
如果给**每条** user 消息都加时间戳，这些断点会被不断"刷新"，效果等同于每轮冷启动。
本插件因此默认且强烈建议开启 `only_last_user_message`。

## 安装

```bash
git clone https://github.com/icefairy/hermes-time-context.git
cp -r hermes-time-context ~/.hermes/plugins/time-context
```

然后在 `$HERMES_HOME/config.yaml` 的 `plugins.enabled` 里加一行：

```yaml
plugins:
  enabled:
    - time-context
    # ... 你已有的其他插件
```

**重启 Hermes 进程**（网关用 `supervisorctl restart <worker>`，CLI 退出重进）。

> ⚠️ Hermes 的插件是 **opt-in**：只把目录放进去不会生效，
> 必须在 `plugins.enabled` 里显式列出，否则加载日志会出现
> `not enabled in config`。
>
> 目录名必须叫 `time-context`，与 `plugin.yaml` 的 `name` 保持一致。

## 配置

写在 `config.yaml` → `plugins.entries.time-context.settings`。
所有键都在 `plugin.yaml` 的 `config_schema` 里声明过，Desktop 的插件设置页会渲染成表单。

```yaml
plugins:
  entries:
    time-context:
      settings:
        tz: Asia/Shanghai
        gap_threshold_minutes: 30
        min_gap_tokens_alert: 0.3
```

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `format` | `【当前时间】{datetime} {weekday}（{tz}）` | 模板；可用 `{datetime}` `{date}` `{time}` `{weekday}` `{tz}` |
| `date_format` | `%Y-%m-%d %H:%M` | strftime 格式 |
| `tz` | `Asia/Shanghai` | IANA 时区；`zoneinfo` 不可用时回退 UTC+8 |
| `gap_enabled` | `true` | 是否追加「距上一轮」 |
| `gap_threshold_minutes` | `30` | 低于该间隔不追加（避免每轮都提示"刚刚"） |
| `inject_on_first_turn` | `true` | 新会话首轮是否也注入 |
| `only_last_user_message` | `true` | **请保持开启**，关闭会让历史缓存作废 |
| `min_gap_tokens_alert` | `0.0` | >0 时，未命中比例超阈值的请求写一条日志（0 = 关闭） |

### 换个风格

```yaml
format: "[2026-10-02 15:30 CST, Friday | last turn: {gap}]"
```

更简洁的一行版：

```yaml
format: "{date} {time} {weekday}"
gap_enabled: false
```

## 使用方法

安装后在会话里直接用斜杠命令：

```text
/time-context status     # 当前配置、已记录的会话数
/time-context preview    # 试算三种间隔下的前缀长什么样
/time-context stats      # 本进程缓存命中统计（看有没有伤到缓存）
/time-context test 90    # 按"隔 90 分钟"试算一次
```

`stats` 的输出大致是：

```
time-context 缓存观测（本进程累计）
  请求数：42
  prompt tokens：1,284,930
  命中缓存 tokens：1,201,447（93.5%）
  未命中偏高告警次数：0
```

跑几轮之后这个数字应该稳定在 90% 以上（取决于你的历史长度和上游）。
如果它明显下降，先检查 `only_last_user_message` 有没有被关掉。

## 它是怎么工作的

全部只用 Hermes 的**公开扩展面**，没有任何 hack：

```plain
请求链路（Hermes 内部，简化）
  messages 组装
    → prompt-cache 装饰（放置 cache_control 断点）
    → sanitize
    → api_kwargs
    → ★ llm_request 中间件  ← 本插件在这里改写最后一条 user 消息的 content
    → pre_api_request hook
    → 发往 provider
    → post_api_request hook  ← 本插件在这里统计 usage 里的缓存桶
```

- **`llm_request` middleware**：在 provider 请求 kwargs 上替换文本。
  它在缓存装饰**之后**执行，所以 `cache_control` 已经就位，插件只管改字符串，
  不新增内容块（不占新的断点位）。
- **`post_api_request` hook**：读 `usage.prompt_tokens` / `cache_read_tokens` 累计命中率。
- **插件 state**：`$HERMES_HOME/plugin-data/<ns>/state.json` 里按 `session_id`
  记录"上一次交互时间"，进程重启后仍然有效。
- **防误伤**：`role=user` 但 content 全是 `tool_result` 块（Claude 工具结果）的轮次直接跳过；
  字符串和内容块数组两种形态都保持原有结构；任何异常静默降级，绝不影响对话。

## 已知边界

- **只影响发往模型的内容**，终端里显示的仍是你的原文（这是设计，不是 bug）。
- **紧凑后的摘要轮**也会注入时间——这是期望行为，摘要也是模型生成的文本。
- 时间格式用的是本机时区的 IANA 名。Windows 上如果没有 tzdata，会静默回退到 UTC+8。

## 开发与测试

```bash
# 单元测试：纯函数 + 中间件行为，不启动 Hermes
python -m pytest tests/test_time_context.py -q

# 端到端：临时 HERMES_HOME + 真实 PluginManager 发现加载 + 真实中间件链
# HERMES_REPO 指向你的 hermes-agent 源码目录
HERMES_REPO=~/hermes-agent python tests/e2e_check.py
```

`e2e_check.py` 会断言：system 与历史消息未被改动、`cache_control` 保留、
第二轮出现「距上一轮」、`tool_result` 轮不注入。

## License

Apache-2.0
