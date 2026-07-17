"""Standard-E-Mail-Templates (DE/EN). Im UI überschreibbar, per Reset wiederherstellbar.

Platzhalter (Jinja2): displayName, upn, daysLeft, expiryDate, resetUrl,
companyName, logoUrl, primaryColor, appName.

``logoUrl`` ist beim echten Versand ``cid:pwnotify-logo`` (eingebettetes Inline-Bild,
funktioniert ohne Netzwerkzugriff); in der UI-Vorschau eine normale http-URL.
"""

from __future__ import annotations

DEFAULT_SUBJECT_DE = "Dein Passwort läuft in {{ daysLeft }} Tag(en) ab"
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
    intro1="Dein Kennwort läuft in <strong>{{ daysLeft }}</strong> Tag(en) ab. "
    "Bitte ändere es rechtzeitig, um eine Unterbrechung deines Zugangs zu vermeiden.",
    expiry_label="Ablaufdatum",
    account_label="Konto",
    intro2="Du kannst dein Passwort direkt über die folgende Schaltfläche zurücksetzen.",
    cta="Passwort jetzt ändern",
    help="Wenn du dein Passwort bereits geändert hast, kannst du diese E-Mail ignorieren.",
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
    "Dein Kennwort läuft in {{ daysLeft }} Tag(en) ab (am {{ expiryDate }}).\n"
    "Konto: {{ upn }}\n\n"
    "Bitte ändere dein Passwort: {{ resetUrl }}\n\n"
    "{{ companyName or appName }}"
)
DEFAULT_TEXT_EN = (
    "Hello {{ displayName }},\n\n"
    "Your password expires in {{ daysLeft }} day(s) (on {{ expiryDate }}).\n"
    "Account: {{ upn }}\n\n"
    "Please change your password: {{ resetUrl }}\n\n"
    "{{ companyName or appName }}"
)


# --------------------------------------------------------------------------------------- #
# Einladung + Passwort-Reset (Console+Groups+Invite Task 5, §7b/§7c).
#
# Platzhalter: email, inviteUrl/resetUrl, companyName, logoUrl, primaryColor, appName --
# bewusst KEIN displayName/upn/daysLeft/expiryDate (vor der Einladungs-Annahme existiert
# noch kein Anzeigename, ein Reset trägt keine Ablaufinformation). Derselbe `.card`-Look
# (Logo, `primaryColor`-CTA, `companyName`-Fusszeile) wie oben, nur ohne den
# Ablauf-Infoblock -- eigener, schlankerer Shell-String statt Wiederverwendung von `_HTML`
# (dessen Platzhalter fest an daysLeft/expiryDate/upn gebunden sind).
# --------------------------------------------------------------------------------------- #
_HTML_ACTION = """<!doctype html>
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
      <h1>{hello}</h1>
      <p>{intro}</p>
      <a class="cta" href="{{{{ {url_key} }}}}">{cta}</a>
      <p class="meta">{help}</p>
    </div>
    <div class="foot">{{{{ companyName or appName }}}} · {footer}</div>
  </div>
</div>
</body>
</html>"""

DEFAULT_SUBJECT_INVITE_DE = "Einladung zu {{ appName }}"
DEFAULT_SUBJECT_INVITE_EN = "You're invited to {{ appName }}"

DEFAULT_HTML_INVITE_DE = _HTML_ACTION.format(
    lang="de",
    url_key="inviteUrl",
    hello="Du wurdest eingeladen",
    intro="Für dich wurde ein Konto bei <strong>{{ appName }}</strong> angelegt. Wähle über "
    "die folgende Schaltfläche deinen Namen, deinen Benutzernamen und ein Passwort, um "
    "dein Konto zu aktivieren.",
    cta="Konto jetzt einrichten",
    help="Dieser Link ist 7 Tage gültig und nur einmal verwendbar. Hast du diese Einladung "
    "nicht erwartet, kannst du diese E-Mail einfach ignorieren.",
    footer="Automatische Benachrichtigung – bitte nicht antworten.",
)

DEFAULT_HTML_INVITE_EN = _HTML_ACTION.format(
    lang="en",
    url_key="inviteUrl",
    hello="You have been invited",
    intro="An account at <strong>{{ appName }}</strong> has been created for you. Use the "
    "button below to choose your name, username and password to activate your account.",
    cta="Set up account now",
    help="This link is valid for 7 days and can only be used once. If you did not expect "
    "this invitation, you can safely ignore this email.",
    footer="Automated notification – please do not reply.",
)

DEFAULT_TEXT_INVITE_DE = (
    "Du wurdest zu {{ appName }} eingeladen.\n\n"
    "Wähle über den folgenden Link deinen Namen, Benutzernamen und Passwort: "
    "{{ inviteUrl }}\n\n"
    "Der Link ist 7 Tage gültig und nur einmal verwendbar.\n\n"
    "{{ companyName or appName }}"
)
DEFAULT_TEXT_INVITE_EN = (
    "You have been invited to {{ appName }}.\n\n"
    "Use the following link to choose your name, username and password: "
    "{{ inviteUrl }}\n\n"
    "The link is valid for 7 days and can only be used once.\n\n"
    "{{ companyName or appName }}"
)

DEFAULT_SUBJECT_RESET_DE = "Passwort zurücksetzen für {{ appName }}"
DEFAULT_SUBJECT_RESET_EN = "Reset your {{ appName }} password"

DEFAULT_HTML_RESET_DE = _HTML_ACTION.format(
    lang="de",
    url_key="resetUrl",
    hello="Passwort zurücksetzen",
    intro="Für dein Konto bei <strong>{{ appName }}</strong> wurde ein Zurücksetzen des "
    "Passworts angefordert. Über die folgende Schaltfläche vergibst du ein neues Passwort.",
    cta="Neues Passwort vergeben",
    help="Dieser Link ist 1 Stunde gültig und nur einmal verwendbar. Hast du das nicht "
    "angefordert, kannst du diese E-Mail ignorieren -- dein Passwort bleibt unverändert.",
    footer="Automatische Benachrichtigung – bitte nicht antworten.",
)

DEFAULT_HTML_RESET_EN = _HTML_ACTION.format(
    lang="en",
    url_key="resetUrl",
    hello="Reset your password",
    intro="A password reset was requested for your account at <strong>{{ appName }}</strong>. "
    "Use the button below to set a new password.",
    cta="Set new password",
    help="This link is valid for 1 hour and can only be used once. If you did not request "
    "this, you can ignore this email -- your password stays unchanged.",
    footer="Automated notification – please do not reply.",
)

DEFAULT_TEXT_RESET_DE = (
    "Für dein Konto bei {{ appName }} wurde ein Zurücksetzen des Passworts angefordert.\n\n"
    "Neues Passwort vergeben: {{ resetUrl }}\n\n"
    "Der Link ist 1 Stunde gültig und nur einmal verwendbar. Hast du das nicht angefordert, "
    "kannst du diese E-Mail ignorieren.\n\n"
    "{{ companyName or appName }}"
)
DEFAULT_TEXT_RESET_EN = (
    "A password reset was requested for your account at {{ appName }}.\n\n"
    "Set a new password: {{ resetUrl }}\n\n"
    "The link is valid for 1 hour and can only be used once. If you did not request this, "
    "you can ignore this email.\n\n"
    "{{ companyName or appName }}"
)
