# C-Check 重构优化记录（2026-06-13）

## 分支

- 工作分支：`C-Check-Branch01`
- 基线来源：`origin/master` 最新代码
- 说明：拉取前本地工作区存在大量删除状态，已先通过 `git stash push -u` 备份，避免直接丢失本地痕迹。

## 后端重构

- 重构 `backend/app/services/submissions.py` 中压缩包和文件夹提交的重复源码收集逻辑。
- 新增内部 `_SourceCollection` 收集器，统一处理：
  - 路径去重；
  - `.c` / `.h` 源文件过滤；
  - 单文件大小限制；
  - 总源码大小限制；
  - 源码数量限制；
  - 空源码集合校验。
- 保持原有 `collect_archive_submission`、`collect_folder_submission` 对外函数签名和错误文案，降低 API 行为变化风险。

## 前端重构

- 拆分 `WorkspaceView.vue` 中的工具逻辑，降低页面组件复杂度：
  - `frontend/src/views/c-highlight.ts`：C 代码预览高亮和 HTML 转义。
  - `frontend/src/views/workspace-files.ts`：源码文件识别、文件大小格式化、模型描述本地化、文件夹展示名推导。
- 整理 `frontend/src/api/client.ts` 的审查提交 FormData 构造逻辑，减少单文件、压缩包、文件夹提交路径里的重复代码。
- 补充前端单元测试：
  - `c-highlight.test.ts`
  - `workspace-files.test.ts`

## UI 梳理与美化

- 工作台桌面端改为主操作区 + 任务状态侧栏的双栏布局。
- 移动端保持单列内容流，底部导航不产生横向溢出。
- 调整主题色、背景、面板圆角、上传区和任务配置行视觉层次，让页面更稳、更易扫描。

## 验证结果

- `npm.cmd test`：通过，7 个测试文件，27 个用例。
- `npm.cmd run build`：通过。
- 浏览器检查：
  - 桌面端工作台双栏正常，无横向溢出。
  - 移动端 390px 宽度单列正常，无横向溢出。
  - 创建任务表单展开后布局正常。

## 已知限制

- 后端测试未能在本机完整执行：当前本机仅发现 Python 3.14，而项目 `backend/pyproject.toml` 声明运行环境为 Python `>=3.12`。在 Python 3.14 下，SQLAlchemy 解析 ORM 类型注解时报错：
  - `TypeError: descriptor '__getitem__' requires a 'typing.Union' object but received a 'tuple'`
- 建议在 Python 3.12 环境中执行 `python -m pytest` 作为合并前最终后端验证。
- `npm install` 后 `npm audit` 报告 3 个 high severity 依赖告警。本次未执行 `npm audit fix --force`，避免引入破坏性依赖升级。
- 前端构建存在 Vite 大 chunk 提示，未在本次谨慎重构中拆分路由 chunk 之外的第三方包。
