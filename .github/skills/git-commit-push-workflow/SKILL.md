---
name: git-commit-push-workflow
description: "根据当前 git 修改内容自动整理可用的中文提交信息并执行 commit 与 push。用于提交代码、按变更类型生成 fix/feat/docs/test/refactor/chore 提交前缀、检查冲突并在冲突时提示人工处理，也用于处理 pre-commit 钩子导致的自动改写或提交阻断。"
argument-hint: "说明是否需要拆分多次提交、scope 名称、是否直接 push"
---

# Skill: Git 提交与推送工作流

## 目标

将“查看修改 -> 归类提交类型 -> 生成中文提交信息 -> 执行 commit -> 执行 push”固化为统一流程。

核心要求：

- 基于当前修改内容生成可用的中文提交信息
- 提交类型可区分：`fix`、`feat`、`docs`、`test`、`refactor`、`chore`
- 发现冲突时停止自动流程并提示用户先处理冲突

## 触发场景

- "帮我提交并推送代码"
- "根据当前改动生成中文 commit message"
- "按 conventional commit 类型自动提交"
- "提交时先检查冲突"

## 输入建议

- 是否拆分提交：`single` / `split`
- 可选 scope：如 `api`、`sales`、`freight`
- 是否直接 push：`true` / `false`

## 标准流程

### 0. 工具选择约束

处理提交与推送任务时，默认优先使用本地 git 命令与工作区信息：

- 优先使用 `git status`、`git diff`、`git add`、`git commit`、`git push` 等本地命令完成整个流程
- 尽量避免使用 MCP 获取仓库状态、创建提交或执行推送，除非本地 git 信息不足以完成任务
- 不应将 MCP 作为常规提交路径的默认实现，尤其不应在本地仓库可直接操作时绕过 git CLI
- 只有在用户明确要求依赖特定 MCP 能力，或任务必须读取 MCP 独有的远端上下文时，才可例外使用 MCP
- 即使使用 MCP，也应先完成本地工作区检查，确保提交依据仍来自当前仓库实际状态

### 1. 冲突与工作区预检查

先执行状态检查：

- `git status --porcelain`

若存在未合并冲突（如 `UU`、`AA`、`DD` 或 `both modified`）：

- 立即停止提交流程
- 明确提示用户先完成冲突解决
- 不自动执行 `git add` / `git commit` / `git push`

### 2. 读取改动并判定提交类型

基于 `git diff --name-status`、`git diff --staged --name-status` 与关键改动内容进行判定。

推荐判定顺序（从高到低）：

1. `feat`：新增业务能力、接口、模型、可见功能
2. `fix`：修复 bug、异常分支、错误逻辑
3. `refactor`：重构代码结构但不改变业务行为
4. `test`：仅新增或修改测试用例
5. `docs`：仅文档、注释、说明更新
6. `chore`：工程维护类（依赖、构建、脚本、配置）

判定原则：

- 若单次改动仅覆盖一种类型，直接使用该类型
- 若混合多种类型且可拆分，优先建议 `split`
- 若用户要求一次提交，则选择“主类型 + 中文摘要”并在正文补充次要变更

### 3. 生成中文提交信息

遵循以下格式：

- 标题：`<type>(<scope>): <中文动宾短句>`
- 正文（可选）：说明关键改动点、影响范围、验证方式

标题要求：

- 中文语义明确，长度建议 12-30 字
- 避免空泛描述（如“更新代码”“修复问题”）
- 直接体现业务意图或修复对象

示例：

- `feat(freight): 新增订舱任务批量创建接口`
- `fix(sales): 修复订单筛选分页总数计算错误`
- `docs(api): 补充系统菜单接口返回字段说明`
- `test(freight): 增加货运任务异常场景集成测试`
- `refactor(system): 重构菜单服务查询构建逻辑`
- `chore(devops): 调整开发环境日志轮转配置`

### 4. 执行提交

默认步骤：

1. `git add -A`
2. `git commit -m "<生成的中文标题>"`

如需正文，使用多行 `-m`：

- `git commit -m "<标题>" -m "<正文第一行>" -m "<正文第二行>"`

若提交失败（如 pre-commit、lint、test 阻断）：

- 返回失败原因摘要
- 提示用户修复后重试
- 不跳过校验强行提交

### 4.1 pre-commit 处理约定

若 `git commit` 过程中触发 pre-commit：

- 先区分是“hook 自动修改文件”还是“hook 校验失败且未修改文件”
- 若 hook 自动修改了文件（如格式化、import 排序、代码生成），先重新读取工作区差异，确认改动仍与当前提交意图一致
- 若自动修改仅为格式化或机械性修正，应重新 `git add` 后再次执行 `git commit`，不要直接忽略这类改动
- 若 hook 报出真实失败（如 lint、test、type check、secret scan），应先修复失败项，再重新提交
- 不应默认使用 `--no-verify` 跳过 pre-commit；只有用户明确要求且已知风险时才可考虑
- 若 pre-commit 暴露出与本次任务无关但会阻断提交的问题，应向用户明确说明阻断原因，并等待用户决定是继续修复还是拆分处理

### 5. 执行推送

- 已存在上游分支：`git push`
- 无上游分支：`git push -u origin <current-branch>`

若推送被拒绝（non-fast-forward）：

1. 提示先同步远端（常见策略：`git pull --rebase`）
2. 若同步过程出现冲突，停止并提示用户手动处理冲突
3. 冲突解决后再执行 push

## 完成标准

- [ ] 无未解决冲突
- [ ] 提交类型已明确（`fix|feat|docs|test|refactor|chore`）
- [ ] 提交标题为可读中文，符合 `<type>(<scope>): <摘要>`
- [ ] commit 成功
- [ ] push 成功（若用户要求 push）

## 推荐对话指令

- "根据当前改动生成中文 commit message 并提交"
- "按修改类型拆分成多次提交并推送"
- "先检查冲突，再执行 commit 和 push"
