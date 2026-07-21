import hashlib
import hmac
import html
import json
import os
import re
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
import requests
import datetime

NOTION_API_KEY  = os.environ.get("NOTION_API_KEY")
PROD_DB_ID      = os.environ.get("PROD_DB_ID")
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY")
# NOTE: GITHUB_TOKEN / GITHUB_REPO / GITHUB_BRANCH used to live here for
# the legacy github_write_file / github_flip_is_draft helpers. After the
# Vercel KV migration the served HTML lives in KV (not the vf-quotes
# repo), so both helpers were modifying a stale repo copy nobody served
# from — pure dead code. Removed alongside the helpers themselves. The
# vf-quotes-webhook fine-grained PAT can be retired in Vercel env vars.

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
    # Retry transient failures. This is the canonical "the quote is signed" write,
    # and a one-off Notion rate-limit (429) or timeout used to leave the quote
    # looking unsigned in the Studio forever — the "signed docs don't always show
    # as signed" symptom. Retry rate-limit / 5xx / network errors a few times with
    # a short backoff; fail fast on a real 4xx (bad request) so we don't loop.
    last_err = ""
    for attempt in range(3):
        try:
            r = requests.patch(url, headers=HEADERS, json=payload, timeout=HTTP_TIMEOUT)
            if r.status_code < 400:
                return True, ""
            last_err = f"Notion {r.status_code}: {r.text[:200]}"
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return False, last_err
        except Exception as e:
            last_err = str(e)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return False, last_err
    return False, last_err


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
    signer_email   = (payload.get("signerEmail") or "").strip()
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

    # ── What they signed for: itemise everything ─────────────────────
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

    # ── Signature block ──────────────────────────────────────────────
    # The signature must be a `cid:` attachment, NOT an inline data: URI.
    # Gmail / Outlook / Apple Mail strip data: images from emails, which is why
    # the signature previously showed as just the word "Signature" (the alt
    # text). A cid-referenced attachment renders in every client. We attach the
    # PNG below and reference it as cid:signature here.
    sig_data_url  = payload.get("signatureDataUrl") or ""
    sig_attachments = []
    if sig_data_url and sig_data_url.startswith("data:image/"):
        m = re.match(r"data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", sig_data_url, re.DOTALL)
        if m:
            mime, b64 = m.group(1), m.group(2).strip()
            ext = "png" if "png" in mime else ("jpg" if ("jpeg" in mime or "jpg" in mime) else "img")
            sig_attachments.append({
                "filename":   f"signature.{ext}",
                "content":    b64,           # base64, as Resend expects
                "content_id": "signature",   # referenced as cid:signature below
            })
            sig_html = (
                f'<div style="margin-top:8px;padding:12px;background:#fafafa;'
                f'border:1px solid #eee;border-radius:6px;display:inline-block;">'
                f'<img src="cid:signature" alt="Signature" '
                f'style="max-width:280px;height:auto;display:block;background:white;" />'
                f'</div>'
            )
        else:
            # Malformed data URI — keep the raw inline as a last resort.
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
            {f'<tr style="border-bottom:1px solid #f0f0f0;"><td style="padding:10px 0;color:#999;font-size:13px;">Email</td><td style="padding:10px 0;font-size:13px;"><a href="mailto:{e(signer_email)}" style="color:#095EDF;text-decoration:none;">{e(signer_email)}</a></td></tr>' if signer_email else ''}
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

          {f'<h2 style="font-size:13px;font-weight:700;letter-spacing:0.4px;text-transform:uppercase;color:#999;margin:0 0 8px;border-top:1px solid #f0f0f0;padding-top:18px;">What they signed for</h2><table style="width:100%;border-collapse:collapse;margin-bottom:8px;">{selected_rows_html}</table>' if selected_rows_html else ''}

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
                **({"attachments": sig_attachments} if sig_attachments else {}),
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


# Loose date check: just the YYYY-MM-DD prefix (Notion accepts ISO strings
# with optional time suffixes, so prefix-matching is deliberately permissive).
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


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
        # Accept either a plain string ("2026-06-11") or a {start, end} dict
        # for date ranges. Empty / missing start clears the property.
        if isinstance(value, dict):
            start = value.get("start")
            if not start:
                prop = {"date": None}
            else:
                if not _DATE_PREFIX_RE.match(str(start).strip()):
                    return False, f"bad date start '{start}' (want YYYY-MM-DD)"
                date_obj = {"start": start}
                end = value.get("end")
                if end:
                    if not _DATE_PREFIX_RE.match(str(end).strip()):
                        return False, f"bad date end '{end}' (want YYYY-MM-DD)"
                    date_obj["end"] = end
                prop = {"date": date_obj}
        elif value:
            if not _DATE_PREFIX_RE.match(str(value).strip()):
                return False, f"bad date '{value}' (want YYYY-MM-DD)"
            prop = {"date": {"start": value}}
        else:
            prop = {"date": None}
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
# Property names + types verified against the LIVE Productions DB schema.
#   • 'Project' is the title (NOT 'Project Title').
#   • Location is the 'Shoot Location' *place* property — the public Notion
#     API CANNOT write place properties, so it's intentionally NOT here; the
#     brief-grid Location row is display-only and edited in Notion directly.
#   • 'Location' and 'Shoot Hours' do not exist in the schema, so neither the
#     map nor the snake_case fallback should ever target them — fields not in
#     this map are refused below rather than guessed at.
PROD_FIELD_MAP = {
    "project_title":     ("Project", "title"),
    "production_date":   ("Production Date", "date"),
    "shooting_days":     ("Shooting Days", "number"),
    "job_type":          ("Job Type", "select"),
    "default_delivery":  ("Default Delivery", "select"),
    "quote_type":        ("Quote Type", "select"),
    "draft_deadline":    ("1st Draft Deadline", "date"),
}


# ── GitHub helpers ────────────────────────────────────────────────────────────
# Removed: _gh_headers, github_write_file, github_flip_is_draft.
#
# These powered the pre-KV publish path that wrote rendered HTML directly
# into HenryMaxPaterson/vf-quotes via the GitHub API. After the Vercel KV
# migration the served HTML lives in KV — every operation that *used* to
# go through these helpers (save_draft, approved_for_sending → publish)
# now goes through /api/quotes/upload, /api/quotes/sync-state, and
# /api/quotes/publish on the valley-films Vercel project. The GitHub
# writes were modifying a stale repo copy no one served from.


# ── Actions dispatch (instant cloud generation) ──────────────────────────────
# After set_trigger flips the Trigger select, fire a repository_dispatch so
# GitHub Actions runs the generation sweep immediately instead of waiting for
# the next poll. Strictly best-effort: the Mac monitor polls every ~30s
# regardless, so a missing token or a failed dispatch just means "slower",
# never "broken".
GH_DISPATCH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN", "")
GH_DISPATCH_REPO  = "HenryMaxPaterson/vf-quote-generator"
GH_DISPATCH_EVENT = "quote-generate"


def dispatch_actions(page_id, reason="unspecified"):
    """Fire a repository_dispatch event (soft-fail on every error path).
    Silently no-ops when GH_DISPATCH_TOKEN isn't configured."""
    if not GH_DISPATCH_TOKEN:
        return
    try:
        r = requests.post(
            f"https://api.github.com/repos/{GH_DISPATCH_REPO}/dispatches",
            headers={
                "Authorization": f"Bearer {GH_DISPATCH_TOKEN}",
                "Accept":        "application/vnd.github+json",
                "User-Agent":    "vf-quotes-webhook",
            },
            json={
                "event_type": GH_DISPATCH_EVENT,
                "client_payload": {"page_id": page_id or "", "reason": reason},
            },
            timeout=HTTP_TIMEOUT,
        )
        # GitHub returns 204 No Content on success.
        if r.status_code == 204:
            print(f"Actions dispatched for {(page_id or '?')[-8:]} (reason={reason})")
        else:
            print(f"Actions dispatch returned {r.status_code}: {(r.text or '')[:200]}")
    except Exception as e:
        # Never let a dispatch failure break the webhook handler.
        print(f"Actions dispatch failed (non-fatal): {type(e).__name__}: {str(e)[:160]}")


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
            # Post-KV-migration the only meaningful payload is editor_state.
            # The legacy `html` branch (which posted whole rendered HTML to
            # the vf-quotes GitHub repo) is gone — its served-from location
            # was already replaced by Vercel KV, so the GitHub write was
            # touching a stale repo copy no client ever read.
            editor_state = data.get("editor_state")
            ok = False
            warning   = None
            error_msg = None
            try:
                if editor_state is not None and page_id:
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

        # ── Preflight: verify autosave will land ──────────────────────────────
        # The editor calls this once on load so it can warn BEFORE the operator
        # types, instead of letting every keystroke save fail silently.
        # Always 200 (ok:true/false) — soft signal, not an HTTP error.
        if action == "preflight" and page_id:
            def _pf_reply(payload):
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
                return
            try:
                resp = requests.get(
                    f"https://api.notion.com/v1/pages/{page_id}",
                    headers=HEADERS, timeout=HTTP_TIMEOUT,
                )
                if resp.status_code == 404:
                    return _pf_reply({
                        "ok": False, "reason": "page_not_found",
                        "hint": "This Production page no longer exists in Notion.",
                    })
                resp.raise_for_status()
                props = resp.json().get("properties", {}) or {}
                es = props.get("Editor State")
                if es is None:
                    return _pf_reply({
                        "ok": False, "reason": "missing_property",
                        "hint": "Add a 'Rich text' property called 'Editor State' "
                                "to the Productions database, then refresh.",
                    })
                if "rich_text" not in es:
                    return _pf_reply({
                        "ok": False, "reason": "wrong_property_type",
                        "hint": "The 'Editor State' property must be type 'Rich text'. "
                                "Delete and recreate it as Rich text.",
                    })
                return _pf_reply({"ok": True})
            except Exception as e:
                return _pf_reply({
                    "ok": False, "reason": "preflight_error",
                    "hint": f"Could not verify save target: {type(e).__name__}",
                })

        # NOTE: 'revise' and 'set_trigger' deliberately do NOT live here.
        # Both are state-mutating (revise un-sends a quote; set_trigger's
        # "Reset & Regenerate" wipes the operator's Editor State), and this
        # webhook is unauthenticated in its current posture (WEBHOOK_AUTH_MODE
        # defaults to 'off' and no VF_WEBHOOK_SECRET is configured, so every
        # quote ships with webhookToken=""). A client who reads VF.pageId out
        # of their own served page could otherwise POST these and destroy the
        # operator's work. They now run through valley.film/api/quotes/admin,
        # which is gated on the operator's session cookie (a client has none).

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
                # Unknown field → refuse. The old snake_case→Title-Case guess
                # would PATCH a property that may not exist (e.g. 'location' →
                # 'Location', which 404s) or, worse, an attacker-named one.
                # Only the curated PROD_FIELD_MAP fields are writable.
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ok": False, "field": field,
                    "error": f"field '{field}' is not editable from the quote "
                             f"(edit it in Notion directly)",
                }).encode())
                return
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
        # The flip-isDraft work now lives entirely on the website side: the
        # editor JS calls /api/quotes/publish on valley.film before opening
        # Gmail, which rewrites the KV-stored HTML in place. Previously
        # we *also* flipped isDraft in the vf-quotes GitHub repo via
        # github_flip_is_draft — kept around for a while after the KV
        # migration as belt-and-braces, but the served HTML hasn't come
        # from that repo for months, so the GitHub round-trip was pure
        # tax. Webhook now only owns the Notion status transition.
        if action == "approved_for_sending" and page_id:
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

            # Persist the full acceptance snapshot to KV via the website's
            # /api/quotes/acceptance endpoint. This is what powers the
            # operator's "View confirmation" link in the /quotes picker —
            # serve.js reads it on the public client URL and renders a
            # confirmation overlay instead of the live editor surface for
            # signed quotes. Best-effort: a failure here doesn't block the
            # sign (Notion is the canonical record), the operator just
            # won't get the pretty confirmation view for this quote.
            try:
                quote_url = data.get("quoteUrl") or ""
                token_match = re.search(r"/quotes/([A-Za-z0-9]+)", quote_url)
                token = token_match.group(1) if token_match else ""
                if token:
                    snap = dict(data)
                    snap["clientIp"] = client_ip
                    requests.post(
                        f"https://valley.film/api/quotes/acceptance?token={token}",
                        json={"acceptance": snap},
                        timeout=HTTP_TIMEOUT,
                    )
            except Exception as snap_e:
                print(f"acceptance snapshot store failed (soft): {snap_e}")

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
