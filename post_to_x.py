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
USERS_ME_URL = "https://api.x.com/2/users/me"
BASE_DIR = Path(__file__).resolve().parent
POSTS_FILE = BASE_DIR / "posts.txt"
STATE_FILE = BASE_DIR / ".state" / "last_post.sha256"
HISTORY_FILE = BASE_DIR / ".state" / "post_history.json"
PERFORMANCE_SUMMARY_FILE = BASE_DIR / ".state" / "performance_summary.json"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
REQUIRED_SECRETS = (
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
)
REQUEST_TIMEOUT_SECONDS = 30
AI_MODEL = "gemini-3.1-flash-lite"
THEMES = ("貞観政要", "論語", "孫子", "韓非子", "菜根譚", "老子", "荘子", "孟子", "大学", "中庸", "君主論", "自省録")
LAST_GENERATION_META: dict[str, object] = {}
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


def diagnose_x_auth(credentials: Mapping[str, str], session: requests.Session | None = None) -> bool:
    auth = OAuth1(credentials["X_API_KEY"], credentials["X_API_SECRET"], credentials["X_ACCESS_TOKEN"], credentials["X_ACCESS_TOKEN_SECRET"])
    client = session or requests.Session()
    try:
        response = client.get(USERS_ME_URL, auth=auth, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        LOG.error("X認証診断の接続に失敗しました: %s", redact(str(exc), credentials)); return False
    try: payload = response.json()
    except (ValueError, TypeError): payload = {}
    if response.status_code == 200:
        data = payload.get("data", {})
        LOG.info("X認証診断: HTTP 200, user_id=%s, username=%s", data.get("id", "不明"), data.get("username", "不明"))
        LOG.info("OAuth認証は有効です。POST /2/tweetsだけ403の場合、書き込み権限・Xアカウント制限・App権限を確認してください。")
        return True
    error = payload.get("errors", [{}])[0] if isinstance(payload.get("errors"), list) else payload.get("error", {})
    LOG.error("X認証診断: HTTP %s, title=%s, detail=%s, type=%s", response.status_code, error.get("title", "不明"), error.get("detail", "不明"), error.get("type", "不明"))
    if response.status_code == 401: LOG.error("OAuth認証情報が無効または不一致です。4つのSecretsを再確認してください。")
    elif response.status_code == 403: LOG.error("認証は認識されているが、この操作へのアクセスが拒否されています。App権限またはアカウント制限を確認してください。")
    return False


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


def load_history(path: Path, limit: int = 100) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [item.get("text", "") if isinstance(item, dict) else str(item) for item in data if isinstance(item, str) or isinstance(item, dict)][-limit:]
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return []


def save_history(path: Path, text: str, limit: int = 100) -> None:
    history = load_history(path, limit)
    history.append(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history[-limit:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_post_record(path: Path, record: Mapping[str, object], limit: int = 300) -> None:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        records = existing if isinstance(existing, list) else []
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        records = []
    records.append(dict(record))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records[-limit:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def select_theme(history: list[str]) -> str:
    recent = history[-3:]
    choices = [theme for theme in THEMES if not any(theme in text for text in recent)] or list(THEMES)
    return secrets.choice(choices)


def quality_score(text: str, period: str) -> int:
    hook = 30 if any(mark in text[:45] for mark in ("？", "?", "なら", "実は", "意外", "ほど", "強い", "危険")) else 18
    learning = 25 if (any(book in text for book in THEMES) or any(mark in text for mark in ("方法", "手順", "知恵", "教え"))) and len(text) >= 45 else 12
    application = 20 if any(mark in text for mark in ("仕事", "職場", "部下", "上司", "人間関係", "組織", "リーダー")) else 10
    specificity = 15 if any(char.isdigit() for char in text) or any(mark in text for mark in ("まず", "反対意見", "失敗", "勝つ")) else 8
    natural = 10 if "することが重要です" not in text and len(text) >= 40 else 5
    return min(100, hook + learning + application + specificity + natural)


def gemini_error_message(response: requests.Response, api_key: str) -> str:
    """Return only the safe API error message; never log the full response or key."""
    try:
        message = response.json().get("error", {}).get("message", "")
    except (ValueError, TypeError, AttributeError):
        message = ""
    message = redact(str(message), {"GEMINI_API_KEY": api_key}).replace("\n", " ").strip()
    return message[:500] if message else "詳細メッセージなし"


def generate_ai_post(period: str, api_key: str, history: list[str], session: requests.Session | None = None, theme: str | None = None, performance_summary: Mapping[str, object] | None = None) -> str:
    theme = theme or select_theme(history)
    length = {"朝": "60〜120文字", "昼": "90〜180文字", "夜": "100〜220文字"}.get(period, "60〜120文字")
    prompt = (f"{period}向け、書名『{theme}』を軸に、古典の考え方を自分の言葉で要約し現代の仕事・人間関係・リーダーシップへつなげた投稿を1件だけ作成してください。"
              f"{length}、Hook→古典の学び→現代への応用（必要なら自然な問い）の構成。専門家ぶりすぎず、実際にXで人間が投稿する自然な日本語にしてください。"
              "宣伝なし、ハッシュタグなし、絵文字は最大1個。"
              "架空の原文引用・架空の出典・章名の断定・翻訳文の転載、政治・宗教勧誘・差別・攻撃・ニュース・日付曜日・医療投資の断定は禁止。"
              "中身のない挨拶、過度な煽り、架空の実績、実体験の偽装は禁止。書名は確実な場合だけ明記し、本文は要約・現代語化してください。"
              "本文だけを返し、引用符や説明は付けないでください。")
    if performance_summary:
        prompt += f"過去の高成績傾向（丸コピーや同じHookは禁止）: {json.dumps(performance_summary, ensure_ascii=False)}。参考にするのは生成の80%、残り20%は新しい表現を試してください。"
    client = session or requests.Session()
    try:
        url = f"{GEMINI_API_BASE}/{AI_MODEL}:generateContent"
        response = client.post(url, params={"key": api_key}, headers={"Content-Type": "application/json"},
                               json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.8, "maxOutputTokens": 120}}, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise AutoPostError(f"Gemini APIエラー: HTTP {response.status_code}, message={gemini_error_message(response, api_key)}")
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
        max_length = {"朝": 120, "昼": 180, "夜": 220}.get(period, 120)
        if not text or not (40 <= len(text) <= max_length) or too_similar(text, history):
            raise AutoPostError("AI生成文が条件不適合または履歴と類似しています")
        score = quality_score(text, period)
        if score < 75:
            raise AutoPostError(f"品質スコア不足: {score}")
        return text
    except requests.RequestException as exc:
        raise AutoPostError(f"AI APIへの接続に失敗しました: {type(exc).__name__}") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise AutoPostError("AI API応答の形式が不正です") from exc


def choose_post(environment: Mapping[str, str], now: datetime, session: requests.Session | None = None) -> tuple[str, bool]:
    posts = load_posts(POSTS_FILE)
    history = load_history(HISTORY_FILE)
    dry_run = environment.get("AI_DRY_RUN", "true").strip().lower() != "false"
    period = posting_period(now.hour)
    theme = select_theme(history)
    try:
        performance_summary = json.loads(PERFORMANCE_SUMMARY_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        performance_summary = None
    api_key = environment.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        LOG.warning("GEMINI_API_KEYが未設定のため、固定文へフォールバックします")
        last = post_digest(history[-1]) if history else None
        fallback = select_post(posts, last).text
        LAST_GENERATION_META.update(period=period, theme=theme, quality_score=None, source="fallback", duplicate_check="fallback")
        LOG.info("生成 metadata: period=%s theme=%s characters=%d quality_score=n/a duplicate_check=fallback source=fallback", period, theme, len(fallback))
        return fallback, dry_run
    for attempt in range(3):
        try:
            text = generate_ai_post(period, api_key, history, session, theme, performance_summary)
            mode = "exploitation" if secrets.randbelow(100) < 80 and performance_summary else "exploration"
            LAST_GENERATION_META.update(period=period, theme=theme, quality_score=quality_score(text, period), source="ai", duplicate_check="pass", mode=mode)
            LOG.info("生成 metadata: period=%s theme=%s characters=%d quality_score=%d duplicate_check=pass source=ai mode=%s", period, theme, len(text), quality_score(text, period), mode)
            return text, dry_run
        except AutoPostError as exc:
            LOG.warning("AI生成%d回目に失敗: %s", attempt + 1, exc)
    LOG.warning("AI生成を3回試行したため、固定文へフォールバックします")
    last = post_digest(history[-1]) if history else None
    fallback = select_post(posts, last).text
    LAST_GENERATION_META.update(period=period, theme=theme, quality_score=None, source="fallback", duplicate_check="fallback", mode="fallback")
    LOG.info("生成 metadata: period=%s theme=%s characters=%d quality_score=n/a duplicate_check=fallback source=fallback mode=fallback", period, theme, len(fallback))
    return fallback, dry_run


def main() -> int:
    configure_logging()
    try:
        credentials = load_credentials(os.environ)
        if os.environ.get("X_AUTH_DIAGNOSTIC", "false").strip().lower() == "true":
            return 0 if diagnose_x_auth(credentials) else 1
        generated, dry_run = choose_post(os.environ, datetime.now(ZoneInfo("Asia/Tokyo")))
        selected = SelectedPost(generated, 0, post_digest(generated))
        LOG.info("最終候補: characters=%d, sha256=%s", len(generated), selected.digest[:12])
        if dry_run:
            LOG.info("AI_DRY_RUN=true のためX投稿をスキップしました")
            return 0
        post_id = publish_post(selected, credentials)
        save_last_digest(STATE_FILE, selected.digest)
        record = {"post_id": post_id, "posted_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(), "text": generated, "characters": len(generated), **LAST_GENERATION_META}
        save_post_record(HISTORY_FILE, record)
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
