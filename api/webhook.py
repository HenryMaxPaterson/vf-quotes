import base64
import hashlib
import hmac
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

# ── Webhook auth (shared-secret HMAC per page) ────────────────────────────
# Each generated quote embeds a token = hmac_sha256(VF_WEBHOOK_SECRET, page_id).
# Server re-derives + compares using constant-time compare. The secret never
# leaves Vercel; a leak from one quote can't be used against another.
#
# Three modes (env: WEBHOOK_AUTH_MODE):
#   off     — no check, current behaviour. DEFAULT (back-compat).
#   warn    — check token; log unauthenticated requests; let them through.
#   enforce — reject unauthenticated requests with HTTP 401.
#
# Rollout: deploy with mode=off, regenerate all live quotes so they have
# tokens, flip to mode=warn for a week (watch Vercel logs for false
# positives), then flip to mode=enforce. The OPTIONS preflight and the
# GET pixel are always exempt.
VF_WEBHOOK_SECRET = os.environ.get("VF_WEBHOOK_SECRET", "")
WEBHOOK_AUTH_MODE = (os.environ.get("WEBHOOK_AUTH_MODE", "off")
                     .strip().lower())

# Standard timeout for any third-party API call. Vercel functions cap at
# 10s on the free tier; keep individual calls under that so an upstream
# stall doesn't take the whole handler down.
HTTP_TIMEOUT = 8


def expected_webhook_token(page_id: str) -> str:
    """HMAC-SHA256(secret, page_id), hex. Stable for the lifetime of the
    secret; rotating VF_WEBHOOK_SECRET invalidates every quote's token at
    once (force-regenerate to mint new ones)."""
    if not VF_WEBHOOK_SECRET or not page_id:
        return ""
    return hmac.new(
        VF_WEBHOOK_SECRET.encode("utf-8"),
        page_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_webhook_auth(provided_token: str, page_id: str) -> tuple[bool, str]:
    """Returns (ok, reason). `ok=True` means the request is authenticated
    OR the configured mode permits unauthenticated traffic."""
    if WEBHOOK_AUTH_MODE == "off" or not VF_WEBHOOK_SECRET:
        return True, "auth disabled"
    expected = expected_webhook_token(page_id)
    if expected and provided_token and hmac.compare_digest(expected, provided_token):
        return True, "authenticated"
    reason = ("missing X-VF-Token header" if not provided_token
              else "invalid token for this page_id")
    if WEBHOOK_AUTH_MODE == "warn":
        return True, f"warn: {reason}"
    return False, reason

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


def _fmt_money_str(v):
    """Best-effort GBP rendering. Accepts the already-formatted strings the
    client sends ("£723.65") AND raw numbers ("123.4"). Falls back to
    'TBC' on None / unparseable input."""
    if v is None: return "TBC"
    s = str(v).strip()
    if not s:     return "TBC"
    if s.startswith("£") or s.startswith("\xa3"): return s
    try:
        return f"\xa3{float(s):,.2f}"
    except Exception:
        return s


def send_signed_email(payload, client_ip=""):
    """Send the operator a detailed notification email when a client signs.
    No-op (returns True) if RESEND_API_KEY isn't set.

    `payload` is the full `accepted`-action POST body from the editor JS
    (quote_generator.py). It carries everything the client saw at sign
    time: selectedPackage / selectedPostProd / selectedAddons with
    detail + discounts, selectedDelivery, rushSelected, addonDiscounts,
    signerName, signatureDataUrl, timestamp, total. We render all of
    this so the email is a complete audit record of what was signed,
    by whom, for how much, from where (IP), at what time.

    All interpolations are HTML-escaped — signers can put anything in
    their name; we don't want stray <script> or <img onerror> reaching
    Max's inbox.
    """
    if not RESEND_API_KEY:
        return True, ""
    e = html.escape

    # ── Header fields ─────────────────────────────────────────────
    quote_ref      = payload.get("quoteRef") or "—"
    signer_name    = payload.get("signerName") or "—"
    project_title  = payload.get("projectTitle") or "TBC"
    client_company = payload.get("clientCompany") or ""
    timestamp      = payload.get("timestamp") or ""
    total_str      = _fmt_money_str(payload.get("total"))
    production_date = payload.get("productionDate") or "TBC"
    quote_url       = payload.get("quoteUrl") or ""
    page_id         = payload.get("page_id") or ""

    try:
        ts_dt = datetime.datetime.fromisoformat((timestamp or "").replace("Z", "+00:00"))
        signed_date_full = ts_dt.strftime("%-d %B %Y, %H:%M UTC")
    except Exception:
        signed_date_full = timestamp or "—"

    client_line = e(signer_name)
    if client_company:
        client_line += f", {e(client_company)}"

    # ── What she signed for: itemise everything ──────────────────────
    rows = []

    # Package (Package Based mode only)
    pkg = payload.get("selectedPackage") or {}
    if pkg and pkg.get("name"):
        rows.append((
            "Package",
            f"{e(pkg.get('name'))} — {_fmt_money_str(pkg.get('price'))}",
        ))

    # Post-production (optional)
    post_name = payload.get("selectedPostProdName") or ""
    if post_name:
        rows.append(("Post-production", e(post_name)))

    # Add-ons w/ unit prices + any per-addon discounts the operator applied
    addons_detail = payload.get("selectedAddonsDetail") or []
    addon_discs   = payload.get("addonDiscounts") or {}
    if addons_detail:
        items_html = []
        for a in addons_detail:
            name  = a.get("name") or a.get("id") or "—"
            price = a.get("price")
            disc  = addon_discs.get(a.get("id")) or {}
            disc_str = ""
            if isinstance(disc, dict) and float(disc.get("value") or 0) > 0:
                if disc.get("type") == "pct":
                    disc_str = f' <span style="color:#B91C1C;font-weight:600;">&minus;{int(float(disc["value"]))}%</span>'
                else:
                    disc_str = f' <span style="color:#B91C1C;font-weight:600;">&minus;\xa3{float(disc["value"]):,.2f}</span>'
            items_html.append(
                f'<div style="padding:2px 0;">{e(name)} — {_fmt_money_str(price)}{disc_str}</div>'
            )
        rows.append(("Add-ons", "".join(items_html)))

    # Delivery method
    delivery = payload.get("selectedDeliveryName") or payload.get("selectedDelivery") or ""
    if delivery:
        rows.append(("Delivery", e(delivery)))

    # Rush selection (if any)
    rush = payload.get("rushSelected") or {}
    if isinstance(rush, dict) and rush.get("sublabel"):
        rush_line = e(str(rush.get("sublabel")))
        if rush.get("price"):
            rush_line += f" — {_fmt_money_str(rush.get('price'))}"
        rows.append(("Turnaround", rush_line))

    # Whole-quote discounts (if any)
    qd = payload.get("selectedDiscountsDetail") or []
    if qd:
        qd_html = "".join(
            f'<div style="padding:2px 0;">{e(d.get("name") or d.get("id") or "—")}</div>'
            for d in qd
        )
        rows.append(("Quote discounts", qd_html))

    selected_rows_html = "".join(
        f'<tr style="border-bottom:1px solid #f0f0f0;vertical-align:top;">'
        f'  <td style="padding:10px 0;color:#999;font-size:13px;width:140px;">{label}</td>'
        f'  <td style="padding:10px 0;font-size:13px;">{value}</td>'
        f'</tr>'
        for label, value in rows
    )

    # ── Signature block (inline data-URI image) ──────────────────────
    sig_data_url = payload.get("signatureDataUrl") or ""
    if sig_data_url and sig_data_url.startswith("data:image/"):
        sig_html = (
            f'<div style="margin-top:8px;padding:12px;background:#fafafa;'
            f'border:1px solid #eee;border-radius:6px;display:inline-block;">'
            f'<img src="{sig_data_url}" alt="Signature" '
            f'style="max-width:280px;height:auto;display:block;background:white;" />'
            f'</div>'
        )
    else:
        sig_html = '<span style="color:#bbb;font-size:12px;">No signature image captured.</span>'

    # ── Build subject + body ─────────────────────────────────────────
    subject = f"Quote Accepted: {quote_ref} — {signer_name} — {total_str}"

    quote_link_html = (
        f'<p style="margin:24px 0 0;font-size:12px;">'
        f'<a href="{e(quote_url)}" style="color:#095EDF;text-decoration:none;">View signed quote &rarr;</a>'
        f'</p>'
    ) if quote_url else ""

    body_html = f"""
      <div style="font-family:Helvetica,Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
        <div style="background:#095EDF;padding:28px 32px;border-radius:12px 12px 0 0;">
          <p style="color:white;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:2px;margin:0 0 6px;">Valley Films</p>
          <h1 style="color:white;margin:0;font-size:24px;font-weight:700;letter-spacing:-0.5px;">Quote Accepted</h1>
        </div>
        <div style="border:1px solid #e4e4e4;border-top:none;padding:28px 32px;border-radius:0 0 12px 12px;">
          <p style="font-size:14px;color:#555;margin:0 0 24px;">
            <strong style="color:#111;">{e(quote_ref)}</strong> has been accepted and signed.
          </p>

          <h2 style="font-size:13px;font-weight:700;letter-spacing:0.4px;text-transform:uppercase;color:#999;margin:0 0 8px;border-top:1px solid #f0f0f0;padding-top:18px;">Signed by</h2>
          <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:10px 0;color:#999;font-size:13px;width:140px;">Name on signature</td>
              <td style="padding:10px 0;font-size:13px;font-weight:600;">{client_line}</td>
            </tr>
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:10px 0;color:#999;font-size:13px;">Signed at</td>
              <td style="padding:10px 0;font-size:13px;">{e(signed_date_full)}</td>
            </tr>
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:10px 0;color:#999;font-size:13px;">Client IP</td>
              <td style="padding:10px 0;font-size:12px;color:#888;font-family:monospace;">{e(client_ip or '—')}</td>
            </tr>
            <tr>
              <td style="padding:10px 0;color:#999;font-size:13px;vertical-align:top;">Signature</td>
              <td style="padding:10px 0;font-size:13px;">{sig_html}</td>
            </tr>
          </table>

          <h2 style="font-size:13px;font-weight:700;letter-spacing:0.4px;text-transform:uppercase;color:#999;margin:0 0 8px;border-top:1px solid #f0f0f0;padding-top:18px;">Quote</h2>
          <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:10px 0;color:#999;font-size:13px;width:140px;">Project</td>
              <td style="padding:10px 0;font-size:13px;">{e(project_title)}</td>
            </tr>
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:10px 0;color:#999;font-size:13px;">Production date</td>
              <td style="padding:10px 0;font-size:13px;">{e(production_date)}</td>
            </tr>
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:10px 0;color:#999;font-size:13px;">Total signed</td>
              <td style="padding:10px 0;font-size:14px;font-weight:700;">{e(total_str)}</td>
            </tr>
          </table>

          {f'<h2 style="font-size:13px;font-weight:700;letter-spacing:0.4px;text-transform:uppercase;color:#999;margin:0 0 8px;border-top:1px solid #f0f0f0;padding-top:18px;">What she signed for</h2><table style="width:100%;border-collapse:collapse;margin-bottom:8px;">{selected_rows_html}</table>' if selected_rows_html else ''}

          {quote_link_html}

          <p style="font-size:11px;color:#bbb;margin:24px 0 0;border-top:1px solid #f0f0f0;padding-top:16px;">
            Automated notification from the Valley Films quote system.<br>
            Page&nbsp;ID: <span style="font-family:monospace;">{e(page_id or '—')}</span>
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

        # ── Auth check (HMAC per page_id) ─────────────────────────────────────
        # See header doc + verify_webhook_auth(). Default mode 'off' makes
        # this a no-op until VF_WEBHOOK_SECRET is set in Vercel and
        # WEBHOOK_AUTH_MODE is flipped to 'warn' or 'enforce'.
        provided_tok = (self.headers.get('X-VF-Token') or
                        self.headers.get('x-vf-token') or '')
        auth_ok, auth_reason = verify_webhook_auth(provided_tok, page_id or '')
        if not auth_ok:
            print(f"webhook auth denied: action={action} page={page_id} "
                  f"reason={auth_reason}")
            self.send_response(401)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "error",
                "reason": auth_reason,
            }).encode())
            return
        if auth_reason.startswith("warn:"):
            print(f"webhook auth warn: action={action} page={page_id} "
                  f"reason={auth_reason}")

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

            # Capture client IP from the standard Vercel/Cloudflare proxy header
            # FIRST so we can include it in the email audit trail. x-forwarded-for
            # is a comma-separated list; the original client IP is always the
            # first entry.
            xff = self.headers.get('x-forwarded-for', '') or self.headers.get('X-Forwarded-For', '')
            client_ip = (xff.split(',')[0].strip() if xff else
                         self.headers.get('x-real-ip', '') or
                         self.client_address[0])

            # Notify Max by email via Resend (no-op if RESEND_API_KEY unset).
            # Detailed email now renders package, addons w/ discounts, delivery,
            # turnaround tier, signature image, IP — a full audit record of
            # what the client saw and signed for. All interpolations are
            # HTML-escaped inside send_signed_email.
            email_ok, email_err = send_signed_email(data, client_ip=client_ip)
            if not email_ok:
                print(f"send_signed_email failed: {email_err}")

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
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-VF-Token')
        self.end_headers()
