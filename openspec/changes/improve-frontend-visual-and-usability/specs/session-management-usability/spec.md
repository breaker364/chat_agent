## ADDED Requirements

### Requirement: 应用内会话重命名对话框
会话重命名 SHALL 使用应用内对话框输入新标题,替换浏览器原生 `window.prompt`;对话框 SHALL 支持 Esc 关闭、确认提交,并对空标题或未变更标题不做提交。

#### Scenario: 重命名会话
- **WHEN** 用户在会话项操作中选择重命名并输入新标题确认
- **THEN** 会话标题 SHALL 更新,且过程中不出现浏览器原生弹窗

### Requirement: 应用内会话删除确认
会话删除 SHALL 使用应用内确认对话框展示会话标题并要求显式确认,替换浏览器原生 `window.confirm`;确认前 SHALL 不执行删除。

#### Scenario: 取消删除
- **WHEN** 用户在删除确认对话框中取消
- **THEN** 会话 SHALL 保持不变

#### Scenario: 确认删除当前会话
- **WHEN** 用户确认删除当前活跃会话
- **THEN** 前端 SHALL 按现有会话切换逻辑载入其余会话或新建草稿会话

### Requirement: 对话框无障碍原语
应用内对话框 SHALL 以 `role="dialog"` 与 `aria-modal` 声明,打开时 SHALL 将焦点移入对话框并在其中圈闭(focus trap),关闭后 SHALL 将焦点归还触发元素。

#### Scenario: 键盘使用对话框
- **WHEN** 用户使用 Tab 在对话框内移动焦点
- **THEN** 焦点 SHALL 始终停留在对话框内部,关闭后回到触发按钮

### Requirement: 会话搜索过滤
会话列表 SHALL 提供即时搜索框,按标题(及标识)对会话进行不区分大小写的过滤;搜索无结果时 SHALL 显示明确的空态提示。

#### Scenario: 过滤会话
- **WHEN** 用户在搜索框输入关键词
- **THEN** 列表 SHALL 仅保留标题或标识匹配该关键词的会话

### Requirement: 会话时间分组
会话列表 SHALL 按最近更新时间分组展示(今天、昨天、本周、更早),每个会话项 SHALL 显示相对更新时间;分组 SHALL 不改变既有选择、重命名与删除行为。

#### Scenario: 分组渲染
- **WHEN** 会话列表渲染且存在不同日期的会话
- **THEN** 会话 SHALL 按时间分组归置并显示相对时间,当前会话高亮保持可用
