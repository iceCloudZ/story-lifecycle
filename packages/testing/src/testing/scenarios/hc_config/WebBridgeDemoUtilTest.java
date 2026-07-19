package com.ys.hc.config.utils;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * WebBridge e2e 判定测试：验证 AI 实现的 WebBridgeDemoUtil 行为正确。
 *
 * <p>由 story-lifecycle 测试框架在 scenario 启动时注入到
 * {@code hc-config/hc-config-business/src/test/java/com/ys/hc/config/utils/}，
 * scenario 结束后清理。基线（红）= WebBridgeDemoUtil 类不存在 → 编译失败；
 * AI 实现 → 编译+断言通过（绿）。
 */
class WebBridgeDemoUtilTest {

  @Test
  void squareOfSum_twoPositives() {
    assertEquals(25, WebBridgeDemoUtil.squareOfSum(2, 3));
  }

  @Test
  void squareOfSum_withZero() {
    assertEquals(16, WebBridgeDemoUtil.squareOfSum(4, 0));
  }

  @Test
  void squareOfSum_withNegative() {
    // (-2) + 6 = 4, 4*4 = 16
    assertEquals(16, WebBridgeDemoUtil.squareOfSum(-2, 6));
  }

  @Test
  void isPositive_positiveIsTrue() {
    assertTrue(WebBridgeDemoUtil.isPositive(1));
    assertTrue(WebBridgeDemoUtil.isPositive(100));
  }

  @Test
  void isPositive_zeroAndNegativeAreFalse() {
    assertFalse(WebBridgeDemoUtil.isPositive(0));
    assertFalse(WebBridgeDemoUtil.isPositive(-1));
  }
}
