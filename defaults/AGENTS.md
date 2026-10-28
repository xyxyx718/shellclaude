# shellclaude：a-Shell / iOS 平台规则

本文件描述本机运行约束，与项目自己的 AGENTS.md 一起使用。项目规则可以补充编码规范、测试方法和业务背景，但不能创造系统没有的能力，也不能取消工具的审批和安全检查。

## 先确认环境

- 当前目标是非越狱 iOS/iPadOS 上的 App Store a-Shell，使用其 Python 3.13；不是 Linux、macOS 桌面或远程开发服务器。
- 不假定存在 Bash、Node.js、npm、Rust、Docker、桌面 Codex CLI、GNU 工具链、root 权限或守护进程。项目里有 package.json / Cargo.toml 不代表本机能运行它的构建工具。
- 使用 python3。优先 Python 标准库和纯 Python 依赖；不能指望 pip 在设备上编译 C/Rust 扩展。只有实际确认已内置的扩展才可使用。
- 安装前检查命令是否存在和是否适用于 a-Shell；不要建议 apt、brew、sudo、系统权限绕过或下载 Linux 可执行文件。pkg 只能安装其实际提供的 a-Shell 命令。

## 命令必须适合本应用

- 每次只执行一个短小、前台、非交互命令，独立工具调用也顺序执行。不要创建子代理、后台任务或常驻服务器来绕开执行限制。
- shellclaude 的命令过滤器禁止管道 |、后台 &、命令替换 $(...) / 反引号、进程替换、分号串联和 Bash here-string。即使某版本 a-Shell 在别处支持部分语法，本应用仍不接受它们。不要编码、嵌套 sh 或换工具来绕过过滤器。
- 使用 read_file / list_files / search_files，避免 grep 管道。没有 rg 时使用 Python 遍历和搜索。先读再改，优先 patch_file。
- 避免通过 run_command 启动 vim、交互式 Python、分页器或会等待输入的命令。不要在 Python 代码里使用 subprocess、fork、multiprocessing 或 shell=True 来假定能启动原生子进程；使用现有工具提供的执行入口。
- 不假定 BSD 命令支持 GNU 参数（例如 sed -i、grep -P、find -printf、readlink -f、date -d）。不确定时先看本机帮助，或用 Python 实现。
- 对 Git 优先考虑 a-Shell 的 lg2 或本应用 /git，先确认可用操作。C/C++ 通常编译成 WebAssembly，不是可直接运行的 Linux ELF 或 iOS 原生程序；使用既有 run_c / run_cpp，并以本机实际结果为准。

## 路径与文件安全

- 从运行时上下文取得当前项目目录和数据目录；不要硬编码 Linux /home、容器 /workspace 或 iOS 容器 UUID。
- 文件访问受 iOS 沙箱及用户授予的目录访问权限限制。需要其他目录时由用户通过文件选择器授权，不尝试越权访问其他 App 或系统目录。
- 使用 pathlib、os.path.expanduser、tempfile 处理路径；正确处理空格和 Unicode。不要假定所有 /tmp、/var、/usr 等桌面路径在这里都可写。
- shellclaude-data 保存应用配置、会话、允许规则和自定义资源，不是项目源码。OAuth 资料单独在本机 Library；不要读取、复制进上下文、打印、上传或修改认证资料。
- 不覆盖配置、数据库、允许规则、平台 AGENTS.md 或插件来取消保护。按工具提示让用户批准写入和命令；用户拒绝后停止该操作。a 表示进入允许范围选择，不擅自扩大为通配授权。
- 不随意删除数据或把明文 API key、会话库和登录资料同步到 iCloud / Git。移动安装位置时保留 shellclaude-data；不要修改 host ID 来伪造新安装。

## 网络、挂起和验证

- WASM 的限制不等于 Python 的全部限制；不要一概声称本机 Python 不能监听 loopback。只有实际测试才能确认当前版本的网络能力。
- 切到 Safari、后台或锁屏可能暂停 a-Shell；线程、asyncio 和额外窗口不保证后台存活。OAuth 使用官方支持的回调；有问题先做 /auth probe，iPad 上让 Safari 和 a-Shell 同时可见。
- 不提取其他应用凭据、不借用客户端身份、不使用未记载的认证接口；订阅失败时不自动改用 API 计费。
- 区分“代码已修改”“模拟测试通过”“设备上真实运行成功”。Linux 的测试结果不能证明 iPad 可用，未安装的编译器或没有凭据的真实调用必须如实说明。
- 报错后先检查：命令是否存在、语法是否被本应用限制、沙箱路径、依赖是否需要原生扩展、网络是否中断。不要反复执行相同失败命令。
- 回复使用用户的语言，适合手机阅读；给出短小、可直接复制的命令，并说明真正需要用户操作的部分。
