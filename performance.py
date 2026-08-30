"""Refresh lightweight X post metrics and summarize winning patterns."""
from __future__ import annotations
import json, os
from collections import Counter
from datetime import datetime
from pathlib import Path
import requests
from requests_oauthlib import OAuth1

BASE = Path(__file__).resolve().parent
HISTORY = BASE / ".state" / "post_history.json"
SUMMARY = BASE / ".state" / "performance_summary.json"
URL = "https://api.x.com/2/tweets"

def score(metrics: dict) -> float:
    return (metrics.get("reply_count", 0)*4 + metrics.get("retweet_count", 0)*4 + metrics.get("quote_count", 0)*5 + metrics.get("like_count", 0) + metrics.get("bookmark_count", 0)*3)

def fetch_metrics(records, env, session=None):
    client = session or requests.Session()
    auth = OAuth1(env["X_API_KEY"], env["X_API_SECRET"], env["X_ACCESS_TOKEN"], env["X_ACCESS_TOKEN_SECRET"])
    ok = failed = 0
    for record in records:
        try:
            response = client.get(f"{URL}/{record['post_id']}", params={"tweet.fields": "created_at,public_metrics,non_public_metrics"}, auth=auth, timeout=30)
            if response.status_code != 200:
                failed += 1; continue
            data = response.json().get("data", {})
            metrics = {**data.get("public_metrics", {}), **data.get("non_public_metrics", {})}
            record["metrics"] = metrics
            record["engagement_score"] = score(metrics)
            impressions = metrics.get("impression_count")
            record["engagement_rate"] = score(metrics) / impressions if impressions else None
            ok += 1
        except (requests.RequestException, ValueError, KeyError, TypeError):
            failed += 1
    return records, ok, failed

def summarize(records):
    measured = [r for r in records if isinstance(r, dict) and "engagement_score" in r]
    top = sorted(measured, key=lambda r: r.get("engagement_score", 0), reverse=True)[:max(1, min(20, len(measured)))]
    def pick(key): return Counter(r.get(key) for r in top if r.get(key)).most_common(3)
    lengths = [r.get("characters", 0) for r in top if r.get("characters")]
    return {"sample_size": len(measured), "top_theme": pick("theme"), "top_period": pick("period"), "top_lengths": lengths, "top_cta": pick("cta"), "top_question": pick("question"), "average_engagement": sum(r.get("engagement_score", 0) for r in measured) / len(measured) if measured else 0, "updated_at": datetime.now().astimezone().isoformat()}

def main():
    try:
        records = json.loads(HISTORY.read_text(encoding="utf-8"))
        if not isinstance(records, list): return 0
        required = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            print(f"Metrics skipped: missing credentials {', '.join(missing)}"); return 0
        records, ok, failed = fetch_metrics(records[-300:], os.environ)
        print(f"Metrics fetched: success={ok} failed={failed}")
        HISTORY.parent.mkdir(parents=True, exist_ok=True); HISTORY.write_text(json.dumps(records[-300:], ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        summary = summarize(records); SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(f"Top themes={summary['top_theme']} periods={summary['top_period']} average_engagement={summary['average_engagement']:.2f}")
    except Exception as exc:
        print(f"Performance update skipped: {type(exc).__name__}: {exc}")
    return 0
if __name__ == "__main__": raise SystemExit(main())
