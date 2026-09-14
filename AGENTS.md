# cyRouter 项目约定

## 费用显示格式

统一使用 **`¥理论总价(¥实付)`** 格式：

- 理论总价：按标准定价计算，不计套餐折扣、缓存折扣，保留 4 位小数 → `¥x.xxxx`
- 实付：扣掉套餐/缓存优惠后的实际开销，保留 2 位小数 → `¥x.xx`
- 当理论总价 = 实付时，只显示 `¥x.xxxx`

示例：

- `¥0.0150(¥0.01)` — 理论 0.0150 元，实付 0.01 元
- `¥0.0050` — 无优惠，两个数一样

## auto 虚拟模型

客户端请求 `model: "auto"` 时，路由器按消息长度 + 总上下文大小 + 是否有
工具自动选档（simple/complex），图片输入自动路由到 vision_model
（三种格式都识别：Anthropic `image` / OpenAI `image_url` / `input_image`
——2026-08-13 修复：此前只认 Anthropic 格式，OpenAI 图片请求被误投到
纯文本模型报 400）。
所有选档尊重健康检查（熔断器 + 失败冷却）和 fallback 配置。
（2026-08-13 已删除 auto/simple|complex|vision|code 显式子路由——零客户端
使用，且 auto/code 与 auto/complex 行为重复。）

**当前档位配置（2026-09-11）**：simple=qwen3.8-flash（fallback=
glm-5.3）、complex=glm-5.3（fallback=qwen3.8-max）、vision_model=
glm-5.3-flash（vision_fallback=glm-5.3-flash；kimi-k3 订阅过期后从
vision 档移除，volcano/zhipu 两家的 glm-5.3-flash 实测都收图）。

**图片轮次的降级链**（2026-08-29 事故修复：kimi-k3 配额耗尽后图片请求
fall through 到纯文本 glm-5.3 → zhipu 400 code 1210
"messages.content.type 参数非法，取值范围 ['text']"，图片是硬约束，
绝不能盲降）：

1. `auto.vision_model`（健康**且支持图片**时直接用——2026-09-11 补上能力
   检查：kimi 过期后 vision_model 被误指纯文本 glm-5.3，第 1 步只查健康
   不查能力，图片盲发到 volcano-apig 吃 400 code 1210；现在纯文本
   vision_model 会被跳过并打 WARNING 提醒改配置，图片是硬约束，
   任何一步选模型都必须验 vision 能力）
2. 档位候选里的 vision-capable 模型（`_model_supports_vision`，与
   `/api/models` capabilities.vision 同一解析链：model_overrides >
   model_meta > 名字启发式，两侧永不漂移）
3. `auto.vision_fallback`（新配置，当前=glm-5.3-flash）：若它支持图片
   则带图直发；若是纯文本模型则**剥图降级**（`_strip_image_blocks` 把
   image 块替换成「[图片内容已省略…]」占位符，日志
   `degraded to vision_fallback … stripped N image block(s)`）
4. 全部不可用 → 503 明确报错（ truthful，不再让上游报莫名其妙的 400）
实测结论：zhipu **glm-5.3-flash 支持图片**（红/蓝/绿三色 1×1 PNG 全对，
model_meta vision=true 正确），**glm-5.3 不支持**（事故源）。所以 kimi-k3
冷却时图片实际路由到 glm-5.3-flash 真识图，不丢图。

**auto 的模型级健康 = 额度标记 + 链全灭判定**（2026-08-13 晚重构）：
`_is_healthy` = 未中 `_is_model_failed` 额度冷却（insufficient_quota /
rate_limit_exceeded，15min）**且 `_model_has_live_provider` 为真**——后者
检查链上是否还有可用供应商（熔断 OPEN 或 (provider,model) 致命冷却的
跳过），**只在链上所有供应商全灭时才判模型不健康**。单供应商模型
（glm-5.3 = [zhipu-coding]）熔断时 auto 会切 fallback 模型（kimi-k3），
修复了「zhipu 熔断 → auto 仍选 glm-5.3 → 链上最后一家直接 503」的
 guaranteed-failure 循环；多供应商模型一家 OPEN 仍不跳档（避免当年
`is_model_open` 过宽 bug），瞬态故障优先由供应商链接住。**熔断器新增
只读 `is_open(provider, model)`**：健康检查专用，不做状态迁移、不消耗
 half-open 探测名额（不能用 `allow()`，它有副作用）。**配套
restore-to-primary**：会话粘在 fallback 模型上时，一旦规则选档回到
primary（即其链恢复），立即切回 primary，跳过粘性 TTL 和 cache-aware
——否则套餐模型对（stay ¥0 ≤ switch ¥0）会把会话永远钉在 fallback 上。
互备语义：5.3 主 → 全灭切 kimi → kimi 也全灭则 `_pick_model` give-up 回
5.3（吃 503 或等半开探测）。日志：`auto: restore A → B (primary healthy
again)`。

**auto 配额压力降权**（2026-08-26）：`auto.quota_pressure_threshold`
（默认 90，0 禁用）。`_model_quota_pressure(model)` 读 `_usages_cache`
（/usages 实测周池使用率，取链上有新鲜读数供应商的最小值）；规则选档
的 target 压力≥阈值且 fallback 无压 → 直接选 fallback（`auto: quota
pressure X≥90% → prefer fallback Y`）；粘性会话在压力下同样被释放
（粘性条件加「无压」）。无实测数据的供应商永不拖累其模型。阈值到期
/接口失败自动回到原有规则。

**请求内换模型重试（in-request reroute，2026-08-30）**：auto 请求遇
**致命错误且选中的模型链上无供应商可切**时，排除刚死的模型重跑一次
auto 选档并重试，发现故障的请求本身不再报错（此前只有下一个请求才
切换——2026-08-30 事故：kimi 周池 100% 吃 403 时 glm-5.3 恰在
900s 标记期内，发现请求把 403 透传给了客户端）。机制：
`_run_auto_reroute` 包装 OpenAI / Anthropic 两条 raw-proxy 路由
（路由函数只做 `request.json()` + 转发，原逻辑移入
`_openai_chat_once` / `_anthropic_messages_once`）；impl 把 auto 解析
结果写到 `request.state.cy_auto_resolved`。重试条件（全部满足）：
客户端请求 model=auto、响应 status≥400、resolved 非空、
`_is_healthy(resolved)` 为假（致命标记/链全灭——错误分类路径已把
模型标记死亡）、resolved 未重试过；每请求最多消化 3 个死模型后如实
返回最后一个错误。`_resolve_auto_model` 新增 `exclude` 参数——
从 avail 集合里剔除，统一作用于档位选档/vision 候选/粘性。客户端
钉死时不重试：`x-cyrouter-model-override` 或 `x-cyrouter-provider`
存在时 impl 清空 `cy_auto_resolved`。致命错误是 pre-content（上游
未产出任何内容），重试无副作用；已开始流式输出（status 200）的
断流不重试（那是 finish=length 合成路径的职责）。测试：
tests/test_auto_model.py 新增 exclude 分发 + 包装循环 11 例。

**`/api/models` 暴露 auto 虚拟模型**（2026-08-13）：让 pi 等客户端能选中
auto（此前两层都漏注册——cyRouter 端点不列、pi models.json 没加——pi
根本看不到 auto）。context_window / max_output_tokens 取**所有档位候选
（simple/complex + fallback）解析值的最小值**（`limit_source: auto-min`），
保证会话 fallback 到小窗口模型时 pi 的 auto-compact 阈值仍然安全；
vision=true 当且仅当配了 vision_model。pi 侧 models.json 同步加了 auto
条目（sync 只覆盖 contextWindow/maxTokens/input 三字段）。pi 重启后生效。

会话粘性（2026-08-13 重构为**档位序决策**）：同一 `x-cyrouter-session-id`
（或 `x-conversation-id`）的请求锁定到上次选中的模型，但**升级永远立即执行**
——规则选档档位（complex=1 > simple=0，`_auto_tier_of`）高于粘性模型时，
无视 TTL 直接跳档（日志 `auto: upgrade A → B (tier 0→1, …)`），修复了
「首轮被钉在简单档后后续长消息升不上来」的质量锁死。只有**平级/降级**
才享受粘性：TTL(10min) 内无条件留旧模型并刷新 TTL；TTL 过期才走
cache-aware 成本比较（见下）。粘性模型不健康或不在可用列表时直接按规则重选。
**客户端没发 header 时用派生 key 兜底**：hash(system prompt 前缀 2KB +
首条 user 消息前缀 2KB)——与 prompt cache 前缀语义对齐，追加消息同 key、
compact/新会话自动换 key。此前 pi/opencode 不发 header 导致粘性空转、
auto 每轮重选反复跳档，每次跳档上游前缀缓存全价重算。

**延迟感知路由（slow-mark，2026-09-14）**：慢而成功的上游对既有四层健康
信号（配额标记/断路器/链全灭/配额压力）**全部隐形**——都不是错误，只是慢。
实证：qwen3.8-flash 当日 6~32s 慢慢吞吞答对（pi 的 pre-ask judge 弹框等满
客户端超时），auto 照单全收。第三维健康信号：`_note_latency`（非流式成功
路径，4 个 _cb_ok 站点旁）采样完成耗时，门控看**响应侧实际输出 token**
（usage.completion_tokens / output_tokens ≤ `auto.slow_mark_max_output_tokens`
=512）且耗时 ≥ `auto.slow_mark_ms`=4000 → `_mark_model_slow`（`_slow_models`，
基础冷却 120s，复发指数升级 ×2 封顶 64min，静默 4×基础窗口后等级归 1），
`_is_healthy` 加 `not _is_model_slow` 层 → auto 自动滑到档位 fallback，
冷却到期后由既有 restore-to-primary 切回。设计约束：①门控必须看响应侧
usage 而非请求侧 max_tokens——`auto_elevate_max_tokens` 会原地改写 body
（8→32768），请求侧门控会被打穿；②只参与非流式站点——流式 TTFB 会被
thinking 模型合法的 60s+ 首 token 慢启动误伤（gate_timeout 90s 的存在
就是为此）；③无 usage 不标记（绝不猜）；④慢标记只影响 auto 选档，
客户端钉死模型不受影响；⑤`_note_latency` 永不抛异常（延迟健康不得
碰坏请求路径）。pi 侧配套：pre-ask judge 超时 3000ms 作纵深保险
（2026-09-14 8000→3000，网关健康时 judge ~2s 正常自动消化）。测试：
tests/test_auto_model.py 新增 11 例（标记/双 usage 形状/大输出豁免/
无 usage 豁免/零阈值禁用/配置覆盖/冷却过期自愈/升级与衰减/事故形状
端到端 reroute）。V1 已知边界：simple 档两候选同时被标记时
`_pick_model` 按既有 give-up 路径回落 primary（judge 场景两种路径同为
3s fail-open 弹框，结果中性；短标记先到期自愈分流）；实测 glm-5.3-flash
思考深度无界、512 预算偶被吃空重标自身（240s+），属自愈循环非死锁。

**glm-5.3-flash 小预算空响应根因（2026-09-14 同日，勿重蹈）**：pre-ask judge
（maxTokens=8）当日 6/6 空响应（finish=length、content 空）。探针四组对照定论：
volcano-apig 对 glm-*-flash **完全忽略** `thinking: {type: disabled}`（三种
方言都试过），思考照发且**始终计入 max_tokens 预算**——短思考恰好挤进小
预算则 finish=stop，长思考（如 judge 分类题 90~300 token）必然截空。
曾误判为「字段存在翻转计账」并在网关加过 glm-flash 丢字段的 repair，
当日回滚：glm-5.3-flash 双供应商（volcano + zhipu-coding），zhipu 原生
路由该字段是生效的，丢字段会引入不请自来的思考。正解 = 调用方预算：
pi 侧 judgeAsk maxTokens 8→512（思考+答案都够，slow-mark 门控
slow_mark_max_output_tokens=512 仍能覆盖 judge 形态）。

选档除消息长度/工具外还纳入**总上下文大小**：`_estimate_request_tokens`
估算超 `auto.context_threshold_tokens`（默认 32768）强制 complex 档——
「继续」式短尾消息 + 巨型历史不会被误投到弱模型。

**cache-aware 重选**（`auto.cache_aware_routing`，默认开）：只作用于
**平级/降级且 TTL 已过期**的重选（升级不走成本比较，直接跳）。用 pricing 的
input/cache_input/output 对比「留旧模型（按 `est_cache_ratio`=0.85 热缓存价）
vs 跳 rule 选档（首轮全价冷缓存）」的单轮成本（输出按 `est_output_tokens`
=2048 估），选便宜的——缓存连续性远比模型单价差值钱（实测 cache_read
占 84~95%，cache_input 是 input 的 1/5~1/120）。`plan_monthly` 套餐模型
边际成本按 0 计（额度耗尽会被健康检查踢出候选，不在这里处理）。
图片输入是硬约束，不参与成本比较；模型无 pricing 时回退规则选档。
日志：`auto: cache-aware stay/switch …`。

## models — 以模型为准的有序供应商链

config.json 的 `models` 把每个模型映射到一条**有序供应商链**：第一个是
默认供应商，上游失败时自动按顺序切换到下一个（如套餐额度用完）。供应商
本身不再持有模型列表，模型链是路由的唯一事实来源：

```json
"models": {
  "qwen3.8-max": ["aliyun-token-plan", "aliyun-dashscope"],
  "qwen-max": ["aliyun-dashscope"]
}
```

- 链上的 provider 按名字引用、model id 原样转发，无需在 provider 里声明模型。
- 支持 glob 模型键（如 `"qwen3.8-*"`），同 `model_aliases` 语法。
- **`models` 是路由的唯一事实来源（2026-09-04 起）**：旧兼容层
  （provider 的 `models` 字段反查、`model_fallbacks`、`routes` 表、
  「未知模型默投 providers[0]」catch-all）已全部删除，未知模型一律
  404 `No provider for model`（OpenAI/Anthropic/Responses 三端点同）。
  旧模型 qwen-max/qwen-turbo/qwen3.6-flash/glm-5.1/gpt-5.6-luna 等
  未在 `models` 里的一律 404——要复活就显式加链。
- 回退触发条件：上游在**产出任何内容之前**出错（非 200 / soft-error /
  SSE in-band error / 连接超时 / 空流重试耗尽）。已经开始向客户端流式
  输出后无法回退。
- 额度耗尽等致命错误（`insufficient_quota` / `rate_limit_exceeded`）会把
  `(provider, model)` 标记失败，冷却期内直接跳到下一个候选，不再浪费
  一次失败请求。冷却按时长分级：额度耗尽 `provider_quota_cooldown`
  （默认 3600s，额度通常按账期恢复），限流 `provider_fallback_cooldown`
  （默认 900s）。
  **错误识别与 failover 决策已统一为单一架构（2026-08-14 重构，对齐
  LiteLLM）**：`_classify_upstream_failure(status/exc/body_text)` 是唯一
  分类入口，`_cb_fail` 是唯一决策表，全路径（raw OpenAI / raw Anthropic /
  SDK / gate-probe）接入。分类优先级：**结构化 `error.type`/`error.code`
  字段正则**（API 契约，比文案稳定；`quota|terminat|exhaust|billing|
  insufficient|payment` 族）→ 关键词表（`_ERROR_CLASSIFICATION`，仅文案
  兼害）→ HTTP 状态码。决策表：quota→供应商 3600s+模型标记；
  rate_limit→供应商 900s+模型标记；**auth(401/403)/not_found(404)→仅
  供应商 900s**（key/模型对该供应商死了，模型健康不毒化，LiteLLM 把
  401/404 列为 non-retryable）；server/timeout/network→熔断器（升级冷却）；
  其他 4xx→透传不动。历史教训：Kimi Code 计费周期额度耗尽是 403 +
  `access_terminated_error`，旧关键词表不认 → 永不标记 → auto 钉死
  kimi-k3 反复吃 403；结构化字段族正则后此类新错误零登记自动覆盖。
- 可选后台探测：config `"probe": {"enabled": true, "interval_seconds": 900}`
  开启后，定期向多供应商链的首个 provider 发最小请求，提前发现额度
  耗尽/故障并进入冷却（用户请求零感知），探测成功会提前解除冷却。
  每次探测消耗少量 token（thinking 模型注意 reasoning 开销）。
  `GET /api/probe/status` 查看当前冷却与最近探测结果。
- 覆盖路径：OpenAI 与 Anthropic 两个 raw-proxy 端点的 stream / non-stream；
  SDK 转换路径（ClaudeProvider 走 OpenAI 格式等）不做回退。
- Anthropic 端点只纳入支持 Anthropic 协议的候选（有 `base_urls.anthropic`
  或原生 Claude provider）；OpenAI-only provider 自动跳过。
- `GET /v1/routes/{model}` 返回 `providers` 可查看生效的供应商链顺序。
- UI：「模型列表」页管理模型→供应商链；「供应商」页只管连接信息。
- **per-provider 模型名映射（`model_map`）**：链上某个 provider 若需要不同的
  上游模型 id（例：cyRouter 面向客户端叫 `kimi-k3`，但 kimi 官方端点要
  `k3`），在 provider 里配 `"model_map": {"kimi-k3": "k3"}`。
  `_build_chain_candidates` 会把映射后的 id 透传给 SDK `ChatRequest` 与
  raw-proxy upstream，所以链 `["kimi code","opencode-go"]` 会给 kimi 发
  `k3-256k`、给 opencode-go 发 `kimi-k3`。`model_aliases`（全局改名）不同——它是
  先于路由的重命名，会作用到链上所有 provider。
- **provider 扩展字段**：`model_map`、`console_url`（dashboard 卡片渲染「控制台↗」
  跳转链接，如 kimi code 的 `https://www.kimi.com/code/console`）等直接挂在
  provider 对象上。`POST /api/providers` 是**合并语义**：UI 表单只管理连接字段
  （name/type/urls/key/proxy/console_url），表单没带的扩展字段从旧对象保留，
  不会被 UI 编辑静默丢掉；表单显式传 `null` 才删字段（如清空 console_url）。
  proxy 特殊：表单只在 provider 原本有 proxy 键或勾选时才显式发送 true/false，
  避免把「缺省=自动（国内直连/国外走全局代理）」误写成硬 false。
- provider 命名可以带空格（如 `kimi code`）：所有内部引用都是字符串字典键，
  统计按 URL 记账（daily_stats.by_provider），与名字解耦。改名只需同步
  `models` 链里的名字引用。

## 配置导入导出（`/api/config/export` · `/api/config/import` · `/api/config/clone`）

只导出**可移植**字段，本地安装相关信息永不离开本机：

- **导出**（白名单）：`providers`（含明文 API Key，便于在另一台机器恢复）、
  `models`、`model_meta`（2026-09-07 起——元数据是模型自身属性，跨机器可
  移植；丢了会让新机器所有模型元数据退化为名字启发式，kimi-k3 的 vision
  标记直接消失、pi 拦死图片输入）、`pricing`（2026-09-09 起——价格同样是
  模型固有属性；全新安装的默认 config 没有 pricing，不随导出走会让恢复
  后的实例所有成本显示 0/—。`plan_monthly` 一并随行：克隆/导入连 provider
  key 一起拿，骑的是同一套订阅）。
- **导出元数据信封 `__meta__`（2026-09-08）**：schema_version=2 /
  exported_at / source（版本号）/ machine / counts / warnings。旧格式
  文件（无 `__meta__`）仍可导入（视为 v1）。`__meta__` 永不写入本地
  config，只在 dry-run 响应里回显给 UI 展示备份时间/机器。
- **decrypt 失败安全化（2026-09-08 修 bug）**：导出时 provider 的 API key
  解密失败（.key 不匹配/密文损坏）→ 该 provider 导出为**空 key +
  warnings 条目**，而不再把密文标成明文（旧 `suppress()` 路径的密文会在
  导入时被**二次加密**，key 直接损坏；现有单测恰好用密文原样断言掩盖了
  此 bug）。导入后需手动补 key。
- **导入 dry-run diff 预览（2026-09-08）**：`POST /api/config/import?dry_run=true`
  返回结构化 diff（providers/models 的 added/changed/removed +
  model_meta 的 added/changed），不落盘。UI 导入流程：选文件 → dry-run →
  confirm 展示变更摘要（含「备份里没有的本地供应商/模型链将被删除」
  警告，防旧备份静默回滚）→ 确认后才真导入。diff 比较 provider 时剔除
  api_key/encrypted（本地是密文、文件是明文，恒不相等；key 变更不预览）。
- **跨实例克隆 `/api/config/clone`（2026-09-08）**：body `{source_url,
  source_key, dry_run}`，从另一台运行中的 cyRouter 拉取 `/api/config/export`
  并导入本地。典型用途：发布版（20108）从开发版（20100）同步配置，
  免去手动复制 config.json + .key（key 以明文传输后用本机 .key 重新
  加密）。直连 httpx 客户端（trust_env=False，防代理劫持 localhost）；
  源 401 → 明确报错；源旧版本（无 `__meta__` 的 v1 export）兼容。UI
  「系统设置 → 从其他实例克隆配置」。
- **导入语义**：`providers`/`models` 整份覆盖（文件版本赢——**导出后新加的
  模型链会被旧备份回滚掉**，dry-run diff 的 removed 警告就是防这个）；
  `model_meta` 与 `pricing` 按**模型粒度合并**（文件同名模型赢，文件没有的
  本地条目保留——旧备份不能删掉本地新模型的元数据/价格）。
- **永不导出 / 导入不覆盖**（本地保留）：`port`、`host`、`proxy`、
  `api_keys`（cyRouter 客户端密钥）、`proxy_prefix`、`auto`、
  `circuit_breaker`、`semantic_cache`、`probe`、`token_plan_quota`、
  `model_overrides`、`routes`。（`pricing` 2026-09-09 起已移入白名单，
  见上；`model_overrides` 里的 kimi-k3 240000 边距目前需手动同步。）
- ⚠️ **2026-09-04 之前的旧备份文件**：当时路由靠 provider 级 models 列表 /
  routes / model_fallbacks 兜底，顶层 `models` 链可能不全——导入到现行版本
  （models 是唯一事实来源、未知模型 404）后，当时藏在别处的模型会 404。
- `PUT /api/config`（UI 整份保存）同样不让客户端覆盖上述本地字段。
- 导入用白名单反取：旧导出文件里混入的本地字段直接丢弃，不写入。

## 内置价格目录（builtin_pricing.py，2026-09-10）

`BUILTIN_PRICING` 随 exe 分发 18 个模型的价格（含 plan_monthly），
与 `BUILTIN_MODEL_META` 同构：**config.pricing[m] 同名条目整体赢**，
内置只补空，永不写回 config、不随导出走（导出仍只导用户自己的
pricing）。背景事故：价格此前只存在于本机 config，全新安装的默认
config 是 `{port,host,providers:[],routes:[]}` —— 发布版实例 glm-5.3
成本显示 0/—，v1.2.0.5 的导出/克隆白名单只解决“配置搬家”，内置目录
才解决“开箱即有”。消费点全部走统一入口：`effective_pricing(cfg,m)`
（单模型）/ `effective_pricing_map(cfg)`（合并视图）：
router._entry_metrics（记账）、main._auto_turn_cost（cache-aware 路由）、
/api/stats（missing_pricing 现在只报真没价的模型）、/api/usage、
/api/models（逐模型 pricing + source 标签 + 顶层 builtin_pricing 供 UI
给未添加的模型显示价格）、/api.json。UI（web/index.html）用
`effectivePricingMap()` 合并显示（加模型下拉「未定价」标签、套餐供应商
分类）。新模型接入默认链或价格调整时改 builtin_pricing.py。

### 官网现价检查（pricing_sources.py，2026-09-10）

手动触发的定价页抓取器，**结果只进提示通道，永不自动生效**
（生效价永远是 config > BUILTIN_PRICING，爬错只影响提示不影响记账）。
当日实测四家：deepseek（Docusaurus 静态表）与阿里云（SSR，只取
「华北2-北京*」段，阶梯取首区间，限时折扣记原价+promo 标注）可抓；
zhipu（JS 渲染空壳）与 kimi（价格在模型子页）标 unavailable 如实呈现。
语义陷阱（也是拒绝无人值守全自动的原因）：deepseek 是 USD 分时价
（off-peak/peak 各半）、阿里云多地域不同价——verdict 只对 CNY 单一价
给 same/differs，区间/外币一律 info（展示原样，禁止一键采纳）。
端点：`GET/POST /api/pricing/check`（POST 触发抓取，20s 超时代理感知
follow_redirects，结果缓存 data/pricing_check.json）；`POST /api/pricing/adopt`
{model, from: "fetched"|"builtin"}——fetched 从检查缓存取官网价写入
config.pricing（仅 CNY 单值可采纳，服务端原子、不接受客户端传价）；
builtin=「跟随内置」（删用户条目价格字段保留 max_context，此后内置
目录更新自动跟进）。UI：模型列表页「检查官网现价」按钮 + 差异面板
（verdict 渲染时用最新 config 现算，采纳后立即变 same）。

**每日自动检查 + 护栏内自动采纳**（2026-09-10 第二版，用户显式开启）：
config `pricing_check = {auto, interval_hours=24, auto_adopt,
max_ratio_change=1.0}`。`_pricing_check_loop` 启动 5 分钟后首跑、
每 24h 一次 `_run_pricing_check(auto=True)`；未开 auto 时懒轮询配置。
**自动采纳护栏**（`_auto_adopt_from_check`，只碰「已有用户条目」的
模型）：① verdict=differs（CNY 单一数值）；② 有 promo（限时折扣）
跳过——促销是临时口径，采纳会固化折扣价；③ builtin 兜底的模型
（config 无条目）**不新建条目**——否则破坏「跟随内置」语义
（config 条目永远赢，内置更新就不跟进了）；④ 跳变熔断：
new/old ∉ [1/(1+r), 1+r]（默认 r=1.0，即最多翻倍/腰斩）拒绝——
官网改版提取错位时防污染；⑤ 保留 cache_input/plan_monthly/
max_context 等非价字段，只换 input/output；⑥ 采纳写日志
`pricing-check auto-adopt: …`，结果 payload 带 auto_adopted 明细，
UI 面板绿框展示，打开模型页自动渲染最新缓存（≤48h 且有差异时）。
测试：tests/test_pricing_auto_adopt.py 5 例。

**多机制冲突审计（2026-09-10，四套机制并行后例行检查）**，已修：
① **并发写丢失（真 bug）**：_run_pricing_check 在 fetch（~20s await）
之前取 config 快照，期间 UI 改动 save 后会被采纳路径的旧快照整份
回滚——修复：_auto_adopt_from_check 内部重新 load_config 取最新，
只动 pricing 字段；同名条目若已与官网价一致（用户恰好手改过）跳过。
② 手动检查与每日循环并发：_PRICING_CHECK_LOCK 串行化（fetch+
写缓存+采纳原子化）。③ 重启风暴：循环先看缓存新鲜度（< 周期 80%
跳过），一天多次 restart 不再重复抓。④ 模板重名：UI 预填后同名
预警（保存会更新存量连接信息，key 不动）。审计确认无冲突的交互：
导入/克隆回滚旧价 → 下日自动检查会按护栏改回（价格保鲜特性）；
pricing_check 配置不在导出白名单 → 目标机默认 auto=false 保守；
「跟随内置」与自动采纳互不侵犯（后者不新建条目）；limits 链与
pricing 链各自独立解析。

**第二轮冲突审计（2026-09-10 晚，v1.2.0.11 DeepSeek 同步后）**：
① **model_meta 用户条目遮蔽内置**（真冲突）：config.model_meta 的
deepseek-v4-pro/v4-flash 旧回填条目（vision=False）按「用户条目
整条覆盖」遮蔽了内置目录的新元数据——9/14 起旧名请求由 V4.1-Flash
服务（支持图），遮蔽会让 /api/models 报 vision=false、pi 拦死图片
输入。修复：删掉规格与内置完全一致的冗余用户条目跟随内置。
**模式教训：规格与内置目录一致的用户 model_meta/pricing 条目毫无
价值——不会改变任何行为，只会在内置更新时变陈旧遮蔽，定期清理。**
② 旧名 vision 保守策略：builtin 里 v4-pro/v4-flash 保持
vision=False——计费路由到 Flash ≠ 官方承诺旧名支持图（计费转正
≠能力转正），宁可拦死不误投，9/14 后实测再翻。③ v4-flash 链
[volcano-apig, deepseek] 保持现状（用户确认）：记账按官方 Flash
折算价，与网关实际扣费的漂移仅影响成本展示。④ 运维教训：改
**运行中服务**的 config.json 直写有并发覆盖风险（UI save 会用内存
快照整份回滚直写内容）——应走 PUT /api/config 或先停服务。页面结构
漂移时提取器抛 ExtractError → status=error，不猜。测试：
tests/test_pricing_sources.py（fixture 是真实页面最小结构切片）。

## 内置供应商模板（builtin_providers.py，2026-09-10）

全新安装没有任何 provider 配置，要自己翻文档找 base_url。模板把四家
常用供应商（智谱 GLM coding 套餐 / DeepSeek / Kimi Code / 阿里云百炼）
的连接信息随版本内置：endpoint（实测双协议）+ key 获取页 + 控制台
链接 + 计费说明 + 推荐模型链。`GET /api/providers/templates` 暴露；
UI 供应商页「从模板添加」弹窗选模板 → 预填表单（品牌/协议/双 URL/
console_url/key 提示链接）+ 推荐模型勾选区（默认全勾）→ 用户只填
API key，保存时自动补建模型路由链（**已有同名链不覆盖**）。

约束：**模板永不落 config**（与 BUILTIN_PRICING 运行时兜底不同——
provider 没有用户 key 就是死的，模板只在添加瞬间参与）；不含任何
key；suggested_models 必须都在 BUILTIN_PRICING 和 BUILTIN_MODEL_META
里（添加后零配置可记账）；volcano-apig 这类私有网关不内置。测试：
tests/test_builtin_providers.py 7 例（含模板无密钥字段断言、
suggested_models 与目录一致性）。UI 品牌下拉同步补了 aliyun/kimi/
volcengine option 与 PROVIDER_PROFILES 键（修复存量 provider 回显
时品牌 value 无对应 option 的显示错位）。

## 模型元数据（model_meta —— 自有格式，已放弃 models.dev）

`model_meta` 是每模型的**本地权威元数据**（config.json 顶层键），下游消费方：

- `/api/models` → cyrouter-sync 扩展 → pi 的 contextWindow / maxTokens（
  提前 compact 信号 + 输出上限）
- `/api.json` → opencode（limit 决定客户端回复上限）
- 上下文预检（`_context_preflight`，limit 走统一解析器，写 model_meta 即生效）

```json
"model_meta": {
  "kimi-k3": {"context": 256000, "max_output": 32768, "vision": true, "reasoning": true, "notes": "…"}
}
```

- **解析链**（`_resolve_model_limit`，逐字段取首个命中，2026-09-07 加内置层）：
  `model_overrides[m].limit` > `model_meta[m]`（用户条目，整条覆盖内置）
  > `builtin_meta.BUILTIN_MODEL_META`（**随版本内置的目录**，14 个模型，
  冷启动零回填；用户写了同名条目则内置完全不参与该模型——不是字段级合并）
  > `pricing[m].max_context`（遗留）> `_infer_model_limit` 启发式。每个值带
  per-field 来源标签（override/model_meta/builtin/max_context/heuristic），
  `/api/models` 的 `limit_source` 与 dashboard「本地/手动/内置/预检/推断」
  徽标可见。vision 解析（`_model_supports_vision` / capabilities.vision）
  同链同序。新模型或实测纠正限值时改 `builtin_meta.py`（随版本分发），
  本机临时覆盖用 model_meta / UI ⚙。
- **管理端点**：`GET/PUT/DELETE /api/models/meta/{model}`（PUT 写
  model_overrides[m].limit，null 清单项；DELETE 整个清空回自动）。UI
  模型列表页有 ⚙ 编辑按钮。
- **models.dev 已删除**（2026-08）：它几乎不覆盖 coding-API 模型
  （kimi-k3 等），拉取必须走代理（Servy 无 shell 代理 env），且启发式
  兜底错配过 kimi-k3（131072/8192）导致输出截断 bug。删除内容：
  `_refresh_models_dev_cache`、`/api/sync-models-dev`、启动 spawn；
  `/api.json` 与 `/api/models` 统一走解析链。
- **`/api/models` 的 `capabilities.vision` 也走解析链**（2026-08-10 修）：
  `model_overrides[m].vision` > `model_meta[m].vision` > 名字启发式
  （`-vl`/`vision`/`vl-`）。此前只看名字——kimi-k3 名字不含 vl，写了
  model_meta vision 也不生效，pi（cyrouter-sync 读 /api/models 的
  capabilities.vision 决定 `input: [text,image]/[text]`）据此拦死图片
  输入。`/api.json`（opencode）此前就读 model_meta，不受影响。
- **kimi-k3 视觉已打通**（2026-08-10）：实测 kimi code 编程端点收
  base64 图片（发 64×64 纯红 PNG 问颜色，正确答 "Red"，日志确认
  api.kimi.com/coding/v1 应答非 fallback）→ model_meta vision 置 true。
  上游要求：content 对象数组 + base64 或 ms://file-id，**不收公网 URL**。
  预检无需改：`_estimate_request_tokens` 对 image_url/input_image/image
  块按 1500 tok/张 定额估算，不碰 base64 字符。⚠️ pi 需重启才能在
  session_start 拉到新 vision 标记；`auto/vision` 仍走 qwen-vl-max 不变。
- **回填现状**：14 个模型已写 model_meta（kimi-k3=256K/32K/vision、glm-5.2/
  5.3/5.3-flash=1M/128K、deepseek=1M/384K、qwen3.x=1M/65K 或 131K、
  legacy=128K/8K、gpt-5.6-luna=128K/8K）。新模型照此格式补。
  qwen3.8-flash=1M/131K/vision+reasoning（2026-09-04 接入，链仅
  aliyun-token-plan，图片实测 64×64 红 PNG → "红色"；⚠️ 上游要求图片
  高宽 >10px，1×1 测试图会被 400 invalid_parameter_error）。
- `auto_elevate_max_tokens`（**已开启**，2026-08-09）开启时，
  `_model_output_ceiling` 用 limit 抬高转发给上游的 max_tokens。
  开启原因：pi 的 cyrouter-sync 只在 session_start 拉 `/api/models`，
  旧会话会带着过时的 maxTokens=8192 跑完全程 → finish=length 截断。
  cyRouter 侧抬高对所有客户端（含 stale 会话）兜底，实测验证：
  发 max_tokens=100 → 上游按 32768 执行、完整输出 finish=stop。
  ⚠️ 配套保护：`model_overrides["glm-5.2"].limit.output = 65536` ——
  GLM 套餐 max_tokens >~65536 静默断流（无 finish_reason），此 override
  同时让 `/api/models` 对 glm-5.2 报 65536（pi 不再发 131072）并限制
  抬高上限；kimi-k3 的 32768 实测安全，无此问题。
- 手工改 config.json 后，`load_config` 有 30s 内存缓存；要立即生效就重启服务。

## Reasoning 回放缓存（DeepSeek thinking 多轮工具调用）

DeepSeek thinking 模式要求：执行过 tool call 的轮次，其 assistant 的
`reasoning_content` 必须在后续请求原样回传。pi/opencode 等客户端普遍
丢弃该字段——缺失会 400，填空串则破坏推理连续性（模型不报错，而是
**自信地编造**，详见 OmniRoute issue #9573 的对照实验）。

cyRouter 的对策（`reasoning.py`，仅内存）：

- 响应流经时，把带 tool_calls 的 assistant 的真实 reasoning 按
  `tool_call_id` 缓存（流式/非流式都捕获），TTL 2h、LRU 上限 2000。
- 请求进来时，历史里缺/空 reasoning 的工具轮次按 tool_call_id 查缓存
  回填**真实推理**；miss 才回退空串（保 400 不炸）并记 miss 日志。
- 防自污染（OmniRoute 踩过的坑）：空串、纯空白、已知占位符文本
  **永不入库**；miss 永不写回。
- 回填发生在密钥脱敏**之前**（脱敏会把 tool_call_id 打码导致查不中）。
- 观测：`GET /api/reasoning/cache`（hits/misses/entries），
  `DELETE /api/reasoning/cache` 清空。
- 重启后缓存清空，首个工具轮 miss 一次即重新预热，属预期行为。
- 仅作用于 OpenAI raw-proxy 路径；Anthropic 协议客户端（Claude Code
  等）原生回放 thinking 块，不需要此机制。
- ⚠️ **副作用：回放对客户端的上下文计量不可见**（2026-08-09 根因记录）。
  cyRouter 回填的 reasoning 计入上游窗口，但 pi 自己算 contextTokens 时
  看不到这部分（它早已丢弃 reasoning）——pi 的 auto-compact 阈值因此
  系统性偏低失效：实测 pi 以为自己还在 247.8k 阈值内，上游实际已 251k
  （251049 = 681 input + 250368 cache_read），reasoning 烧完仅剩的
  ~5k 余量后 finish=length → pi 报 "Response was truncated"。
  对策：`model_overrides["kimi-k3"].limit.context = 240000`（真实墙
  256000，留 16k 边距覆盖累计回放 reasoning + 预检字符启发式 ±20%
  误差），pi 提前到 231.8k compact、预检提前 400 让 pi 反应式
  compact+重试，两层都在撞墙前触发。

## Token Plan 本地额度估算（无官方 API）

阿里云 Token Plan **没有额度查询 API**（探测过 billing/usage 路径与响应
头，均无；官方只能在控制台「我的订阅」页查看）。dashboard 的额度条是
**本地估算**，config：

```json
"token_plan_quota": {
  "provider": "aliyun-token-plan",
  "window_days": 7,
  "limit_credits": 40000,
  "rmb_per_credit": 0.01,
  "window_reset": "2026-08-14 17:18:00"
}
```

- 机制：`_update_daily_stat` 把该 provider 服务的每个请求的缓存折扣后
  理论成本按日累计到 `data/token_plan_usage.json`；quota 端点按滚动
  窗口汇总，换算 `估算 credits = 成本 / rmb_per_credit`，对照
  `limit_credits` 出百分比条（zhipu 同形状，UI 零改动）。
- `limit_credits` 按套餐档：Lite 2500 / Standard 10000 / Pro 40000（7 天）。
- `rmb_per_credit` = 0.01，依据官方 qwen3.6-plus 换算示例反推
  （¥2/M input↔200 credits/M、¥12/M output↔1204 credits/M ≈ 100 credits/¥）。
  控制台「我的订阅」读数有延迟（非实时），不要拿它微调系数；如需校准，
  等窗口接近重置时对比累计值。
- `window_reset`：控制台显示的精确重置时间。设置后用真实窗口（非滚动
  近似），额度条显示准确重置时刻；过期自动按整窗口推进（使用不连续时
  从控制台刷新此值）。日粒度账本在窗口起始日最多高估一天内的早期用量。
- 限制：只统计经 cyRouter 的流量；官方 7 天窗口从首次调用起算，本地用
  滚动窗口近似；服务启用本功能前的历史日无数据。

## local_quota —— per-provider token 额度估算 + Kimi 实时用量拉取

**2026-08-26 更正：Kimi Code 其实有官方用量接口**
`GET {base_urls.openai}/usages`（即 `https://api.kimi.com/coding/v1/usages`，
API key Bearer 鉴权，无需特殊 UA）——官方 CLI 的 `/usage` 命令背后就是它，
此前「探测 14 个端点仅 /me 200」是漏了复数形式。响应（百分比刻度）：
`usage{limit,used,remaining,resetTime}`=周期池、
`limits[0]{window{duration:300,timeUnit:MINUTE},detail{limit,remaining,resetTime}}`=5h
频限窗口、`parallel{limit}`=并发上限、`user.membership.level`=档位。
resetTime 是 UTC ISO 带微秒，精确到秒。

开启（已配置在 kimi code provider 上）：`local_quota.fetch_usages: true`。
`_usages_fetch_loop` 每 300s 拉一次（_fetch_provider_usages，走代理感知的
http client），存内存 `_usages_cache`；`_estimate_local_quota` 优先用新鲜
读数（≤ 900s）出真实百分比+精确重置时间（label 无「估算」后缀，
`estimated:false, source:"usages"`），过期/失败自动回退本地估算。UI 额度
条零改动。实测（2026-08-26）：周池 33%、重置 08-31 12:59、5h 窗口 0%、
并发 30、LEVEL_ADVANCED(Allegro)。新读数也修正了旧反推：limit_tokens
=331M 系按含缓存口径反推，实际 UI 读数百分比以 /usages 为准。

**订阅过期判定与显示（2026-09-11）**：Kimi Code 订阅到期后全 API 429，
连 /usages 都拒，body 明确标因：
`{"code":"resource_exhausted","message":"insufficient balance",
"details":[{"debug":{"reason":"REASON_QUOTA_EXCEEDED",
"localizedMessage":{"message":"Credits used up."}}}]}`——与「周池耗尽
（对话 429 但 /usages 仍 200）」可靠区分。机制：① 判定——连续 3 次
（~15min）balance-429 确认（`_insufficient_balance_signal` 只认明确
balance/credits 标记，普通 429 不计数；防瞬时故障误判），
`_usages_cache[name]` 写 `subscription:"exhausted"` + `exhausted_since`，
任何一次 200 立即全量恢复（续费即生效，日志 `subscription recovered`）；
② 显示——`_estimate_local_quota` 对 exhausted 返回定论红条（100%、
label「订阅已过期」、`nextResetTime:0`——只有续费能恢复，无重置时刻），
不再显示无意义的本地估算百分比；UI（dash 供应商卡片）识别
`q.subscription==="exhausted"` 全卡灰化：灰条（label/进度条/附注全
var(--muted)）+「Credits 已耗尽」+「自 {since} 判定 · 续费后自动恢复」+
状态灯 dot-ok/pulse→dot-off 灰 + 徽章「已配置」→「已过期」（badge-muted）

- 整卡 opacity 0.75。灰色=muted：过期是停用终态不是告警，红色留给额度
≥80% 这类需要行动的警告。「供应商」页2026-09-14 起同语义补齐（灰点+已过期徽章+卡片降透明+提示行，随下版本发布；dash 与供应商页两处可见）；③ auto 联动——
`_model_quota_pressure` 对 exhausted 返回满压 100（参与链上 MIN：
另一家健康则模型整体可用），quota pressure 降权让 auto 直接绕开过期
供应商选 fallback；④ 退避——判定后 /usages 探测降频到 1800s
（`_usages_next_attempt`），持续 429 日志降为 DEBUG；⑤ 持久化
（2026-09-11 第二版）——exhausted 判定写 `data/usages_state.json`
（`_persist_usages_state`），启动时 `_load_persisted_usages_state` 恢复、
任何 200 清除：过期是持续性事实，重启不该忘记（首版判定只在内存，
重启后退回显示误导性的本地估算百分比 ~15 分钟）。测试：
tests/test_usages_fetch.py 15 例（信号识别/连续确认/恢复/普通 429
不计/exhausted payload/pressure 联动/双供应商 MIN/持久化写入恢复/
续费清除）。

以下为本地估算机制（现为回退路径，仍适用阿里云 Token Plan 等无接口场景）：
消耗条机制与 token_plan_quota 平行，但按 **token 计价**且配置挂在 provider 上：

```json
"local_quota": {
  "window_days": 7, "limit_tokens": 331000000, "window_reset": "2026-08-17 13:00:00",
  "rate_window_hours": 5, "rate_limit_tokens": 67600000, "rate_window_reset": "..."
}
```

- `_estimate_local_quota` 返回 zhipu 形状 payload（两条 limits：周额度 +
  5h 频限），UI 零改动；`quotas[p.name].limits` 非空即进「套餐订阅」组。
  配合 `pricing[model].plan_monthly`（kimi-k3=699，Allegro 档，2026-08-10
  从 Allegretto ¥199 升级；套餐升级会立即作底旧周窗、从升级时刻开新窗）——
  后者同时让成本按套餐覆盖计（pay_cost 不计实付、累计 plan_saved）。
- **数据源分两层**：周窗口从 `daily_stats.by_provider`（日粒度，按 provider
  URL hostname 匹配双协议端点）；5h 频限日粒度太粗，改扫 request_log
  （`get_log_entries(limit=2000)`）。
- **token 口径 = 含缓存 input + output（cache_read/creation 全计入）**。
  kimi 池子按含缓存口径扣减（2026-08-10 校准实锤：新周窗 3.3h 控制台
  19%，raw 口径仅 5.46M → 隐含池 28.7M/周，与 Allegro 社区估计月池
  ~23.75 亿差 20 倍；含缓存 62.9M → 隐含 331M/周，量级吻合）。坑：
  kimi 的 usage 是 DeepSeek/Anthropic 风格（input=cache miss，
  cache_read 独立），`_normalize_usage` 会把 cache 并入 input ——
  by_provider 的 `in` 是含缓存的，周窗口直接 `in + out`；request_log
  里是原始值，5h 频限要显式加 `cache_read_input_tokens` +
  `cache_creation_input_tokens`。⚠️ 2026-08-10 之前用 raw 口径
  （`in - cache_read + out`），消耗条严重低估 ~11.5 倍，已修正。
- **限额是反推估算**：用控制台百分比读数 ÷ 同期 cyRouter 统计得出。
  2026-08-10 校准（Allegro 新周窗首日，request_log 自 13:00 起完整覆盖）：
  19%→331M/周、93%→67.6M/5h（62.9M 含缓存 ÷ 3.3h 窗口用量）。
  ⚠️ 2026-08-09 旧校准（19%→4.73M/周、45%→1.875M/5h）基于错误的
  raw 口径，作废。下窗口用新读数重新校准 `limit_tokens` / `window_reset`。
- ✅ **`rebuild_today_stats` / `_update_daily_stat` 已统一**（2026-08-09 重构）：
  两者共享 `_entry_metrics`（单请求归一化）+ `_accumulate_day_stat`（累计），
  不再有「改一处必须同步另一处」的复制风险。曾踩过的坑（重建清掉 by_provider）
  已由统一实现根治。新增累计字段只需改 `_accumulate_day_stat` 一处。

## Anthropic 端点 500 事故与 volcano-apig 接入（2026-09-07）

**事故**：`/anthropic/v1/messages` 自 2026-08-30 起所有请求 100% 500（与
模型/供应商无关）。根因：in-request reroute 重构时 `_anthropic_messages_once`
签名是 `(request, path, body, ...)` —— `path` 排在第二位，而
`_run_auto_reroute` 统一按 `(request, body, **impl_kwargs)` 调用 impl，
位置参数 `body` 落进 `path` 位 + 关键字 `path=` 再撞车 →
`TypeError: got multiple values for argument 'path'`。`path` 在函数体内
从未使用（死参数）。修复：签名对齐为 `(request, body, path="", ...)`；
回归测试 test_anthropic_impl_signature_matches_wrapper_convention。

**volcano-apig Anthropic 协议接入**：火山 API 网关的 Anthropic 兼容路径是
`<host>/compatible`（POST `/compatible/v1/messages`，响应真 Anthropic 形状、
SSE 是 message_start/content_block_delta 标准事件）。配置：
`base_urls.anthropic = https://st8tp3…apigateway-cn-beijing.volceapi.com/compatible`。
⚠️ 该网关的其余任意路径（含 `/anthropic/v1/messages`、`/v1/messages`）
都返回 **200 + OpenAI 格式**——绝不能当作 anthropic 端点配置，会把
Anthropic 客户端喂成无法解析的 OpenAI 形状（静默毒化，比 404 更糟）。

**无空格 SSE（2026-09-08 事故）**：kimi coding 的 Anthropic 端点 SSE
data 行冒号后**无空格**（`data:{...}`，WHATWG 规范合法），而
`_sse_line_has_content`（first-token gate）、`_restore_sse_line`（脱敏
还原）、`_observe_line`（取证/usage）三处硬编码 `b"data: "` → kimi
Anthropic 流式全部被判空流（buffered 11KB 仍 empty_response 502，
重试 3×90s），zhipu（带空格）一直正常掩盖了它。修复：统一辅助
`_sse_data_payload`（带/不带空格都认），OpenAI 主循环同改。测试：
tests/test_sse_spaceless.py 11 例。⚠️ 排查此类问题时自写探测脚本
也要用宽容解析（本次测试脚本自己复刻了同款 bug 干扰了判断）。

**双头认证**：网关 consumer 认证只读 `Authorization: Bearer`，标准
`x-api-key` 单独发会 401。anthropic raw-proxy 现在同时发
`x-api-key` + `authorization: Bearer`（实测对 zhipu/deepseek/kimi coding
的 anthropic 端点无害，均 200）。probe 路径同步。

**运维教训（僵尸进程）**：Servy 显示 Stopped 但 session-0 的 python 子进程
（父进程已死）仍占着 20100 端口 → 后续服务重启全部 10048 失败，跑的还是
旧代码。排查三步：`netstat -ano | findstr 20100` 找 PID →
`Get-CimInstance Win32_Process` 查 ParentProcessId/SessionId → taskkill
僵尸后 `servy-cli start --name cyRouter`。看到 10048 连发先怀疑端口被
僵尸占用，而不是代码问题。

## tool 邻接修复（message_repair）—— mid-turn 注入消息破坏 assistant→tool 邻接

**背景（2026-08-28 DancherLink 事故）**：pi 的 pi-processes 扩展会把进程生命周期
事件（`<process_event type="lifecycle" kind="crash">`）作为 **user 角色自定义消息
注入到正在进行的轮次里**——恰好落在某 assistant 的 `taskkill` tool_call 和它的
toolResult 中间。kimi code 严格校验 OpenAI 邻接（tool 消息必须紧跟携带匹配
`tool_calls` 的 assistant），整单请求 400：

```text
400 messages with role 'tool' must be a response to a preceeding message
   with 'tool_calls'
```

宽容的上游（GLM 等）不校验，所以这种 junction 会潜伏在会话里，直到 compaction
把保留区圈到它、或路由切到 kimi 才爆发；pi 的自愈重试全部撞同一堵墙（同会话
连炸 4 次，81→84 条消息），auto-compact 也救不了（compact 摘要请求本身不带 tool
消息，但修不好主循环请求）。

**修复**：`_repair_tool_adjacency(messages)`（main.py，接在 /v1/chat/completions
的 auto 解析之后、缓存查找之前）。单趟扫描每个带 tool_calls 的 assistant，把
该轮内引用其 id 的 tool 结果整体捉升到紧邻 assistant 之后，插进来的
user/system 消息按原相对顺序移到工具结果块后面——**不丢任何消息**。日志
`tool-adjacency repair: hoisted N tool result(s) above M mid-turn message(s)`。
开关：`config.message_repair.tool_adjacency = false` 可关闭（默认开）。测试：
tests/test_tool_adjacency.py 10 例（真实事故形状、并行 tool、中插、天折轮不动、
陌生 id 不动、合法尾随 user 不动）。实测：同形状请求修复前 400、修复后 200；
原封不动的真实会话 body 重放 → 200。根治仍在 pi 侧（pi-processes 应该在轮末
而不是轮中注入事件），此层是代理侧兼容修复。

## 密钥脱敏（redact.py）—— 按天种子的确定性 Middle-Mask

⚠️ **重要前提：助手（模型）看到的整份对话都经过 cyRouter 脱敏。** 用户粘贴的
密钥在到达模型前已被替换成 surrogate；模型输出的 surrogate 又在响应路径还原
成真实密钥再交给客户端。因此 **永远不要假设模型上下文里的密钥是真实 key**
——拿它去直连测试会 401（测的是 surrogate）。要验证 key，走 cyRouter 路由、
让脱敏层自己回填，或让用户在脱敏层外（控制台）核对。

### 2026-09-07 误伤事故与修复（dashed-UUID / hex / call_id 豁免 + 可观测性）

- **事故**：旧 `_HIGH_ENTROPY_RE` 类内含 `-`，dashed UUID 是连续 36 字符 run，
  过不了（无连字符）hex 豁免，English-likeness 掷硬币决定是否被掩——逐 token
  随机失败，一次 agent 调试会话被误导出「标准库 uuid.py 被 pyc 投毒」的结论
  （连 GitHub raw 拉的源码也"被改"才戰穿：出站工具参数同样过请求体脱敏），
  排查 ~40 分钟。修复：generic 类去掉 `-`（带连字符凭据全部由 _KNOWN_RES
  整体覆盖）；`_is_likely_benign` 加 dashed-UUID 显式豁免 + 纯 hex 扩到
  {16,128} + `call_/toolu_/msg_/fc_/chatcmpl-` 工具 id 豁免（上线首日审计
  日志抓到的残留误伤类）。代价：无已知前缀的 url-safe-base64 凭据不再被
  generic 层捕获，可接受。
- **可观测性**：entropy 级掩码打 WARNING（len+head6）；`X-Cy-Redacted-Count`
  响应头（缺头=零掩码），agent/客户端可即时发现上下文被改写。
- **回归**：tests/test_redact.py 语料断言（uuid/大写 uuid/hex36/sha256/
  ISO8601/ULID/复合 id/call_/chatcmpl- 零掩码；sk-ant/ghp_ 假 key 必掩）。
- **双层腐蚀心智模型**：出站（agent 工具参数里的长 token → 落盘前被掩）与
  入站（工具输出回传 → 显示前被掩）都可能被改；真伪判定用派生统计量
  （长度/连字符数/字符集），长 token 字面量一律运行时拼接。

`redact.py` 的 middle-mask 策略：

- **检测**：已知凭证前缀（sk-/sk-ant-/AIza/hf_/ghp_/xai-/AKIA/LTAI/…）全量匹配；
  通用高熵串 `[A-Za-z0-9_-]{28,128}` 经 Shannon 熵 + benign 过滤（纯 hex
  摘要 / 纯数字 / 近常量串 / **类英文代码标识符**拒绝）；跳过 base64 data URL（避免碰坏图片）
  **及 Anthropic 图片块的裸 base64 `data` 字段**（`_is_anthropic_image_source`：
  父对象有 `type:"base64"` + `media_type` 时跳过 data 键——2026-09-08 事故：
  Anthropic 格式无 data: URL 前缀，裸 base64 被 entropy 掩码击中 → kimi
  解码后 magic bytes 损坏 → 400 "unsupported image format:
  application/octet-stream"；OpenAI 格式因 data URL 前缀一直免疫）。
  - 标识符过滤（**bigram Markov 英文似然**，主判据）：长 PascalCase/camelCase
    符号（信号名/类名，如 `USpringVector3AnimationComponent`）曾被打上
    surrogate 水印，误导模型认为磁盘文件损坏。判据：token 的 a-z 子序列在
    `redact_bigrams.json` 模型下的**中位数**转移对数概率 > `-2.7` 判 benign
    （校准：标识符 ≥-2.59 / 合成密钥 ≤-2.80，250 个随机密钥 0 泄漏）。
    中位数抗离群：QQml 的 `qq`、xcb_ 这类稀有 bigram 不会拖垮整串。
    模型 = Norvig big.txt + 合成拼音音节链（~10%）——拼音标识符
    （chushihua/jisuan）的 zh/ch/sh/ua 转移在纯英文语料里不存在，不混训
    会被系统性误杀。辅助快路径：`_BENIGN_MORPHEMES`（tion/ing/ment/work/…）。
    模型文件缺失时评分返回 -10（永不判 benign，安全侧多脱敏）。
    重训练：语料 +  trainer 逻辑见 git 历史 / 本节（`_big_corpus.txt` 临时文件）。
  - ⚠️ 测试/调试陷阱：向工具调用/文件写入 ≥28 字符的字面量可能撞上当日
    surrogate 空间被还原层转换——测试里用**运行时拼接**构造长串
    （`"USprin" + "gVector3" + …`），tests/test_redact.py 的标识符用例同法。
- **脱敏**：保留结构性前缀 + 2 字符样本 + 尾 4 字符指纹，**只换中间段**。
- **中间段是确定性的，不是随机的**：`_deterministic_middle(secret, mid_len)`
  = `SHA-256(当日种子 | "|redact|" | secret | counter)` 按 counter 展开、
  每字节映射到 `[A-Za-z0-9]`。**同一密钥同一天恒定（含重启）**；换天才轮换。
  这取代了旧的「进程级随机缓存」——旧方案重启就变，会破坏跨重启的多轮
  一致性。`_daily_seed()` 为当日 ISO 日期，带进程缓存。
- **还原**：三层都做 surrogate→真 key 还原。
  - `restore_text`（非流式文本）/`restore_obj`（非流式 JSON，递归全对象，
    含 `tool_calls[].function.arguments` 和 Anthropic `input`）。
  - 流式 `_restore_sse_line`：覆盖 OpenAI `delta.{content,reasoning_content}`
    与 `delta.tool_calls[].function.arguments`（**按 call index 分独立
    ContentRestorer，跨 fragment 累积**），以及 Anthropic `delta.{text,thinking}`
    与 `input_json_delta.partial_json`（按 block index 分）。
    `_tail_content_chunk` 处理流尾残留：content 字段和 tool_call 字段
    都能正确冲刷（key 形如 `openai:tc:N` / `anthropic:tc:N`）。
    ⚠️ 这是个曾漏掉 tool_call 的 gap：模型把脱敏 secret 复述进工具调用
    参数时，旧流式路径不还原 → surrogate 泄给客户端（如 write_file
    把占位符写进配置）。现以修复，测试覆盖 3 个场景。
  - 另含「截断前缀还原」——AI 复述长密钥时常缩写为 `sk-xxxx…`，
    placeholder 前 12 字符开头 + 截断边界（省略号/标点/流尾）也还原成完整真 key。
- 开关：`config.secret_redaction.enabled`（默认 true）。`custom_patterns`
  追加正则，`exclude_substrings` 排除子串。
- **改 redact.py 后必须重启服务**（模块不热重载）。

## 上下文预检（context preflight）

OpenAI raw-proxy 路径在转发上游前估算请求 input tokens（CJK≈1 tok/字符，
其他≈1 tok/4 字符，含 tool_calls / reasoning_content / tools 定义），超限
直接返回 400 `context_length_exceeded`，附带明确提示 compact。**限额来自统一
解析器 `_resolve_model_limit(...)["context"]`**（model_overrides > model_meta
> pricing.max_context 遗留 > 启发式）——凡是有已知上下文的模型（写了
model_meta 即生效）都受保护，不只 kimi；之前只读 pricing.max_context。

背景：上游对超窗口请求**静默截断输入**（不报错），模型只剩 ~0 输出余量，
返回 1-4 tokens 的 finish=length —— pi 显示 "Response was truncated before
completion"，auto-compact 后若仍超限则循环失败。kimi-k3 实测：277k→compact
163k 仍超 k3 窗口（~128k）。对策双保险：

- model_map `kimi-k3 → k3-256k`（256k 窗口，compact 后装得下）
- `pricing["kimi-k3"].max_context = 256000` 预检（估算超 256k 直接 400，
  不再出现迷惑性截断；估算是字符启发式，±20%，边界误拒无副作用）

`max_context` 同一个配置还驱动 **`/api/models` 的 `context_window`**
（优先级：model_overrides > pricing.max_context > 家族启发式）——这是
「提前 compact」的信号链：pi 的 cyrouter-sync 扩展从 `/api/models` 拉
`context_window` 作为 `model.contextWindow`，pi 的 auto-compaction 在
`contextTokens > contextWindow - reserveTokens`（默认 8192）时主动 compact，
不用等撞墙。未设 max_context 的模型走家族启发式（kimi-k3 原本错配成
128k 兼底值）。预检 400 的错误码 `context_length_exceeded` 命中 pi
`isContextOverflow` 的模式表，会触发 pi 反应式 compact+自动重试。
三层防御：pi 阈值提前 compact → cyRouter 400 兼底 → pi 认 400 自动恢复。
改 max_context 后需重启 cyRouter；pi 侧 cyrouter-sync 在 session_start
拉取，pi 重启后生效。

其他模型按需加 `max_context` 即自动生效（glm-5.2 也有同款静默截断史，见
blackbox）。Anthropic 端点暂未接预检。流中途断开（无 [DONE] 的 EOF）是另一
类问题：cyRouter 有 30s 心跳保活 + 合成 finish=length 让客户端自动恢复，
发生时会写 `data/blackbox/truncated-*.json`。

## 截断诊断透传 + 熔断（2026-08-10 加固）

背景：kimi 上游会**静默杀流**（TCP 直接关闭，无错误帧、无 [DONE]、无 429），
且与上下文大小/并发/额度均无关（实测 10万~21万 token 都被杀、并发 0~3 都被杀、
控制台额度 5h 9%）。cyRouter 把所有 EOF 统一合成 finish=length → 客户端永远只
显示同一句 "Response was truncated before completion"，无法区分根因。对策两层：

- **`truncation_notice`（config，默认 true）**：EOF 分支在合成 finish=length
  之前注入一段可读的 `> ⚠️ cyRouter 诊断` content delta（provider / 耗时 /
  已产出 reasoning、content 字符数 / max_tokens / 「上游没发错误帧」的事实），
  用户能在会话里直接看到断流现场；黑盒 trigger 同步增加 `elapsed_s` + `provider`。
- **熔断器已启用**：`circuit_breaker: {enabled: true, failure_threshold: 2,
  cooldown_seconds: 120}`。EOF 分支本就会 `_cb_fail`（此前 enabled=false 导致
  杀流连发时请求仍全打 kimi）。**计数语义是「连续失败」**（`report_success`
  即清零，见 breaker.py）——偶发单杀永远攒不到阈值，连续 2 杀则是明确的
  上游拥堵 streak：OPEN 120s → 自动走供应商链下一个（如 kimi-k3 →
  opencode-go），HALF_OPEN 探测恢复后自动切回。阈值 2026-08-10 从 3 降到 2
  的动机：pi 的自动恢复只重试一次（占掉第 2 次失败），阈值 3 时第 3 次请求
  仍打回生病的 kimi 再赌一次；2 则第 2 杀后立刻 failover。注意：opencode-go
  历史上也有 ~27% 的 None-finish（疑似同 backend），熔断只是减损不是根治。
- **熔断器科学化改造（2026-08-14，对齐 Envoy outlier detection /
  resilience4j）**，四个维度：
  ① **错误分类**：`_cb_fail(provider, model, *, status=None, exc=None)`
  只计上游健康信号（5xx / 超时 / 连接错误 / 断流 EOF / 空流耗尽）；
  **4xx 客户端错误不计**（一个坏请求不能替所有人熔断）；**429 不进熔断器**，
  直接转 `_mark_provider_model_failed`（900s 分级冷却）
  （消灭「额度标记 + 熔断」双重惩罚）。2026-09-04 清理：独立的
  `_check_fatal_error` 已删除——它用弱关键词分类器在 `_cb_fail`
  统一分类之前重复标记，两处判定可能漂移；现在 `_cb_fail`（结构化
  字段→关键词表→状态码）是全路径（含 probe）唯一入口。
  ② **冷却升级**：每次 OPEN 记 `trips`，冷却 = `min(base × trips,
  cooldown_max_seconds)`（linear，Envoy 式；可选 exponential = base×2^(n-1)）。
  病态上游反复发作越关越久（120→240→360…封顶 1800s），不再每轮只关 120s。
  ③ **健康衰减**：CLOSED 状态每持续干净 `decay_seconds`（默认 600s）降一档
  trips，归零后状态整个清除—— Envoy 每健康检查间隔降一档的语义。
  ④ **探测联动**：HALF_OPEN 用户流量探测成功只 CLOSED 但**保留 trips**
  （抖动上游继续升级）；**后台 probe 成功（主动健康检查）则全量复位**
  `breaker.reset(provider, model)`（Envoy：active health check 成功
  uneject + 清零计数器），不再傻等冷却。新增配置项：
  `cooldown_max_seconds`（1800）、`cooldown_backoff`（linear/exponential）、
  `decay_seconds`（600）。只读 `is_open()` 用升级后冷却判定。观测：
  `GET /api/breaker` 的 circuits 带 `trips` + 当次生效 `cooldown_s`。
  测试：tests/test_breaker.py 14 例。
- **panic mode（2026-08-26，Envoy 恐慌阈值语义）**：熔断器是路由智能
  不是硬墙——**拒绝链上最后一个候选 = 自造彻底停机**（2026-08-26 事故：
  单供应商 glm-5.3 在 120s OPEN 窗口被直连会话连吃 5 个瞬时 503）。
  `_cb_check(..., last_candidate=True)` 时放行（panic），真实结果说话：
  成功 → `report_success` 对 OPEN 状态提前闭合（等效 half-open 探测
  成功，保留 trips）；失败 → 照常累计升级。非最后候选照旧 fail fast，
  503 带 `Retry-After`（剩余冷却秒数）。三个调用点：raw-OpenAI 链循环、
  raw-Anthropic 链循环（`is_last_candidate`）、SDK/ClaudeProvider 单家
  路径（恒为最后候选）。auto 的模型级健康判定不受影响（`is_open` 只读
  状态机不变）。
- **会话级供应商惩罚窗（2026-09-07，session penalty window）**：全局熔断器
  的「连续失败」计数在多会话并行下天然失真——2026-09-07 事故：zhipu-coding
  |glm-5.3 在 17 分钟内静默杀流（30s 零字节网关超时）10 次，但 6 个并行
  auto 会话的短回复成功不断把 trips 清零（永远 0↔1 震荡，电路从不开）
  → 被杀会话的重试永远撞回同一家主供应商。根因：杀流按「请求形状」
  （长思考不吐字）选择性发生，全局计数被其他会话的成功掩护。对策：
  EOF 杀流 / pre-content fallback 后，`_session_penalty_record` 对
  (session_key, model) 记一条惩罚——时间窗内（`session_penalty.duration_seconds`
  默认 600s，`enabled` 可关）该会话的候选链把肇事者**稳定排到最后**
  （`_session_penalty_reorder`，不硬拒，单候选照用，保住 panic 语义）。
  **成功请求无权清除惩罚，只随时间过期**——这是与「计数」语义的本质区别：
  连续计数会被会话自己的短请求/compact 摘要成功重置，且 compact 换
  derived key（prefix hash 变）使计数归零。session_key = x-cyrouter-session-id
  → x-conversation-id → `_auto_session_key`（与粘性同源，在消息未被修改前
  计算）。两层互补：全局熔断管「供应商整体挂了」，会话惩罚管「这家对我
  这种形状的请求不行」。OpenAI / Anthropic 两条 raw-proxy 链路都已接入
  （记录点：两处 EOF 分支 + 两处链循环 fallback 分支；重排点：两处
  upstreams 构建后）。日志：`session …: penalty … — demoted to last candidate`、
  `session …: penalty reorder chain → [a, b]`。测试：
  tests/test_session_penalty.py 17 例。残余缺口：compact 换 key 后惩罚
  重建最多多挨一刀，可接受。
- **pi 侧误导文案须知**：连续两杀后 pi 会报 `Context overflow recovery
  failed after one compact-and-retry attempt`——**这不是真的上下文溢出**，
  是 pi 把「finish=length 恢复失败」统一归到这句罐头话。compact 治不了
  30s 空闲杀流（重试只是再赌一次不停顿），看到这句 = 重试也被杀了，
  查黑盒 `idle_gap_s` 确认分类。
- **断流取证（2026-08-10 第二轮）**：`_raw_openai_sse_stream` 现在追踪
  `_last_byte_ts`（最后一次从上游收到任何字节的时刻，空行 keepalive 也算，
  我们发的心跳不算）。EOF 分支计算 `idle_gap_s`（断流时距最后字节多久）
  并抓取上游响应头白名单（request-id / trace / ray / server / via）
  → 两者写入黑盒 trigger（`idle_gap_s`、`upstream_headers`），诊断通知里
  直接给出分类：`idle_gap ≥ 25s` → 「疑似上游网关空闲超时（长思考未吐字
  被杀）」；`< 25s` → 「活跃传输中被杀，更像上游 worker 崩溃/过载」；
  有 request-id 则附「可据此向 kimi 报障」。2026-08-10 13:47 的案例
  （77.5s / reasoning 6165 字符被杀，同期 120/122 ok）证明零星杀流仍在，
  靠这两个字段在下一次发生时定责。改 main.py 后记得重启。
- **取证结论（2026-08-10 第三轮，根因定案）**：两次带取证的现场——
  33.0s / reasoning 52 字符、120.7s / reasoning 9596 字符——`idle_gap_s`
  **均 ≈30s**：模型/队列静默满 ~30s 后连接被回收。实锤**上游网关 ~30s
  零字节空闲超时杀流**（排队或长思考期间未吐字，被中间层按闲置连接杀）。
  与 13:03 批次的延迟分布（32.7s~83.2s，小流量断续重置窗口）完全吻合。
  代理侧无法预防（cyRouter 不能替上游吐字节，心跳只保客户端侧），
  检测（notice+黑盒）、恢复（合成 finish=length）、减损（熔断）已齐备；
  上游响应头无 request-id，无法向 kimi 提供工单级凭证，只能带时间戳报障。
- **透明性审计 + 修复（2026-08-10 第三轮）**：全量审计 main.py 吞错误路径
  （15 处静默 except，多为配置解析兜底，良性），修三处真缺口：
  ① **供应商链错误归因**（`_annotate_final_error`）：链上最终候选失败时，
  错误响应加 `X-CyRouter-Provider` / `X-CyRouter-Tried` 头，并在
  error.message 末尾追加「此错误来自供应商「X」；链上已尝试 A → B」——
  此前多 provider 链的错误体只带上游原始信息，客户端无法分辨是哪家挂的。
  OpenAI / Anthropic 两条链式循环都接上。② **Anthropic 流取证对齐**：
  `_raw_anthropic_sse_stream` 补齐 `_last_byte_ts` / `_open_block_index`
  跟踪、blackbox dump（trigger 带 `shape: anthropic`）、诊断文本块注入
  （content_block_start/delta/stop，index 接在真实块后）、终止序列按真实
  index 闭合（不再盲猜 0）。③ notice 注入失败与 `auto_elevate_max_tokens`
  失败从静默 pass 改为 `logger.warning`。
- **诊断通知紧凑化（第三轮续，第四版极简）**：⚠️ 文本重构为共享辅助
  `_truncation_notice_text`（OpenAI / Anthropic 两路径共用，改文案只动一处），
  单行极简格式：
  `> ⚠️ cyRouter：{provider} 静默断流，耗时 {elapsed}s｜
  {分类：断流前Ns零字节，疑似网关空闲超时杀流 / 断流前Ns仍有数据，疑似上游崩溃或过载}｜
  [request-id 可报障]｜已触发自动恢复`。
  只留四要素：who + 耗时 + 分类 + 是否自动恢复；产出字符数 / max_tokens /
  请求体 / 上游响应头等细节一律在 `data/blackbox/` 黑盒 trigger 里，不进
  通知文本。函数签名同步瘦身（去掉 reasoning_chars/content_chars/max_tokens/
  shape 参数）。
- **⚠️ surrogate 污染源码事件（已修复）**：`main.py` 里的
  `_anthropic_raw_stream_once` 曾被历史某次 AI 编辑写成脱敏 surrogate
  （`_anthrh<18字符>once`，def 与调用点自洽所以能跑），2026-08-10 用正则
  替换修复（2 处）。⚠️ 2026-09-04 起该名字与 `_anthropic_raw_nonstream_once`
  **互换了含义**（历史命名本来就颠倒：stream 实非流式、nonstream 实流式），
  现已名实相符。教训：read/grep 输出里出现 surrogate 外观的标识符时，
  **先用 python 数磁盘上真实名的出现次数**（`src.count(真名)`）区分「视图被
  脱敏」与「文件被污染」；修复用运行时构造串/正则替换，不要手工复述
  surrogate 字面量。另外补丁脚本字符串里嵌 `"""` 会被提前终结（本次
  踩过），统一用 `r'''...'''` 定界补丁文本；em-dash 等容易写入歧义的字符
  在锚点里用 `\u2014` 显式转义。

## 运维操作

cyRouter 由 Servy 注册为 Windows 系统服务（守护进程），
CLI 前缀统一为 `sudo servy-cli`，所有命令都需 `--name cyRouter`。

### 代码改动后必须重启

Python 进程**不热重载**。改了 `main.py` / `redact.py` / `router.py` 等源码后，
必须重启服务才能生效，否则运行的是旧代码。改完立即重启，别等用户提醒。

```bash
sudo servy-cli restart --name cyRouter   # 重启（最常用）
sudo servy-cli status   --name cyRouter   # 查状态，应显示 Running
```

验证新进程已起来且健康：

```bash
# 1) PID 变了 = 重启成功（netstat 找监听 20100 的 PID）
netstat -ano | findstr "127.0.0.1:20100" | findstr LISTENING
# 2) /health 返回 ok
curl -s http://localhost:20100/health
#    → {"status":"ok", ...}
```

### Servy 常用命令

| 命令 | 用途 |
| ------ | ------ |
| `sudo servy-cli status --name cyRouter` | 查看运行状态 |
| `sudo servy-cli start  --name cyRouter` | 启动 |
| `sudo servy-cli stop   --name cyRouter` | 停止 |
| `sudo servy-cli restart --name cyRouter` | 重启（代码改动后用这个） |
| `sudo servy-cli install ...` | 注册新服务（参数多，按需查） |
| `sudo servy-cli uninstall --name=XXX` | 卸载服务 |

注意：命令必须用 `sudo servy-cli`（不是 `Servy.exe`），且服务名是 `cyRouter`（大小写敏感）。

### 端口与路径

- 默认端口：开发模式 `20100`，打包后 `20108`（见 `config.py: DEFAULT_PORT`）
- 工作目录：`D:\Programs\cyRouter`
- 日志：`logs/server-out.log`（stdout）、`logs/server-err.log`（stderr）
- 运行数据：**exe 同目录 `data/`**（可写时；不可写才回退 `~/AppData/Roaming/cyRouter/data/`；开发模式即 `D:\Programs\cyRouter\data\`）
- 截断诊断黑盒：`data/blackbox/truncated-*.json`（流式截断时自动写，用于定位上游问题）

### 发布流水线（build.ps1 → 5.7 settings sync，2026-09-14）

发布产物只搬 exe/安装包/version.json；**安装包永不携带设置**（公开分发包，密钥绝不入包）。本机发布实例（Release\cyRouter\ 的 exe，端口 20108）的 `data/` 由 build.ps1 第 5.7 步自动与 dev 同步：

- 发布实例在跑 → `POST /api/config/clone` 从 dev 拉（port/api_keys 不随行，key 按发布实例 .key 重加密）
- 没在跑 → 直接拷三件套：`config.json`（port 改写为发布端口）+ `.key` + `usages_state.json`
- `-NoConfigSync` 可跳过；注意 PS 往返会把 api_keys 的 created_at 时区规范化（UTC→+08:00，同瞬间，无害）
- 其他机器装 Setup 后：从任一在跑实例 clone（LAN），或手拷上述三件套——**光拷 config.json 不够**（key 解不开 → usages fetch 直接跳过，2026-09-14 "发布版没检测出 kimi 过期"的教训）
- `/api/provider/quota` 有 5 分钟缓存，改配置后要 `?refresh=true`
