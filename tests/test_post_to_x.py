import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
