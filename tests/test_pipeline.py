from types import SimpleNamespace

import pytest

import PRs as P


# --- helpers ----------------------------------------------------------------

def make_commit(sha="c1", login="me", message="change", files=None, parents=1,
                authored="2026-01-02T00:00:00Z", additions=None, deletions=0):
    files = files if files is not None else [{"filename": "src/app.py", "patch": "+x = compute(1)", "changes": 1}]
    if additions is None:
        additions = sum(f.get("changes", 0) for f in files)
    return {
        "commit": {
            "sha": sha,
            "author": {"login": login} if login else None,
            "parents": [{}] * parents,
            "commit": {"message": message, "author": {"name": login or "x", "date": authored}},
        },
        "files": files,
        "stats": {"additions": additions, "deletions": deletions},
    }


def make_ctx(shas=("c0", "c1", "c2"), reviews=(), comments=(), final_lines=None):
    return P.PRContext("org/repo", "PR title", final_lines or {}, list(comments), list(reviews), list(shas))


def review(state, login, commit_id, at):
    return {"state": state, "user": {"login": login}, "commit_id": commit_id, "submitted_at": at}


# --- pre-filter -------------------------------------------------------------

@pytest.mark.parametrize("commit, reason", [
    (make_commit(parents=2), "merge"),
    (make_commit(files=[]), "empty"),
    (make_commit(files=[{"filename": "web/package-lock.json", "patch": "+x", "changes": 1}]), "lock"),
    (make_commit(login="dependabot[bot]"), "bot"),
    (make_commit(login="teammate"), "other author"),
    (make_commit(login=None), None),  # email GitHub couldn't link: kept
    (make_commit(), None),
])
def test_simple_filter(commit, reason):
    assert P.simple_filter_reason(commit, "me") == reason


@pytest.mark.parametrize("trailer", [
    "Co-authored-by: Someone <123+me@users.noreply.github.com>",
    "Co-authored-by: me <me@company.com>",
])
def test_co_author_keeps_teammates_commit(trailer):
    commit = make_commit(login="teammate", message=f"pair on parser\n\n{trailer}")
    assert P.simple_filter_reason(commit, "me") is None


def test_revert_chain_keeps_only_the_recommit():
    add = [{"filename": "a.py", "patch": "+feature()\n+more()", "changes": 2}]
    remove = [{"filename": "a.py", "patch": "-feature()\n-more()", "changes": 2}]
    commits = [make_commit("c0", files=add), make_commit("c1", files=remove), make_commit("c2", files=add)]
    kept, dropped = P.pre_filter(commits, "me")
    assert [c["commit"]["sha"] for c in kept] == ["c2"]
    assert [reason for _, reason in dropped] == ["reverted", "reverted"]


# --- step 1, 3, 4 -------------------------------------------------------------

@pytest.mark.parametrize("path, expected", [
    ("tests/test_api.py", True),
    ("src/__tests__/app.js", True),
    ("pkg/test_utils.py", True),
    ("web/button.spec.ts", True),
    ("server/handler_test.go", True),
    ("src/latest_utils.py", False),
    ("contest_service.py", False),
])
def test_is_test_file(path, expected):
    assert P.is_test_file(path) is expected


def test_step1_reasons():
    config_only = make_commit(files=[{"filename": "tsconfig.json", "patch": "+a\n+b\n+c", "changes": 3}])
    assert P.step1_lint_config(config_only) == ("lint/config", "step 1: config files only")
    tiny = make_commit(files=[{"filename": "a.py", "patch": "+x = 1", "changes": 1}])
    assert P.step1_lint_config(tiny)[1].startswith("step 1: 2 changed lines")


def test_files_without_a_diff_are_never_lint():
    big = [{"filename": "generated.bin"}]
    assert not P.is_whitespace_only(big)
    assert not P.is_lint_by_content(big)


def test_step3_mostly_tests():
    files = [{"filename": "tests/test_a.py", "changes": 40}, {"filename": "src/a.py", "changes": 5}]
    assert P.step3_test_check(make_commit(files=files)) == ("testing", "step 3: mostly test files")


# --- survival -----------------------------------------------------------------

def survival(patch, final):
    commit = make_commit(files=[{"filename": "a.py", "patch": patch}])
    return P.survival_ratio(commit, make_ctx(final_lines={"a.py": final}))


def test_survival_exact_and_trivial_lines():
    # `}` is skipped; one of the two real lines made it into the final diff
    assert survival("+value = compute(x)\n+}\n+old_helper()", {"value = compute(x)"}) == 0.5


def test_survival_counts_renamed_lines():
    assert survival("+total_count = parse_items(rows)", {"total_items = parse_items(rows)"}) == 1.0


def test_survival_file_removed_inside_the_pr():
    commit = make_commit(files=[{"filename": "debug.py", "patch": "+print_debug(state)"}])
    assert P.survival_ratio(commit, make_ctx(final_lines={})) == 0


# --- step 3.5 -----------------------------------------------------------------

def test_addressed_by_commit_position():
    # reviewer asked on c0, the user pushed c1, the reviewer approved c1
    ctx = make_ctx(reviews=[review("CHANGES_REQUESTED", "rev", "c0", "2026-01-01T00:00:00Z"),
                            review("APPROVED", "rev", "c1", "2026-01-03T00:00:00Z")])
    assert P.step35_conversation_check(make_commit("c1"), ctx)[0] == "addressed comments"


def test_positions_beat_rebased_timestamps():
    # after a rebase every commit is dated after the review, but positions still
    # say c2 came before the request on c2 — so c2 didn't address it
    ctx = make_ctx(reviews=[review("CHANGES_REQUESTED", "rev", "c2", "2026-01-01T00:00:00Z"),
                            review("APPROVED", "rev", "c2", "2026-01-05T00:00:00Z")])
    assert P.step35_conversation_check(make_commit("c2", authored="2026-01-04T00:00:00Z"), ctx) is None


def test_rewritten_review_commit_falls_back_to_author_date():
    ctx = make_ctx(reviews=[review("CHANGES_REQUESTED", "rev", "gone", "2026-01-01T00:00:00Z"),
                            review("APPROVED", "rev", "c2", "2026-01-05T00:00:00Z")])
    assert P.step35_conversation_check(make_commit("c1"), ctx)[0] == "addressed comments"


def test_inline_comment_on_touched_file_counts_and_keywords_dont():
    approval = review("APPROVED", "rev", "c2", "2026-01-03T00:00:00Z")
    on_file = {"path": "src/app.py", "original_commit_id": "c0", "created_at": "2026-01-01T00:00:00Z", "user": {"login": "rev"}}
    other_file = {**on_file, "path": "docs/readme.md"}
    commit = make_commit("c1", message="address feedback")
    assert P.step35_conversation_check(commit, make_ctx(reviews=[approval], comments=[on_file]))
    assert P.step35_conversation_check(commit, make_ctx(reviews=[approval], comments=[other_file])) is None


def test_own_and_deleted_user_comments_ignored():
    approval = review("APPROVED", "rev", "c2", "2026-01-03T00:00:00Z")
    own = {"path": "src/app.py", "original_commit_id": "c0", "created_at": "2026-01-01T00:00:00Z", "user": {"login": "me"}}
    ghost = {**own, "user": None}
    ctx = make_ctx(reviews=[approval], comments=[own, ghost])
    assert P.step35_conversation_check(make_commit("c1"), ctx) is None


# --- AI step ------------------------------------------------------------------

def message(text, stop_reason="end_turn"):
    content = [SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)] if text else []
    return SimpleNamespace(stop_reason=stop_reason, content=content)


def test_ai_answer_keeps_the_reason():
    record = P.commit_record(make_commit(), "org/repo")
    P.apply_ai_response(record, message('{"category": "meaningful", "reason": "Adds a parser."}'))
    assert (record["bucket"], record["decided_by"], record["ai_reason"]) == ("meaningful", "AI", "Adds a parser.")


@pytest.mark.parametrize("msg", [
    message(None, stop_reason="refusal"),
    message('{"category": "meaning', stop_reason="max_tokens"),
    message('{"category": "addressed comments", "reason": "x"}'),  # not an AI bucket
])
def test_ai_failures_fail_closed(msg):
    record = P.commit_record(make_commit(), "org/repo")
    P.apply_ai_response(record, msg)
    assert record["bucket"] == "unclassified"
    assert record["ai_reason"] == "N/A"


def test_no_ai_mode_marks_unclassified_without_calling_claude():
    ai = P.AIClassifier("off")
    record = P.organize_commit(make_commit(files=[{"filename": "src/app.py", "patch": "+a = parse(x)\n+b = run(a)\n+c = save(b)", "changes": 3}]),
                               make_ctx(final_lines={"src/app.py": {"a = parse(x)", "b = run(a)", "c = save(b)"}}), ai)
    assert record["bucket"] == "unclassified"
    assert record["decided_by"] == "skipped (--no-ai)"


def test_batch_mode_queues_requests():
    ai = P.AIClassifier("batch")
    record = P.commit_record(make_commit(), "org/repo")
    ai.classify(record, make_commit(), make_ctx(), False)
    assert record["bucket"] is None and len(ai.pending) == 1
    assert ai.pending[0][1]["output_config"]["format"]["schema"] == P.COMMIT_ANALYSIS_SCHEMA


def test_non_ai_commits_get_na_reason():
    record = P.organize_commit(make_commit(files=[{"filename": "tsconfig.json", "patch": "+a\n+b\n+c", "changes": 3}]),
                               make_ctx(), P.AIClassifier("off"))
    assert record["ai_reason"] == "N/A"
    assert record["decided_by"] == "step 1: config files only"


# --- scoring ------------------------------------------------------------------

def buckets(**counts):
    return {b: counts.get(b.replace(" ", "_").replace("/", "_"), 0) for b in P.BUCKETS}


def test_one_commit_earns_the_full_weight():
    assert P.calculate_pr_score(buckets(meaningful=1), 0, 0) == 3


def test_splitting_has_diminishing_returns():
    one = P.calculate_pr_score(buckets(meaningful=1), 500, 0)
    ten = P.calculate_pr_score(buckets(meaningful=10), 500, 0)
    assert ten / one < 2  # was 10x with a linear count


def test_size_is_capped():
    assert P.size_term(1_000_000, 0) == P.size_term(10_000, 0)


def test_team_and_org_comparison():
    users = {
        "a": {"username": "a", "prs": [{"score": 10}, {"score": 20}]},
        "b": {"username": "b", "prs": [{"score": 10}]},
        "c": {"username": "c", "prs": [{"score": 30}]},
    }
    bar, org_average, teams = P.summarize_users(users, {"x": ["a", "b"], "y": ["c"]})
    assert org_average == pytest.approx(70 / 3, abs=0.01)
    assert teams["x"]["average_total_score"] == 20
    a = teams["x"]["members"][0]
    assert (a["total_score"], a["vs_team_pct"]) == (30, 50)
    assert users["c"]["meaningful_prs"] == 1 and users["b"]["meaningful_prs"] == 0
