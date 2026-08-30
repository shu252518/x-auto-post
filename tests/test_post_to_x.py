import tempfile
import unittest
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import post_to_x


class PostSelectionTests(unittest.TestCase):
    def test_load_posts_ignores_empty_and_duplicate_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "posts.txt"
            path.write_text("first\n\nsecond\nfirst\n", encoding="utf-8")

            posts = post_to_x.load_posts(path)

        self.assertEqual([post.text for post in posts], ["first", "second"])
        self.assertEqual([post.line_number for post in posts], [1, 3])

    def test_select_post_excludes_previous_post(self):
        posts = [
            post_to_x.SelectedPost("first", 1, post_to_x.post_digest("first")),
            post_to_x.SelectedPost("second", 2, post_to_x.post_digest("second")),
        ]

        selected = post_to_x.select_post(posts, posts[0].digest)

        self.assertEqual(selected.text, "second")

    def test_select_post_fails_when_only_previous_post_exists(self):
        post = post_to_x.SelectedPost("only", 1, post_to_x.post_digest("only"))

        with self.assertRaisesRegex(post_to_x.AutoPostError, "2件以上"):
            post_to_x.select_post([post], post.digest)


class PublishTests(unittest.TestCase):
    credentials = {
        "X_API_KEY": "api-key",
        "X_API_SECRET": "api-secret",
        "X_ACCESS_TOKEN": "access-token",
        "X_ACCESS_TOKEN_SECRET": "access-secret",
    }
    selected = post_to_x.SelectedPost("hello", 1, post_to_x.post_digest("hello"))

    def test_publish_calls_v2_endpoint_and_returns_id(self):
        response = Mock(status_code=201, headers={})
        response.json.return_value = {"data": {"id": "123", "text": "hello"}}
        session = Mock()
        session.post.return_value = response

        post_id = post_to_x.publish_post(self.selected, self.credentials, session)

        self.assertEqual(post_id, "123")
        session.post.assert_called_once()
        _, kwargs = session.post.call_args
        self.assertEqual(session.post.call_args.args[0], "https://api.x.com/2/tweets")
        self.assertEqual(kwargs["json"], {"text": "hello"})
        self.assertEqual(kwargs["timeout"], 30)

    def test_publish_includes_api_error_detail(self):
        response = Mock(status_code=401, headers={})
        response.json.return_value = {"title": "Unauthorized", "detail": "Bad token"}
        session = Mock()
        session.post.return_value = response

        with self.assertRaisesRegex(post_to_x.AutoPostError, "HTTP 401"):
            post_to_x.publish_post(self.selected, self.credentials, session)

    def test_publish_wraps_network_error(self):
        session = Mock()
        session.post.side_effect = requests.Timeout("timed out")

        with self.assertRaisesRegex(post_to_x.AutoPostError, "接続に失敗"):
            post_to_x.publish_post(self.selected, self.credentials, session)


class StateTests(unittest.TestCase):
    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".state" / "last_post.sha256"
            digest = post_to_x.post_digest("hello")

            post_to_x.save_last_digest(path, digest)

            self.assertEqual(post_to_x.load_last_digest(path), digest)


class AiTests(unittest.TestCase):
    def test_posting_period(self):
        self.assertEqual(post_to_x.posting_period(8), "朝")
        self.assertEqual(post_to_x.posting_period(12), "昼")
        self.assertEqual(post_to_x.posting_period(20), "夜")

    def test_ai_generation_success(self):
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": "朝の光を感じながら、できることを一つずつ丁寧に始める時間を大切にします。"}]}}]}
        session = Mock(); session.post.return_value = response
        text = post_to_x.generate_ai_post("朝", "secret", [], session)
        self.assertTrue(text.startswith("朝の"))

    def test_ai_failure_falls_back_to_fixed(self):
        response = Mock(status_code=500)
        response.json.return_value = {"error": {"message": "model unavailable for key secret"}}
        session = Mock(); session.post.return_value = response
        with tempfile.TemporaryDirectory() as directory, patch.object(post_to_x, "POSTS_FILE", Path(directory) / "posts.txt"), patch.object(post_to_x, "HISTORY_FILE", Path(directory) / "history.json"):
            post_to_x.POSTS_FILE.write_text("固定の投稿文です。今日も無理せず過ごします。\n", encoding="utf-8")
            text, dry = post_to_x.choose_post({"GEMINI_API_KEY": "key", "AI_DRY_RUN": "true"}, datetime(2026, 1, 1, 8), session)
        self.assertEqual(text, "固定の投稿文です。今日も無理せず過ごします。")
        self.assertTrue(dry)

    def test_gemini_error_message_is_reported_without_key(self):
        response = Mock()
        response.json.return_value = {"error": {"message": "bad key secret"}}
        detail = post_to_x.gemini_error_message(response, "secret")
        self.assertEqual(detail, "bad key ***")

    def test_dry_run_does_not_publish(self):
        with patch.object(post_to_x, "choose_post", return_value=("生成された投稿文です。安全な内容で確認します。", True)), patch.object(post_to_x, "publish_post") as publish:
            with patch.dict(os.environ, {**PublishTests.credentials, "AI_DRY_RUN": "true"}, clear=True):
                self.assertEqual(post_to_x.main(), 0)
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
