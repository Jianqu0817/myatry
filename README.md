# myatry

独立的小温暖运行项目。`src/myatri` 是冻结的 MYATRI 0.2.8 核心，`src/myatry` 是群记忆、电脑任务、管理窗口与直接微信传输。一个 Python 3.12 / uv 环境，不需要安装 MYATRI wheel、AstrBot 或插件。来源和许可见 `docs/SOURCES.json`、`LICENSES/`，接口见 `docs/ARCHITECTURE.md`。

在本项目目录执行（Windows 需安装 uv 与 Python 3.12）：

```powershell
uv sync --frozen --python 3.12
.venv/Scripts/python.exe -m myatry prepare
.venv/Scripts/python.exe -m myatry check
```

也可运行 `Setup.cmd`；`Run.cmd` 后接相同子命令。`prepare` 只在缺失时创建 bridge/wechat 本地模板、管理令牌与 Harness 本地模板，不覆盖已有文件。默认空白白名单，不导入记忆。`check` 检查导入、TOML、角色卡、管理令牌与库路径，不联网、不连接微信、不读取模型凭据、不调用模型；通过不表示微信或模型已经可用。

所有子命令支持 `--config config/bridge.local.toml`。`prepare/check/bind/wechat` 还支持 `--wechat-config config/wechatagent.local.toml`。配置文件位于项目的 `config/` 目录，库路径相对项目根目录。

默认未配置本地模型权重时使用 TF-IDF，不安装神经嵌入依赖。有已核验的 BGE 模型目录时，执行 `uv sync --frozen --extra neural`，再将 `config/engine.toml` 的 `[embedding].path` 指向该模型目录。模型文件不随源码提供，也不会在聊天时自动下载；配置已有 BGE 时必须装此可选依赖。

## 微信与第一次绑定

外部 WeChatAgent reader/native 必须已经安装、配置且匹配微信 **4.1.10.27**。本项目只保留 HTTP 契约，不提供完整微信客户端、DLL、reader 或上游 Node 环境。使用外部组件自己的安装/启动入口，亲自完成微信登录；启动前先正常退出旧项目启动器，端口冲突会直接报错。

编辑 `config/wechatagent.local.toml`，把 `project_dir` 指向实际外部运行区（绝对路径或相对本项目路径均可），填写准确 `group_name`、`bot_nickname`。该目录应有 `state/wechat-io-config.json` 和 `state/hook/native-token.txt`；已有不同相对路径时修改 `io_config/native_token`。保留外部账号配置与令牌，不复制到源码，也不把运行区限制在旧项目 `var`。

先手动打开外部配套微信并完成登录。reader 运行区须已按外部组件流程配置认证读取凭据；本项目不安装后端或提取密钥。单独开一个终端，只启动 reader：

```powershell
.venv/Scripts/python.exe scripts/wechat_reader.py --config config/wechatagent.local.toml
```

该脚本从配置的 `project_dir/config/distribution.json` 读取 Python 路径，在外部工作区前台运行 `scripts/reader-api.py`；Ctrl+C 退出自己的 reader，不启动旧 Bridge 或 AstrBot。保持该终端运行，再开一个终端绑定并启动：

```powershell
.venv/Scripts/python.exe -m myatry bind
.venv/Scripts/python.exe -m myatry wechat --offline --ui
```

`bind` 连接本机认证接口，核对账号/版本后获取真实群与机器人ID，写入微信本地配置和 bridge 白名单，不根据昵称猜ID。群聊平台ID沿用 `xiaowennuan-wechat`。`--offline` 使用测试回复，但仍连接真实微信且可能发送测试回复；只观察接收时加 `--observe-only`。`--live` 才允许模型调用。运行时 Ctrl+C 正常退出；不自动停止已有微信/reader或其他进程。

管理API绑定 `127.0.0.1:8766`，使用 `var/bridge-token.txt` 的 Bearer 令牌。`--ui` 同进程提供 [监视窗口](http://127.0.0.1:8767/) 与 [上下文窗口](http://127.0.0.1:8768/)。窗口沿用 `config/bridge.local.toml`，因此自定义其他配置文件运行时不要加 `--ui`。监视窗口可调整概率/恢复开关；上下文窗口可能显示本地会话内容。

## 模型、搜索与电脑操作

模型配置在 `config/engine.toml`。在交互终端保存凭据：

```powershell
.venv/Scripts/python.exe -m myatri.credentials store-deepseek
```

凭据存入当前 Windows 用户凭据管理器；也支持进程环境变量 `DEEPSEEK_API_KEY`。`--live` 会使用该用户已有凭据。修改模型、思考和预算参数后再显式选择 live。

联网搜索默认关闭；在 bridge 本地配置 `[bridge.web]` 设置 `enabled=true`，默认 `deepseek-api` 后端复用聊天凭据，受本地每日限额和缓存约束。只在 live 下使用。

电脑操作只授予 **菅蘧的稳定 owner_id**，展示名不能作为认证。将 `[bridge].owner_id/owner_name` 填为用户确认的真实ID/菅蘧，`[bridge.computer].owner_id` 必须一致。安装固定 Harness（需要 Node 与 pnpm）：

```powershell
.venv/Scripts/python.exe scripts/setup_computer_control.py --node C:/path/node.exe --pnpm-js C:/path/pnpm.cjs --full
.venv/Scripts/python.exe scripts/harness_cli.py --version
```

使用 Node **22.19+** 与 **pnpm 10**。例如先取得 pnpm 的 JS 入口，再把实际入口路径传给安装脚本：

```powershell
& C:/path/node.exe --version
npm install --prefix var/pnpm pnpm@10
.venv/Scripts/python.exe scripts/setup_computer_control.py --node C:/path/node.exe --pnpm-js var/pnpm/node_modules/pnpm/bin/pnpm.cjs --full
```

安装脚本使用 `config/harness/` 锁文件，把运行依赖放到新项目 `var/harness/`；不启动服务。配置在 `config/harness/executor.local.json`，电脑执行与自然语言任务结果沿用同轮回传协议。live + 已绑定 owner + 启用电脑配置后才使用；保持原任务去重、暂停/取消与 unknown 发送结果不重发规则。

## 同库终端与旧记忆迁入

先 Ctrl+C 退出群聊，再运行：

```powershell
.venv/Scripts/python.exe -m myatry chat --offline
# 多个已有群库时选择编号，或增加 --scope 32位scope
.venv/Scripts/python.exe -m myatry chat --live
```

终端复用已有授权群的同一库，关闭主动群发；`/exit` 退出，`/retry` 延续上条消息。`var/service.lock` 使终端/群聊互斥。没有群路由时明确报错，不虚构新群库；先通过微信建立真实群会话，或迁入已有完整群库。

迁入必须由用户显式执行：正常停止旧启动器及新群聊/终端，备份新旧目录，确认 SQLite 连接已关闭，再把旧项目完整 `var/groups` 复制到新项目 `var/groups`（自定义 `state_root` 则复制完整对应目录）。保留全部归档、数据库及配套文件；不要只复制某一个 SQLite。旧项目保留原状。将旧配置中确认后的平台/群/机器人/owner ID、白名单及必要业务参数手动合入新本地配置，保持平台ID、scope与身份；凭据不复制进配置。旧 `state_root` 为绝对旧路径时改为新项目相对路径；本次准备不会迁入真实配置或记忆。然后运行 check，先 offline 确认同库，再自行选择 live。

## 修改与最少验证

核心记忆/情绪在 `src/myatri/`；消息和投递在 `src/myatry/bridge.py`；微信契约/直传在 `wechat_client.py`、`wechat.py`；统一入口在 `__main__.py`、`launch.py`；管理窗口在 `server.py`、`monitor.py`、`context_viewer.py`。修改传输时保留 `receive → claim → acknowledge`，不要复制第二套 Bridge。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_entry.py tests/test_wechat.py -q --basetemp var/test-run
uv build
```

测试均使用临时配置/假后端，不需要登录或付费模型。续接状态见 `docs/STATUS.md` 与 `docs/HANDOFF.md`。
