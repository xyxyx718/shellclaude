# shellclaude
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org)
![Platform](https://img.shields.io/badge/Platform-iOS%20%2F%20iPadOS-brightgreen)

Terminal coding tool for [a-Shell](https://github.com/holzschu/a-Shell). Supports OpenAI and Anthropic API-key endpoints, plus experimental official Sign in with ChatGPT.

## shellclaude v1.6.0

## ChatGPT 订阅登录（实验性，尚待 iPad 真机验证）

本 fork 新增 `chatgpt` provider：通过官方 Sign in with ChatGPT 动态注册和 PKCE 登录，使用公开 Responses API 进行文本与本地工具调用。保留 `openai_api` 与 `anthropic` 的 API-key 路径，旧配置无需迁移。**没有执行过真实 OAuth 登录或订阅推理；Linux 测试通过不等于 iOS 可用。**

需要符合 OpenAI 当前条件的 ChatGPT 账号／workspace（目标为有资格的 Plus／Pro 用户），并同意 `chatgpt.tokens.use.direct`。最终是否可用取决于账号、地区、workspace 策略和服务端授权。订阅用量与 API 账单是不同路径，不会自动改用 API key 计费；订阅也不意味着无限调用。查看 [ChatGPT 设置 → Usage](https://chatgpt.com/#settings/Usage) 管理应用用量与授权。

### Quick start：在 a-Shell 直接下载

在 App Store a-Shell 中执行以下命令。三个 Python 文件放在同一目录，无需下载或解压整个仓库：

```sh
cd ~/Documents
python3 -m pip install rich 'PyJWT>=2.10,<3' 'rsa>=4.9.1,<5'
curl -fsSL https://raw.githubusercontent.com/xyxyx718/shellclaude/feature/chatgpt-sign-in/shellclaude.py -o shellclaude.py
curl -fsSL https://raw.githubusercontent.com/xyxyx718/shellclaude/feature/chatgpt-sign-in/shellclaude_auth.py -o shellclaude_auth.py
curl -fsSL https://raw.githubusercontent.com/xyxyx718/shellclaude/feature/chatgpt-sign-in/shellclaude_responses.py -o shellclaude_responses.py
python3 shellclaude.py
```

使用 a-Shell 提供的 Python 3.13，不需要越狱、Node、Rust、Codex CLI、Docker 或远程服务器。上述链接指向本实现的 [feature/chatgpt-sign-in 分支](https://github.com/xyxyx718/shellclaude/tree/feature/chatgpt-sign-in)。

`rich` 是原项目可选的显示依赖；新登录只增加纯 Python PyJWT、rsa 及 rsa 的 pyasn1 依赖。不要安装 `PyJWT[crypto]`，不需要编译扩展。只用旧 API-key provider 时，仍可继续单文件安装，不需新依赖。

### 首次登录与模型选择

先让 Safari 与 a-Shell 在 iPad 上分屏／多窗口同时可见，然后在 shellclaude 中输入：

```text
/auth probe
/login
/auth status
/model
/model <上一步返回的slug>
/safe on
```

`/auth probe` 提供无凭据的本机连通性测试地址。在 Safari 打开后，终端应报告收到回调。它只验证当前布局的 TCP 连通性，不验证 OAuth。

`/login` 显示 **Continue with ChatGPT** 和一次性授权链接。使用 Safari 打开并授权，自动回到 `127.0.0.1` 后返回 a-Shell 完成验证。请勿把链接或真实回调参数贴进日志／问题报告。登录成功会选择 ChatGPT provider；`/model` 每次读取所选账号目录，展示名称与可用 slug。

首次注册使用应用名 shellclaude；重登录复用 issued client ID 和同一安装 host ID。若只获得身份登录、未授权订阅使用，`/auth status` 会提示禁用推理。可主动 `/login consent` 重新授权，或选择 API-key 路径。

### 认证命令

| 命令 | 功能 |
|---|---|
| `/login` | 登录／重新授权当前注册 |
| `/login new` | 添加另一个账号／workspace 注册 |
| `/login consent` | 主动重新请求使用订阅计划的授权 |
| `/logout` | 尝试撤销当前会话并删除其本地 tokens |
| `/auth status` | 查看状态，不显示 tokens |
| `/auth method` | 查看认证方式 |
| `/auth method chatgpt` | 使用已保存的 ChatGPT 登录 |
| `/auth method openai_api` | 使用原 OpenAI-compatible API-key 路径 |
| `/auth method anthropic` | 使用原 Anthropic API-key 路径 |
| `/auth accounts` | 查看稳定账号标签和已验证邮箱（如有） |
| `/auth account account-1` | 切换已保存注册；之后重新 `/model` |
| `/auth probe` | 不登录，仅测试 Safari → loopback 连通性 |

切换 provider 后使用 `/model` 选择该路径的模型。ChatGPT 的 `/url` 不影响固定官方推理地址；`/stream off` 只关闭即时显示，HTTP 仍按照协议流式读取。ChatGPT 不发送 temperature、max_tokens 或 max_output_tokens；这些设置仍适用于旧 provider。美元费用估算不是订阅用量。

### 原 API-key 使用方式

```text
/endpoint openai
/url https://api.openai.com/v1
/config api_key=自己的API密钥
/model 自己API账号可用的模型
```

Anthropic 使用 `/endpoint anthropic` 和自己的 key、model；也可继续通过 `OPENAI_API_KEY`／`ANTHROPIC_API_KEY` 环境变量配置。保留原来的环境变量优先级，环境密钥不保存到配置。配置和会话现在统一保存在 `shellclaude.py` 旁的 `shellclaude-data/` 子目录中；历史版本升级会自动迁移，详见下节。

### 数据目录与历史版本升级

普通运行数据统一放在 **`shellclaude.py` 所在目录的 `shellclaude-data/`**，不是启动命令时的当前目录。例如脚本放在 `~/Documents/shellclaude.py` 时，数据位于 `~/Documents/shellclaude-data/`。切换项目或执行 `/cd` 不会换一套配置。

**通常不需要手动迁移，也不需要重新登录。** 首次启动新版时会自动导入：

- `~/Documents/shellclaude.json` 和 `~/Documents/shellclaude.db`；也兼容较早的 `~/Documents/shellclaude/` 内同名文件。两处都有时优先使用 Documents 根目录的版本。
- `~/Documents/shellclaude/` 内的 allowlist、plugins、skills、snippets、personas。

新目录已有的文件或资源目录不会被覆盖或合并。旧文件会保留作升级前备份；确认配置、会话及插件都正常后，退出旧实例，再自行删除不需要的旧文件即可。历史导出文件和临时文件不自动迁移，新导出默认保存到 `shellclaude-data/exports/`。

会话迁移使用 SQLite backup，包含已提交但尚未 checkpoint 的 WAL 内容。迁移失败时停止启动并保留源文件；排除权限、配置损坏或数据库占用问题后重试。不要同时运行新旧版本。迁移成功后只使用新路径，不再从旧路径反复导入。非标准自定义路径仍需自行确认；以后移动安装目录时，请将 `shellclaude-data/` 和脚本一起移动。

OAuth 凭据继续单独放在本机 Library，不放入项目目录；本次路径调整不改变已有 ChatGPT 登录。

### 凭据与登出

OAuth 资料独立保存在 `~/Library/Application Support/shellclaude/auth/`，尽可能使用 0700／0600 权限、原子写入与跨进程锁。它们不进入配置、会话 DB 或调试输出。access token 自动到期刷新，轮换后的 refresh token 一并替换。文件损坏时停止使用并提示恢复，网络暂时失败不会清除凭据。

`/logout` 只退出当前注册；要退出所有账号，请逐个切换后登出。注册映射与 host ID 保留以便重登录。远程撤销未确认时会提示到 ChatGPT 设置断开应用。凭据文件未加密，也不是 Keychain；请勿共享、上传或放入 iCloud，系统备份行为仍需自行管理。

### 已知限制与排错

- **Safari 无法访问／回调超时**：iOS 可能暂停后台 a-Shell。先运行 probe，让两款应用同时可见，保持设备解锁；超时后用新的 `/login` 重试。线程不能解决系统暂停。iPhone 上可靠性风险更大。
- **没有其他回调方式吗？** 目前这条官方动态注册流程只记载 `127.0.0.1` HTTP 回调。未加入未记载的 device-code、自定义 scheme 或粘贴回调方案。
- **文件锁不支持**：为防止 refresh-token 轮换竞争，OAuth 会明确停止；请记录 a-Shell／iPadOS 版本，使用 API-key provider。不要移除锁强行继续。
- **订阅权限禁用／401／403**：检查账号、授权 scope、地区与 workspace 策略；必要时显式 `/login` 或 `/login consent`。不会循环登录或偷偷切换 API 账单。
- **用量限制**：暂停新请求，在 ChatGPT Settings → Usage 检查应用限额；不能仅凭错误码推断整个套餐耗尽或重置时间。
- **503／网络故障**：稍后手动重试。推理 POST 与中断流不会自动重放；未收到 completed 时不会执行流中的工具调用。
- **签名验证失败**：检查时间与网络；不能关闭验证绕过。当前实现只接受 RS256，协议算法变化需要更新实现。
- **会话历史错误**：不要手动保留只有一半的工具调用／结果；必要时 `/new`。保留原有工具审批，建议 `/safe on`；已批准的任意代码仍拥有 a-Shell 用户权限。

完整协议架构、安全边界、自动化测试与逐步真机验收见 [技术说明](docs/CHATGPT_AUTH.md)。

## Tools

shellclaude exposes these to the model:

- **read_file** — read code or configs, with line ranges for big files
- **write_file** — create or overwrite files (shows diff first, makes .bak)
- **patch_file** — surgical edits, preferred over write_file for changes
- **run_command** — shell commands; pipes blocked for a-Shell compatibility
- **search_files** — ripgrep across your project, respects .gitignore
- **list_files** — directory listing, recursive capped at 3000 entries
- **web_search** — DuckDuckGo search with UA rotation and retry
- **read_url** — fetch docs or GitHub raw files, strips HTML
- **run_python** — Python 3 snippets with traceback parsing
- **run_c** — compile and run C via clang (-std=c11). Requires `pkg install clang`
- **run_cpp** — compile and run C++ via clang++ (-std=c++17). Requires `pkg install clang`

Cancel a running turn with `c` + Enter.

## Commands

### Setup
| Command | What it does |
|---------|--------------|
| `/config [key=val]` | Show or change settings |
| `/model <name>` | Switch model |
| `/url <base_url>` | Set API URL (OpenAI mode) |
| `/endpoint [openai\|anthropic]` | Switch provider |
| `/stream [on\|off]` | Toggle streaming |
| `/debug [on\|off]` | Print full API traffic |

### Sessions
| Command | What it does |
|---------|--------------|
| `/new [name]` | New session |
| `/sessions [tag]` | List past sessions, optionally filtered by tag |
| `/load <id>` | Load a session |
| `/delete <id>` | Delete one |
| `/clear` | Wipe context, keep session |
| `/compact` | Force context summarization |
| `/context` | Show tokens, cost, usage bar |
| `/branch [name]` | Branch from current session |
| `/tag [id tags]` | Tag for searching |
| `/export [file]` | Save as Markdown |

### Context control
| Command | What it does |
|---------|--------------|
| `/rewind [N]` | Drop last N turns |
| `/forget <N>` | Drop last N messages |
| `/pin [index]` | Pin a message (survives compaction) |
| `/unpin <index>` | Unpin |
| `/import <sid> <idx...>` | Copy messages between sessions |

### Tools
| Command | What it does |
|---------|--------------|
| `/websearch <query>` | Search, inject results |
| `/browse <url>` | Fetch page, inject text |
| `/read <path>` | Inject file into chat |
| `/run <cmd>` | Run command, inject output |
| `/ls [path]` | List directory |
| `/search <pattern>` | Ripgrep search |
| `/cd <path>` | Change directory |
| `/pwd` | Current directory |

### System
| Command | What it does |
|---------|--------------|
| `/system [text]` | View or set system prompt |
| `/system save <name>` | Save prompt as template |
| `/system load <name>` | Load template |
| `/system list` | List templates |
| `/system reset` | Restore default prompt |
| `/agents [reload]` | Show or reload AGENTS.md |
| `/pick [dir]` | iOS file picker, inject selection |
| `/format [json\|yaml\|none]` | Structured output mode |

### Safety
| Command | What it does |
|---------|--------------|
| `/safe [on\|off]` | Confirm every tool call |
| `/diff [on\|off]` | Dry-run mode: show diffs, don't write |
| `/budget <N>\|off` | Limit iterations this turn |
| `/thinking-budget <N>\|off` | Anthropic extended thinking |
| `/allowlist [add\|rm]` | Manage command allowlist |

### Git
| Command | What it does |
|---------|--------------|
| `/git [s\|d\|l\|commit...]` | Shortcuts: status, diff, log, commit |

### Project smarts
| Command | What it does |
|---------|--------------|
| `/scope` | Detect project type (Cargo.toml, package.json, etc.) |
| `/scope inject` | Inject project info into context |

### Developer helpers
| Command | What it does |
|---------|--------------|
| `/clip` | Copy last AI response to clipboard |
| `/save <name>` | Save last response as snippet |
| `/persona <name>` | Switch persona file |
| `/persona list` | List personas |
| `/persona reset` | Restore default system prompt |
| `/skills [reload\|<name>]` | Load SKILL.md guides |
| `/trail [clear]` | View edit history |
| `/undo` | Revert last file write |
| `/edit --revert <path>` | Restore from .bak |
| `/cache [list\|clear]` | Tool result cache |

### MCP and plugins
| Command | What it does |
|---------|--------------|
| `/mcp add <name> <url>` | Connect MCP server |
| `/mcp list` | List servers |
| `/mcp remove <name>` | Disconnect |
| `/mcp tools <name>` | List server tools |
| `/plugins` | List loaded plugins |

### Help and exit
| Command | What it does |
|---------|--------------|
| `/help` | Show all commands |
| `/exit` | Quit |

## Key features

### Phase labels
Tool calls get colored labels: gathering, acting, verifying. You know what the model is doing at a glance.

### Context management
- **Auto-compaction**: Old messages summarize automatically when you approach the model's context limit (~80%)
- **Pinned messages**: `/pin` marks context that survives compaction
- **Cost tracking**: Running dollar total per session, tracked per message
- **Context bar**: Token usage as a visual percentage

### Project detection
Auto-detects Cargo.toml, package.json, pyproject.toml, go.mod, and others. Pulls out name, version, scripts. Inject with `/scope inject`.

### AGENTS.md
Drop an `AGENTS.md` in your project root. shellclaude finds it up the directory tree, loads it, and appends the rules to the system prompt. Capped at 8000 chars. Checks for injection patterns. Reloads on directory change.

### Web search
Uses `html.duckduckgo.com/html/` -- the stable static DDG page. No API key needed. Rotates user-agents, retries with backoff on failure, surfaces errors instead of returning empty results silently.

### C and C++
`run_c` and `run_cpp` compile via a-Shell's clang package. Install once with `pkg install clang`. Compile errors are shown separately from runtime output. Pass extra flags like `-lm` or `-O2` via the `flags` parameter. Compiler output-redirecting flags (`-o`, `-MF`, `-include`, etc.) are stripped automatically.

### Security
- **SSRF protection**: Blocks private IPs and metadata endpoints in URL fetching and MCP connections
- **Sensitive files**: Blocks reads of .env, .pem, credentials, SSH keys, and the command allowlist
- **Safe mode**: Every tool call needs confirmation
- **Diff mode**: Preview changes without writing
- **Allowlist**: Auto-approve trusted commands with prefix wildcards (`cargo *`)
- **Symlink checks**: Warns if config resolves outside ~/Documents
- **Blocked patterns**: Dangerous constructs blocked -- rm -rf /, sudo, credential exfiltration, semicolon chaining, and more
- **a-Shell safety**: Pipes, command substitution, background jobs, and here-strings are blocked. These hang a-Shell permanently because it runs everything in a single process.

### Session persistence
SQLite at `<script directory>/shellclaude-data/shellclaude.db`:
- Full message history
- Working directory restoration
- Branching and tagging
- Pinned message preservation
- Per-message cost tracking

## iOS specifics

- **Sandboxed**: Write only to `~/Documents`, `~/Library`, `~/tmp`
- **Python 3.13** built in. Use `python3`, never `python`
- **Packages**: `pkg install <name>` for WASM commands
- **No bash**: POSIX `sh` only
- **No pipes**: Single-process architecture
- **BSD tools**: `sed -i ''`, `grep -E`, `date -v` -- not GNU
- **C/C++**: Compiles to WASM via `clang`. No sockets, no fork
- **Git**: Uses `lg2` (libgit2 wrapper)

## Config

```json
{
  "api_key": "",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4o",
  "max_tokens": 8192,
  "temperature": 0.8,
  "system": "",
  "format": "none",
  "stream": true,
  "endpoint_type": "openai",
  "plugins_enabled": true,
  "mcp_servers": {}
}
```

Priority: `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` env vars beat config file. Env keys never get written to disk.

## Directory layout

```
<shellclaude.py 所在目录>/shellclaude-data/
├── shellclaude.json      # Config
├── shellclaude.db        # Session database
├── allowlist.txt         # Command allowlist
├── .migration.sqlite3    # One-time legacy migration state / lock
├── exports/              # Exported conversations
├── .tmp/                 # Temp files for run_c, run_cpp, run_python
├── plugins/              # Python plugin files
├── snippets/             # Saved responses
├── personas/             # Persona .md files
└── skills/               # SKILL.md guides
```

## Plugins

Check [plugins.md](https://github.com/Dav31012/shellclaude/blob/main/plugins.md)

## Skills

Create `<脚本目录>/shellclaude-data/skills/<name>/SKILL.md`. Content gets injected into the system prompt under `## Skills`.

## Personas

Drop `.md` files in `<脚本目录>/shellclaude-data/personas/`. Switch with `/persona <name>`.

## Security notes

- **API keys**: Config stores plaintext. On iOS this may sync to iCloud. Use env vars instead.
- **Plugins**: Run with full Python stdlib access. Only install what you trust.
- **MCP servers**: Non-HTTPS MCP URLs are rejected for non-localhost URLs.
- **History**: `shellclaude.db` stores everything unencrypted.
- **Secrets**: No automatic redaction in chat input. Don't paste credentials into the conversation.
- **Paths**: Protected paths stop self-modification, but symlink edge cases might exist.

## Requirements

- a-Shell on iOS (or any POSIX terminal)
- Python 3.13+
- `rich` (optional, for colors)
- API key for OpenAI, Anthropic, or compatible endpoint

## Legal disclaimer

"shellclaude" is an independent, open-source project. It is not affiliated with,
endorsed by, or sponsored by Anthropic PBC. "Claude" is a trademark of
Anthropic PBC. This tool uses Anthropic's API and is inspired by the general
concept of agentic coding assistants, but is not Claude Code or any other
Anthropic product.

## License

MIT
