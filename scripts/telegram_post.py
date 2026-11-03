"""Posts one message, typed by the owner, to the Telegram channel (telegram-post.yml).

For announcements the automatic posts don't cover (a call for testers, news): plain text, no formatting,
sent by the same bot as scripts/telegram.py. A run form has one line per field, so "\\n" in the text
becomes a new line. TELEGRAM_BOT_TOKEN (secret) and TELEGRAM_CHAT (variable) as for telegram.py; without
them, or with --dry-run, it prints the message and sends nothing. A refused post fails the run.
"""

import argparse
import os
import sys

from telegram import TEXT_LIMIT, send


def message(text):
    """The text as it will be posted: "\\n" turned into new lines, spaces trimmed"""
    return text.replace("\\n", "\n").strip()


def post(text, token, chat, dry_run=False):
    body = message(text)
    if not body:
        print("Nothing to post: the message is empty.")
        return 1
    if len(body) > TEXT_LIMIT:
        print(f"The message has {len(body)} characters; Telegram takes at most {TEXT_LIMIT}.")
        return 1
    if dry_run or not token or not chat:
        print("Dry run (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT not set, or --dry-run): nothing is sent.")
        print(body)
        return 0
    if not send(token, "sendMessage", {"chat_id": chat, "text": body, "disable_web_page_preview": True}):
        print("Telegram refused the post (see the reason above).")
        return 1
    print(f"Posted {len(body)} characters to the channel.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the message, send nothing")
    args = parser.parse_args()
    # the text comes in an environment variable, never on the command line, so a run form can't inject shell
    return post(
        os.environ.get("POST_TEXT", ""),
        os.environ.get("TELEGRAM_BOT_TOKEN"),
        os.environ.get("TELEGRAM_CHAT"),
        args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
