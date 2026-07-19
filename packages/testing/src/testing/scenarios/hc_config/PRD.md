# PRD：WebBridge 演示工具类

## 概述

在 hc-all 工作区的 `hc-config` 子项目的 business 模块（`com.ys.hc.config.utils`
包）下，新增一个纯计算工具类 `WebBridgeDemoUtil`，用于验证 AI 在真实 Java
工作区中实现需求的能力。

> 目标工作区是 `D:\hc-all`（包含多个子项目的工作区容器），AI 在其中的
> `hc-config` 子项目里改代码。判定测试跑该子项目 business 模块的 Maven 测试。

## 功能需求

在 `com.ys.hc.config.utils.WebBridgeDemoUtil` 类中提供以下**静态方法**：

| 方法 | 签名 | 说明 |
|------|------|------|
| `squareOfSum` | `squareOfSum(int a, int b) -> int` | 返回 `(a + b)` 的平方，即 `(a+b)*(a+b)` |
| `isPositive` | `isPositive(int n) -> boolean` | 当 `n > 0` 时返回 `true`，否则 `false` |

## 技术约束

- 纯 Java 实现，无第三方依赖
- 类放在 `hc-config/hc-config-business/src/main/java/com/ys/hc/config/utils/WebBridgeDemoUtil.java`
- 类名为 `WebBridgeDemoUtil`，方法为 `public static`
- 返回值类型严格按上表（`int` / `boolean`）

## 验收标准

- 预置测试（`WebBridgeDemoUtilTest`）全部通过（在 `hc-config` 子项目下
  `mvnw -pl hc-config-business -am test -Dtest=WebBridgeDemoUtilTest` 退出码 0）
