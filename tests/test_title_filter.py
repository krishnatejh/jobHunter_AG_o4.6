"""Tests for the title pre-filter module."""

import sys
from pathlib import Path

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.title_filter import make_skip_analysis, should_skip_job


# ── Built-in exclusions (always skip) ──


def test_skip_intern():
    skip, reason = should_skip_job("Software Intern", {})
    assert skip is True
    assert "built-in" in reason


def test_skip_internship():
    skip, reason = should_skip_job("Summer Internship 2026", {})
    assert skip is True


def test_skip_trainee():
    skip, reason = should_skip_job("Graduate Trainee - Engineering", {})
    assert skip is True


def test_skip_coop():
    skip, reason = should_skip_job("Engineering Co-Op", {})
    assert skip is True


# ── Config-driven exclusions ──


def test_skip_sales_via_config():
    prefs = {"exclude_role_families": ["Sales"]}
    skip, reason = should_skip_job("Senior Sales Manager", prefs)
    assert skip is True
    assert "Sales" in reason


def test_skip_hr_via_config():
    prefs = {"exclude_role_families": ["HR"]}
    skip, reason = should_skip_job("HR Business Partner", prefs)
    assert skip is True


def test_backward_compat_avoid_key():
    """The old 'avoid_role_families' key should still work."""
    prefs = {"avoid_role_families": ["Account Management"]}
    skip, reason = should_skip_job("Account Management Lead", prefs)
    assert skip is True


# ── Should NOT skip ──


def test_no_skip_solution_architect():
    prefs = {"exclude_role_families": ["Sales", "HR"]}
    skip, _ = should_skip_job("Solution Architect - Banking", prefs)
    assert skip is False


def test_no_skip_product_manager():
    prefs = {"exclude_role_families": ["Sales"]}
    skip, _ = should_skip_job("Product Manager - Payments", prefs)
    assert skip is False


def test_no_skip_empty_prefs():
    skip, _ = should_skip_job("Any Title", {})
    assert skip is False


def test_no_skip_empty_title():
    prefs = {"exclude_role_families": ["Sales"]}
    skip, _ = should_skip_job("", prefs)
    assert skip is False


# ── make_skip_analysis ──


def test_make_skip_analysis_shape():
    analysis = make_skip_analysis("test reason")
    assert analysis["score"] == 0
    assert "test reason" in analysis["verdict"]
    assert analysis["role_family_match"] == "mismatch"
    assert isinstance(analysis["gaps"], list)
