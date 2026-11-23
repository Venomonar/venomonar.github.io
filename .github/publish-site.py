import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree


SITE = "https://venomonar.github.io"
ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def gh(*args):
    return subprocess.check_output(["gh", "api", *args], text=True).strip()


def due_articles(articles, now):
    due = []
    for article in articles:
        release = datetime.fromisoformat(article["at"])
        if now.astimezone(release.tzinfo).date() == release.date() and now >= release:
            due.append(article)
    return due


def activate(articles):
    added = []
    for article in articles:
        slug = article["slug"]
        page = f"articles/{slug}/index.html"
        cover = f"assets/{slug}.jpg"
        if Path(page).exists():
            if git("hash-object", page) != article["files"][page]:
                raise RuntimeError(f"The published page differs from the queued article: {slug}")
            continue

        for path, expected in article["prior"].items():
            if git("hash-object", path) != expected:
                raise RuntimeError(f"The site changed before the release of {slug}: {path}")

        paths = ("index.html", "rss.xml", page, cover)
        git("restore", f"--source={article['snapshot']}", "--staged", "--worktree", "--", *paths)
        for path in paths:
            if git("hash-object", path) != article["files"][path]:
                raise RuntimeError(f"Incorrect release content: {path}")
        added.append(slug)
    return added


def fetch(path):
    request = Request(f"{SITE}/{path}", headers={"Cache-Control": "no-cache"})
    with urlopen(request, timeout=15) as response:
        return response.read()


def check_publication(slugs):
    try:
        items = ElementTree.fromstring(fetch("rss.xml")).findall("./channel/item")
        for slug in slugs:
            link = f"{SITE}/articles/{slug}/"
            item = next((item for item in items if item.findtext("link") == link), None)
            cover = f"{SITE}/assets/{slug}.jpg"
            if item is None:
                return False
            text = item.findtext(ENCODED, "")
            if cover not in text or "https://t.me/shelknulo27" not in text:
                return False
            if not fetch(f"articles/{slug}/") or not fetch(f"assets/{slug}.jpg").startswith(b"\xff\xd8"):
                return False
    except (OSError, URLError, ElementTree.ParseError):
        return False
    return True


def main():
    queue = json.loads(Path(".github/site-publications.json").read_text(encoding="utf-8"))
    articles = due_articles(queue["articles"], datetime.now(timezone.utc))
    test = os.environ["GITHUB_EVENT_NAME"] in {"push", "workflow_dispatch"}
    if not articles and not test:
        return

    api = f"repos/{os.environ['GITHUB_REPOSITORY']}/pages/builds"
    if articles:
        git("fetch", "--no-tags", "origin", f"refs/heads/{queue['branch']}")
        released = activate(articles)
        if released:
            git("config", "user.name", "github-actions[bot]")
            git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
            git("commit", "-m", "Publish scheduled articles: " + ", ".join(released))
            git("push", "origin", "HEAD:main")
        slugs = [article["slug"] for article in articles]
        if not test and not released and check_publication(slugs):
            return
    else:
        slugs = ["osago"]

    build = json.loads(gh("--method", "POST", api))
    build_url = build["url"]
    for attempt in range(12):
        result = json.loads(gh(build_url))
        if result.get("status") == "errored":
            raise RuntimeError("GitHub Pages reported a failed build.")
        if result.get("status") == "built" and check_publication(slugs):
            print("Verified Pages build, article pages, covers and RSS.")
            return
        if attempt < 11:
            time.sleep(15)

    raise RuntimeError("The Pages build or the public RSS could not be verified.")


if __name__ == "__main__":
    main()
