"""
countries.py — every country's dialling code, and one forgiving phone normaliser.

WHY THIS EXISTS
  The signup form asks for a WhatsApp number, and a wrong number means a code that
  never arrives and a prospect who gives up. People type numbers in every shape there
  is: with the country code, without it, with a leading 0, with the code twice, with
  spaces, brackets and dashes, as 00 instead of +.

  So the page and the server share ONE list and ONE normaliser. The page uses the list
  to offer a searchable country picker (all of them, not the fifteen we thought of);
  the server uses it to work out what the visitor meant before it spends a WhatsApp
  message on it.

  The page is not trusted to do this: /api/otp/send re-normalises whatever arrives,
  from `cc` + `phone` when the page sends both, or from `phone` alone when it doesn't
  (an older cached page, or anything else posting to the endpoint).

WHAT "FUZZY" MEANS HERE — and what it deliberately does NOT mean
  fuzzy_phone() fixes SHAPE, never digits. It will drop a trunk 0, un-duplicate a
  country code, turn 00 into +, and prefer a country code the number itself carries
  over the one left in the picker. It never invents, drops or reorders the subscriber
  digits, and when the result isn't a plausible E.164 number it says so instead of
  guessing — the visitor gets the field back, which is cheaper than a code sent into
  the void.

  Length is checked against E.164 (8-15 digits including the country code) rather than
  per-country rules: national lengths change, and being wrong about one would block a
  real customer. Only the shape is our business; the provider decides if it's live.
"""

import re
import unicodedata

# (ISO 3166-1 alpha-2, name, dialling code). Territories that share a code with their
# parent (+1 for the NANP, +44 for the Crown dependencies, +590 for the French
# Antilles) are listed separately so they can be FOUND in the picker by name — dialling
# is identical, which is all this file cares about.
COUNTRIES = [
    ("AF", "Afghanistan", "93"),            ("AX", "Åland Islands", "358"),
    ("AL", "Albania", "355"),               ("DZ", "Algeria", "213"),
    ("AS", "American Samoa", "1"),          ("AD", "Andorra", "376"),
    ("AO", "Angola", "244"),                ("AI", "Anguilla", "1"),
    ("AG", "Antigua and Barbuda", "1"),     ("AR", "Argentina", "54"),
    ("AM", "Armenia", "374"),               ("AW", "Aruba", "297"),
    ("AU", "Australia", "61"),              ("AT", "Austria", "43"),
    ("AZ", "Azerbaijan", "994"),            ("BS", "Bahamas", "1"),
    ("BH", "Bahrain", "973"),               ("BD", "Bangladesh", "880"),
    ("BB", "Barbados", "1"),                ("BY", "Belarus", "375"),
    ("BE", "Belgium", "32"),                ("BZ", "Belize", "501"),
    ("BJ", "Benin", "229"),                 ("BM", "Bermuda", "1"),
    ("BT", "Bhutan", "975"),                ("BO", "Bolivia", "591"),
    ("BA", "Bosnia and Herzegovina", "387"), ("BW", "Botswana", "267"),
    ("BR", "Brazil", "55"),                 ("IO", "British Indian Ocean Territory", "246"),
    ("VG", "British Virgin Islands", "1"),  ("BN", "Brunei", "673"),
    ("BG", "Bulgaria", "359"),              ("BF", "Burkina Faso", "226"),
    ("BI", "Burundi", "257"),               ("KH", "Cambodia", "855"),
    ("CM", "Cameroon", "237"),              ("CA", "Canada", "1"),
    ("CV", "Cape Verde", "238"),            ("KY", "Cayman Islands", "1"),
    ("CF", "Central African Republic", "236"), ("TD", "Chad", "235"),
    ("CL", "Chile", "56"),                  ("CN", "China", "86"),
    ("CO", "Colombia", "57"),               ("KM", "Comoros", "269"),
    ("CG", "Congo - Brazzaville", "242"),   ("CD", "Congo - Kinshasa", "243"),
    ("CK", "Cook Islands", "682"),          ("CR", "Costa Rica", "506"),
    ("CI", "Côte d'Ivoire", "225"),         ("HR", "Croatia", "385"),
    ("CU", "Cuba", "53"),                   ("CW", "Curaçao", "599"),
    ("CY", "Cyprus", "357"),                ("CZ", "Czechia", "420"),
    ("DK", "Denmark", "45"),                ("DJ", "Djibouti", "253"),
    ("DM", "Dominica", "1"),                ("DO", "Dominican Republic", "1"),
    ("EC", "Ecuador", "593"),               ("EG", "Egypt", "20"),
    ("SV", "El Salvador", "503"),           ("GQ", "Equatorial Guinea", "240"),
    ("ER", "Eritrea", "291"),               ("EE", "Estonia", "372"),
    ("SZ", "Eswatini", "268"),              ("ET", "Ethiopia", "251"),
    ("FK", "Falkland Islands", "500"),      ("FO", "Faroe Islands", "298"),
    ("FJ", "Fiji", "679"),                  ("FI", "Finland", "358"),
    ("FR", "France", "33"),                 ("GF", "French Guiana", "594"),
    ("PF", "French Polynesia", "689"),      ("GA", "Gabon", "241"),
    ("GM", "Gambia", "220"),                ("GE", "Georgia", "995"),
    ("DE", "Germany", "49"),                ("GH", "Ghana", "233"),
    ("GI", "Gibraltar", "350"),             ("GR", "Greece", "30"),
    ("GL", "Greenland", "299"),             ("GD", "Grenada", "1"),
    ("GP", "Guadeloupe", "590"),            ("GU", "Guam", "1"),
    ("GT", "Guatemala", "502"),             ("GG", "Guernsey", "44"),
    ("GN", "Guinea", "224"),                ("GW", "Guinea-Bissau", "245"),
    ("GY", "Guyana", "592"),                ("HT", "Haiti", "509"),
    ("HN", "Honduras", "504"),              ("HK", "Hong Kong SAR China", "852"),
    ("HU", "Hungary", "36"),                ("IS", "Iceland", "354"),
    ("IN", "India", "91"),                  ("ID", "Indonesia", "62"),
    ("IR", "Iran", "98"),                   ("IQ", "Iraq", "964"),
    ("IE", "Ireland", "353"),               ("IM", "Isle of Man", "44"),
    ("IL", "Israel", "972"),                ("IT", "Italy", "39"),
    ("JM", "Jamaica", "1"),                 ("JP", "Japan", "81"),
    ("JE", "Jersey", "44"),                 ("JO", "Jordan", "962"),
    ("KZ", "Kazakhstan", "7"),              ("KE", "Kenya", "254"),
    ("KI", "Kiribati", "686"),              ("KW", "Kuwait", "965"),
    ("KG", "Kyrgyzstan", "996"),            ("LA", "Laos", "856"),
    ("LV", "Latvia", "371"),                ("LB", "Lebanon", "961"),
    ("LS", "Lesotho", "266"),               ("LR", "Liberia", "231"),
    ("LY", "Libya", "218"),                 ("LI", "Liechtenstein", "423"),
    ("LT", "Lithuania", "370"),             ("LU", "Luxembourg", "352"),
    ("MO", "Macao SAR China", "853"),       ("MG", "Madagascar", "261"),
    ("MW", "Malawi", "265"),                ("MY", "Malaysia", "60"),
    ("MV", "Maldives", "960"),              ("ML", "Mali", "223"),
    ("MT", "Malta", "356"),                 ("MH", "Marshall Islands", "692"),
    ("MQ", "Martinique", "596"),            ("MR", "Mauritania", "222"),
    ("MU", "Mauritius", "230"),             ("YT", "Mayotte", "262"),
    ("MX", "Mexico", "52"),                 ("FM", "Micronesia", "691"),
    ("MD", "Moldova", "373"),               ("MC", "Monaco", "377"),
    ("MN", "Mongolia", "976"),              ("ME", "Montenegro", "382"),
    ("MS", "Montserrat", "1"),              ("MA", "Morocco", "212"),
    ("MZ", "Mozambique", "258"),            ("MM", "Myanmar", "95"),
    ("NA", "Namibia", "264"),               ("NR", "Nauru", "674"),
    ("NP", "Nepal", "977"),                 ("NL", "Netherlands", "31"),
    ("NC", "New Caledonia", "687"),         ("NZ", "New Zealand", "64"),
    ("NI", "Nicaragua", "505"),             ("NE", "Niger", "227"),
    ("NG", "Nigeria", "234"),               ("NU", "Niue", "683"),
    ("NF", "Norfolk Island", "672"),        ("KP", "North Korea", "850"),
    ("MK", "North Macedonia", "389"),       ("MP", "Northern Mariana Islands", "1"),
    ("NO", "Norway", "47"),                 ("OM", "Oman", "968"),
    ("PK", "Pakistan", "92"),               ("PW", "Palau", "680"),
    ("PS", "Palestinian Territories", "970"), ("PA", "Panama", "507"),
    ("PG", "Papua New Guinea", "675"),      ("PY", "Paraguay", "595"),
    ("PE", "Peru", "51"),                   ("PH", "Philippines", "63"),
    ("PL", "Poland", "48"),                 ("PT", "Portugal", "351"),
    ("PR", "Puerto Rico", "1"),             ("QA", "Qatar", "974"),
    ("RE", "Réunion", "262"),               ("RO", "Romania", "40"),
    ("RU", "Russia", "7"),                  ("RW", "Rwanda", "250"),
    ("BL", "Saint Barthélemy", "590"),      ("SH", "Saint Helena", "290"),
    ("KN", "Saint Kitts and Nevis", "1"),   ("LC", "Saint Lucia", "1"),
    ("MF", "Saint Martin", "590"),          ("PM", "Saint Pierre and Miquelon", "508"),
    ("VC", "Saint Vincent and the Grenadines", "1"), ("WS", "Samoa", "685"),
    ("SM", "San Marino", "378"),            ("ST", "São Tomé and Príncipe", "239"),
    ("SA", "Saudi Arabia", "966"),          ("SN", "Senegal", "221"),
    ("RS", "Serbia", "381"),                ("SC", "Seychelles", "248"),
    ("SL", "Sierra Leone", "232"),          ("SG", "Singapore", "65"),
    ("SX", "Sint Maarten", "1"),            ("SK", "Slovakia", "421"),
    ("SI", "Slovenia", "386"),              ("SB", "Solomon Islands", "677"),
    ("SO", "Somalia", "252"),               ("ZA", "South Africa", "27"),
    ("KR", "South Korea", "82"),            ("SS", "South Sudan", "211"),
    ("ES", "Spain", "34"),                  ("LK", "Sri Lanka", "94"),
    ("SD", "Sudan", "249"),                 ("SR", "Suriname", "597"),
    ("SE", "Sweden", "46"),                 ("CH", "Switzerland", "41"),
    ("SY", "Syria", "963"),                 ("TW", "Taiwan", "886"),
    ("TJ", "Tajikistan", "992"),            ("TZ", "Tanzania", "255"),
    ("TH", "Thailand", "66"),               ("TL", "Timor-Leste", "670"),
    ("TG", "Togo", "228"),                  ("TK", "Tokelau", "690"),
    ("TO", "Tonga", "676"),                 ("TT", "Trinidad and Tobago", "1"),
    ("TN", "Tunisia", "216"),               ("TR", "Türkiye", "90"),
    ("TM", "Turkmenistan", "993"),          ("TC", "Turks and Caicos Islands", "1"),
    ("TV", "Tuvalu", "688"),                ("UG", "Uganda", "256"),
    ("UA", "Ukraine", "380"),               ("AE", "United Arab Emirates", "971"),
    ("GB", "United Kingdom", "44"),         ("US", "United States", "1"),
    ("UY", "Uruguay", "598"),               ("UZ", "Uzbekistan", "998"),
    ("VU", "Vanuatu", "678"),               ("VA", "Vatican City", "39"),
    ("VE", "Venezuela", "58"),              ("VN", "Vietnam", "84"),
    ("WF", "Wallis and Futuna", "681"),     ("EH", "Western Sahara", "212"),
    ("YE", "Yemen", "967"),                 ("ZM", "Zambia", "260"),
    ("ZW", "Zimbabwe", "263"),
]

# Shown first in the picker, in this order: where our prospects actually are. Everything
# else follows alphabetically. Purely presentational — any country can be chosen.
POPULAR = ("IN", "AE", "SA", "QA", "KW", "OM", "BH", "GB", "US", "AU", "NZ", "SG",
           "MY", "ZA", "NG", "KE", "CA", "DE")

DEFAULT_ISO = "IN"

_BY_ISO = {iso: (iso, name, dial) for iso, name, dial in COUNTRIES}
# Longest dialling code first, so +1 never shadows +1-something and +44 is tried before
# +4. (There is no overlap in E.164 today; ordering makes that not matter.)
_DIALS = sorted({dial for _, _, dial in COUNTRIES}, key=len, reverse=True)
# One representative country per dial code, for reporting which country we landed on.
_DIAL_ISO = {}
for _iso, _name, _dial in COUNTRIES:
    _DIAL_ISO.setdefault(_dial, _iso)
for _iso in ("US", "GB", "IN"):                 # the obvious answer for a shared code
    _DIAL_ISO[_BY_ISO[_iso][2]] = _iso

E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def dial_for(iso: str) -> str:
    row = _BY_ISO.get((iso or "").strip().upper())
    return row[2] if row else ""


def country_for(e164: str) -> str:
    """ISO code for a normalised number, or '' — the country code alone is ambiguous
    for shared codes (+1, +44, +7), so this returns the representative country."""
    digits = (e164 or "").lstrip("+")
    for dial in _DIALS:
        if digits.startswith(dial):
            return _DIAL_ISO.get(dial, "")
    return ""


def _sortable(name: str) -> str:
    """Åland sorts with A, not after Z: ASCII order would exile every accented name
    to the bottom of the list."""
    return "".join(c for c in unicodedata.normalize("NFKD", name)
                   if not unicodedata.combining(c)).lower()


def public_list() -> list[dict]:
    """What the page's picker renders: popular countries first, then A-Z."""
    rank = {iso: i for i, iso in enumerate(POPULAR)}
    rows = sorted(COUNTRIES, key=lambda r: (rank.get(r[0], len(POPULAR)), _sortable(r[1])))
    return [{"iso": iso, "name": name, "dial": "+" + dial} for iso, name, dial in rows]


def _digits(raw) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def fuzzy_phone(raw, cc: str = "", iso: str = "") -> tuple[str, str]:
    """Best-effort E.164 from what a human typed. Returns (e164, problem).

    `raw` is the number as typed (national or international), `cc` the picker's dialling
    code ('+91' or '91'), `iso` its country when the page sends one. On success problem
    is ''; on failure e164 is '' and problem is a sentence for the field.

    The order below is the whole design: anything the NUMBER says about its country
    beats anything the PICKER says, because the picker is a default the visitor may
    never have touched, while the digits are what they typed on purpose.
    """
    s = str(raw or "").strip()
    picked = _digits(cc) or _digits(dial_for(iso))
    had_plus = s.startswith("+") or s.startswith("00")
    digits = _digits(s)

    if not digits:
        return "", "Enter your mobile number."
    if len(digits) > 20:
        return "", "That number is too long. Check it and try again."

    if s.startswith("00"):                      # 0044… — the international prefix
        digits = digits[2:]
        had_plus = True

    if had_plus:
        # They typed the country code themselves. Trust it, and ignore the picker.
        candidate = digits
    elif picked:
        # Drop the national trunk prefix first — 07700…, 04…, (044) — so the country-code
        # test below sees the same digits whether or not they typed one.
        national = digits.lstrip("0")
        if national.startswith(picked) and len(national) - len(picked) >= 6:
            # Typed the country code without a +, e.g. picker +91 and "919876543210".
            # The length guard keeps a coincidence (a number that merely starts with the
            # same digits) from losing its first digits.
            candidate = national
        else:
            candidate = picked + national
    else:
        # No country code in the number and none from the picker. Guessing here is how a
        # visitor gets a code sent to a stranger in another country.
        return "", "Pick your country, or type the number with its country code."

    # "+91 91 98765 43210": the code once from the picker, once from the paste.
    for dial in _DIALS:
        if candidate.startswith(dial + dial) and len(candidate) - len(dial) >= 8:
            candidate = candidate[len(dial):]
            break

    e164 = "+" + candidate.lstrip("0")
    if not E164.match(e164):
        if len(candidate) < 8:
            return "", "That number looks too short. Include the country code."
        return "", "Enter a valid mobile number, including the country code."
    if not country_for(e164):
        return "", "We don't recognise that country code. Pick your country from the list."
    return e164, ""
