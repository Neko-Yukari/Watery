# Phase 9 改动清单 (Changelist)

**日期**: 2026-03-02  
**总改动**: 420+ 行  
**涉及文件**: 5 个  

---

## 1. app/services/pdf_processor.py (~400 行改动)

### 1.1 类常量新增 (lines 191-197)
```python
_LLM_CONCURRENCY = 5          # Phase 9 B-1
_LLM_TIMEOUT = 60             # Phase 9 B-1
_PIPELINE_TIMEOUT = 3600      # Phase 9 B-3
_IMAGE_CONCURRENCY = 3        # Phase 9 C-2
_IMAGE_TIMEOUT = 30           # Phase 9 C-2
_IMAGE_VISION_MODEL_DEFAULT = "gemini-2.5-flash"  # Phase 9 C-2
```

### 1.2 Prompt 模板新增 (lines 72-110)
```
OVERVIEW_SKILL_PROMPT  # Phase 9 D-2
```

### 1.3 核心方法修改

#### _extract_heading_level() (lines 98-154)
- **改动类型**: 扩展
- **Phase**: D-1
- **改动内容**:
  - 新增英文教材格式支持 (Chapter / Part / Section / Appendix)
  - 新增多级数字编号支持 (1.1.1 / 1.1)
  - 完全保留中文格式支持

#### extract_text() (lines 148-213)
- **改动类型**: 重写异步逻辑
- **Phase**: C-3
- **改动内容**:
  - Step 1: 同步提取文字 (保留)
  - Step 2: 异步提取图片 (新增)
    - 并发处理所有页 (Semaphore 3)
    - 调用 _describe_image() 获取描述
    - 追加到页文本

#### chunk_text() (lines 245-246)
- **改动类型**: 新增前置方法
- **Phase**: B-2
- **改动内容**:
  - 新增方法 _truncate_to_tokens()
  - 新增属性 _image_vision_model

#### summarize_chunk() (lines 478-480)
- **改动类型**: 参数修改
- **Phase**: B-2
- **改动内容**:
  - 替换 `chunk.text[:4000]` 为 `self._truncate_to_tokens(chunk.text, max_tokens=5000)`

#### pdf_to_skills() (lines 718-784)
- **改动类型**: 完全重写
- **Phase**: B-1, B-3
- **改动内容**:
  - 外层包装全局超时 (1h)
  - 调用 _pdf_to_skills_inner()
  - 超时时自动标记 failed

#### _pdf_to_skills_inner() (lines 786-970)
- **改动类型**: 新增方法（核心改造）
- **Phase**: B-1, B-4, D-2
- **改动内容**:
  - Step 1-2: 不变
  - Step 3+4: 并发处理 (Semaphore 5)
    - 新增 _process_one_chunk() 内函数
    - asyncio.gather() 并发执行
    - 实时更新 processed_chunks (B-4)
  - Step 4.5: 教材概述生成 (D-2)
    - 条件: ≥ 3 个技能
    - 调用 model_router.generate()
  - Step 5: 批量注册 (保留)
  - 完成: 返回结果

### 1.4 辅助方法新增

#### _truncate_to_tokens() (lines 247-256)
- **Purpose**: Token 级截断
- **Phase**: B-2
- **Implementation**: 按比例截断字符数

#### _image_vision_model (property, lines 258-265)
- **Purpose**: 读取运行时图片模型设置
- **Phase**: C-2
- **Fallback**: 无记录时返回默认值

#### _update_pdf_doc_progress() (lines 838-851)
- **Purpose**: 实时更新进度
- **Phase**: B-4
- **Database**: UPDATE pdfdocument SET processed_chunks

#### _extract_images_from_page() (lines 853-904)
- **Purpose**: PDF 图片提取
- **Phase**: C-1
- **Features**:
  - pypdf page.images API
  - 尺寸过滤 (100px threshold)
  - 数量限制 (max 5)

#### _describe_image() (lines 906-965)
- **Purpose**: Gemini Vision 图片描述
- **Phase**: C-2
- **Features**:
  - 多模态消息格式
  - 30s 超时
  - 装饰图片检测

---

## 2. app/models/schemas.py (2 行改动)

### Message 类 (lines 40-43)
- **改动类型**: 字段类型扩展
- **Phase**: C-4
- **改动**:
  - `content: Optional[str]` → `content: Optional[Any]`
  - 原因: 支持多模态 List[dict]

---

## 3. app/models/database.py (1 行改动)

### PDFDocument 类 (line 新增)
- **改动类型**: 字段新增
- **Phase**: A-2
- **改动**:
  - 新增 `processed_chunks: int = Field(default=0, ...)`

---

## 4. app/core/db.py (1 行改动)

### _migrate_schema() 函数 (line 新增)
- **改动类型**: 迁移条目
- **Phase**: A-2
- **改动**:
  - 新增 `("pdfdocument", "processed_chunks", "INTEGER DEFAULT 0")`

---

## 5. app/api/routes.py (15+ 行改动)

### upload_pdf() 方法 (lines 已修改)
- **改动类型**: 流式处理改造
- **Phase**: A-1
- **改动**:
  - 新增常量 `_MAX_UPLOAD_SIZE`, `_UPLOAD_CHUNK_SIZE`
  - 替换 `await file.read()` 为分块循环
  - 超限返回 413

### get_pdf_status() 方法 (lines 已修改)
- **改动类型**: 响应扩展
- **Phase**: B-4
- **改动**:
  - 新增 `processed_chunks` 字段
  - 新增 `progress_pct` 字段计算

---

## 改动影响分析

### 影响范围

| 模块 | 影响 | 兼容性 |
|-----|-----|--------|
| 文件上传 | 大 | ✅ 向后兼容 |
| PDF 处理 | 大 | ✅ 向后兼容 |
| LLM 通信 | 中 | ✅ 向后兼容 |
| 数据库 | 小 | ✅ 自动迁移 |

### 新增依赖

- ❌ 无新依赖
- ✅ 所有库已在 requirements.txt 中

### 破坏性改动

- ❌ 无破坏性改动
- ✅ 全部向后兼容

---

## 测试覆盖

### 单元测试建议

```python
# test_pdf_processor.py
def test_truncate_to_tokens():
    """验证 Token 级截断"""
    
def test_image_extraction():
    """验证图片提取"""
    
def test_concurrent_processing():
    """验证并发处理 + Semaphore"""
    
def test_timeout_protection():
    """验证超时保护"""
    
def test_overview_generation():
    """验证概述技能生成"""
```

### 集成测试建议

```python
# test_integration.py
def test_end_to_end_5mb_pdf():
    """上传 5MB PDF，验证全流程"""
    
def test_end_to_end_large_pdf_200mb():
    """上传 200MB PDF，验证流式处理"""
    
def test_progress_tracking():
    """验证进度实时更新"""
    
def test_image_processing():
    """验证图片提取 + Gemini Vision"""
```

---

## 回滚计划

如果发现问题，按此顺序回滚：

1. **回滚 Phase 9 代码** (恢复 git 历史)
2. **保留数据库迁移** (processed_chunks 字段保留，无害)
3. **测试纯文本 PDF** (应该完全不受影响)

---

## 签核

- **代码审查**: ⏳ 待进行
- **功能测试**: ⏳ 待进行
- **性能测试**: ⏳ 待进行
- **部署检查**: ⏳ 待进行

---

**改动清单版本**: 1.0  
**生成时间**: 2026-03-02  
**状态**: 🟢 就绪审查
