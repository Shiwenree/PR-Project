from datetime import datetime, timedelta
import json
import requests
import time
import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
# GITHUB_USERNAME, not USERNAME — Windows already sets USERNAME to the computer's login name
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")
TEAMS_FILE = os.getenv("TEAMS_FILE", "teams.json")
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
                "tsconfig", ".gitignore", "package.json", "requirements.txt",
                # other ecosystems' manifests
                "Cargo.toml", "pyproject.toml", "setup.cfg", "go.mod", "Gemfile",
                "pom.xml", "build.gradle", "rust-toolchain", ".editorconfig",
                # release bookkeeping: version bumps usually touch only these
                "CHANGELOG"]  # lock files (LOCK_FILES) also count as config

# --- step 1: docs ---
# prose files. config files (CHANGELOG.md) are never docs, even with a doc extension
DOC_EXTENSIONS = [".md", ".rst", ".adoc"]
DOC_FILE_NAMES = ["README", "LICENSE", "COPYING", "UNLICENSE", "AUTHORS",
                  "CONTRIBUTING", "CODE_OF_CONDUCT"]  # matched against the start of the file name

# --- step 2: WIP flag ---
WIP_KEYWORDS = ["wip", "tmp", "temp", "fixup", "squash", "checkpoint"]
DUMMY_LINE_PATTERNS = ["pass", "TODO", "placeholder", "fixme", "hack"]

# --- step 3: test files ---
TEST_DIRS = ["test", "tests", "__tests__", "spec",   # any directory in the path
             "testdata", "test-data", "test_data", "fixtures"]
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
BATCH_POLL_SECONDS = 30   # how often --batch checks whether the batch has finished
# the diff sent to the AI is trimmed so huge commits stay cheap and fit the model's context.
# every file is still listed with its line counts; only the patch text is cut
AI_MAX_LINES_PER_FILE = 200  # patch lines shown per file
AI_MAX_DIFF_LINES = 1500     # patch lines shown across the whole commit
SURVIVAL_THRESHOLD = 0.5  # share of added lines that must survive into the PR's final diff
SURVIVAL_SIMILARITY = 0.75  # a line this similar to a final line counts as surviving (catches renames)

# --- shadow rule: a candidate free rule recorded next to the AI, never used for scoring ---
# it calls a commit meaningful when it's substantial source code that made it into the final PR.
# `python evaluate.py shadow` compares it with the AI's answers to see if it can replace AI calls
SHADOW_MIN_ADDED_LINES = 50  # added lines with letters or digits
SHADOW_MIN_SURVIVAL = 0.9    # share of those lines that reach the PR's final diff
# tests written inside source files (Rust's `mod tests`, a Python test_ function) — the rule
# stays quiet when an added line looks like one, since the commit may be mostly tests
TEST_CODE_MARKERS = [r"#\[test\]", r"#\[cfg\(test\)\]", r"\bdef test_", r"@Test\b",
                     r"\bfunc Test", r"\b(describe|it|test)\(\s*['\"`]"]
SHADOW_LOG_FILE = "shadow_log.jsonl"  # every AI decision with the rule's guess, kept across runs
# the rule is trusted to replace the AI once all three hold
SHADOW_MIN_COMMITS = 100     # commits the rule would have handled
SHADOW_MIN_AGREEMENT = 0.98  # share of those where the AI also said meaningful
SHADOW_MIN_PEOPLE = 5        # different authors among them

# --- scoring weights ---
# each bucket scores weight * log2(1 + commits), so one commit = the full weight and
# splitting work into more commits has diminishing returns
MEANINGFUL_COMMIT_WEIGHT = 3
ADDRESS_COMMENT_WEIGHT = 1
TESTING_WEIGHT = 1
DOCS_WEIGHT = 1
ADDITION_WEIGHT = 1.5  # additions count more than deletions
DELETION_WEIGHT = 1.0
# size term = SIZE_WEIGHT * log2(1 + weighted meaningful lines in the PR), with lines capped.
# taken over the whole PR, so splitting a feature into more commits doesn't change it
# only code counts toward size: docs, config, and lines moved within the commit are left out
SIZE_WEIGHT = 1.0
SIZE_LINE_CAP = 2000  # weighted lines past this add nothing (generated files, vendored code)

# --- results ---
MEANINGFUL_PR_PERCENTILE = 75  # PRs at or above this percentile of every PR in the run (all teams) count as meaningful
TOP_N_PRS = 10
RESULTS_FILE = "results.json"  # full results, per commit — the data source for a dashboard

# rate limiting
RATE_LIMIT_PAUSE = 1  # seconds to wait between API calls
RATE_LIMIT_THRESHOLD = 100  # pause more aggressively when remaining calls drop below this

def load_teams():
    """
    {team name: [github usernames]} from teams.json. without that file, a one-person
    team from GITHUB_USERNAME. the org baseline is everyone across all teams
    """
    if os.path.exists(TEAMS_FILE):
        with open(TEAMS_FILE) as f:
            return json.load(f)
    if GITHUB_USERNAME:
        return {"default": [GITHUB_USERNAME]}
    raise SystemExit(f"Set GITHUB_USERNAME in .env, or list your teams in {TEAMS_FILE}")


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
