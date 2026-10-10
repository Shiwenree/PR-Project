"""
check the pipeline against your own judgement.

  python evaluate.py sample   # pick commits from results.json into labels.csv
  (open labels.csv, read each commit at its url, and fill in the label column)
  python evaluate.py score    # compare your labels with the pipeline's buckets
  python evaluate.py shadow   # is the shadow rule ready to replace AI calls?

labels.csv deliberately leaves out the pipeline's answer, so it can't sway your labels.
"""
import argparse
import csv
import json
import math
import os
import random
import re
from collections import Counter, defaultdict

from config import (
    RESULTS_FILE, SHADOW_LOG_FILE, SHADOW_MIN_COMMITS, SHADOW_MIN_AGREEMENT, SHADOW_MIN_PEOPLE
)

LABELS = ["meaningful", "addressed comments", "testing", "lint/config", "WIP"]
LABELS_FILE = "labels.csv"
FIELDS = ["sha", "repo", "pr", "url", "message", "label"]


def load_commit_records(results_file):
    """every bucketed commit in the results (pre-filtered and unclassified commits have no bucket to check)"""
    with open(results_file) as f:
        results = json.load(f)
    records = []
    for user in results["users"]:
        for pr in user["prs"]:
            for record in pr["commits"]:
                if record["bucket"] in LABELS:
                    records.append({**record, "repo": pr["repo"], "pr": pr["pr_number"]})
    return records


def stratified_sample(records, n, seed=0):
    """
    an even share from every bucket, so rare buckets get checked too, topped up at random.
    this makes the per-bucket numbers trustworthy, but the overall agreement rate
    over-weights rare buckets compared with a real run
    """
    rng = random.Random(seed)
    by_bucket = defaultdict(list)
    for r in records:
        by_bucket[r["bucket"]].append(r)

    picked = []
    per_bucket = n // max(len(by_bucket), 1)
    for bucket_records in by_bucket.values():
        picked += rng.sample(bucket_records, min(per_bucket, len(bucket_records)))
    rest = [r for r in records if r not in picked]
    picked += rng.sample(rest, min(n - len(picked), len(rest)))
    rng.shuffle(picked)
    return picked


def agreement(labels, records):
    """
    compare human labels {sha: label} with the pipeline's records.
    returns overall agreement, per-bucket precision/recall, the confusion counts,
    and which check made each wrong call — that tells you which step to tune
    """
    by_sha = {r["sha"]: r for r in records}
    pairs = [(label, by_sha[sha]) for sha, label in labels.items() if sha in by_sha]
    confusion = Counter((label, r["bucket"]) for label, r in pairs)
    # "only 20% of lines..." and "only 35% of lines..." are the same check
    wrong_by_check = Counter(re.sub(r"\d+%", "N%", r["decided_by"]) for label, r in pairs if label != r["bucket"])

    per_bucket = {}
    for bucket in LABELS:
        predicted = sum(n for (_, p), n in confusion.items() if p == bucket)
        actual = sum(n for (a, _), n in confusion.items() if a == bucket)
        correct = confusion[(bucket, bucket)]
        per_bucket[bucket] = {
            "precision": correct / predicted if predicted else None,
            "recall": correct / actual if actual else None,
            "labeled": actual,
        }

    correct = sum(n for (a, p), n in confusion.items() if a == p)
    return {
        "labeled": len(pairs),
        "agreement": correct / len(pairs) if pairs else None,
        "per_bucket": per_bucket,
        "confusion": confusion,
        "wrong_by_check": wrong_by_check,
    }


def wilson_lower_bound(agree, total, z=1.96):
    """the agreement rate we can be 95% sure of, given how few commits were checked"""
    if not total:
        return 0.0
    p = agree / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (centre - margin) / (1 + z * z / total)


def load_shadow_log(path):
    """every logged AI decision, keeping only the latest entry per commit"""
    entries = {}
    with open(path) as f:
        for line in f:
            if line.strip():
                entry = json.loads(line)
                entries[entry["sha"]] = entry
    return list(entries.values())


def shadow_report(entries):
    """how often the AI agreed with the shadow rule, and whether that's enough to trust the rule"""
    handled = [e for e in entries if e["shadow_rule"] == "meaningful"]
    agree = sum(e["ai_bucket"] == "meaningful" for e in handled)
    people = {e["username"] for e in handled}
    rate = agree / len(handled) if handled else None
    missing = []
    if len(handled) < SHADOW_MIN_COMMITS:
        missing.append(f"{SHADOW_MIN_COMMITS - len(handled)} more commits the rule handles")
    if rate is None or rate < SHADOW_MIN_AGREEMENT:
        missing.append(f"agreement of at least {SHADOW_MIN_AGREEMENT:.0%}")
    if len(people) < SHADOW_MIN_PEOPLE:
        missing.append(f"{SHADOW_MIN_PEOPLE - len(people)} more people")
    return {
        "ai_decisions": len(entries),
        "handled": len(handled),
        "agree": agree,
        "rate": rate,
        "lower_bound": wilson_lower_bound(agree, len(handled)),
        "people": len(people),
        "repos": len({e["repo"] for e in handled}),
        "disagreements": [e for e in handled if e["ai_bucket"] != "meaningful"],
        "missing": missing,
    }


def pct(value):
    return "—" if value is None else f"{value:.0%}"


def sample_command(args):
    if os.path.exists(args.labels) and not args.force:
        raise SystemExit(f"{args.labels} already exists — it may hold your labels. Use --force to overwrite it.")
    records = load_commit_records(args.results)
    picked = stratified_sample(records, args.n)
    with open(args.labels, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for r in picked:
            writer.writerow({"sha": r["sha"], "repo": r["repo"], "pr": r["pr"],
                             "url": r["url"], "message": r["message"], "label": ""})
    print(f"Wrote {len(picked)} commits to {args.labels}.")
    print(f"Fill in the label column with one of: {', '.join(LABELS)}")


def score_command(args):
    with open(args.labels, newline="") as f:
        rows = [row for row in csv.DictReader(f) if row["label"].strip()]
    bad = [row for row in rows if row["label"].strip() not in LABELS]
    if bad:
        raise SystemExit(f"Unknown label '{bad[0]['label']}' for {bad[0]['sha'][:7]} — use one of: {', '.join(LABELS)}")
    labels = {row["sha"]: row["label"].strip() for row in rows}

    result = agreement(labels, load_commit_records(args.results))
    if not result["labeled"]:
        raise SystemExit("No labeled commits match the results file — label some rows first.")

    print(f"Labeled commits: {result['labeled']}")
    print(f"Agreement with the pipeline: {pct(result['agreement'])}")

    print(f"\n  {'bucket':<20} {'labeled':>8} {'precision':>10} {'recall':>8}")
    for bucket, m in result["per_bucket"].items():
        print(f"  {bucket:<20} {m['labeled']:>8} {pct(m['precision']):>10} {pct(m['recall']):>8}")
    print("  precision: when the pipeline says this bucket, how often you agree")
    print("  recall: of the commits you put in this bucket, how many the pipeline found")

    mistakes = {k: n for k, n in result["confusion"].items() if k[0] != k[1]}
    if mistakes:
        print(f"\nDisagreements (your label -> pipeline's bucket):")
        for (label, bucket), n in sorted(mistakes.items(), key=lambda kv: -kv[1]):
            print(f"  {label} -> {bucket}: {n}")
        print(f"\nWhich check made the calls you disagree with:")
        for check, n in result["wrong_by_check"].most_common():
            print(f"  {check}: {n}")


def shadow_command(args):
    if not os.path.exists(args.shadow_log):
        raise SystemExit(f"No {args.shadow_log} yet — it fills up with each run that uses the AI.")
    r = shadow_report(load_shadow_log(args.shadow_log))
    print(f"AI decisions logged: {r['ai_decisions']}")
    print(f"The shadow rule would have handled: {r['handled']} "
          f"({pct(r['handled'] / r['ai_decisions'] if r['ai_decisions'] else None)} of AI calls saved)")
    print(f"AI agreed it was meaningful: {r['agree']} of {r['handled']} ({pct(r['rate'])}), "
          f"at least {pct(r['lower_bound'])} with 95% confidence")
    print(f"Across {r['people']} people and {r['repos']} repos")

    if r["missing"]:
        print(f"\nNot ready to replace the AI. Still needs: {'; '.join(r['missing'])}.")
    else:
        print(f"\nReady: hand-label the disagreements below, and if the rule is right about as often "
              f"as the AI on them, it can replace those AI calls.")

    if r["disagreements"]:
        print(f"\nDisagreements to hand-label (the rule said meaningful):")
        for e in r["disagreements"]:
            print(f"  AI said {e['ai_bucket']}: {e['url']}")
            print(f"    {e['message'][:70]} — {e['ai_reason']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check the pipeline's buckets against hand labels.")
    parser.add_argument("--results", default=RESULTS_FILE)
    parser.add_argument("--labels", default=LABELS_FILE)
    parser.add_argument("--shadow-log", default=SHADOW_LOG_FILE)
    sub = parser.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("sample", help="pick commits to label")
    sample.add_argument("--n", type=int, default=100, help="how many commits (default 100)")
    sample.add_argument("--force", action="store_true", help="overwrite an existing labels file")
    sub.add_parser("score", help="compare your labels with the pipeline")
    sub.add_parser("shadow", help="is the shadow rule ready to replace AI calls?")
    args = parser.parse_args()
    {"sample": sample_command, "score": score_command, "shadow": shadow_command}[args.command](args)
