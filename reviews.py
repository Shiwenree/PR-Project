from datetime import datetime, timedelta
from config import (
    USERNAME, lookback_start, lookback_end, make_request, get_paginated
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

def analyze_review_impact(username, reviews, pr_comments, commits):
    """
    analyze whether reviews given by username were actioned upon
    signal 1: REQUEST_CHANGES followed by APPROVED from same reviewer
    signal 2: PR author replied to or a commit was made after reviewer comment
    """
    actioned_count = 0
    acknowledged_count = 0
    no_response_count = 0

    user_reviews = [r for r in reviews if login_of(r) == username and r.get("submitted_at")]

    commit_times = [parse_time(c["commit"]["committer"]["date"]) for c in commits]

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

    # signal 2: reply or commit after reviewer inline comment
    reviewer_comments = [
        c for c in pr_comments
        if login_of(c) == username
        and "in_reply_to_id" not in c
        and lookback_start <= parse_time(c["created_at"]) < window_end
    ]

    for comment in reviewer_comments:
        comment_id = comment["id"]
        comment_time = parse_time(comment["created_at"])

        replies = [
            c for c in pr_comments
            if c.get("in_reply_to_id") == comment_id
            and login_of(c) != username
        ]

        commits_after = [t for t in commit_times if t > comment_time]

        if replies or commits_after:
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
            commits = get_pr_commits(repo_full_name, pr_number)

            for review in reviews:
                in_window = review.get("submitted_at") and lookback_start <= parse_time(review["submitted_at"]) < window_end
                if login_of(review) == username and in_window:
                    review_count += 1
                    if review["state"] == "APPROVED":
                        approval_count += 1

            impact = analyze_review_impact(username, reviews, pr_comments, commits)
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
    print(f"Fetching review stats for {USERNAME}...")
    review_stats = get_reviews_given(USERNAME)

    print(f"  GitHub review stats for {USERNAME} ({lookback_start.strftime('%Y-%m-%d')} to {lookback_end.strftime('%Y-%m-%d')}):")
    print(f"  Total reviews given: {review_stats['review_count']}")
    print(f"  Approvals given: {review_stats['approval_count']}")
    print(f"  Reviews actioned (REQUEST_CHANGES -> APPROVED): {review_stats['actioned']}")
    print(f"  Reviews acknowledged (reply or commit after): {review_stats['acknowledged']}")
    print(f"  Reviews with no response: {review_stats['no_response']}")