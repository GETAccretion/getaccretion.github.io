import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import telegram

TODAY = dt.date(2026, 10, 22)
YESTERDAY = "2026-10-21"


def app(bid, name, version="1.0", **extra):
    return {
        "bundleIdentifier": bid,
        "name": name,
        "version": version,
        "iconURL": f"https://getaccretion.app/icons/{bid}.jpg",
        "versions": [{"version": version, "localizedDescription": "Fixes."}],
        **extra,
    }


OWN = app("io.github.getaccretion.Accretion", "Accretion", "1.0.40", subtitle="The store")
MOD = app("io.github.accretionmods.swiftradioplus", "Swift Radio+", "2.0")
FREE = app("com.example.reader", "Reader", subtitle="Read <books>")
CRACKED = app("com.example.cracked", "Paid Game Cracked")
UNLOCKED = app("com.example.photo", "Photo Studio")
OLD = app("com.example.old", "Old App")
APPS = [OWN, MOD, FREE, CRACKED, UNLOCKED, OLD]
FIRST_SEEN = {a["bundleIdentifier"]: YESTERDAY for a in APPS} | {"com.example.old": "2026-10-10"}
LISTINGS = {"com.example.photo": {"features": ["Unlocked"], "tags": ["photo"]}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.write("source.json", {"apps": APPS})
        self.write("first_seen.json", FIRST_SEEN)
        self.write("ai/listings.json", LISTINGS)
        patcher = mock.patch("common.ROOT", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sent = []
        sender = mock.patch.object(telegram, "send", side_effect=self.fake_send)
        sender.start()
        self.addCleanup(sender.stop)
        uploader = mock.patch.object(telegram, "send_file", side_effect=self.fake_send_file)
        uploader.start()
        self.addCleanup(uploader.stop)
        self.card = None  # no animated card unless a test sets one
        maker = mock.patch.object(telegram, "card", side_effect=lambda *a: self.card)
        maker.start()
        self.addCleanup(maker.stop)
        self.ok = True

    def fake_send_file(self, token, method, fields, field, filename, data, mime):
        self.sent.append((method, {**fields, field: data}))
        return self.ok

    def fake_send(self, token, method, payload):
        self.sent.append((method, payload))
        return self.ok

    def write(self, name, data):
        os.makedirs(os.path.dirname(os.path.join(self.dir.name, name)), exist_ok=True)
        with open(os.path.join(self.dir.name, name), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def state(self):
        with open(os.path.join(self.dir.name, telegram.STATE), encoding="utf-8") as f:
            return json.load(f)


class FirstRun(Fixture):
    def test_records_current_versions_and_posts_nothing(self):
        telegram.run(TODAY, "token", "@chan", dry_run=False)
        self.assertEqual(self.sent, [])
        state = self.state()
        self.assertEqual(state["posted"], {OWN["bundleIdentifier"]: "1.0.40", MOD["bundleIdentifier"]: "2.0"})
        self.assertEqual(state["digest"], YESTERDAY)


class Posting(Fixture):
    def setUp(self):
        super().setUp()
        self.write(
            telegram.STATE,
            {
                "posted": {OWN["bundleIdentifier"]: "1.0.39", MOD["bundleIdentifier"]: "2.0"},
                "digest": "2026-10-20",
            },
        )

    def test_posts_new_release_and_digest(self):
        telegram.run(TODAY, "token", "@chan", dry_run=False)
        methods = [m for m, _ in self.sent]
        self.assertEqual(methods, ["sendPhoto", "sendMessage"])
        self.assertIn("Accretion 1.0.40", self.sent[0][1]["caption"])
        self.assertIn("/app/io.github.getaccretion.Accretion/", self.sent[0][1]["caption"])
        text = self.sent[1][1]["text"]
        self.assertIn("Reader", text)
        self.assertIn("Read &lt;books&gt;", text)
        for left_out in ("Cracked", "Photo Studio", "Old App", "Swift Radio"):
            self.assertNotIn(left_out, text)
        state = self.state()
        self.assertEqual(state["posted"][OWN["bundleIdentifier"]], "1.0.40")
        self.assertEqual(state["digest"], YESTERDAY)

    def test_second_run_posts_nothing(self):
        telegram.run(TODAY, "token", "@chan", dry_run=False)
        self.sent.clear()
        telegram.run(TODAY, "token", "@chan", dry_run=False)
        self.assertEqual(self.sent, [])

    def test_failed_posts_are_retried_later(self):
        self.ok = False
        telegram.run(TODAY, "token", "@chan", dry_run=False)
        state = self.state()
        self.assertEqual(state["posted"][OWN["bundleIdentifier"]], "1.0.39")
        self.assertEqual(state["digest"], "2026-10-20")

    def test_without_token_sends_and_saves_nothing(self):
        telegram.run(TODAY, None, None, dry_run=False)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state()["posted"][OWN["bundleIdentifier"]], "1.0.39")

    def test_digest_is_capped(self):
        many = [app(f"com.example.app{i:02d}", f"App {i:02d}") for i in range(14)]
        text = telegram.digest(many, {a["bundleIdentifier"]: YESTERDAY for a in many}, {}, set(), YESTERDAY)
        self.assertEqual(text.count("•"), telegram.DIGEST_LIMIT)
        self.assertIn("And 4 more", text)

    def test_nsfw_icons_are_left_out(self):
        self.assertFalse(telegram.safe_for_digest(FREE, {}, {"com.example.reader"}))


WANDERER_TEXT = (
    "The Wanderer with the Accretion mod menu built in.\n\n"
    "Work on any phone, no JIT needed:\n• Health never drops\n• Lockpicks never break\n\n"
    "With JIT:\n• One-hit kills\n\nInstalls next to the original game."
)


class Cheats(unittest.TestCase):
    def mod(self, text):
        return app(
            "io.github.accretionmods.thewandererplus",
            "The Wanderer+",
            "10.1-mod1.0.7",
            localizedDescription=text,
            category="games",
        )

    def test_cheats_grouped_under_their_headings(self):
        self.assertEqual(
            telegram.cheat_list(self.mod(WANDERER_TEXT)),
            [
                ("Work on any phone, no JIT needed", ["Health never drops", "Lockpicks never break"]),
                ("With JIT", ["One-hit kills"]),
            ],
        )

    def test_caption_lists_the_cheats(self):
        caption = telegram.release_caption(self.mod(WANDERER_TEXT))
        self.assertIn("<b>Cheats (3)</b>", caption)
        self.assertIn("<i>With JIT</i>\n• One-hit kills", caption)
        self.assertNotIn("Installs next", caption)

    def test_apps_that_are_not_mods_have_no_cheat_list(self):
        self.assertEqual(telegram.cheat_list(dict(FREE, localizedDescription=WANDERER_TEXT)), [])
        self.assertNotIn("Cheats", telegram.release_caption(dict(OWN, localizedDescription=WANDERER_TEXT)))

    def test_mods_that_are_not_games_list_a_mod_menu(self):
        caption = telegram.release_caption(dict(self.mod(WANDERER_TEXT), category="entertainment"))
        self.assertIn("<b>Mod menu (3)</b>", caption)

    def test_long_lists_are_cut_to_the_message_limit(self):
        many = "Cheats:\n" + "\n".join(f"• Cheat number {i} " + "x" * 60 for i in range(120))
        caption = telegram.release_caption(self.mod(many))
        self.assertLessEqual(len(caption), telegram.TEXT_LIMIT)
        self.assertIn("<b>Cheats (120)</b>", caption)
        self.assertRegex(caption, r"and \d+ more on the app page")


if __name__ == "__main__":
    unittest.main()


class AnimatedCards(Fixture):
    def setUp(self):
        super().setUp()
        self.write(
            telegram.STATE,
            {
                "posted": {OWN["bundleIdentifier"]: "1.0.39", MOD["bundleIdentifier"]: "2.0"},
                "digest": "2026-10-20",
            },
        )
        self.card = b"mp4"

    def test_release_and_digest_go_with_the_card(self):
        telegram.run(TODAY, "token", "@chan", dry_run=False)
        methods = [m for m, _ in self.sent]
        self.assertEqual(methods, ["sendAnimation", "sendAnimation", "sendMessage"])
        release = self.sent[0][1]
        self.assertEqual(release["animation"], b"mp4")
        self.assertIn("Accretion 1.0.40", release["caption"])
        self.assertIn("Reader", self.sent[2][1]["text"])
        self.assertEqual(self.state()["digest"], YESTERDAY)

    def test_without_a_card_the_release_goes_as_a_photo(self):
        self.card = None
        telegram.run(TODAY, "token", "@chan", dry_run=False)
        self.assertEqual([m for m, _ in self.sent], ["sendPhoto", "sendMessage"])


class CardHelpers(unittest.TestCase):
    def test_badge(self):
        self.assertEqual(telegram.release_badge(MOD), "ACCRETION MOD")
        self.assertEqual(telegram.release_badge(OWN), "NEW VERSION")

    def test_card_failure_is_not_fatal(self):
        def broken(*args):
            raise OSError("no ffmpeg")

        with mock.patch.object(telegram, "telegram_card", object()):
            self.assertIsNone(telegram.card(broken, OWN))

    def test_frames_render(self):
        try:
            import telegram_card
        except ImportError:
            self.skipTest("Pillow not installed")
        from PIL import Image

        icon = Image.new("RGBA", (64, 64), (200, 80, 40, 255))
        frames = telegram_card.release_frames(
            icon, "A very long app name that will not fit on the card", "1.2.3", "NEW"
        )
        first = next(frames)
        self.assertEqual(first.size, (telegram_card.W, telegram_card.H))
        digest = telegram_card.digest_frames([icon, None, icon], "New on Accretion", "3 new apps")
        self.assertEqual(next(digest).mode, "RGB")
