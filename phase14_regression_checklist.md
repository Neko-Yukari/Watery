# Phase 14 回归清单（P4）

## 自动化冒烟（推荐先跑）
- 命令：`python scripts/phase14_smoke.py --base-url http://127.0.0.1:18000`
- 通过标准：
  - `health` PASS
  - `create_conversation` PASS
  - `chat_stream_events` PASS（包含 `text_delta` 与 `done`，可选 `usage`）
  - `rollback` PASS（`rolled_back > 0`）
  - `archive_list` PASS

## 手动验收（前端行为）

### 1) 发送与流式
- 操作：输入文本后发送。
- 预期：
  - 用户消息立即显示。
  - assistant 文本增量出现（非整段突现）。
  - 完成后消息内操作栏出现。

### 2) 停止生成
- 操作：长回复时点击 Stop。
- 预期：
  - 按钮变为停止态后可恢复。
  - 输入框重新可编辑。
  - 不残留跨会话停止态。

### 3) 会话切换
- 操作：在生成完成后切换到另一会话，再切回。
- 预期：
  - 无停止态残留。
  - 仅最后一条 assistant 显示 `回退/Retry`，历史消息仅 `Copy`。

### 4) 回退与回收站一致性
- 操作：点击最后一条 assistant 的 `回退`。
- 预期：
  - 当前会话回退成功。
  - 回收站列表新增归档。
  - 回退后输入框恢复上轮用户内容。

### 5) Retry 行为
- 操作：点击最后一条 assistant 的 `Retry`。
- 预期：
  - 先回退，再自动重发。
  - 新回复重新流式生成。

### 6) 附件路径一致性
- 操作：文件选择与拖拽各上传一次（图片 + 文本文件）。
- 预期：
  - 两种入口均展示附件预览。
  - 发送后附件内容进入请求上下文。

## 已知非阻塞项
- 依赖提示：`pdfplumber` / `python-docx` / `pypdf` 在编辑器中可能显示未解析（环境安装项）。
- CSS 提示：`-webkit-line-clamp` 建议补充标准 `line-clamp`（兼容性建议，功能不受阻）。
