# 后续修改入口

先读根README、AGENTS.md与ARCHITECTURE.md。新项目不需要AstrBot或旧项目源码运行；来源及适配见SOURCES.json。**myatry 是主开发仓**：可移植改动从这里同步到兄弟仓库。

## 三仓同步（scripts/sync_repos.py）

- 核心 `src/myatri/**` → MYATRI 同路径（字节一致）；微信契约（wechat_versions/wechat-versions.json/升级指南/wechat_client）→ AstrBot 插件（字节一致）；应用层 `src/myatry/**` → AstrBot `src/myatri_bridge/**`（包名改写）。
- 默认 dry-run，`--apply` 写入并更新 `docs/sync_manifest.json`；目标侧有本地适配的文件标 NEEDS REVIEW，不盲写。内核 `myatri/companion/` 只报告漂移不自动写入（CLI 适配需人工移植）。
- 核心 src/myatri 改动不会自动到达 AstrBot 项目（它用冻结 0.2.8 wheel）；需要时按显式发布步骤重建 wheel。
- 新增微信版本支持只改 `wechat-versions.json` + 两仓保持字节一致，流程见 `docs/微信版本升级指南.md`；`python -m myatry versions` 查看表与运行区实况。

## 记忆证据修复与真人化（2026-10-24）

- 记忆误拒修复：机器人自身 ID 经认证后是合法记忆主体（`short_memory.known_subjects`，Bridge 从路由与每轮消息注册）；引用校验改为"精确子串优先，`evidence_key` 确定性归一化兜底"（U+2005→空格、空白折叠），改写引用依旧拒收。A1 路径（memory_repair）同样兜底。health 把 transient_personal_claim 从待核中分离（by_design/pending_review）。
- 真人化机制：@唤醒跟随轮数 2-4（attention-v1 采样改 2+hash%3），且被@后最初一两轮近乎必跟（0.95）再衰减到基础概率；proactive 新增 `tick_interval_minutes`（固定观察节奏，如10分钟）、`revive_silence_hours`（群沉寂后每天至多一次主动暖场，group_revive 候选 + revive 闸门豁免）、`interest_*`（消息密度+关键词/问句 → interesting_exchange 选择性插话）；群感参考规则进 group_prompt（自然化用玩笑/口头禅，不复读不冒犯）。默认值保持保守（全关），本机配置已启用。
- sync_repos 修复：import_module("myatry.x") 字符串路径参与包名改写；NEEDS REVIEW 配对在目标侧漂移未解决前不再被源更新覆盖。

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
