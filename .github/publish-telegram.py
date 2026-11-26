import base64
import html
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.etree import ElementTree


CHANNEL = "shelknulo27"
SITE = "https://venomonar.github.io"
ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"
COVER_TAG = '<img src="tg://photo?id=cover"/>'


class TelegramRejected(RuntimeError):
    pass


def telegram(method, fields, cover=None):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    headers = {"Content-Type": "application/json"}
    data = json.dumps(fields).encode("utf-8")
    if cover is not None:
        boundary = uuid4().hex
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        parts = []
        for name, value in fields.items():
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                .encode("ascii") + value.encode("utf-8") + b"\r\n"
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="cover"; filename="cover.jpg"'
            '\r\nContent-Type: image/jpeg\r\n\r\n'.encode("ascii") + cover + b"\r\n"
        )
        data = b"".join(parts) + f"--{boundary}--\r\n".encode("ascii")
    request = Request(f"https://api.telegram.org/bot{token}/{method}", data, headers)
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            result = json.load(error)
        except (OSError, ValueError):
            raise RuntimeError(f"Telegram {method} returned unreadable HTTP {error.code}.") from None
    except (OSError, URLError, ValueError):
        raise RuntimeError(f"No usable response from Telegram {method}.") from None
    if result.get("ok") is False and 400 <= result.get("error_code", 0) < 500:
        raise TelegramRejected(f"Telegram rejected {method} (HTTP {result['error_code']}).")
    if result.get("ok") is not True or "result" not in result:
        raise RuntimeError(f"Telegram did not confirm {method}.")
    return result["result"]


def queued_file(article, path):
    object_sha = subprocess.check_output(
        ["git", "rev-parse", f"{article['snapshot']}:{path}"], text=True
    ).strip()
    if object_sha != article["files"][path]:
        raise RuntimeError(f"Queued file differs from the approved snapshot: {path}")
    return subprocess.check_output(["git", "cat-file", "blob", object_sha])


def prepare(article):
    slug = article["slug"]
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise RuntimeError("Invalid queued article slug.")
    rss = ElementTree.fromstring(queued_file(article, "rss.xml"))
    link = f"{SITE}/articles/{slug}/"
    item = next(item for item in rss.findall("./channel/item") if item.findtext("link") == link)
    title = item.findtext("title", "")
    content = item.findtext(ENCODED, "")
    image_pattern = rf'<figure><img src="{re.escape(SITE)}/assets/{slug}\.jpg"[^>]*></figure>'
    content, image_count = re.subn(image_pattern, COVER_TAG, content)
    if not title or image_count != 1 or f"https://t.me/{CHANNEL}" not in content:
        raise RuntimeError(f"Invalid scheduled content for {slug}.")
    cover = queued_file(article, f"assets/{slug}.jpg")
    if not cover.startswith(b"\xff\xd8") or len(cover) > 10 * 1024 * 1024:
        raise RuntimeError(f"Invalid cover for {slug}.")
    return title, content, cover


def state_api(method, path, fields=None):
    args = ["gh", "api", "--method", method, path]
    if fields is not None:
        args += ["--input", "-"]
    result = subprocess.run(args, input=None if fields is None else json.dumps(fields),
                            text=True, capture_output=True)
    if result.returncode:
        if method == "GET" and "HTTP 404" in result.stderr:
            return None
        raise RuntimeError(f"Could not {method} Telegram publication state: {result.stderr.strip()}")
    return json.loads(result.stdout)


def save_state(path, branch, slug, state, previous_sha=None):
    content = base64.b64encode(json.dumps(state, ensure_ascii=False).encode("utf-8")).decode("ascii")
    fields = {"message": f"Record Telegram publication for {slug}", "content": content, "branch": branch}
    if previous_sha:
        fields["sha"] = previous_sha
    return state_api("PUT", path, fields)["content"]["sha"]


def publish(channel, branch, article, title, content, cover):
    slug = article["slug"]
    path = f"repos/{os.environ['GITHUB_REPOSITORY']}/contents/.github/telegram-publications/{slug}.json"
    previous = state_api("GET", f"{path}?ref={quote(branch, safe='')}")
    if previous:
        state = json.loads(base64.b64decode(previous["content"]))
        if state.get("url") and state.get("snapshot") == article["snapshot"]:
            if not state.get("format_verified"):
                raise RuntimeError(f"Published post needs a format check: {state['url']}")
            print(f"Already published {slug}: {state['url']}")
            return
        raise RuntimeError(f"A previous send for {slug} has no confirmed result. Check the channel before retrying.")

    state = {"scheduled_for": article["at"], "snapshot": article["snapshot"]}
    claim_sha = save_state(path, branch, slug, state)
    try:
        message = telegram("sendRichMessage", {
            "chat_id": str(channel["id"]),
            "rich_message": {
                "html": f"<h1>{html.escape(title)}</h1>\n{content}",
                "skip_entity_detection": True,
                "media": [{"id": "cover", "media": {"type": "photo", "media": "attach://cover"}}],
            },
        }, cover)
    except TelegramRejected:
        state_api("DELETE", path, {
            "message": f"Clear rejected Telegram publication for {slug}", "sha": claim_sha, "branch": branch,
        })
        raise

    if message.get("chat", {}).get("id") != channel["id"] or message.get("message_id", 0) <= 0:
        raise RuntimeError("Telegram did not return a post in the approved channel.")
    state["url"] = f"https://t.me/{CHANNEL}/{message['message_id']}"
    state["published_at"] = datetime.fromtimestamp(message["date"], timezone.utc).isoformat()
    blocks = message.get("rich_message", {}).get("blocks", [])
    returned_content = json.dumps(blocks, ensure_ascii=False)
    photo_positions = [index for index, block in enumerate(blocks) if block.get("type") == "photo"]
    state["format_verified"] = (
        title in returned_content and f"https://t.me/{CHANNEL}" in returned_content
        and photo_positions == [cover_block_index(content)]
    )
    save_state(path, branch, slug, state, claim_sha)
    if not state["format_verified"]:
        raise RuntimeError(f"Published post needs a format check: {state['url']}")
    print(f"Published {slug}: {state['url']}")


def cover_block_index(content):
    before_cover, _, _ = content.partition(COVER_TAG)
    # Telegram's title is a separate first block.
    return 1 + len(re.findall(r"<(?:p|h[1-6]|ul|ol|blockquote)\b", before_cover))


def due_articles(articles, now):
    return [article for article in articles if datetime.fromisoformat(article["at"]) <= now]


def main():
    queue = json.loads(Path(".github/site-publications.json").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    event = os.environ["GITHUB_EVENT_NAME"]
    slots = [(item, datetime.fromisoformat(item["at"])) for item in queue["articles"]]
    due = due_articles(queue["articles"], now)
    if event == "schedule" and not due:
        return
    subprocess.run(["git", "fetch", "--no-tags", "origin", f"refs/heads/{queue['branch']}"], check=True)
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("GitHub Actions secret TELEGRAM_BOT_TOKEN is missing.")
    bot = telegram("getMe", {})
    channel = telegram("getChat", {"chat_id": f"@{CHANNEL}"})
    member = telegram("getChatMember", {"chat_id": channel["id"], "user_id": bot["id"]})
    if channel.get("username") != CHANNEL or channel.get("type") != "channel" or not member.get("can_post_messages"):
        raise RuntimeError("Bot cannot publish to the approved Telegram channel.")

    failures = []
    for article, at in slots:
        if event in {"schedule", "workflow_dispatch"} and article in due:
            try:
                publish(channel, queue["branch"], article, *prepare(article))
            except RuntimeError as error:
                failures.append(f"{article['slug']}: {error}")
        elif event != "schedule":
            title, content, _ = prepare(article)
            print(f"Validated {at.isoformat()} {title}; cover block {cover_block_index(content)}")

    if failures:
        raise RuntimeError("\n".join(failures))


if __name__ == "__main__":
    main()
