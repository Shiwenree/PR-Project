import evaluate as E
import reviews as R


def comment(id, login, path=None, reply_to=None, at="2026-03-01T00:00:00Z", **extra):
    c = {"id": id, "user": {"login": login}, "created_at": at, **extra}
    if path:
        c["path"] = path
    if reply_to:
        c["in_reply_to_id"] = reply_to
    return c


# --- reviews.py ---------------------------------------------------------------

def test_thread_replies_are_not_reviews():
    reply = comment(2, "me", path="a.py", reply_to=1, pull_request_review_id=50)
    new = comment(3, "me", path="a.py", pull_request_review_id=60)
    reply_only = {"id": 50, "state": "COMMENTED", "body": ""}
    real = {"id": 60, "state": "COMMENTED", "body": ""}
    assert not R.counts_as_review(reply_only, [reply, new])
    assert R.counts_as_review(real, [reply, new])
    assert R.counts_as_review({"id": 70, "state": "APPROVED", "body": ""}, [])


def test_acknowledged_by_reply_or_outdated_code():
    asked = comment(1, "me", path="a.py", line=10, original_line=10)
    assert not R.was_acknowledged(asked, [asked], "me", "author")
    assert R.was_acknowledged(asked, [asked, comment(2, "author", path="a.py", reply_to=1)], "me", "author")
    outdated = comment(1, "me", path="a.py", line=None, original_line=10)
    assert R.was_acknowledged(outdated, [outdated], "me", "author")


def test_general_comment_needs_the_author_to_respond():
    asked = comment(1, "me")
    later = comment(2, "author", at="2026-03-02T00:00:00Z")
    bystander = comment(3, "someone", at="2026-03-02T00:00:00Z")
    assert R.was_acknowledged(asked, [asked, later], "me", "author")
    assert not R.was_acknowledged(asked, [asked, bystander], "me", "author")


# --- eval.py ------------------------------------------------------------------

def record(sha, bucket, decided_by):
    return {"sha": sha, "bucket": bucket, "decided_by": decided_by}


def test_agreement_metrics():
    records = [record("a", "meaningful", "AI"), record("b", "WIP", "step 4: only 20% of added lines reached the final PR"),
               record("c", "meaningful", "AI"), record("d", "testing", "step 3: mostly test files")]
    labels = {"a": "meaningful", "b": "meaningful", "c": "meaningful", "d": "testing", "zz": "WIP"}
    result = E.agreement(labels, records)
    assert result["labeled"] == 4  # zz isn't in the results
    assert result["agreement"] == 0.75
    assert result["per_bucket"]["meaningful"]["recall"] == 2 / 3
    assert result["per_bucket"]["meaningful"]["precision"] == 1
    assert result["wrong_by_check"] == {"step 4: only N% of added lines reached the final PR": 1}


def test_stratified_sample_covers_rare_buckets():
    records = [record(f"m{i}", "meaningful", "AI") for i in range(50)] + [record("w", "WIP", "x")]
    picked = E.stratified_sample(records, 10)
    assert len(picked) == 10
    assert any(r["bucket"] == "WIP" for r in picked)
