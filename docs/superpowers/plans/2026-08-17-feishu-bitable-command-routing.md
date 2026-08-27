# Feishu Bitable Command Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 feishu-personal 的 Bitable 子命令在通过技能边界时能够被安全分类、正常派发，并用测试证明读写操作和未知命令仍受保护。

**Architecture:** 保留后端通用 CommandManifest 和未知命令拒绝策略，不在通用解析器中硬编码具体业务实体。技能通过随技能目录提供的声明式子命令分类配置，告诉后端如何把自己的 CLI 子命令映射为 read/append/edit/create/delete。技能 runner 继续执行原始 CLI 文本，manifest 只负责安全策略、幂等和审计。

**Tech Stack:** Python 3、pytest、现有 backend.skills 技能边界、CommandManifest、skills/feishu-personal/skill_runner.py、本地 Feishu session cookie。

## Global Constraints

- 遵守 D:\NoobhekCode\chat_agent\AGENTS.md：不得按公司、学校、人名、域名等具体实体硬编码；命令分类必须参数化或配置驱动。
- 不得把未知远程命令默认当成只读命令放行。
- 保留 CommandManifest 的原始命令参数，用于幂等键和审计；不得用归一化后的伪命令替换用户实际执行的 CLI。
- 单元测试不得依赖网络或真实账号；真实飞书验证必须使用专用测试资源，并记录创建、验证和清理结果。
- 本计划阶段只生成计划文档，不修改生产代码；执行时每个任务先写失败测试，再写最小实现。

---

### Task 1: 固化当前故障和安全分类边界

**Files:**
- Modify: D:\NoobhekCode\chat_agent\backend\tests\test_mutation_manifest.py
- Modify: D:\NoobhekCode\chat_agent\backend\tests\test_mutation_guard.py

**Interfaces:**
- Consumes: backend.mutation_manifest.normalize_command_manifest。
- Produces: 明确的 Bitable 子命令分类测试，供后续解析器实现使用。

- [ ] **Step 1: 添加失败测试，覆盖读、写和未知命令**

在 test_mutation_manifest.py 增加以下测试数据和测试函数。测试中的 token 使用固定占位值，不访问网络：

~~~python
@pytest.mark.parametrize(
    ("command", "operation", "mutating"),
    [
        ("lark bitable tables base-token", "read", False),
        ("lark bitable schema base-token tbl-token", "read", False),
        ("lark bitable records base-token tbl-token --all", "read", False),
        ("lark bitable download base-token tbl-token --out export.json", "read", False),
        ("lark bitable create TestBase", "create", True),
        ("lark bitable add-record base-token tbl-token Name=one", "append", True),
        ("lark bitable set-record base-token tbl-token rec-token Name=two", "edit", True),
        ("lark bitable add-field base-token tbl-token Amount --type number", "edit", True),
        ("lark bitable rename-field base-token tbl-token fld-token Amount", "edit", True),
        ("lark bitable delete-record base-token tbl-token rec-token", "delete", True),
    ],
)
def test_normalizes_skill_cli_subcommands_to_safe_operations(command, operation, mutating):
    manifest = normalize_command_manifest(
        command,
        subcommand_operations={
            "lark bitable": {
                "tables": "read",
                "schema": "read",
                "records": "read",
                "download": "read",
                "create": "create",
                "add-record": "append",
                "set-record": "edit",
                "add-field": "edit",
                "rename-field": "edit",
                "delete-record": "delete",
            }
        },
    )
    assert manifest.operation == operation
    assert manifest.mutating is mutating
    assert manifest.arguments["command"] == command
~~~

在同一文件增加未知命令测试：

~~~python
def test_rejects_unclassified_skill_subcommand():
    with pytest.raises(ValueError, match="operation"):
        normalize_command_manifest(
            "lark bitable publish base-token",
            subcommand_operations={"lark bitable": {"tables": "read"}},
        )
~~~

- [ ] **Step 2: 运行测试确认当前实现失败**

Run:

~~~powershell
python -m pytest backend/tests/test_mutation_manifest.py -q
~~~

Expected: 新增的 tables/schema/records 测试失败，当前实现会报告 Command manifest cannot normalize operation: tables；现有文档类 manifest 测试仍应保持通过。

- [ ] **Step 3: 提交测试基线**

~~~powershell
git add backend/tests/test_mutation_manifest.py backend/tests/test_mutation_guard.py
git commit -m "test: reproduce bitable command manifest routing failure"
~~~

### Task 2: 扩展通用 manifest 解析器，但保持未知命令拒绝

**Files:**
- Modify: D:\NoobhekCode\chat_agent\backend\mutation_manifest.py:80-125
- Modify: D:\NoobhekCode\chat_agent\backend\tests\test_mutation_manifest.py

**Interfaces:**
- Consumes: subcommand_operations: Mapping[str, Mapping[str, str]] | None；键为前两个命令 token，例如 lark bitable；内层键为技能子命令，值为通用 operation。
- Produces: normalize_command_manifest(command, *, subcommand_operations=None) -> CommandManifest。

- [ ] **Step 1: 添加最小解析逻辑**

保持现有函数对文档命令的行为不变，只在原始第三 token 不是通用 operation 时查找配置：

~~~python
def normalize_command_manifest(
    command: str | Sequence[str] | Mapping[str, Any] | CommandManifest,
    *,
    subcommand_operations: Mapping[str, Mapping[str, str]] | None = None,
) -> CommandManifest:
    # 现有 Mapping、CommandManifest 和 shell 参数拆分逻辑保持不变。
    parts = _split_command(command)
    if len(parts) < 3:
        raise ValueError("Command manifest requires provider, resource, and operation.")

    provider, raw_resource, raw_operation = (part.strip().lower() for part in parts[:3])
    resource = "document" if raw_resource in _DOCUMENT_RESOURCE_ALIASES else raw_resource
    operation = _DOCUMENT_OPERATION_ALIASES.get(raw_operation, raw_operation)

    if operation not in _RECOGNIZED_OPERATIONS:
        command_family = f"{provider} {resource}"
        configured_operations = (subcommand_operations or {}).get(command_family, {})
        operation = str(configured_operations.get(raw_operation) or "")

    if operation not in _RECOGNIZED_OPERATIONS:
        raise ValueError(f"Command manifest cannot normalize operation: {raw_operation}")

    target = "" if operation == "create" else (
        parts[3] if len(parts) > 3 and not parts[3].startswith("-") else ""
    )
    arguments = _canonical_arguments(parts)
    return CommandManifest(
        provider=provider,
        resource=resource,
        target=target,
        operation=operation,
        mutating=operation in _MUTATING_OPERATIONS,
        arguments=arguments,
        idempotency_input=_idempotency_input(provider, resource, target, operation, arguments),
        verification_mode="read_back" if operation == "replace" else "none",
    )
~~~

执行时保留现有对 Mapping 和 CommandManifest 的校验分支，不把上面的代码替换成“未知即 read”。

- [ ] **Step 2: 运行解析器测试**

Run:

~~~powershell
python -m pytest backend/tests/test_mutation_manifest.py backend/tests/test_mutation_guard.py -q
~~~

Expected: Bitable 分类测试通过；lark bitable publish ... 仍失败；原有 lark doc ... manifest 测试全部通过。

- [ ] **Step 3: 提交通用解析器变更**

~~~powershell
git add backend/mutation_manifest.py backend/tests/test_mutation_manifest.py
git commit -m "feat: support configured remote command subcommands"
~~~

### Task 3: 把 Bitable 命令分类放入技能配置，并传入技能边界

**Files:**
- Modify: D:\NoobhekCode\chat_agent\backend\skills.py:20-65,95-135,325-350
- Create: D:\NoobhekCode\chat_agent\skills\feishu-personal\command_manifest.json
- Modify: D:\NoobhekCode\chat_agent\backend\tests\test_mutation_guard.py

**Interfaces:**
- Consumes: 技能目录中的 command_manifest.json。
- Produces: SkillDefinition.manifest_config，以及将其传给 normalize_command_manifest 的技能边界。

- [ ] **Step 1: 添加声明式技能配置**

创建 command_manifest.json，内容为：

~~~json
{
  "subcommand_operations": {
    "lark bitable": {
      "tables": "read",
      "schema": "read",
      "views": "read",
      "records": "read",
      "download": "read",
      "create": "create",
      "add-record": "append",
      "set-record": "edit",
      "add-field": "edit",
      "rename-field": "edit",
      "delete-record": "delete"
    }
  }
}
~~~

只登记当前 lark_tools/cli.py 实际存在的命令。add-fields-batch 和 add-records-batch 在当前 CLI 中没有实现，不能先写入 manifest 伪装成可执行命令。

- [ ] **Step 2: 让技能定义加载可选配置**

在 SkillDefinition 增加默认空字典字段：

~~~python
manifest_config: dict[str, Any] = field(default_factory=dict)
~~~

在 _load_skill_from_markdown_dir 中读取 path / "command_manifest.json"；文件不存在时保持空字典，JSON 无效时抛出带路径的配置错误。_load_skill_from_json 保持兼容，未提供配置时使用空字典。

- [ ] **Step 3: 在执行前传入配置**

把 execute_skill 中的解析调用改为：

~~~python
request_manifest = normalize_command_manifest(
    request,
    subcommand_operations=(skill.manifest_config or {}).get("subcommand_operations"),
)
~~~

对外仍保留原有错误包装；解析失败时不调用 runner。传给 runner 的 params["manifest"] 仍必须是由原始 request 生成的 manifest。

- [ ] **Step 4: 添加技能边界测试**

在 test_mutation_guard.py 增加 fake runner 测试，构造带 manifest_config 的 fake SkillDefinition，并断言 runner 收到 manifest.operation == "read"：

~~~python
skill = SkillDefinition(
    name="remote-command-runner",
    description="",
    prompt_template="",
    source_path=".",
    manifest_config={
        "subcommand_operations": {"lark bitable": {"tables": "read"}}
    },
)
~~~

测试 execute_skill(..., {"request": "lark bitable tables base-token"}) 时 runner 被调用一次且收到 read manifest；对 lark bitable publish ... 断言 runner 调用次数为零；lark doc replace ... 的既有测试必须保持通过。

- [ ] **Step 5: 运行后端技能测试**

Run:

~~~powershell
python -m pytest backend/tests/test_mutation_manifest.py backend/tests/test_mutation_guard.py backend/tests/test_skill_policy.py -q
~~~

Expected: 全部通过；未知命令仍在派发前拒绝。

- [ ] **Step 6: 提交技能边界修复**

~~~powershell
git add backend/skills.py backend/mutation_manifest.py skills/feishu-personal/command_manifest.json backend/tests
git commit -m "fix: route configured bitable commands through skill boundary"
~~~

### Task 4: 校正技能文档与实际 CLI 能力

**Files:**
- Modify: D:\NoobhekCode\chat_agent\skills\feishu-personal\SKILL.md
- Modify: D:\NoobhekCode\chat_agent\skills\feishu-personal\README.md
- Modify: D:\NoobhekCode\chat_agent\skills\feishu-personal\README_CN.md
- Test: D:\NoobhekCode\chat_agent\skills\feishu-personal\tests\test_claude_skill_structure.py

**Interfaces:**
- Consumes: D:\NoobhekCode\chat_agent\skills\feishu-personal\lark_tools\cli.py 的实际子命令。
- Produces: 文档中只出现可执行的命令调用，避免模型在修复 manifest 后继续调用不存在的 batch 子命令。

- [ ] **Step 1: 先用静态检查确认实际命令集合**

Run:

~~~powershell
rg -n "sub ==|bitable create|bitable tables|bitable schema|bitable records|bitable views|bitable download|bitable set-record|bitable add-record|bitable delete-record|bitable add-field|bitable rename-field" skills/feishu-personal/lark_tools/cli.py
rg -n "add-fields-batch|add-records-batch" skills/feishu-personal/lark_tools
~~~

Expected: 当前实现包含 tables/schema/views/records/download/create/set-record/add-record/delete-record/add-field/rename-field，没有 batch 子命令实现。

- [ ] **Step 2: 统一文档契约**

保留显式 CLI 调用说明，例如：

~~~text
lark bitable tables <url-or-token>
lark bitable schema <url-or-token> [tableId]
lark bitable records <url-or-token> [tableId] --all
lark bitable add-field <url-or-token> <tableId> <name> --type text
lark bitable add-record <url-or-token> <tableId> Name=value
~~~

删除或明确标记当前不存在的 add-fields-batch、add-records-batch，并删除“技能 runner 自动支持自然语言路由”的声明，除非同时实现并测试该路由。当前 runner 的实际契约是只接受以 lark 或 lark_cli 开头的显式命令。

- [ ] **Step 3: 运行技能结构测试**

Run:

~~~powershell
python -m pytest skills/feishu-personal/tests/test_claude_skill_structure.py -q
~~~

Expected: 技能结构、命令文档和 runner 契约测试通过。

- [ ] **Step 4: 提交契约同步**

~~~powershell
git add skills/feishu-personal/SKILL.md skills/feishu-personal/README.md skills/feishu-personal/README_CN.md skills/feishu-personal/tests/test_claude_skill_structure.py
git commit -m "docs: align feishu skill contract with implemented commands"
~~~

### Task 5: 验证端到端读写和回归保护

**Files:**
- Modify: D:\NoobhekCode\chat_agent\backend\tests\test_mutation_guard.py if an integration assertion is missing.
- Test: D:\NoobhekCode\chat_agent\skills\feishu-personal\tests\test_claude_skill_structure.py
- External test resource: sessionss/feishu_web_session.json 对应的专用 Feishu 测试空间。

**Interfaces:**
- Consumes: 已加载的 feishu-personal 技能、技能 manifest 配置和有效 session cookie。
- Produces: 可复现的本地测试结果和一次真实 Feishu 读写验证记录。

- [ ] **Step 1: 运行完整本地回归**

Run:

~~~powershell
python -m pytest backend/tests/test_mutation_manifest.py backend/tests/test_mutation_guard.py backend/tests/test_skill_policy.py skills/feishu-personal/tests/test_claude_skill_structure.py -q
~~~

Expected: 全部通过；至少验证以下负向和正向断言：

~~~text
lark bitable publish ...       -> 派发前拒绝
lark bitable tables ...        -> 允许派发，manifest.operation=read
lark bitable add-record ...    -> 允许派发，manifest.operation=append
lark bitable delete-record ... -> 允许派发，manifest.operation=delete
~~~

- [ ] **Step 2: 验证登录状态但不暴露 cookie**

调用现有 feishu_login_status，只检查 logged_in=true、has_session=true、server_check.valid=true、HTTP 状态为 200。日志中不得输出 session cookie 值。

- [ ] **Step 3: 验证真实读链路**

用专用测试 base 的 URL 执行：

~~~text
lark bitable tables <test-base-url>
lark bitable schema <test-base-url> <table-id>
lark bitable records <test-base-url> <table-id> --all
~~~

Expected: 请求不再出现 Unclassified standardized remote command was blocked before dispatch，并返回结构化 JSON；失败若出现，应区分权限、网络、CSRF 和解析错误。

- [ ] **Step 4: 验证真实写链路并读回**

在专用测试表中使用实际存在的单条命令：

~~~text
lark bitable add-field <test-base-url> <table-id> TestName --type text
lark bitable add-field <test-base-url> <table-id> TestAmount --type number
lark bitable add-record <test-base-url> <table-id> TestName=route-check TestAmount=1
lark bitable records <test-base-url> <table-id> --all
~~~

Expected: 字段和记录都能在读回结果中找到；runner 返回成功结果；manifest/ledger 不会把重复同一调用执行两次。

- [ ] **Step 5: 验证失败边界和审计信息**

分别执行一个未知子命令和一个参数不完整的合法子命令：

~~~text
lark bitable publish <test-base-url>
lark bitable add-record <test-base-url>
~~~

Expected: 未知命令在后端解析阶段拒绝；参数不完整的命令由 CLI 返回明确 usage 错误；两者都不应产生远程写入。

- [ ] **Step 6: 记录验证结果并提交最终变更**

保存测试命令、通过数量、真实请求返回摘要和残留测试数据位置；不要保存 cookie 或完整响应中的敏感字段。确认 git diff 只包含本计划文件和修复相关文件后提交：

~~~powershell
git status --short
git diff --check
git add backend skills/feishu-personal docs/superpowers/plans/2026-08-17-feishu-bitable-command-routing.md
git commit -m "fix: restore safe feishu bitable skill execution"
~~~

## Acceptance Criteria

- lark bitable tables ... 能通过技能边界进入 runner，不再被 generic manifest parser 提前拦截。
- tables/schema/views/records/download 被分类为 read；add-record 被分类为 append；set-record/add-field/rename-field 被分类为 edit；delete-record 被分类为 delete；create 保持为 create。
- 未配置的 Bitable 子命令和其他未知远程命令仍然在派发前拒绝。
- 原有 lark doc read/append/edit/replace/create/delete 测试全部保持通过。
- 技能文档不再承诺当前 runner 或 CLI 尚未实现的自然语言/批量命令。
- 真实 session 下完成一次 tables、schema、records 读取以及一次字段/记录写入和读回验证。
