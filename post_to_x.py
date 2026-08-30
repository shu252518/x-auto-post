"""Post one randomly selected line from posts.txt to X API v2."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import requests
from requests_oauthlib import OAuth1


API_URL = "https://api.x.com/2/tweets"
BASE_DIR = Path(__file__).resolve().parent
POSTS_FILE = BASE_DIR / "posts.txt"
STATE_FILE = BASE_DIR / ".state" / "last_post.sha256"
HISTORY_FILE = BASE_DIR / ".state" / "post_history.json"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
REQUIRED_SECRETS = (
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
)
REQUEST_TIMEOUT_SECONDS = 30
AI_MODEL = "gemini-2.5-flash-lite"
LOG = logging.getLogger("x_auto_post")


class JSTFormatter(logging.Formatter):
    """Render log timestamps in Asia/Tokyo regardless of runner locale."""

    _timezone = ZoneInfo("Asia/Tokyo")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        value = datetime.fromtimestamp(record.created, tz=self._timezone)
        return value.strftime(datefmt) if datefmt else value.isoformat(timespec="seconds")


class AutoPostError(RuntimeError):
    """An expected, user-actionable auto-posting failure."""


@dataclass(frozen=True)
class SelectedPost:
    text: str
    line_number: int
    digest: str


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSTFormatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.handlers.clear()
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False


def post_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_posts(path: Path) -> list[SelectedPost]:
    try:
        raw_lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError as exc:
        raise AutoPostError(f"投稿ファイルが見つかりません: {path}") from exc
    except (OSError, UnicodeError) as exc:
        raise AutoPostError(f"投稿ファイルを読み込めません: {path} ({exc})") from exc

    posts: list[SelectedPost] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(raw_lines, start=1):
        text = raw_line.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        posts.append(SelectedPost(text, line_number, post_digest(text)))

    if not posts:
        raise AutoPostError(
            f"{path} に投稿可能な文がありません。空行ではない投稿文を追加してください。"
        )
    return posts


def load_last_digest(path: Path) -> str | None:
    try:
        digest = path.read_text(encoding="ascii").strip().lower()
    except FileNotFoundError:
        LOG.info("前回投稿の状態はありません（初回実行またはキャッシュ未復元）。")
        return None
    except (OSError, UnicodeError) as exc:
        raise AutoPostError(f"前回投稿の状態を読み込めません: {path} ({exc})") from exc

    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise AutoPostError(f"前回投稿の状態ファイルが不正です: {path}")
    return digest


def select_post(posts: list[SelectedPost], last_digest: str | None) -> SelectedPost:
    candidates = [post for post in posts if post.digest != last_digest]
    if not candidates:
        raise AutoPostError(
            "連続投稿できる別の投稿文がありません。posts.txt に異なる投稿文を2件以上追加してください。"
        )
    return secrets.choice(candidates)


def load_credentials(environment: Mapping[str, str]) -> dict[str, str]:
    missing = [name for name in REQUIRED_SECRETS if not environment.get(name, "").strip()]
    if missing:
        raise AutoPostError(
            "必要な認証情報が設定されていません: " + ", ".join(missing)
        )
    return {name: environment[name] for name in REQUIRED_SECRETS}


def redact(text: str, credentials: Mapping[str, str]) -> str:
    redacted = text
    for value in credentials.values():
        if value:
            redacted = redacted.replace(value, "***")
    return redacted


def response_detail(response: requests.Response, credentials: Mapping[str, str]) -> str:
    try:
        body = json.dumps(response.json(), ensure_ascii=False)
    except (ValueError, requests.exceptions.JSONDecodeError):
        body = response.text or "<empty response>"

    body = redact(body, credentials)
    if len(body) > 4000:
        body = body[:4000] + "...<truncated>"

    details = [f"HTTP {response.status_code}", f"response={body}"]
    for header in ("x-request-id", "x-rate-limit-limit", "x-rate-limit-remaining"):
        if value := response.headers.get(header):
            details.append(f"{header}={value}")

    if reset := response.headers.get("x-rate-limit-reset"):
        try:
            reset_at = datetime.fromtimestamp(int(reset), tz=timezone.utc).astimezone(
                ZoneInfo("Asia/Tokyo")
            )
            details.append(f"x-rate-limit-reset={reset_at.isoformat(timespec='seconds')}")
        except (ValueError, OSError):
            details.append(f"x-rate-limit-reset={reset}")
    return ", ".join(details)


def publish_post(
    selected: SelectedPost,
    credentials: Mapping[str, str],
    session: requests.Session | None = None,
) -> str:
    auth = OAuth1(
        credentials["X_API_KEY"],
        credentials["X_API_SECRET"],
        credentials["X_ACCESS_TOKEN"],
        credentials["X_ACCESS_TOKEN_SECRET"],
    )
    client = session or requests.Session()

    try:
        response = client.post(
            API_URL,
            json={"text": selected.text},
            auth=auth,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise AutoPostError(
            "X APIへの接続に失敗しました: " + redact(str(exc), credentials)
        ) from exc

    if response.status_code != 201:
        raise AutoPostError("X APIが投稿を拒否しました: " + response_detail(response, credentials))

    try:
        payload = response.json()
        post_id = payload["data"]["id"]
    except (ValueError, KeyError, TypeError) as exc:
        raise AutoPostError(
            "X APIの成功応答に投稿IDがありません: " + response_detail(response, credentials)
        ) from exc

    return str(post_id)


def save_last_digest(path: Path, digest: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        temporary_path.write_text(digest + "\n", encoding="ascii")
        temporary_path.replace(path)
    except OSError as exc:
        raise AutoPostError(
            "投稿は成功しましたが、重複防止の状態を保存できませんでした。"
            f"次回実行前に確認してください: {path} ({exc})"
        ) from exc


def posting_period(hour: int) -> str:
    if 5 <= hour < 11:
        return "朝"
    if 11 <= hour < 17:
        return "昼"
    return "夜"


def load_history(path: Path, limit: int = 30) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [str(item) for item in data if isinstance(item, str)][-limit:]
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return []


def save_history(path: Path, text: str, limit: int = 30) -> None:
    history = load_history(path, limit)
    history.append(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history[-limit:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def too_similar(candidate: str, history: list[str], threshold: float = 0.78) -> bool:
    normalized = set(candidate.replace(" ", ""))
    if not normalized:
        return True
    for old in history:
        if candidate == old:
            return True
        other = set(old.replace(" ", ""))
        score = len(normalized & other) / max(len(normalized | other), 1)
        if score >= threshold:
            return True
    return False


def generate_ai_post(period: str, api_key: str, history: list[str], session: requests.Session | None = None) -> str:
    prompt = (f"{period}向けの自然な日本語の短文を1件だけ作成してください。"
              "30〜100文字、宣伝なし、ハッシュタグなし、絵文字は最大1個。"
              "政治・宗教・差別・攻撃・ニュース・日付曜日・医療投資の断定は禁止。"
              "本文だけを返し、引用符や説明は付けないでください。")
    client = session or requests.Session()
    try:
        url = f"{GEMINI_API_BASE}/{AI_MODEL}:generateContent"
        response = client.post(url, params={"key": api_key}, headers={"Content-Type": "application/json"},
                               json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.8, "maxOutputTokens": 120}}, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise AutoPostError(f"Gemini APIエラー: HTTP {response.status_code}")
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
        if not text or not (30 <= len(text) <= 100) or too_similar(text, history):
            raise AutoPostError("AI生成文が条件不適合または履歴と類似しています")
        return text
    except requests.RequestException as exc:
        raise AutoPostError(f"AI APIへの接続に失敗しました: {type(exc).__name__}") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise AutoPostError("AI API応答の形式が不正です") from exc


def choose_post(environment: Mapping[str, str], now: datetime, session: requests.Session | None = None) -> tuple[str, bool]:
    posts = load_posts(POSTS_FILE)
    history = load_history(HISTORY_FILE)
    dry_run = environment.get("AI_DRY_RUN", "true").strip().lower() != "false"
    api_key = environment.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        LOG.warning("GEMINI_API_KEYが未設定のため、固定文へフォールバックします")
        last = post_digest(history[-1]) if history else None
        return select_post(posts, last).text, dry_run
    period = posting_period(now.hour)
    for attempt in range(3):
        try:
            return generate_ai_post(period, api_key, history, session), dry_run
        except AutoPostError as exc:
            LOG.warning("AI生成%d回目に失敗: %s", attempt + 1, exc)
    LOG.warning("AI生成を3回試行したため、固定文へフォールバックします")
    last = post_digest(history[-1]) if history else None
    return select_post(posts, last).text, dry_run


def main() -> int:
    configure_logging()
    try:
        credentials = load_credentials(os.environ)
        generated, dry_run = choose_post(os.environ, datetime.now(ZoneInfo("Asia/Tokyo")))
        selected = SelectedPost(generated, 0, post_digest(generated))
        LOG.info("最終候補: characters=%d, sha256=%s", len(generated), selected.digest[:12])
        if dry_run:
            LOG.info("AI_DRY_RUN=true のためX投稿をスキップしました")
            return 0
        post_id = publish_post(selected, credentials)
        save_last_digest(STATE_FILE, selected.digest)
        save_history(HISTORY_FILE, generated)
        LOG.info("投稿に成功しました: post_id=%s", post_id)
        return 0
    except AutoPostError as exc:
        LOG.error("投稿処理に失敗しました: %s", exc)
        return 1
    except Exception:
        LOG.exception("予期しないエラーで投稿処理に失敗しました。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
