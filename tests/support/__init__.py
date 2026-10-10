"""Helpers, fakes and factories shared across the test tiers.

A test imports what it shares from here, never from another test module or a conftest:
`tests/unit/test_invariants.py:test_tests_share_helpers_only_through_tests_support` enforces it.
"""
