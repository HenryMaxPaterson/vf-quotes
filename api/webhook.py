import base64
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler
import requests
import datetime

NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
PROD_DB_ID     = os.environ.get("PROD_DB_ID")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO    = "HenryMaxPaterson/vf-quotes"
GITHUB_BRANCH  = "main"

HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}


def update_notion_status(page_id, status_name):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {"properties": {"Financial Status": {"status": {"name": status_name}}}}
    try:
        response = requests.patch(url, headers=HEADERS, json=payload)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error updating Notion: {e}")
        return False


# ── GitHub helpers ─────────────────────────────────────────────────────────────

def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

def github_write_file(filename, html_content):
    """Create or update a file in the quotes repo."""
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    # Get current SHA so GitHub accepts the update
    get_resp = requests.get(api_url, headers=_gh_headers())
    sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None
    payload = {
        "message": f"Save: {filename}",
        "content": base64.b64encode(html_content.encode("utf-8")).decode(),
        "branch":  GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    put_resp = requests.put(api_url, headers=_gh_headers(), json=payload)
    put_resp.raise_for_status()

def github_flip_is_draft(filename):
    """Fetch the live HTML, set isDraft to false, write back."""
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    get_resp = requests.get(api_url, headers=_gh_headers())
    get_resp.raise_for_status()
    file_data = get_resp.json()
    html = base64.b64decode(file_data["content"]).decode("utf-8")
    # Flip the flag in the VF data blob
    html = re.sub(r'"isDraft"\s*:\s*true', '"isDraft": false', html)
    payload = {
        "message": f"Publish: {filename}",
        "content": base64.b64encode(html.encode("utf-8")).decode(),
        "branch":  GITHUB_BRANCH,
        "sha":     file_data["sha"],
    }
    put_resp = requests.put(api_url, headers=_gh_headers(), json=payload)
    put_resp.raise_for_status()


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        """Tracking pixel — sets Notion status to Viewed."""
        parsed_path = urllib.parse.urlparse(self.path)
        query  = urllib.parse.parse_qs(parsed_path.query)
        action = query.get('action', [''])[0]
        page_id = query.get('page_id', [''])[0]

        if action == 'viewed' and page_id:
            update_notion_status(page_id, "Viewed")
            print(f"Set page {page_id} to Viewed.")

        self.send_response(200)
        self.send_header('Content-type', 'image/gif')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        gif = b'GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
        self.wfile.write(gif)

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_response(400)
            self.end_headers()
            return

        post_data = self.rfile.read(content_length)
        try:
            data = json.loads(post_data.decode('utf-8'))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        action  = data.get("action")
        page_id = data.get("page_id")

        # ── Save draft edits to GitHub ─────────────────────────────────────────
        if action == "save_draft":
            filename = data.get("filename", "")
            html     = data.get("html", "")
            ok = False
            if filename and html and GITHUB_TOKEN:
                try:
                    github_write_file(filename, html)
                    ok = True
                    print(f"Draft saved to GitHub: {filename}")
                except Exception as e:
                    print(f"GitHub save failed: {e}")
            self.send_response(200 if ok else 500)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "saved" if ok else "error"}).encode())
            return

        # ── Approve: publish to client ─────────────────────────────────────────
        if action == "approved_for_sending" and page_id:
            filename = data.get("filename", "")
            # Flip isDraft in the live file so the client sees no draft UI
            if filename and GITHUB_TOKEN:
                try:
                    github_flip_is_draft(filename)
                    print(f"Published (isDraft→false): {filename}")
                except Exception as e:
                    print(f"GitHub publish failed (continuing): {e}")
            update_notion_status(page_id, "Quotation Sent")
            print(f"Set page {page_id} to Quotation Sent.")
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
            return

        # ── Client acceptance / signature ──────────────────────────────────────
        if action == "accepted" and page_id:
            update_notion_status(page_id, "Signed")
            print(f"Set page {page_id} to Signed.")
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "message": "Quote accepted and Notion updated!"}).encode())
            return

        self.send_response(400)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
