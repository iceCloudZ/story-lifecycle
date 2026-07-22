"""Acceptance tests for greeter.py (consult_demo scenario)."""

from greeter import greet


def test_greet_named():
    assert greet("World") == "Hello, World!"


def test_greet_empty_falls_back():
    assert greet("") == "Hello, stranger!"


def test_greet_arbitrary_name():
    assert greet("Ada") == "Hello, Ada!"
