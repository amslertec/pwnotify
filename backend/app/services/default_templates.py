"""Standard-E-Mail-Templates (DE/EN). Im UI überschreibbar, per Reset wiederherstellbar.

Platzhalter (Jinja2): displayName, upn, daysLeft, expiryDate, resetUrl,
companyName, logoUrl, primaryColor, appName.

``logoUrl`` ist beim echten Versand ``cid:pwnotify-logo`` (eingebettetes Inline-Bild,
funktioniert ohne Netzwerkzugriff); in der UI-Vorschau eine normale http-URL.
"""

from __future__ import annotations

DEFAULT_SUBJECT_DE = "Ihr Passwort läuft in {{ daysLeft }} Tag(en) ab"
DEFAULT_SUBJECT_EN = "Your password expires in {{ daysLeft }} day(s)"

_HTML = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{{{ appName }}}}</title>
<style>
  body {{ margin:0; padding:0; background:#eef1f6; }}
  .wrap {{ width:100%; background:#eef1f6; padding:24px 0; }}
  .card {{ max-width:560px; margin:0 auto; background:#ffffff; border-radius:16px;
           overflow:hidden; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
           box-shadow:0 1px 3px rgba(15,23,42,.08); }}
  .head {{ padding:26px 32px 0; }}
  .logo {{ height:28px; width:auto; border:0; display:block; }}
  .body {{ padding:6px 32px 8px; color:#0f172a; font-size:15px; line-height:1.6; }}
  h1 {{ font-size:20px; margin:6px 0 4px; color:#0f172a; }}
  .cta {{ display:inline-block; margin:20px 0 4px; padding:12px 22px; border-radius:10px;
          background:{{{{ primaryColor }}}}; color:#ffffff !important; text-decoration:none; font-weight:600; }}
  .meta {{ margin-top:18px; font-size:13px; color:#64748b; }}
  .foot {{ padding:18px 32px 28px; font-size:12px; color:#94a3b8; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="head">
      <img class="logo" src="{{{{ logoUrl }}}}" alt="{{{{ appName }}}}">
    </div>
    <div class="body">
      <h1>{hello} {{{{ displayName }}}},</h1>
      <p>{intro1}</p>
      <p><strong>{expiry_label}:</strong> {{{{ expiryDate }}}}<br>
         <strong>{account_label}:</strong> {{{{ upn }}}}</p>
      <p>{intro2}</p>
      {{% if resetUrl %}}<a class="cta" href="{{{{ resetUrl }}}}">{cta}</a>{{% endif %}}
      <p class="meta">{help}</p>
    </div>
    <div class="foot">{{{{ companyName or appName }}}} · {footer}</div>
  </div>
</div>
</body>
</html>"""

DEFAULT_HTML_DE = _HTML.format(
    lang="de",
    hello="Hallo",
    intro1="Ihr Kennwort läuft in <strong>{{ daysLeft }}</strong> Tag(en) ab. "
    "Bitte ändern Sie es rechtzeitig, um eine Unterbrechung Ihres Zugangs zu vermeiden.",
    expiry_label="Ablaufdatum",
    account_label="Konto",
    intro2="Sie können Ihr Passwort direkt über die folgende Schaltfläche zurücksetzen.",
    cta="Passwort jetzt ändern",
    help="Wenn Sie Ihr Passwort bereits geändert haben, können Sie diese E-Mail ignorieren.",
    footer="Automatische Benachrichtigung – bitte nicht antworten.",
)

DEFAULT_HTML_EN = _HTML.format(
    lang="en",
    hello="Hello",
    intro1="Your password will expire in <strong>{{ daysLeft }}</strong> day(s). "
    "Please change it in time to avoid losing access.",
    expiry_label="Expiry date",
    account_label="Account",
    intro2="You can reset your password directly using the button below.",
    cta="Change password now",
    help="If you have already changed your password, you can ignore this email.",
    footer="Automated notification – please do not reply.",
)

DEFAULT_TEXT_DE = (
    "Hallo {{ displayName }},\n\n"
    "Ihr Kennwort läuft in {{ daysLeft }} Tag(en) ab (am {{ expiryDate }}).\n"
    "Konto: {{ upn }}\n\n"
    "Bitte ändern Sie Ihr Passwort: {{ resetUrl }}\n\n"
    "{{ companyName or appName }}"
)
DEFAULT_TEXT_EN = (
    "Hello {{ displayName }},\n\n"
    "Your password expires in {{ daysLeft }} day(s) (on {{ expiryDate }}).\n"
    "Account: {{ upn }}\n\n"
    "Please change your password: {{ resetUrl }}\n\n"
    "{{ companyName or appName }}"
)
