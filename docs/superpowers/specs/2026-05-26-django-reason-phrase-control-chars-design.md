# Django #37100: HttpResponse reason_phrase 控制字符校验

## 问题

`HttpResponse.reason_phrase` 未校验控制字符，允许 `\r\n` 等 CRLF 序列注入 HTTP 响应行，导致 HTTP Header Injection。

HTTP 响应行格式: `HTTP/1.1 200 OK\r\n`。若 `reason_phrase` 包含 `\r\n`，攻击者可伪造额外头部或响应体。

## 方案

在 `reason_phrase` 的 property setter 中添加正则校验，拦截所有赋值路径。

### 修改 1: `django/http/response.py`

新增模块级正则:

```python
_control_chars_re = _lazy_re_compile(r"[\x00-\x1f\x7f-\x9f]")
```

覆盖: C0 控制字符 (`\x00-\x1f`)、DEL (`\x7f`)、C1 控制字符 (`\x80-\x9f`)。

修改 `reason_phrase` setter:

```python
@reason_phrase.setter
def reason_phrase(self, value):
    if value and _control_chars_re.search(value):
        raise BadHeaderError("reason_phrase can't contain control characters.")
    self._reason_phrase = value
```

设计要点:
- `if value` 确保 `None` 和空字符串不被拒绝
- `BadHeaderError` 与 Django header newline 校验保持一致
- `_reason_phrase = None` 表示使用默认 reason phrase

### 修改 2: `tests/httpwrappers/tests.py`

新增测试方法:

```python
def test_reason_phrase_rejects_control_chars(self):
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

## 影响范围

| 变更点 | 风险 | 说明 |
|--------|------|------|
| setter 校验 | 低 | 仅 reject 原本不合法的输入 |
| `__init__` 赋值 | 无 | 通过 setter 走同一路径 |
| 回归风险 | 极低 | 正常 reason_phrase 不含控制字符 |

## 验证标准

- `test_reason_phrase_rejects_control_chars` PASS
- `test_http_response_basic` PASS（回归）
- 现有 header 校验测试不受影响
