# ChatGPT 登录架构与 iPadOS 验收

## 可行性结论（2026-10-28）

官方的开源、本地应用订阅共享流程可以用 Python 实现，不依赖 Codex CLI、Node、Rust、远程代理或原生可执行文件。已实现 OAuth 与独立 Responses 适配器并完成离线测试。**尚未验证真实 ChatGPT 登录、订阅推理或任何 iOS/iPadOS 行为。**

a-Shell 项目中有 Python HTTP 服务器的使用记录，说明不能简单认定 iOS 禁止监听 socket；但具体 App Store 版本仍需验收。维护者指出，让 Safari 和 a-Shell 同时处于前台有助于服务器访问。因此 iPad 上优先采用分屏／多窗口同时可见；线程和异步本身不能防止 iOS 暂停后台应用。

官方目前为此动态注册流程规定 `http://127.0.0.1:<port>/auth/callback`，端口可变，scheme、主机与路径不能改变。不使用 `localhost`、自定义 scheme、复制授权码、粘贴回调或未记载的设备码授权。网站身份登录流程不等同于本地应用订阅使用流程。若以后官方支持适合 iOS 的替代回调，应优先重新评估。

## 官方依据

实现前核对以下官方文档，协议如有变化应以当前文档为准：

- [订阅共享概览](https://developers.openai.com/siwc/token-sharing-open-source)
- [注册与登录](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [模型与推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [错误恢复](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)
- [账号与会话、刷新与撤销](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [令牌字段](https://developers.openai.com/siwc/token-sharing-open-source/token-reference)
- [身份验证示例](https://developers.openai.com/siwc/website)
- [Responses 函数调用与 namespace](https://developers.openai.com/api/docs/guides/function-calling)

平台依据：[a-Shell README](https://github.com/holzschu/a-shell)、[维护者关于前台服务器的说明 #707](https://github.com/holzschu/a-shell/issues/707)。这些是平台风险判断依据，不是本 fork 的真机测试结果。

## 最小架构

`auth_provider(cfg)` 集中识别三种 provider。没有 `auth_method` 的旧配置继续按照 `endpoint_type` 工作；`/endpoint` 也更新新的选择字段。原来的 OpenAI-compatible Chat Completions 和 Anthropic 实现仍保留。

- `shellclaude_lib/auth.py`：OAuth、HTTP 传输、发现元数据、ID token 校验、loopback listener、账号选择、凭据存储和刷新。
- `shellclaude_lib/responses.py`：Responses 消息转换、namespace 工具、模型目录和 SSE 解析。
- `shellclaude.py`：仅接入 provider、终端命令及会话记录，继续使用现有 agent loop、工具分发、AGENTS.md、审批和工具结果处理。

新模块按需导入，所有程序文件通过 install.sh 与 install.py 统一安装／更新，平台规则从独立 defaults/AGENTS.md 读取。ChatGPT 模式需要shellclaude_lib 内的模块与两个可选的纯 Python 包：PyJWT、rsa（后者依赖纯 Python pyasn1）。不使用 `PyJWT[crypto]` 或自己编写 RSA 运算。签名先由 rsa 校验，并严格要求 RS256/SHA-256，再用 PyJWT 校验 claims；不接受 `none`、HS256 或动态算法降级。如果官方改用其他算法，会停止登录，而非忽略签名。

## 登录和凭据边界

1. 在本地保存随机 UUIDv4 host ID，登出不改变它。
2. 先绑定 `127.0.0.1` 随机端口，再生成全新 state、nonce、PKCE verifier 与 S256 challenge。
3. 初次请求使用 `dynamic_agent_client` 和真实名称 `shellclaude`；后续请求复用所选注册的 issued client ID，省略名称提示。
4. 授权链接只输出到交互终端，用于在 Safari 打开。特意不带可选 `id_token_hint`，避免终端出现带 token 的 URL；代价是可能多一次账号选择。链接仍是一次性登录上下文，不应复制到问题报告或日志。
5. 回调检查 Host、路径、state、时限、重复参数、重复使用及 client ID。callback 的错误在 state 验证后处理。callback 请求日志完全禁用，不回显授权码。
6. 使用 issued client ID、原始 redirect URI、resource 和 verifier 换码。不会把动态注册入口 ID 用于换码或刷新。
7. 从固定官方 issuer 的 discovery 中获取 JWKS URL，只接受 `https://auth.openai.com` 的端点；HTTP 重定向一律拒绝。JWKS 缓存一小时，未知 kid 时更新。
8. 验证签名、issuer、audience、exp、iat、nbf（如有）、nonce、sub，以及多 audience 时的 azp。返回账号必须匹配已选择的注册。
9. 仅成功验证后激活账号。没有 direct scope 时保留身份登录，但拒绝订阅推理；`/login consent` 是用户主动重新请求授权。

凭据位置：`~/Library/Application Support/shellclaude/auth/`，与脚本旁 `shellclaude-data/` 内的普通 JSON 配置、SQLite 会话数据库分开。`host.json` 只包含安装标识；`credentials.json` 按 issued client ID 保存经过验证的 subject、账号标签和令牌。`/auth accounts` 展示稳定标签与可选的已验证邮箱，邮箱不能作为注册主键。

目录权限尽可能为 0700，文件为 0600；同目录临时文件、flush/fsync、`os.replace`，目录 fsync 在支持时执行。检测明显 iCloud 路径并拒绝存储。**这些文件未加密，不是 iOS Keychain**；Library 也可能进入系统备份，不能保证防备份或防恢复克隆。不要把该目录放进 iCloud、git 或支持附件；复制到另一设备运行时不要复用旧 host.json。卸载前可先 `/logout`，需要清除所有注册时对各账号依次登出，再删除凭据文件。

刷新使用跨进程非阻塞 `fcntl.flock`：无法获取锁或平台不支持锁时明确停止，以免多个进程消耗同一个轮换令牌。锁在进程退出时释放。令牌接近到期时刷新，access token、refresh token、scope 和到期时间一起替换。网络错误保留凭据；明确的终止刷新错误清除令牌并保留注册映射。磁盘故障发生在服务端轮换之后时，新令牌可能无法落盘，需要重新登录，这是本地文件存储不能消除的故障窗口。

`/logout` 从官方 discovery 获取撤销端点，尝试撤销 refresh token，再清除当前账号的全部令牌，保留注册映射与 host ID。撤销未确认时明确提示去 ChatGPT 设置断开应用。多账号的其他会话不自动登出。

认证响应、tokens、授权码和 verifier 不进入 debug hook。错误默认显示 HTTP 状态、代码、参数、请求 ID 和响应形状，避免回显可能包含凭据的服务端原文。运输异常和 JWT 异常经过净化。调试对象还会递归隐藏 token 字段、Bearer 值和回调／授权 URL。

OAuth 目录加入现有文件读写保护。现有 run_python／run_command／插件仍在用户权限下运行，**这不是针对恶意代码的进程沙箱**。不要批准访问凭据的命令；建议 `/safe on`，审查第三方插件。没有通过 monkey-patch Python 文件 API 等脆弱方式伪造隔离。

## 普通数据目录升级

普通数据路径由 `__file__` 的真实目录确定，统一放入 `shellclaude-data/`。不依赖当前项目路径，也不会因 `/cd` 改变。首次启动通过 `.migration.sqlite3` 序列化导入并记录完成状态；SQLite 锁在进程退出时释放，无需 fcntl。

支持 Documents 根目录与旧 shellclaude 子目录的已知配置／数据库布局；迁移 allowlist、plugins、skills、snippets、personas。只复制，不删除源，不覆盖已存在目标，不合并既有目录。配置先检查 JSON 对象；数据库通过只读连接和 backup 接口复制完整已提交快照，包括 WAL，然后执行 quick_check。配置和数据库通过同目录临时文件发布，资源目录先 staging 再改名。失败不标记完成，可以重试；成功后不复活用户后来删除的目标文件。迁移失败时停止启动，避免看起来像配置或会话丢失。

旧凭据配置的文件读取保护保留，避免升级备份被模型读取。OAuth host ID 和 tokens 不迁移，原 Library 存储与登录保持不变。普通数据目录本身仍可能位于 iCloud，保存的 API key 和历史记录不是加密文件，用户应选择本地安装目录。

旧版本留下的数据副本会继续留在原路径，确认升级正常后可手动清理。不要在新旧实例间同时写入；旧版本的后续改动不会自动同步。迁移不处理未知自定义路径、历史导出或临时文件，移动安装位置时应一起移动数据子目录。

## 推理、工具和会话

模型列表只使用所选账号的 `GET https://api.openai.com/v1/models`，读取 `models` 数组、筛选 `visibility: list`，展示 display_name 并提交 slug。不使用 API-key catalog 的 `data` 格式。

推理仅发送到公开的 `POST https://api.openai.com/v1/responses`。每次 `store:false`、`stream:true`，发送完整 input 历史，系统规则放 instructions／developer。省略 temperature、max_output_tokens、previous_response_id 和预览不支持的其他参数。不提供 hosted tool_search、托管 MCP、Code Interpreter 等能力。既有本地工具和本地调用的 MCP 工具仍作为 function tools 暴露，执行权限保持原有逻辑。

函数定义转为 `shellclaude` namespace 内的 Responses functions。SSE 支持文本 delta、分片函数参数、CRLF、多行 data 和注释；最终以 `response.completed` 的完整 output 为准。未完成、失败、格式错误、未知函数、重复 call_id、非法 arguments、取消或断流均不会交给 agent 执行。

会话数据库增加可空 `responses_output` 列，保留模型输出（包括加密 reasoning）和 call_id，以便下轮传回正确的 function_call_output。这不是 OAuth 凭据。旧 provider 发送前会剥离 Responses 内部字段。既有会话可自动加列，不迁移 API key 配置。手工删改／导入不完整工具回合仍可能导致服务端拒绝历史；应保留完整调用与结果，或用 `/new` 重新开始。

即使 `/stream off`，ChatGPT 运输层仍然流式读取，只在完成后显示文本。CLI 的 max_tokens/temperature 设置仅用于旧 provider；ChatGPT 模式不会发送它们。订阅调用不计算 API 费用，界面的美元数字只反映旧 API 路径的估算，不代表订阅剩余额度。使用 ChatGPT 设置里的 Usage 查看应用授权和限额。

没有从失败的订阅调用静默切换到 API 计费，也不自动重放 POST。401 本身不等同于撤销确认，不立即删除凭据；应检查 scope/账号，必要时显式重新登录。503 等暂时失败可以稍后由用户重试。流已经开始后，仍等待终止事件来检测用量错误。

## 云端验证范围

使用 Python 3.13 执行语法检查和 `unittest`；不使用个人账号登录或进行真实推理。所有外部 HTTP 均被模拟；签名测试现场生成临时测试 RSA key，不保存私钥。只有 loopback 探针测试使用 Linux 本机 TCP。

覆盖：授权 URL、随机性／PKCE、state／nonce／client ID、防重放／过期、换码、有效与伪造 RSA 签名、claim 错误、JWKS 刷新、身份隔离、拒绝 scope、刷新轮换、终止与暂时错误、原子写入、权限、损坏凭据、文件锁、登出、模型目录、SSE、函数调用／结果续接、reasoning 会话持久化、失败流不执行工具、错误净化，以及旧 provider 和安全审批回归。

```sh
python3 -m pip install -r requirements-chatgpt.txt
python3 -m py_compile shellclaude.py shellclaude_lib/auth.py shellclaude_lib/responses.py
python3 -m unittest discover -s tests -v
```

受限开发沙箱可能禁止 bind/listen；需要允许本机 socket 的环境运行探针测试。不能将该权限错误解释成 a-Shell 不支持 TCP。

## 非越狱 iPad 手工验收（尚待执行）

记录设备型号、iPadOS 版本、App Store a-Shell 版本、`python3 --version`。全程不要提交 tokens、回调地址参数、host ID 或凭据文件。

1. 按照 README 运行 sh install.sh 安装全部程序和独立规则文件；已有用户在原目录更新。安装 ChatGPT 登录需要的纯 Python 依赖；运行语法检查。
2. 创建无敏感数据的测试项目文件 `sample.txt`。启动 `python3 shellclaude.py`，执行 `/auth probe`。在 Safari 打开终端显示的本机地址，同时保持两款应用可见。预期网页显示 callback received，终端显示成功。此时没有 OAuth 或 tokens。
3. 再做一次 probe，这次完全切到 Safari；分别观察立即切换、等待约 30 秒、锁屏后返回等情形。超时或无法访问请记录布局／时间，不要反复刷新已经带 OAuth code 的真实回调。线程不能消除后台挂起。
4. `/login` 开始真实授权。保持分屏，使用 Safari 打开 OpenAI 授权链接，确认应用名为 shellclaude、授权使用 ChatGPT plan。完成后返回 a-Shell。应显示已登录及 plan usage enabled；若禁用，则 `/login consent`，或主动选择 API-key provider。
5. `/auth status` 不应显示任何 token；`/auth accounts` 应显示稳定标签。检查本地凭据目录权限（不读取、截图或输出文件内容）。若报文件锁不支持，停止 OAuth 验收并记录错误；不要移除锁来强行继续。
6. `/model` 列出账号可见模型，`/model <返回的slug>` 选择一个。`/safe on`，输入“读取 sample.txt，总结内容”。允许读文件后，确认工具调用与后续文本正常。
7. 在测试项目中请求编辑文件，确认差异与写入审批仍出现；先拒绝一次，再明确允许。测试文件搜索与 a-Shell 允许的简单命令。若设备已安装相应编译器，分别测试小型 C／C++ 程序；编译器／WASM 执行兼容性属于原项目能力，不由云端推断成功。
8. 放置简单 AGENTS.md，`/agents reload`，验证规则生效。`/sessions` 查看 ID，退出重开 `/load <id>`，继续提问，确认完整工具历史能续接。
9. 同一登录保持约一小时后再发请求，验证自动刷新与重启恢复。不要通过手工改凭据 expiry 或复制 refresh token 来测试轮换。
10. `/login new` 添加另一个有权测试的账号／workspace；`/auth accounts`，`/auth account account-1` 切回，再 `/model`。未验证身份的失败登录不得替换旧账号。
11. 测试期间关闭网络，观察明确错误；恢复后凭据仍应可用。流式处理中断时不得执行尚未完成的工具；再次请求必须由用户明确发起。
12. `/logout` 后 `/auth status` 应显示登出；重新登录应复用该注册和 host。再从 ChatGPT 设置断开应用，后续请求或刷新应停止并提示恢复操作，不循环登录或切换计费。
13. 显式 `/auth method openai_api`、`/endpoint openai`、`/url <自己的兼容端点>`、`/model <API模型>`，确认旧 key 工作；Anthropic 同理。不要把订阅模型名直接当作 API 账号可用模型。

iPhone 无法保证两款应用同时保持前台，首次 OAuth 的可靠性风险更大；若 probe／登录失败，保留 API-key 路径，等待平台或官方协议提供合适方案。

## 仓库所有者操作

审查并提交新增模块、测试和文档；发布包含 install.py、程序模块和 defaults 目录的分支／release，再把 README 的发布引用更新为实际引用。完成上述真机检查后记录结果和版本，才可将“实验性／未验证”标记改成对应的已验证范围。当前实现不要求申请另一个应用的 client ID，不需要 client secret，也不应导入 Codex CLI 凭据。
