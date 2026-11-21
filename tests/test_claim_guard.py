"""The claim lock: one issue, one assignee, one open PR."""

from tools.claim_guard import claim_errors, linked_issue


def test_linked_issue_accepts_the_closing_keywords():
    assert linked_issue("Closes #12") == 12
    assert linked_issue("fixes #7") == 7
    assert linked_issue("Resolved #3") == 3
    assert linked_issue("CLOSES #9") == 9


def test_linked_issue_ignores_a_bare_reference():
    assert linked_issue("see #12 for context") is None
    assert linked_issue("") is None
    assert linked_issue(None) is None


def test_claim_passes_when_the_author_is_assigned():
    assert claim_errors(12, "ada", ["ada"], []) == []


def test_no_linked_issue_fails():
    assert claim_errors(None, "ada", [], []) == [
        "PR body must link an issue with `Closes #N`."
    ]


def test_unassigned_issue_fails_with_a_nobody_message():
    errors = claim_errors(12, "ada", [], [])

    assert len(errors) == 1
    assert "nobody" in errors[0]
    assert "#12" in errors[0]


def test_issue_assigned_to_another_account_fails():
    errors = claim_errors(12, "ada", ["ben"], [])

    assert len(errors) == 1
    assert "ben" in errors[0]
    assert "ada" in errors[0]


def test_another_open_pr_for_the_same_issue_fails():
    errors = claim_errors(12, "ada", ["ada"], [13])

    assert len(errors) == 1
    assert "#13" in errors[0]


def test_every_competing_pr_is_named():
    errors = claim_errors(12, "ada", ["ada"], [13, 14])

    assert len(errors) == 1
    assert "#13" in errors[0]
    assert "#14" in errors[0]
