# 后续修改入口

先读根README、AGENTS.md与ARCHITECTURE.md。新项目不需要AstrBot或旧项目源码运行；旧项目只读，来源及适配见SOURCES.json。

## 运行与配置

- Setup.cmd安装基础环境并prepare/check；Run.cmd转发统一子命令。chat/wechat必须显式--offline或--live。
- 微信：配置wechatagent.local.toml实际project_dir，用户打开匹配微信登录；另开终端运行scripts/wechat_reader.py；bind保存认证ID/白名单，再wechat --ui。
- local_chat延续已有授权群库，与微信共享service.lock。旧库由用户停机后完整复制，合并owner/白名单等真实绑定；不创建虚构群库。
- live用当前Windows凭据；Harness另装锁定运行时。BGE按需--extra neural，不自动下载权重。
- 保持原身份/scope/transport/工具协议；unknown不自动重发，Harness实际结果回原tool_call后由小温暖回复。

## 修改与最小验证

- 参数/启动：__main__.py、launch.py、tests/test_entry.py。认证/白名单先于Bridge构造和后台启动。
- 协议：wechat_client.py；映射/发送/报告轮询：wechat.py、tests/test_wechat.py。Runner只关自身任务/client，管理API负责Bridge生命周期。
- 记忆/情绪/维护：src/myatri及myatry对应模块，保留证据与账本，不手改档案或清表。
- 窗口：server.py、monitor*、context_viewer*；只发现本项目库，tests/test_ui_scope.py验证相邻私聊不可见。
- 原链路的小回归：test_reporting_preserved.py、test_library_preserved.py、test_attention_preserved.py，均为假模型和临时库。不要扩大到全量旧测试或真实群发送。
- 打包uv build，保留资源与LICENSES；var、.model-desk、.venv及本地配置不属于发布物。

本轮3个内置子代理使用中等思考，主脑独立验收22项及隔离CLI/UI冒烟。完整证据范围见STATUS.md。以后完成改动后更新这两份文档，区分离线验证与真实链路。
