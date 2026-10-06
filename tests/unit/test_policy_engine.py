"""Policy and quota evaluation (pure logic, no database)."""

from __future__ import annotations

import pytest

from printquota.policies.engine import (
    Decision,
    JobContext,
    PolicyRule,
    QuotaState,
    evaluate,
    evaluate_policies,
    evaluate_quota,
    quota_pages,
    resolve_rules,
    validate_rule,
)


def ctx(**kwargs) -> JobContext:
    base = dict(username="alex", printer="hp-mono", estimated_pages=10, group_name="finance")
    base.update(kwargs)
    return JobContext(**base)


def test_allows_when_no_rules_and_quota_is_sufficient():
    decision = evaluate([], ctx(), QuotaState(user_limit=100, user_used=0))
    assert decision.allowed and decision.reason is None


def test_block_color_denies_a_colour_job_only():
    rules = [PolicyRule("global", None, "block_color", "true")]
    assert not evaluate_policies(rules, ctx(is_color=True)).allowed
    assert evaluate_policies(rules, ctx(is_color=False)).allowed


def test_force_duplex_sets_a_forced_option_rather_than_denying():
    rules = [PolicyRule("printer", "hp-mono", "force_duplex", "true")]
    decision = evaluate_policies(rules, ctx(is_duplex=False))
    assert decision.allowed
    assert decision.forced_options["sides"] == "two-sided-long-edge"


def test_force_duplex_is_not_applied_where_the_printer_cannot_act_on_it():
    rules = [PolicyRule("printer", "hp-mono", "force_duplex", "true")]
    decision = evaluate_policies(rules, ctx(is_duplex=False, can_force_sides=False))
    assert decision.allowed and "sides" not in decision.forced_options


@pytest.mark.parametrize("sides,copies,two_sided,by_sheet,expected", [
    (10, 1, True, True, 5),     # 10 pages on both sides: 5 sheets
    (3, 1, True, True, 2),      # an odd last page still takes a sheet
    (6, 2, True, True, 4),      # 2 copies of a 3-page document: 2 sheets each
    (1, 1, True, True, 1),
    (0, 1, True, True, 0),
    (10, 1, False, True, 10),   # one-sided: every side is a sheet
    (10, 1, True, False, 10),   # counting sides
])
def test_quota_pages(sides, copies, two_sided, by_sheet, expected):
    assert quota_pages(sides, copies=copies, two_sided=two_sided, by_sheet=by_sheet) == expected


def test_a_two_sided_job_that_fits_by_sheet_is_allowed():
    state = QuotaState(user_limit=10, user_used=5)  # 5 pages left
    job = ctx(estimated_pages=10, is_duplex=True)   # 10 sides = 5 sheets
    assert evaluate([], job, state, count_two_sided_as_sheets=True).allowed
    assert not evaluate([], job, state, count_two_sided_as_sheets=False).allowed


def test_forced_duplex_is_also_counted_by_sheet():
    rules = [PolicyRule("global", None, "force_duplex", "true")]
    state = QuotaState(user_limit=10, user_used=5)
    assert evaluate(rules, ctx(estimated_pages=10), state, count_two_sided_as_sheets=True).allowed


def test_max_pages_and_copies_limits():
    rules = [
        PolicyRule("global", None, "max_pages_per_job", "20"),
        PolicyRule("global", None, "max_copies_per_job", "3"),
    ]
    assert evaluate_policies(rules, ctx(estimated_pages=20)).allowed
    assert not evaluate_policies(rules, ctx(estimated_pages=21)).allowed
    assert not evaluate_policies(rules, ctx(copies=4)).allowed


def test_block_filetype_is_case_and_dot_insensitive():
    rules = [PolicyRule("global", None, "block_filetype", "exe, .PS")]
    assert not evaluate_policies(rules, ctx(filetype="ps")).allowed
    assert not evaluate_policies(rules, ctx(filetype=".EXE")).allowed
    assert evaluate_policies(rules, ctx(filetype="pdf")).allowed


def test_user_scope_overrides_group_and_global():
    rules = [
        PolicyRule("global", None, "block_color", "true"),
        PolicyRule("group", "finance", "block_color", "true"),
        PolicyRule("user", "alex", "block_color", "false"),
    ]
    winner = resolve_rules(rules, ctx())["block_color"]
    assert winner.scope_type == "user"
    assert evaluate_policies(rules, ctx(is_color=True)).allowed


def test_inactive_rules_are_ignored():
    rules = [PolicyRule("global", None, "block_color", "true", is_active=False)]
    assert evaluate_policies(rules, ctx(is_color=True)).allowed


def test_rules_for_another_user_or_printer_do_not_apply():
    rules = [
        PolicyRule("user", "someone-else", "block_color", "true"),
        PolicyRule("printer", "color-mfp", "block_color", "true"),
    ]
    assert evaluate_policies(rules, ctx(is_color=True)).allowed


def test_quota_denies_when_the_job_does_not_fit():
    state = QuotaState(user_limit=100, user_used=95)
    assert evaluate_quota(state, 5).allowed
    denied = evaluate_quota(state, 6)
    assert not denied.allowed and denied.rule == "user_quota"


def test_soft_enforcement_allows_the_overrun_but_notes_it():
    state = QuotaState(user_limit=10, user_used=10)
    decision = evaluate_quota(state, 5, enforcement="soft")
    assert decision.allowed and decision.notes


def test_group_budget_is_enforced_in_addition_to_the_user_quota():
    state = QuotaState(
        user_limit=100, user_used=0, group_shared_quota=20, group_used=18, group_name="finance"
    )
    assert evaluate_quota(state, 2).allowed
    denied = evaluate_quota(state, 3)
    assert not denied.allowed and denied.rule == "group_quota"
    assert evaluate_quota(state, 3, enforce_group_budget=False).allowed


def test_unlimited_group_pool_never_denies():
    state = QuotaState(user_limit=100, user_used=0, group_shared_quota=None, group_used=9999)
    assert evaluate_quota(state, 50).allowed


def test_policy_denial_takes_precedence_over_quota_denial():
    rules = [PolicyRule("global", None, "block_color", "true")]
    decision = evaluate(rules, ctx(is_color=True, estimated_pages=999), QuotaState(100, 100))
    assert decision.rule == "block_color"


def test_forced_options_survive_a_quota_denial_for_logging():
    rules = [PolicyRule("global", None, "force_duplex", "true")]
    decision = evaluate(rules, ctx(estimated_pages=999), QuotaState(10, 0))
    assert not decision.allowed
    assert decision.forced_options == {"sides": "two-sided-long-edge"}


@pytest.mark.parametrize(
    "rule_type,value",
    [("max_pages_per_job", "0"), ("max_pages_per_job", "abc"), ("block_filetype", ""), ("nope", "true")],
)
def test_validate_rule_rejects_nonsense(rule_type, value):
    with pytest.raises(ValueError):
        validate_rule(rule_type, value)


def test_validate_rule_accepts_good_input():
    validate_rule("max_pages_per_job", "50")
    validate_rule("block_color", "true")
    validate_rule("block_filetype", "exe,ps")


def test_decision_deny_mutates_in_place():
    decision = Decision(allowed=True)
    decision.deny("nope", "rule")
    assert not decision.allowed and decision.reason == "nope" and decision.rule == "rule"
