import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import telegram_post


class MessageTests(unittest.TestCase):
    def test_backslash_n_becomes_a_new_line(self):
        self.assertEqual(telegram_post.message("  Hello\\nWorld  "), "Hello\nWorld")

    def test_empty_message_is_refused(self):
        with mock.patch.object(telegram_post, "send") as send:
            self.assertEqual(telegram_post.post("  \\n ", "token", "@chat"), 1)
        send.assert_not_called()

    def test_too_long_message_is_refused(self):
        with mock.patch.object(telegram_post, "send") as send:
            self.assertEqual(telegram_post.post("x" * (telegram_post.TEXT_LIMIT + 1), "token", "@chat"), 1)
        send.assert_not_called()

    def test_dry_run_and_missing_settings_send_nothing(self):
        with mock.patch.object(telegram_post, "send") as send:
            self.assertEqual(telegram_post.post("Hi", "token", "@chat", dry_run=True), 0)
            self.assertEqual(telegram_post.post("Hi", None, "@chat"), 0)
            self.assertEqual(telegram_post.post("Hi", "token", ""), 0)
        send.assert_not_called()

    def test_sends_plain_text_to_the_channel(self):
        with mock.patch.object(telegram_post, "send", return_value=True) as send:
            self.assertEqual(telegram_post.post("Got a Duo?\\nTell us.", "token", "@chat"), 0)
        send.assert_called_once_with(
            "token",
            "sendMessage",
            {"chat_id": "@chat", "text": "Got a Duo?\nTell us.", "disable_web_page_preview": True},
        )

    def test_refused_post_fails(self):
        with mock.patch.object(telegram_post, "send", return_value=False):
            self.assertEqual(telegram_post.post("Hi", "token", "@chat"), 1)


if __name__ == "__main__":
    unittest.main()
