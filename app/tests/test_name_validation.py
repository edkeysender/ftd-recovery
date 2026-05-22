"""Tests for hostname validation in the FastAPI app."""
import re
import pytest

# Don't import app.app — it has heavy module-level side effects (loading YAML,
# binding paths). We test the regex constant directly, which is the value-added
# defense vs. simply trusting Pydantic.
from app.app import _safe_name, _SAFE_NAME_RE


def test_safe_name_accepts_normal_hostnames():
    assert _safe_name("PC-001") == "PC-001"
    assert _safe_name("win10_desktop") == "win10_desktop"
    assert _safe_name("reception PC") == "reception PC"
    assert _safe_name("a.b.c") == "a.b.c"


def test_safe_name_rejects_html_payloads():
    assert _safe_name("<script>") == ""
    assert _safe_name('<img src=x onerror="alert(1)">') == ""
    assert _safe_name("&lt;script&gt;") == ""


def test_safe_name_rejects_empty_and_none():
    assert _safe_name(None) == ""
    assert _safe_name("") == ""
    assert _safe_name("   ") == ""


def test_safe_name_strips_whitespace():
    assert _safe_name("  PC-001  ") == "PC-001"


def test_safe_name_rejects_overlong():
    assert _safe_name("a" * 65) == ""
    # 64 chars exactly should pass (1 leading alnum + 63 trailing)
    assert _safe_name("a" * 64) == "a" * 64


def test_safe_name_rejects_leading_dot_or_hyphen():
    assert _safe_name(".hidden") == ""
    assert _safe_name("-foo") == ""
    # underscore at start is also blocked by the regex (must start alnum)
    assert _safe_name("_foo") == ""
