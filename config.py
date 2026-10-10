from datetime import datetime, timedelta
import requests
import time
import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
USERNAME = os.getenv("USERNAME")
AI_API_KEY = os.getenv("AI_API_KEY")

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

# time window — set START_DATE and END_DATE in .env for your review cycle
# format: YYYY-MM-DD
START_DATE = os.getenv("START_DATE")
END_DATE = os.getenv("END_DATE")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", 90))

if START_DATE and END_DATE:
    lookback_start = datetime.strptime(START_DATE, "%Y-%m-%d")
    lookback_end = datetime.strptime(END_DATE, "%Y-%m-%d")
elif START_DATE:
    lookback_start = datetime.strptime(START_DATE, "%Y-%m-%d")
    lookback_end = datetime.now()
elif END_DATE:
    lookback_end = datetime.strptime(END_DATE, "%Y-%m-%d")
    lookback_start = lookback_end - timedelta(days=LOOKBACK_DAYS)
else:
    # fallback to LOOKBACK_DAYS if no dates provided
    lookback_start = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    lookback_end = datetime.now()

# all thresholds below are starting guesses, not research-backed numbers —
# validate them against real repos before trusting the results

# --- pre-filter ---
LOCK_FILES = ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
              "Pipfile.lock", "Gemfile.lock", "Cargo.lock", "composer.lock", "go.sum"]
REVERT_OVERLAP_THRESHOLD = 0.9  # diff must be >= 90% inverse of an earlier commit

# --- step 1: lint/config ---
MINOR_CHANGE_LINES = 2  # commits at or below this many changed lines are lint/minor
CONFIG_FILES = [".eslintrc", ".prettierrc", ".babelrc", "jest.config",
                "tsconfig", ".gitignore", "package.json", "requirements.txt"]

# --- step 2: WIP flag ---
WIP_KEYWORDS = ["wip", "tmp", "temp", "fixup", "squash", "checkpoint"]
DUMMY_LINE_PATTERNS = ["pass", "TODO", "placeholder", "fixme", "hack"]

# --- step 3: test files ---
TEST_DIRS = ["test", "tests", "__tests__", "spec"]  # any directory in the path
TEST_FILE_PATTERNS = [                               # matched against the file name
    "test_*.py", "*_test.py", "*_test.go", "*.test.*", "*.spec.*",
    "*Test.java", "*Spec.java"
]
TEST_SHARE_THRESHOLD = 0.8  # share of changed lines that must be in test files
MAJOR_DIFF_LINES = 20       # non-test changes above this make it more than a test commit

# --- step 4: lint by content ---
COMMENT_LINE_PREFIXES = ["#", "//", "/*", "*", "*/", "<!--"]

# --- AI + meaningful check ---
AI_MODEL = "claude-haiku-5-5"  # bucket sorting is simple classification — Haiku is ~50x cheaper than Opus
SURVIVAL_THRESHOLD = 0.5  # share of added lines that must survive into the PR's final diff

# --- scoring weights ---
MEANINGFUL_COMMIT_WEIGHT = 3
ADDRESS_COMMENT_WEIGHT = 1
TESTING_WEIGHT = 1
ADDITION_WEIGHT = 1.5  # additions count more than deletions
DELETION_WEIGHT = 1.0
# size term = SIZE_WEIGHT * log2(1 + weighted meaningful lines in the PR), with lines capped.
# taken over the whole PR, so splitting a feature into more commits doesn't change it
SIZE_WEIGHT = 1.0
SIZE_LINE_CAP = 2000  # weighted lines past this add nothing (generated files, vendored code)

# --- results ---
MEANINGFUL_PR_PERCENTILE = 75  # PRs scoring at or above this percentile count as meaningful
TOP_N_PRS = 10

# rate limiting
RATE_LIMIT_PAUSE = 1  # seconds to wait between API calls
RATE_LIMIT_THRESHOLD = 100  # pause more aggressively when remaining calls drop below this

def make_request(url, allow_missing=False):
    """
    wrapper for all API calls with rate limit handling.
    raises on HTTP errors; with allow_missing, a 404 returns None instead
    """
    while True:
        response = requests.get(url, headers=headers, timeout=30)

        # check rate limit headers
        remaining = int(response.headers.get("X-RateLimit-Remaining", 1))
        reset_time = int(response.headers.get("X-RateLimit-Reset", 0))
        retry_after = response.headers.get("Retry-After")

        if response.status_code in (403, 429) and (remaining == 0 or retry_after):
            # primary limit: wait until reset; secondary limit: wait Retry-After
            if retry_after:
                wait_time = int(retry_after)
            else:
                wait_time = reset_time - int(time.time()) + 5  # +5 buffer
            print(f"Rate limit hit — waiting {wait_time} seconds...")
            time.sleep(max(wait_time, 0))
            continue  # retry

        if remaining < RATE_LIMIT_THRESHOLD:
            # getting close — slow down
            print(f"Approaching rate limit ({remaining} remaining) — slowing down...")
            time.sleep(5)
        else:
            # normal pace
            time.sleep(RATE_LIMIT_PAUSE)

        if response.status_code == 404 and allow_missing:
            return None
        response.raise_for_status()
        return response.json()


def get_paginated(url):
    """fetch every page of a list endpoint (url must not already have a query string)"""
    results = []
    page = 1
    while True:
        res = make_request(f"{url}?per_page=100&page={page}")
        results.extend(res)
        if len(res) < 100:
            return results
        page += 1
