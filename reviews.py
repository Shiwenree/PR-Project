from datetime import datetime, timedelta
from config import (
    lookback_start, lookback_end, make_request, get_paginated, load_teams
)

# lookback_end is a date at midnight, so include that whole day
window_end = lookback_end + timedelta(days=1)


def parse_time(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def login_of(item):
    """login of a review/comment/commit's user — None for deleted (ghost) accounts"""
    return (item.get("user") or item.get("author") or {}).get("login")


def get_pr_reviews(repo_full_name, pr_number):
    """fetch all reviews for a PR"""
    return get_paginated(f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/reviews")

def get_pr_comments(repo_full_name, pr_number):
    """fetch all inline and general PR comments"""
    base = f"https://api.github.com/repos/{repo_full_name}"
    return (get_paginated(f"{base}/pulls/{pr_number}/comments") +
            get_paginated(f"{base}/issues/{pr_number}/comments"))

def get_pr_commits(repo_full_name, pr_number):
    """fetch all commits for a PR (oldest first)"""
    return get_paginated(f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/commits")

def counts_as_review(review, pr_comments):
    """
    replying inside an inline comment thread makes GitHub create an empty "commented"
    review — only count a commented review if it has a body or starts a new comment
    """
    if review["state"] != "COMMENTED" or (review.get("body") or "").strip():
        return True
    return any(c.get("pull_request_review_id") == review["id"] and "in_reply_to_id" not in c
               for c in pr_comments)


def was_acknowledged(comment, pr_comments, username, pr_author):
    """
    inline comment: someone else replied in its thread, or the code it was on changed
    afterwards (GitHub marks the comment outdated: `line` becomes null, `original_line` stays).
    general comment: the PR author posted in the conversation after it
    """
    if "path" in comment:
        replied = any(c.get("in_reply_to_id") == comment["id"] and login_of(c) != username
                      for c in pr_comments)
        outdated = comment.get("line") is None and comment.get("original_line") is not None
        return replied or outdated
    comment_time = parse_time(comment["created_at"])
    return any("path" not in c and login_of(c) == pr_author and parse_time(c["created_at"]) > comment_time
               for c in pr_comments)


def analyze_review_impact(username, pr_author, reviews, pr_comments):
    """
    analyze whether reviews given by username were actioned upon
    signal 1: REQUEST_CHANGES followed by APPROVED from same reviewer
    signal 2: each comment was acknowledged — see was_acknowledged
    """
    actioned_count = 0
    acknowledged_count = 0
    no_response_count = 0

    user_reviews = [r for r in reviews if login_of(r) == username and r.get("submitted_at")]

    # signal 1: REQUEST_CHANGES followed by APPROVED from same reviewer
    request_changes_reviews = [r for r in user_reviews if r["state"] == "CHANGES_REQUESTED"]
    approved_reviews = [r for r in user_reviews if r["state"] == "APPROVED"]

    for rc_review in request_changes_reviews:
        rc_time = parse_time(rc_review["submitted_at"])
        if not lookback_start <= rc_time < window_end:
            continue
        later_approvals = [r for r in approved_reviews if parse_time(r["submitted_at"]) > rc_time]
        if later_approvals:
            actioned_count += 1

    # signal 2: each comment that starts a thread, acknowledged or not
    reviewer_comments = [
        c for c in pr_comments
        if login_of(c) == username
        and "in_reply_to_id" not in c
        and lookback_start <= parse_time(c["created_at"]) < window_end
    ]

    for comment in reviewer_comments:
        if was_acknowledged(comment, pr_comments, username, pr_author):
            acknowledged_count += 1
        else:
            no_response_count += 1

    return {
        "actioned": actioned_count,
        "acknowledged": acknowledged_count,
        "no_response": no_response_count
    }

def get_reviews_given(username):
    """get all PRs reviewed by username and analyze review impact"""
    review_count = 0
    approval_count = 0
    total_actioned = 0
    total_acknowledged = 0
    total_no_response = 0

    page = 1
    while True:
        # search can't filter by review date, so take every PR updated since the window
        # opened and keep only the reviews submitted inside it
        url = f"https://api.github.com/search/issues?q=type:pr+reviewed-by:{username}+-author:{username}+updated:{lookback_start.strftime('%Y-%m-%d')}..*&per_page=100&page={page}"
        res = make_request(url)
        items = res.get("items", [])

        for pr in items:
            repo_full_name = pr["repository_url"].split("repos/")[1]
            pr_number = pr["number"]

            reviews = get_pr_reviews(repo_full_name, pr_number)
            pr_comments = get_pr_comments(repo_full_name, pr_number)

            for review in reviews:
                in_window = review.get("submitted_at") and lookback_start <= parse_time(review["submitted_at"]) < window_end
                if login_of(review) == username and in_window and counts_as_review(review, pr_comments):
                    review_count += 1
                    if review["state"] == "APPROVED":
                        approval_count += 1

            impact = analyze_review_impact(username, login_of(pr), reviews, pr_comments)
            total_actioned += impact["actioned"]
            total_acknowledged += impact["acknowledged"]
            total_no_response += impact["no_response"]

        # search returns at most 1000 results (10 pages)
        if len(items) < 100 or page == 10:
            break
        page += 1

    return {
        "review_count": review_count,
        "approval_count": approval_count,
        "actioned": total_actioned,
        "acknowledged": total_acknowledged,
        "no_response": total_no_response
    }

# main
if __name__ == "__main__":
    teams = load_teams()
    print(f"GitHub review stats ({lookback_start.strftime('%Y-%m-%d')} to {lookback_end.strftime('%Y-%m-%d')}):")
    for username in dict.fromkeys(m for members in teams.values() for m in members):
        print(f"\nFetching review stats for {username}...")
        review_stats = get_reviews_given(username)

        print(f"  Total reviews given: {review_stats['review_count']}")
        print(f"  Approvals given: {review_stats['approval_count']}")
        print(f"  Reviews actioned (REQUEST_CHANGES -> APPROVED): {review_stats['actioned']}")
        print(f"  Comments acknowledged (reply, code changed, or author responded): {review_stats['acknowledged']}")
        print(f"  Comments with no response: {review_stats['no_response']}")
