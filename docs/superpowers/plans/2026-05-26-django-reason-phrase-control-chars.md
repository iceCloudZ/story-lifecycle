# Django reason_phrase 控制字符校验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `HttpResponse.reason_phrase` setter 中添加控制字符校验，阻止 HTTP Header Injection。

**Architecture:** 在 `reason_phrase` 的 property setter 中添加正则校验，拦截所有赋值路径（`__init__` 和直接赋值均经过 setter）。使用与 Django 现有 header 校验一致的 `BadHeaderError`。

**Tech Stack:** Django, Python re, pytest/django test runner

**Spec:** `docs/superpowers/specs/2026-05-26-django-reason-phrase-control-chars-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `django/http/response.py` | Modify | 添加 `_control_chars_re` 正则 + 修改 `reason_phrase` setter |
| `tests/httpwrappers/tests.py` | Modify | 添加 `test_reason_phrase_rejects_control_chars` 测试方法 |

---

### Task 1: 写失败测试

**Files:**
- Modify: `tests/httpwrappers/tests.py`

- [ ] **Step 1: 写失败测试**

在 `tests/httpwrappers/tests.py` 中找到 `HttpResponse` 相关的测试类（通常是 `HttpResponseTests`），添加以下测试方法：

```python
def test_reason_phrase_rejects_control_chars(self):
    from django.http.response import BadHeaderError

    response = HttpResponse()
    # 控制字符边界值
    for char in ("\x00", "\x01", "\x1f", "\x7f", "\x80", "\x9f"):
        with self.subTest(char=repr(char)):
            with self.assertRaises(BadHeaderError):
                HttpResponse(reason="OK" + char)
    # CRLF 注入
    with self.assertRaises(BadHeaderError):
        HttpResponse(reason="OK\r\nEvil: header")
    # 合法值不受影响
    response.reason_phrase = None
    response.reason_phrase = "OK"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m django test tests.httpwrappers.tests --settings=test_settings -v2 -k test_reason_phrase_rejects_control_chars`
Expected: FAIL — `BadHeaderError` 未被 raise（因为 setter 还没有校验逻辑）

---

### Task 2: 实现校验逻辑

**Files:**
- Modify: `django/http/response.py`

- [ ] **Step 1: 在 `django/http/response.py` 顶部添加正则常量**

找到已有的 `_lazy_re_compile` 导入位置（通常已从 `django.utils.regex` 导入）。在模块级常量区域添加：

```python
_control_chars_re = _lazy_re_compile(r"[\x00-\x1f\x7f-\x9f]")
```

覆盖范围：
- `\x00-\x1f`：C0 控制字符（含 `\r\n`）
- `\x7f`：DEL
- `\x80-\x9f`：C1 控制字符

- [ ] **Step 2: 修改 `reason_phrase` setter**

找到 `reason_phrase` property setter（当前代码类似）：

```python
@reason_phrase.setter
def reason_phrase(self, value):
    self._reason_phrase = value
```

替换为：

```python
@reason_phrase.setter
def reason_phrase(self, value):
    if value and _control_chars_re.search(value):
        raise BadHeaderError("reason_phrase can't contain control characters.")
    self._reason_phrase = value
```

设计要点：
- `if value` 确保 `None` 和空字符串不被拒绝（`None` 表示使用默认 reason phrase）
- `BadHeaderError` 与 Django 现有 header newline 校验保持一致（`BadHeaderError` 已在该文件中导入或定义）

---

### Task 3: 验证通过 + 回归测试

- [ ] **Step 1: 运行新测试确认通过**

Run: `python -m django test tests.httpwrappers.tests --settings=test_settings -v2 -k test_reason_phrase_rejects_control_chars`
Expected: PASS

- [ ] **Step 2: 运行回归测试**

Run: `python -m django test tests.httpwrappers.tests --settings=test_settings -v2 -k test_http_response_basic`
Expected: PASS（确保现有功能不受影响）

- [ ] **Step 3: 运行完整 httpwrappers 测试套件**

Run: `python -m django test tests.httpwrappers --settings=test_settings -v2`
Expected: ALL PASS

---

### Task 4: 提交

- [ ] **Step 1: 提交变更**

```bash
git add django/http/response.py tests/httpwrappers/tests.py
git commit -m "fix(http): validate reason_phrase rejects control characters

Prevent HTTP header injection by raising BadHeaderError when
reason_phrase contains control characters (C0, DEL, C1).

Closes django__django-test-002"
```
