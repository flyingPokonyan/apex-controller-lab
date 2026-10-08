"""Notify Forge after a successful main push. Inputs come from the CI environment."""
import datetime as dt
import json
import os
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlsplit
from urllib.error import HTTPError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


def main():
    url = os.environ.get("FORGE_RELEASE_URL", "")
    token = os.environ.get("FORGE_RELEASE_TOKEN", "")
    parsed = urlsplit(url)
    if not token or parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit("Configure FORGE_RELEASE_URL and FORGE_RELEASE_TOKEN before publishing.")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as stream:
        event = json.load(stream)
    payload = {"schemaVersion": 1, "repository": os.environ["GITHUB_REPOSITORY"], "branch": "main",
        "sequence": int(os.environ["GITHUB_RUN_NUMBER"]), "commit": os.environ["GITHUB_SHA"],
        "title": " ".join(str(event.get("head_commit", {}).get("message") or "Controller update").splitlines())[:200],
        "publishedAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")}
    for attempt in range(4):
        req = Request(url, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with build_opener(NoRedirect).open(req, timeout=15) as response:
                result = json.loads(response.read(8192))
                if response.status != 200 or result.get("commit") != payload["commit"]:
                    raise ValueError()
            print("Forge release registered: " + payload["commit"][:8])
            return
        except Exception:
            if attempt == 3:
                raise SystemExit("Forge release notification failed; retry this workflow after checking its configuration.") from None
            time.sleep(5 * (attempt + 1))


if __name__ == "__main__":
    main()
