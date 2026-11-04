"""
email_policy.py -- accept company email addresses only.

Rejects free webmail (Gmail, Yahoo, Outlook...), ISP mailboxes (BigPond, BT, Comcast...),
disposable inboxes (Mailinator, 10MinuteMail...) and obvious placeholders (example.com).

This is a QUALITY FILTER for a B2B demo, not verification. A plausible-looking company
domain can still be invented; proving the address exists is the email-OTP step's job.

It lives in its own module so there is exactly one copy of the rule. The page asks
/api/email/check for early feedback; /api/otp/send enforces the same function.

Tunable without a code change (restart after editing .env):
  EMAIL_REQUIRE_COMPANY=1            0 switches the whole check off
  EMAIL_ALLOW_DOMAINS=acme.io,...    exceptions: a real prospect caught by a rule below
  EMAIL_BLOCK_DOMAINS=foo.com,...    extra domains to reject on top of the built-in list
"""

import os
import re


def _csv_env(name: str) -> frozenset:
    return frozenset(d.strip().lower().lstrip("@").rstrip(".")
                     for d in os.getenv(name, "").split(",") if d.strip())


EMAIL_REQUIRE_COMPANY = os.getenv("EMAIL_REQUIRE_COMPANY", "1").strip().lower() not in (
    "0", "false", "no", "off")
ALLOW_DOMAINS = _csv_env("EMAIL_ALLOW_DOMAINS")
EXTRA_BLOCK_DOMAINS = _csv_env("EMAIL_BLOCK_DOMAINS")

MSG_FREE = ("Please use your company email. Personal addresses like Gmail, Yahoo or "
            "Outlook aren't accepted.")
MSG_DISPOSABLE = ("Temporary email addresses aren't accepted. Please use your company "
                  "email.")
MSG_PLACEHOLDER = "Please enter your real company email address."

# Personal mailboxes. Grouped by provider so a missing one is easy to spot and add.
FREE_DOMAINS = frozenset("""
    gmail.com googlemail.com
    yahoo.com ymail.com rocketmail.com yahoo.co.in yahoo.co.uk yahoo.com.au
    hotmail.com hotmail.co.uk outlook.com outlook.in outlook.co.uk outlook.com.au
    live.com live.in live.co.uk live.com.au live.ca live.fr live.de live.nl live.it
    msn.com passport.com windowslive.com
    icloud.com me.com mac.com
    aol.com aol.co.uk aim.com verizon.net
    protonmail.com protonmail.ch proton.me pm.me
    zoho.com zohomail.com zohomail.in
    gmx.com gmx.net gmx.de gmx.at gmx.ch gmx.co.uk web.de t-online.de freenet.de
    yandex.com yandex.ru ya.ru mail.ru inbox.ru list.ru bk.ru rambler.ru
    qq.com 163.com 126.com yeah.net sina.com sina.cn sohu.com aliyun.com foxmail.com
    naver.com daum.net hanmail.net
    rediffmail.com rediff.com sify.com indiatimes.com bsnl.in vsnl.net
    mail.com email.com usa.com post.com inbox.com hushmail.com fastmail.com fastmail.fm
    tutanota.com tuta.io hey.com lycos.com excite.com
    orange.fr wanadoo.fr free.fr laposte.net sfr.fr libero.it virgilio.it tiscali.it
    alice.it seznam.cz wp.pl o2.pl interia.pl onet.pl
    btinternet.com btopenworld.com sky.com talktalk.net virginmedia.com
    blueyonder.co.uk ntlworld.com tiscali.co.uk
    bigpond.com bigpond.net.au optusnet.com.au iinet.net.au tpg.com.au
    internode.on.net dodo.com.au westnet.com.au xtra.co.nz
    comcast.net att.net sbcglobal.net bellsouth.net cox.net charter.net earthlink.net
    juno.com optonline.net rogers.com shaw.ca sympatico.ca
    bol.com.br uol.com.br terra.com.br ig.com.br
""".split())

# Providers with a mailbox on dozens of country TLDs. Matched as the registrable name so
# yahoo.fr, hotmail.de and outlook.com.au are caught without listing every country --
# but outlook.acme.com (a company subdomain) is not, because "acme.com" isn't a TLD.
_BRAND_RE = re.compile(
    r"^(?:gmail|googlemail|yahoo|ymail|rocketmail|hotmail|outlook|aol|icloud|gmx|yandex|"
    r"protonmail|rediffmail|zohomail)\.(?:[a-z]{2,6}|[a-z]{2,3}\.[a-z]{2})$")

DISPOSABLE_DOMAINS = frozenset("""
    mailinator.com guerrillamail.com guerrillamail.net guerrillamailblock.com
    sharklasers.com grr.la 10minutemail.com 10minutemail.net temp-mail.org tempmail.com
    tempmail.net tempmailo.com yopmail.com yopmail.fr trashmail.com getnada.com
    nada.email maildrop.cc dispostable.com throwawaymail.com fakeinbox.com
    mintemail.com mohmal.com emailondeck.com tempr.email discard.email mailnesia.com
    spamgourmet.com 33mail.com burnermail.io moakt.com emailfake.com crazymailing.com
    tmail.ws mail.tm 1secmail.com 1secmail.net mailcatch.com spambox.us jetable.org
    mytemp.email tempinbox.com dropmail.me minuteinbox.com inboxkitten.com mail7.io
""".split())

# Disposable services spin up new domains constantly; these fragments catch the families.
_DISPOSABLE_MARKERS = (
    "mailinator", "guerrillamail", "10minutemail", "tempmail", "temp-mail",
    "throwawaymail", "trashmail", "yopmail", "fakeinbox", "disposable", "burnermail",
    "minutemail", "tempinbox", "spambox", "mytemp", "tempemail",
)

PLACEHOLDER_DOMAINS = frozenset("""
    example.com example.org example.net test.com testing.com domain.com company.com
    yourcompany.com mycompany.com companyname.com yourdomain.com mydomain.com
    website.com sample.com
""".split())

# RFC 2606 / 6761 reserved names -- never a real mailbox.
_RESERVED_RE = re.compile(r"\.(?:test|example|invalid|localhost|local)$")


def _domain(email: str) -> str:
    return str(email or "").rsplit("@", 1)[-1].strip().lower().rstrip(".")


def _candidates(domain: str) -> list:
    """sales.acme.co.uk -> [sales.acme.co.uk, acme.co.uk, co.uk]. Lets a rule for a
    parent domain cover its subdomains without ever matching a bare TLD."""
    parts = domain.split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)]


def classify(email: str):
    """'free' | 'disposable' | 'placeholder' | None. Ignores the on/off switch, so it
    can be used for reporting even when enforcement is disabled."""
    domain = _domain(email)
    if not domain or "." not in domain:
        return None                       # format validation is the caller's job
    cands = _candidates(domain)
    if any(c in ALLOW_DOMAINS for c in cands):
        return None
    if any(c in PLACEHOLDER_DOMAINS for c in cands) or _RESERVED_RE.search(domain):
        return "placeholder"
    if (any(c in DISPOSABLE_DOMAINS for c in cands)
            or any(m in domain for m in _DISPOSABLE_MARKERS)):
        return "disposable"
    if (any(c in FREE_DOMAINS or c in EXTRA_BLOCK_DOMAINS for c in cands)
            or any(_BRAND_RE.match(c) for c in cands)):
        return "free"
    return None


def check_company_email(email: str):
    """None if the address is acceptable, otherwise the sentence to show the visitor."""
    if not EMAIL_REQUIRE_COMPANY:
        return None
    return {"free": MSG_FREE, "disposable": MSG_DISPOSABLE,
            "placeholder": MSG_PLACEHOLDER}.get(classify(email))
