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


def update_notion_editor_state(page_id, state):
    """Persist the operator's editor_state JSON to a Notion rich_text
    property called 'Editor State'. Splits into ≤1900-char chunks since
    Notion limits each rich_text element to 2000 chars; up to ~95 elements
    total fit comfortably under the 100-block array limit."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    state_json = json.dumps(state, separators=(',', ':'))
    chunks = [state_json[i:i+1900] for i in range(0, len(state_json), 1900)] or [""]
    if len(chunks) > 95:
        raise RuntimeError(f"editor_state too large ({len(state_json)} chars, {len(chunks)} chunks)")
    payload = {
        "properties": {
            "Editor State": {
                "rich_text": [{"text": {"content": c}} for c in chunks]
            }
        }
    }
    response = requests.patch(url, headers=HEADERS, json=payload)
    response.raise_for_status()


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

        # ── Save draft edits ──────────────────────────────────────────────────
        # Two payload shapes are accepted:
        #   A. {filename, html}       → server-rendered HTML, written to GitHub
        #                               (used by the mac-mini regenerator)
        #   B. {page_id, editor_state} → operator's in-flight edit state,
        #                               best-effort persisted to Notion.
        #                               The browser also mirrors this to
        #                               localStorage, so the payload is safe
        #                               even if the Notion write rejects.
        if action == "save_draft":
            filename     = data.get("filename", "")
            html         = data.get("html", "")
            editor_state = data.get("editor_state")
            ok = False
            warning   = None
            error_msg = None
            try:
                if filename and html and GITHUB_TOKEN:
                    github_write_file(filename, html)
                    ok = True
                    print(f"Draft saved to GitHub: {filename}")
                elif editor_state is not None and page_id:
                    # Acknowledge receipt — localStorage is the canonical store
                    # for in-flight edits. Best-effort sync to Notion below;
                    # if it fails (e.g. property missing), we still return 200.
                    ok = True
                    if NOTION_API_KEY:
                        try:
                            update_notion_editor_state(page_id, editor_state)
                            print(f"Editor state synced to Notion: {page_id}")
                        except Exception as e:
                            warning = f"editor_state not persisted to Notion: {e}"
                            print(f"Notion editor_state sync failed (soft): {e}")
                else:
                    error_msg = "missing payload (need filename+html or page_id+editor_state)"
            except Exception as e:
                error_msg = str(e)
                print(f"save_draft failed: {e}")

            self.send_response(200 if ok else 500)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            body = {"status": "saved" if ok else "error"}
            if warning:   body["warning"] = warning
            if error_msg: body["reason"]  = error_msg
            self.wfile.write(json.dumps(body).encode())
            return

        # ── Manual status update from editor ──────────────────────────────────
        if action == "update_status" and page_id:
            new_status = data.get("status", "")
            allowed = {"Draft", "Quotation Sent", "Viewed", "Revision Requested", "Signed"}
            if new_status not in allowed:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "reason": f"unknown status '{new_status}'"}).encode())
                return
            ok = update_notion_status(page_id, new_status)
            print(f"Manual status update: {page_id} → {new_status} ({'ok' if ok else 'failed'})")
            self.send_response(200 if ok else 500)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok" if ok else "error"}).encode())
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
