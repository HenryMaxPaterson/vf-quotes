import base64
import html
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler
import requests
import datetime

NOTION_API_KEY  = os.environ.get("NOTION_API_KEY")
PROD_DB_ID      = os.environ.get("PROD_DB_ID")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN")
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY")
GITHUB_REPO    = "HenryMaxPaterson/vf-quotes"
GITHUB_BRANCH  = "main"

# Standard timeout for any third-party API call. Vercel functions cap at
# 10s on the free tier; keep individual calls under that so an upstream
# stall doesn't take the whole handler down.
HTTP_TIMEOUT = 8

HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}


def update_notion_status(page_id, status_name):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {"properties": {"Financial Status": {"status": {"name": status_name}}}}
    try:
        response = requests.patch(url, headers=HEADERS, json=payload, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error updating Notion: {e}")
        return False


def mark_quote_signed(page_id, signer_name, timestamp):
    """Atomic write of all three Signed-side fields. Returns (ok, error_msg).

    Replaces the old `update_notion_status(page_id, 'Signed')` call which
    flipped status only and silently dropped the signature metadata —
    that's why early Signed quotes had empty Signed By / Signed At.
    """
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {"properties": {
        "Signed By":        {"rich_text": [{"text": {"content": signer_name or ""}}]},
        "Signed At":        {"date": {"start": timestamp}} if timestamp else {"date": None},
        "Financial Status": {"status": {"name": "Signed"}},
    }}
    try:
        r = requests.patch(url, headers=HEADERS, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            return False, f"Notion {r.status_code}: {r.text[:200]}"
        return True, ""
    except Exception as e:
        return False, str(e)


def send_signed_email(quote_ref, signer_name, project_title, total,
                       production_date, client_company, timestamp):
    """Send the operator a notification email when a client signs a quote.
    No-op (returns True) if RESEND_API_KEY isn't set.

    All interpolations are HTML-escaped — signers can put anything in
    their name; we don't want stray <script> or <img onerror> reaching
    Max's inbox.
    """
    if not RESEND_API_KEY:
        return True, ""
    try:
        signed_date = datetime.datetime.fromisoformat(
            (timestamp or "").replace("Z", "+00:00")
        ).strftime("%-d %B %Y") if timestamp else "—"
    except Exception:
        signed_date = "—"

    e = html.escape
    client_line = e(signer_name or "")
    if client_company:
        client_line += f", {e(client_company)}"

    subject = f"Quote Accepted: {quote_ref or '—'} — {signer_name or '—'}"
    body_html = f"""
      <div style="font-family:Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;color:#111;">
        <div style="background:#095EDF;padding:28px 32px;border-radius:12px 12px 0 0;">
          <p style="color:white;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:2px;margin:0 0 6px;">Valley Films</p>
          <h1 style="color:white;margin:0;font-size:24px;font-weight:700;letter-spacing:-0.5px;">Quote Accepted</h1>
        </div>
        <div style="border:1px solid #e4e4e4;border-top:none;padding:28px 32px;border-radius:0 0 12px 12px;">
          <p style="font-size:14px;color:#555;margin:0 0 24px;">
            <strong style="color:#111;">{e(quote_ref or '—')}</strong> has been accepted and signed.
          </p>
          <table style="width:100%;border-collapse:collapse;">
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:10px 0;color:#999;font-size:13px;width:140px;">Signed by</td>
              <td style="padding:10px 0;font-size:13px;font-weight:600;">{client_line}</td>
            </tr>
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:10px 0;color:#999;font-size:13px;">Project</td>
              <td style="padding:10px 0;font-size:13px;">{e(project_title or 'TBC')}</td>
            </tr>
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:10px 0;color:#999;font-size:13px;">Production</td>
              <td style="padding:10px 0;font-size:13px;">{e(production_date or 'TBC')}</td>
            </tr>
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:10px 0;color:#999;font-size:13px;">Total</td>
              <td style="padding:10px 0;font-size:13px;font-weight:700;">{e(str(total) if total is not None else 'TBC')}</td>
            </tr>
            <tr>
              <td style="padding:10px 0;color:#999;font-size:13px;">Date signed</td>
              <td style="padding:10px 0;font-size:13px;">{e(signed_date)}</td>
            </tr>
          </table>
          <p style="font-size:12px;color:#bbb;margin:24px 0 0;border-top:1px solid #f0f0f0;padding-top:16px;">
            Automated notification from the Valley Films quote system.
          </p>
        </div>
      </div>
    """
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "from":    "Valley Films <onboarding@resend.dev>",
                "to":      ["max@valley.film"],
                "subject": subject,
                "html":    body_html,
            },
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code >= 400:
            return False, f"Resend {r.status_code}: {r.text[:200]}"
        return True, ""
    except Exception as exc:
        return False, str(exc)


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
    response = requests.patch(url, headers=HEADERS, json=payload, timeout=HTTP_TIMEOUT)
    response.raise_for_status()


def update_notion_property(page_id, prop_name, prop_type, value):
    """PATCH a single Notion property on a Production page.

    prop_type ∈ {'text', 'rich_text', 'title', 'number', 'date', 'select'}.
    Returns (ok, error_msg).
    """
    url = f"https://api.notion.com/v1/pages/{page_id}"
    if prop_type == "number":
        try: val = float(value) if value not in (None, "") else None
        except Exception: return False, f"bad number '{value}'"
        prop = {"number": val}
    elif prop_type == "date":
        prop = {"date": {"start": value}} if value else {"date": None}
    elif prop_type == "select":
        prop = {"select": {"name": value}} if value else {"select": None}
    elif prop_type in ("text", "rich_text"):
        prop = {"rich_text": [{"text": {"content": str(value or "")}}]}
    elif prop_type == "title":
        prop = {"title": [{"text": {"content": str(value or "")}}]}
    else:
        return False, f"unsupported prop_type '{prop_type}'"

    payload = {"properties": {prop_name: prop}}
    try:
        r = requests.patch(url, headers=HEADERS, json=payload, timeout=10)
        if r.status_code >= 400:
            return False, f"Notion {r.status_code}: {r.text[:160]}"
        return True, ""
    except Exception as e:
        return False, str(e)


# Map our snake_case field names → (Notion property name, type)
PROD_FIELD_MAP = {
    "project_title":     ("Project Title", "title"),
    "production_date":   ("Production Date", "date"),
    "shooting_days":     ("Shooting Days", "number"),
    "location":          ("Location", "rich_text"),
    "job_type":          ("Job Type", "select"),
    "default_delivery":  ("Default Delivery", "select"),
    "quote_type":        ("Quote Type", "select"),
}


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
    get_resp = requests.get(api_url, headers=_gh_headers(), timeout=HTTP_TIMEOUT)
    sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None
    payload = {
        "message": f"Save: {filename}",
        "content": base64.b64encode(html_content.encode("utf-8")).decode(),
        "branch":  GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    put_resp = requests.put(api_url, headers=_gh_headers(), json=payload, timeout=HTTP_TIMEOUT)
    put_resp.raise_for_status()

def github_flip_is_draft(filename):
    """Fetch the live HTML, set isDraft to false, write back."""
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    get_resp = requests.get(api_url, headers=_gh_headers(), timeout=HTTP_TIMEOUT)
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
    put_resp = requests.put(api_url, headers=_gh_headers(), json=payload, timeout=HTTP_TIMEOUT)
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

        # ── Production field PATCH (editor changes a brief-grid field) ─────────
        # Editor sends { field, field_type, value }. We map the field to its
        # Notion property + type, then PATCH. Errors return 200 + ok:false so
        # the operator sees the inline pill instead of a generic 400.
        if action == "update_production_field" and page_id:
            field = (data.get("field") or "").strip()
            value = data.get("value")
            field_type = data.get("field_type") or ""
            mapping = PROD_FIELD_MAP.get(field)
            if not mapping:
                # Fallback: use snake_case → Title Case + the type hint sent
                prop_name = " ".join(p.capitalize() for p in field.split("_"))
                prop_type = field_type or "rich_text"
            else:
                prop_name, prop_type = mapping
                if field_type:
                    prop_type = field_type
            ok, err = update_notion_property(page_id, prop_name, prop_type, value)
            print(f"update_production_field {field}={value!r} → {prop_name} ({prop_type}): "
                  + ("ok" if ok else f"failed: {err}"))
            # Quote-type / default-delivery / job-type changes need a regen on
            # the mac mini publisher to take effect — flag that to the client
            # so it can show a "regenerating…" status.
            regen_queued = ok and field in ("quote_type", "default_delivery", "job_type", "shooting_days")
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            body = {"ok": ok, "field": field, "value": value, "regen_queued": regen_queued}
            if not ok: body["error"] = err
            self.wfile.write(json.dumps(body).encode())
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
            # Atomic Signed write — flips status AND records Signed By + Signed
            # At in one PATCH. Previously this branch only flipped status, so
            # every signed quote in production had empty signature metadata
            # (caught by scripts/audit_quotes.py).
            signer_name = data.get("signerName") or ""
            timestamp   = data.get("timestamp") or ""
            ok, err = mark_quote_signed(page_id, signer_name, timestamp)
            if not ok:
                print(f"mark_quote_signed failed: {err}")

            # Notify Max by email via Resend (no-op if RESEND_API_KEY unset).
            # All payload strings are HTML-escaped inside send_signed_email.
            email_ok, email_err = send_signed_email(
                quote_ref       = data.get("quoteRef"),
                signer_name     = signer_name,
                project_title   = data.get("projectTitle"),
                total           = data.get("total"),
                production_date = data.get("productionDate"),
                client_company  = data.get("clientCompany"),
                timestamp       = timestamp,
            )
            if not email_ok:
                print(f"send_signed_email failed: {email_err}")

            # Capture client IP from the standard Vercel/Cloudflare proxy header.
            # x-forwarded-for is a comma-separated list; the original client IP
            # is always the first entry.
            xff = self.headers.get('x-forwarded-for', '') or self.headers.get('X-Forwarded-For', '')
            client_ip = (xff.split(',')[0].strip() if xff else
                         self.headers.get('x-real-ip', '') or
                         self.client_address[0])
            print(f"Signed: page={page_id} signer='{signer_name}' ip={client_ip} "
                  f"notion_ok={ok} email_ok={email_ok}")

            # Return 500 if the canonical Notion write failed so the client
            # JS knows the accept didn't persist — previously we returned
            # success unconditionally and the operator never learned about
            # failures.
            self.send_response(200 if ok else 500)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            body = {
                "status":  "success" if ok else "error",
                "message": "Quote accepted and Notion updated!" if ok else "Notion write failed",
                "ip":      client_ip,
            }
            if not ok:        body["reason"]        = err
            if not email_ok:  body["email_warning"] = email_err
            self.wfile.write(json.dumps(body).encode())
            return

        self.send_response(400)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
