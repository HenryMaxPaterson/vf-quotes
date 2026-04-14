exports.handler = async (event) => {
  if (event.httpMethod !== 'POST') {
    return { statusCode: 405, body: 'Method Not Allowed' };
  }

  const NOTION_TOKEN  = process.env.NOTION_TOKEN;
  const RESEND_API_KEY = process.env.RESEND_API_KEY;

  let body;
  try { body = JSON.parse(event.body); }
  catch (e) { return { statusCode: 400, body: JSON.stringify({ error: 'Invalid JSON' }) }; }

  const {
    pageId, quoteRef, signerName, signatureDataUrl,
    timestamp, projectTitle, total, clientCompany,
    productionDate, validUntil,
  } = body;

  const notionHeaders = {
    'Authorization': `Bearer ${NOTION_TOKEN}`,
    'Notion-Version': '2022-06-28',
    'Content-Type': 'application/json',
  };

  // ── 1. Update Notion ──────────────────────────────
  if (NOTION_TOKEN) {
    try {
      await fetch(`https://api.notion.com/v1/pages/${pageId}`, {
        method: 'PATCH',
        headers: notionHeaders,
        body: JSON.stringify({
          properties: {
            'Signed By':        { rich_text: [{ text: { content: signerName } }] },
            'Signed At':        { date: { start: timestamp } },
            'Financial Status': { select: { name: 'Signed' } },
          },
        }),
      });
    } catch (e) {
      console.error('Notion update failed:', e.message);
    }
  }

  // ── 2. Send confirmation email via Resend ─────────
  if (RESEND_API_KEY) {
    try {
      const signedDate = new Date(timestamp).toLocaleDateString('en-GB', {
        day: 'numeric', month: 'long', year: 'numeric',
      });
      const clientLine = signerName + (clientCompany ? `, ${clientCompany}` : '');

      await fetch('https://api.resend.com/emails', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${RESEND_API_KEY}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          from: 'Valley Films <onboarding@resend.dev>',
          to:   ['max@valley.film'],
          subject: `Quote Accepted: ${quoteRef} — ${signerName}`,
          html: `
            <div style="font-family:Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;color:#111;">
              <div style="background:#1A56FF;padding:28px 32px;border-radius:12px 12px 0 0;">
                <p style="color:white;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:2px;margin:0 0 6px;">Valley Films</p>
                <h1 style="color:white;margin:0;font-size:24px;font-weight:900;letter-spacing:-0.5px;">Quote Accepted</h1>
              </div>
              <div style="border:1px solid #e4e4e4;border-top:none;padding:28px 32px;border-radius:0 0 12px 12px;">
                <p style="font-size:14px;color:#555;margin:0 0 24px;">
                  <strong style="color:#111;">${quoteRef}</strong> has been accepted and signed.
                </p>
                <table style="width:100%;border-collapse:collapse;">
                  <tr style="border-bottom:1px solid #f0f0f0;">
                    <td style="padding:10px 0;color:#999;font-size:13px;width:140px;">Signed by</td>
                    <td style="padding:10px 0;font-size:13px;font-weight:600;">${clientLine}</td>
                  </tr>
                  <tr style="border-bottom:1px solid #f0f0f0;">
                    <td style="padding:10px 0;color:#999;font-size:13px;">Project</td>
                    <td style="padding:10px 0;font-size:13px;">${projectTitle || 'TBC'}</td>
                  </tr>
                  <tr style="border-bottom:1px solid #f0f0f0;">
                    <td style="padding:10px 0;color:#999;font-size:13px;">Production</td>
                    <td style="padding:10px 0;font-size:13px;">${productionDate || 'TBC'}</td>
                  </tr>
                  <tr style="border-bottom:1px solid #f0f0f0;">
                    <td style="padding:10px 0;color:#999;font-size:13px;">Total</td>
                    <td style="padding:10px 0;font-size:13px;font-weight:700;">${total || 'TBC'}</td>
                  </tr>
                  <tr>
                    <td style="padding:10px 0;color:#999;font-size:13px;">Date signed</td>
                    <td style="padding:10px 0;font-size:13px;">${signedDate}</td>
                  </tr>
                </table>
                <p style="font-size:12px;color:#bbb;margin:24px 0 0;border-top:1px solid #f0f0f0;padding-top:16px;">
                  Automated notification from the Valley Films quote system.
                </p>
              </div>
            </div>
          `,
        }),
      });
    } catch (e) {
      console.error('Email send failed:', e.message);
    }
  }

  return {
    statusCode: 200,
    headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
    body: JSON.stringify({ success: true }),
  };
};
