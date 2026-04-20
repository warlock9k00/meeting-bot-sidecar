"""GitHub Contents API — commit single file to vault main branch."""
import os
import base64
import requests


def commit_file(path_in_repo: str, content: str, message: str) -> dict:
    repo = os.environ["GITHUB_REPO"]
    branch = os.environ.get("GITHUB_BRANCH", "main")
    token = os.environ["GITHUB_TOKEN"]
    api = f"https://api.github.com/repos/{repo}/contents/{path_in_repo}"
    h = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # Check if file exists (need sha for update)
    sha = None
    g = requests.get(api, headers=h, params={"ref": branch}, timeout=15)
    if g.status_code == 200:
        sha = g.json().get("sha")
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(api, headers=h, json=body, timeout=30)
    r.raise_for_status()
    return r.json()
