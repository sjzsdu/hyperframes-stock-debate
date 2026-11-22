# ADR-003 · oneapi 作为对话的唯一网关

状态：首版范围已确认（WAN-207）。对话统一经 oneapi，TTS 与封面 AI 沿用现有路径。V1/V2 实网验证移交 WAN-208。

## 背景

已有 OpenAI 兼容 HTTP 适配器使用标准库，可注入传输（`stocktalk/modules/providers/openai_chat.py:20-28`、`stocktalk/modules/providers/openai_chat.py:34-77`），工厂按 LLM/TTS 分派（`stocktalk/modules/providers/factory.py:22-51`）。现有对话默认是 `bailian-cli`，TTS 默认是 `volcengine-doubao`，另有 `bailian-cli` 和 `dashscope-ws`，封面 AI 独立调用 `bl image generate`（`stocktalk/config/default.yaml:5-13`、`stocktalk/config/default.yaml:168-186`、`stocktalk/config/default.yaml:533-558`、`stocktalk/modules/providers/__init__.py:22-27`）。语音契约含逐句韵律能力（`stocktalk/modules/providers/base.py:48-84`）。

## 备选方案与理由

| 方案 | 取舍 |
| --- | --- |
| 复用标准库 HTTP 适配器并增加网关契约 | 依赖小，可沿用传输替身；需自己限定重试、重定向与错误映射 |
| 采用 OpenAI SDK 并指向网关 | 标准模型接口和类型较齐；SDK 版本、内建重试以及网关差异仍需维护 |
| 媒体与对话一次切换，或继续对话多供应商并行 | 一次切换要求先验证媒体能力；多供应商切换与 O1/C4 冲突 |

## 决策

沿用 `create_llm_client` 的 `dialogue` 子映射契约（`stocktalk/modules/providers/factory.py:26-38`），用 `register_llm("oneapi")` 注册新适配器，复用现有标准库 HTTP 传输。S2 在 `stocktalk/config/default.yaml` 声明以下配置。`base_url` 为实网认证前的候选值。

```yaml
dialogue:
  provider: oneapi
  options:
    base_url: https://oneapi.wnbgj.com/v1
    api_key_env: ONEAPI_API_KEY
```

工厂按 `dialogue.provider` 查注册表，把 `dialogue` 子映射传给构造器。构造器从该映射的 `options` 读取 `base_url` 和 `api_key_env`，从进程环境读取 `ONEAPI_API_KEY`。固定验证变量名、HTTPS、目标主机和已确认的 API 根，拒绝跨地址重定向。网关测试注入传输，通过完整配置和扁平 `dialogue` 配置各构造一次，断言同样的地址和环境变量名。缺 key 返回 `CONFIG_MISSING` 和环境变量指引，发请求前失败。`Authorization: Bearer` 为协议假设，需维护方验证。任务只存模型 ID 与配置版本。

平台与升级后的 CLI 对话固定走 oneapi。含显式旧 provider 或旧网关地址的配置由启动或提交校验给出迁移提示，要求用户改为上述配置并从 oneapi 清单选择模型。旧适配器保留给测试和独立旧版本回滚。模型能力验证前使用发布配置与隔离的测试替身分别留证。

| 能力 | 拟用接口或路径 | 当前证据与降级 |
| --- | --- | --- |
| 模型目录 | `GET /v1/models`，用运营者授权上下文读取 | 匿名 401，真实返回待证。服务从实际清单获取 ID，再用维护方的能力映射分组。未识别能力明确标记。前端以清单约束选择，保留原始响应脱敏证据。清单暂不可用时阻止新任务，展示刷新指引 |
| 对话 | `POST /v1/chat/completions` 非流式文本，校验 `choices[0].message.content` | 兼容性与模型 ID 待证。每次任务冻结选择；失败保留可读原因，用户显式调整模型后重试 |
| TTS | 后续评估迁移时验证候选 `/v1/audio/speech`，逐音色测音频格式、韵律、流式和裁剪 | 现有 TTS 路径照常，默认火山，百炼 CLI 与 DashScope WS 仍为既有的显式选项。oneapi 音色及逐句控制待证。无音频不得作为成功成片 |
| 封面图像 | 后续评估迁移时验证候选 `/v1/images/generations` 和输出合同 | 保留现有封面设置和模板降级规则（`stocktalk/pipeline.py:102-133`）。模型、计费、媒体安全和返回形态待证 |

生产选择规则：模型切换仅在当前 oneapi 清单里生效。O6 确认纳入媒体且网关能力能满足所需音色、韵律和封面时，对应适配器改用同一个对话网关基址和环境变量。若网关缺失能力而统一范围包含媒体，交由 juzhong 确认替代产品行为。现有媒体路径是有边界的 C4/O6 保留项，任何错误都不会自动切换供应商或更换模型。账单与时限数据由维护方提供。

## 失败、超时与替身

模型清单预算 5 秒，暂态 502/503 可在总预算内重试一次。对话单次硬截止暂设 60 秒（连接 5 秒），最终取维护方上限和业务预算的较小值。若 HTTP 客户端的 socket timeout 只能限制单次阻塞，由执行监督实施硬截止。

- **现有行为。** `Pipeline._retry` 捕获任意 `Exception` 并最多尝试三次（`stocktalk/pipeline.py:237-250`），`DialogueGenerator.generate` 把 `ProviderError` 包装成 `DialogueGenerationError`（`stocktalk/modules/dialogue_generator.py:363-372`）。仅在传输层停止重试，外层仍会重复提交。
- **S2 修改边界。** 定义 `OneAPIRequestError(LLMProviderError)`，携带稳定错误码和 `outcome_unknown` 标记。适配器在发送后超时或无法证明请求未发出时抛出该终止错误，标记 `outcome_unknown=true`。适配器只对可证明尚未发出的暂态连接故障重试一次。生成器在通用 `ProviderError` 包装前显式透传该错误。`Pipeline._retry` 对它即时抛出并保留错误码，oneapi 对话阶段显式限定为单次尝试，避免解析或归一化失败后重放整个生成器。TTS 和旧适配器仍使用既有重试预算。已有开场门禁只在成功响应经解析后按产品规则另发一次，属于内容修订。
- **跨层回归。** 从普通 Pipeline 入口注入记录请求的传输替身。替身记录一次 `POST /chat/completions` 后抛出读取超时。断言稳定码为 `UPSTREAM_TIMEOUT`、`outcome_unknown=true`、对话 POST 总数恰为 1、没有外层退避或供应商切换。另测成功返回但脚本无效不重放，已确认未发送的连接失败最多重试一次，以及 TTS 的既有重试路径。

| 日志/API 稳定码 | 触发依据 | 展示与操作 |
| --- | --- | --- |
| `UPSTREAM_AUTH` | 401/403 授权拒绝 | 核对令牌与授权范围 |
| `UPSTREAM_TIMEOUT` | 传输或总期限耗尽 | 查看服务状态后显式重试 |
| `MODEL_UNAVAILABLE` | 清单外 ID；确认过的模型错误码 | 刷新并重选 |
| `QUOTA_EXCEEDED` | 网关确认的额度错误码 | 联系维护方或补额度 |
| `RATE_LIMITED` / `UPSTREAM_ERROR` | 通用限流 / 未知协议或服务错误 | 显示等待时间或请求追踪 ID |

未知错误码保持通用错误，不依赖未经确认的上游文案推断业务含义。记录目标 host、阶段、耗时、HTTP 状态和追踪 ID，UI 与日志使用白名单字段。替身沿用 `urlopen` 注入（`tests/test_providers.py:154-185`），替换模型目录与对话传输，测试 200、缺 key 零网络、401/403、异常 JSON、超时、额度、模型不可用、跨域重定向、无隐藏回退和原始错误脱敏。认证测试使用测试进程注入的虚构值，其余测试无需运行环境提供密钥。

## 匿名只读冒烟

在本任务运行环境执行下列仅 GET 的命令，无认证、无响应体落盘。

```bash
for route in / /v1/models; do
  curl -q --max-time 12 --silent --show-error --output /dev/null \
    --write-out "route=$route http=%{http_code} type=%{content_type}\n" \
    "https://oneapi.wnbgj.com$route"
done
```

```text
route=/ http=200 type=text/html; charset=utf-8
route=/v1/models http=401 type=application/json; charset=utf-8
```

这证明运行环境可访问 HTTPS 首页，模型路径受保护。WAN-208 的 V1/V2 实网验收需要维护方交付经授权的脱敏模型清单、认证方式、一次成功对话的响应结构、媒体及能力矩阵、额度错误码与计费/限时。仅凭 401 无法确定 Bearer 及任何模型能力，方式和路径按上述候选验证后落定。

## 成本、风险与回退

估算对话 2–4 人日，固定费用和新进程均为 0，实网按量成本未知。主要风险是网关单点、模型能力和 OpenAI 协议偏差。回退优先 pin 上一可用网关适配器版本或暂停新生成，已生成内容和报告继续使用。旧版 CLI 可单独恢复原行为，属于退出平台切换，不能作为 oneapi 故障时的自动备援或 AC-04 证据。省略本改动会保留多供应商对话出口。
