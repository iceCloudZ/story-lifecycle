import pytest

from calculator import Calculator


class TestStaticMethods:
    """静态方法：四则运算、幂、取模"""

    def test_add(self):
        assert Calculator.add(2, 3) == 5

    def test_add_negative(self):
        assert Calculator.add(-1, 1) == 0

    def test_subtract(self):
        assert Calculator.subtract(5, 3) == 2

    def test_subtract_negative_result(self):
        assert Calculator.subtract(1, 5) == -4

    def test_multiply(self):
        assert Calculator.multiply(4, 3) == 12

    def test_multiply_by_zero(self):
        assert Calculator.multiply(5, 0) == 0

    def test_divide(self):
        assert Calculator.divide(10, 2) == 5

    def test_divide_non_integer(self):
        assert Calculator.divide(7, 2) == 3.5

    def test_divide_by_zero(self):
        with pytest.raises(ZeroDivisionError):
            Calculator.divide(1, 0)

    def test_power(self):
        assert Calculator.power(2, 3) == 8

    def test_power_negative_exponent(self):
        assert Calculator.power(2, -1) == 0.5

    def test_power_zero_exponent(self):
        assert Calculator.power(2, 0) == 1

    def test_mod(self):
        assert Calculator.mod(10, 3) == 1

    def test_mod_by_zero(self):
        with pytest.raises(ZeroDivisionError):
            Calculator.mod(10, 0)


class TestChaining:
    """链式调用"""

    def test_basic_chain(self):
        assert Calculator().add(5).subtract(2).multiply(3).result() == 9

    def test_add_chain(self):
        assert Calculator().add(2).add(3).result() == 5

    def test_divide_chain(self):
        assert Calculator().add(10).divide(2).result() == 5

    def test_chain_starts_at_zero(self):
        assert Calculator().result() == 0

    def test_chain_divide_by_zero(self):
        with pytest.raises(ZeroDivisionError):
            Calculator().add(5).divide(0)
