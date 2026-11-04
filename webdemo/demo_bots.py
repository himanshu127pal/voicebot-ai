"""
demo_bots.py — the voicebots a visitor can pick on the web demo, and everything the
page shows about each one: the script to follow, the fields captured live, and what
each outcome means.

PRESENTATION ONLY. What a bot says and decides lives in its agent file, which the web
worker (agent_voicebot_web.py) runs unchanged. This catalog describes that bot for a
human watching. When a bot's script changes, re-read its `script` steps here — the field
keys are checked against the real agents by the test suite, the wording is not.

  key          must match BOTS in agent_voicebot_web.py
  script       steps the visitor follows. `agent` is what the bot asks (paraphrased —
               it's an LLM, it won't be word-for-word), `you` is a reply that keeps the
               call on the happy path. A step ticks off when its `fields` are captured,
               its `flag` is set ("consent" or "done"), or any LATER step does — so a
               field the bot heard but didn't save never leaves the tracker stuck.
  when         {field: value} — only shown once that field is known and contains value.
               `suggested: True` also shows it BEFORE the field is known (the path we
               recommend the visitor takes).
  fields       the live data panel, in call order. `required` marks what the handoff
               needs; `hideEmpty` hides a row until it has a value.
  outcomes     code -> (label, tone). tone: good | warn | bad | neutral.
  consent_chip [shown when given, shown before] labels for the panel's consent chip.
               Absent = the default wording. None = no chip (nothing is consented to).
  demo_attrs   optional callable -> {name: value}. Sent to the bot as dialer.<name>,
               standing in for data a real campaign would have (the movers bot's online
               inquiry). {name} placeholders in the persona and script are filled from it.
"""

import os
import logging
from datetime import date, timedelta

log = logging.getLogger("webdemo")

# Where each bot's .env lives. Read ONLY for AGENT_NAME and COMPANY_NAME, so the script
# preview says "Emma" when that's what the bot will actually call itself. The worker
# also reports both live once the call starts, so a wrong path only affects the preview.
# mortgage and energy are demo-only; their .env is optional.
BOT_ENV_DEFAULTS = {
    "smarthealth": "/opt/livekit/bot/.env",
    "roof":        "/opt/livekit/roof-bot/.env",
    "finance":     "/opt/livekit/finance-bot/.env",
    "movers":      "/opt/livekit/movers-bot/.env",
    "mortgage":    "/opt/livekit/web-demo/bots/mortgage.env",
    "energy":      "/opt/livekit/web-demo/bots/energy.env",
    "warranty":    "/opt/livekit/warranty-bot/.env",
}


def _next_saturday(min_days: int = 12) -> date:
    d = date.today() + timedelta(days=min_days)
    return d + timedelta(days=(5 - d.weekday()) % 7)


def _warranty_attrs() -> dict:
    """What the appliance call is about. A real campaign would have this from the
    retailer's data; the demo just names the commonest one."""
    return {"appliance": "washing machine"}


def _movers_attrs() -> dict:
    """The online inquiry the movers bot follows up. A date ~2 weeks out, always a
    Saturday, so the demo never confirms a move for yesterday."""
    d = _next_saturday()
    return {"pickup": "Parramatta", "dropoff": "Bondi Junction",
            "move_date": f"Saturday {d.day} {d.strftime('%B')}"}


BOT_CATALOG = {
    # ═══════════════════════════════════════════════════════════════════════════
    "smarthealth": {
        "name": "Health Insurance Savings",
        "market": "Australia",
        "voice": "Australian English",
        "goal": "Qualified advisor callback",
        "tagline": "Checks whether an existing policy holder could save, gathers the "
                   "details an advisor needs, and books the callback.",
        "persona": "You're an Australian with private health insurance, over 25, paying "
                   "around $400 a month for family hospital-and-extras cover.",
        "agent": "Sarah",
        "company": "Smart Health",
        "script": [
            {"agent": "Hi, this is {agent} calling from {company}. How are you today?",
             "you": "Good thanks. What's this about?"},
            {"agent": "It's a quick call about your private health insurance. You have a "
                      "policy, right?",
             "you": "Yes, I do.", "fields": ["holds_policy"]},
            {"agent": "It's an obligation-free check to see if you could save. Can I just "
                      "confirm you're over 25?",
             "you": "Yes, I'm 42.", "fields": ["over_min_age"]},
            {"agent": "Who are you currently with?",
             "you": "Medibank.", "fields": ["insurer"]},
            {"agent": "Does that cover just you, you and your partner, or the family?",
             "you": "The whole family.", "fields": ["cover_type"]},
            {"agent": "Roughly how much is the premium, and how often do you pay it?",
             "you": "About four hundred dollars a month.",
             "fields": ["premium_amount", "premium_frequency"],
             "tip": "Give both the amount and how often. If you leave one out, it asks."},
            {"agent": "Is that hospital only, or hospital and extras?",
             "you": "Hospital and extras.", "fields": ["hospital_extras"]},
            {"agent": "And you're a permanent Australian resident?",
             "you": "Yes, I am.", "fields": ["permanent_resident"]},
            {"agent": "Would you be looking to reduce your premium, or add more benefits?",
             "you": "Reduce the premium.", "fields": ["main_interest"]},
            {"agent": "Can I grab your first and last name?",
             "you": "Alex Taylor.", "fields": ["first_name"]},
            {"agent": "And the best postcode for you?",
             "you": "3121.", "fields": ["postcode"]},
            {"agent": "One of our advisors will give you a call. It's obligation-free. Is "
                      "that okay with you?",
             "you": "Yes, that's fine.", "flag": "consent"},
        ],
        "try": [
            {"say": "About fifty dollars a month.",
             "expect": "The premium check kicks in. It politely declines rather than "
                       "passing on a lead the advisors can't help."},
            {"say": "Are you a robot?",
             "expect": "It says yes, plainly, and carries on. It never claims to be human."},
            {"say": "I don't have private health insurance.",
             "expect": "It thanks you and ends the call without pitching."},
            {"say": "Can you call me back Tuesday at 4pm?",
             "expect": "It books a callback with the day and time you gave."},
            {"say": "Please take me off your list.",
             "expect": "It apologises, registers do-not-call and ends the call."},
        ],
        "fields": [
            {"key": "holds_policy",       "label": "Has private cover"},
            {"key": "over_min_age",       "label": "Aged 25 or over"},
            {"key": "insurer",            "label": "Current insurer",   "required": True},
            {"key": "cover_type",         "label": "Cover type",        "required": True},
            {"key": "premium_amount",     "label": "Premium",           "required": True},
            {"key": "premium_frequency",  "label": "Paid",              "required": True},
            {"key": "hospital_extras",    "label": "Hospital / extras"},
            {"key": "permanent_resident", "label": "Permanent resident"},
            {"key": "main_interest",      "label": "Main interest",     "required": True},
            {"key": "decision_maker",     "label": "Decision maker",    "hideEmpty": True},
            {"key": "first_name",         "label": "First name",        "required": True},
            {"key": "last_name",          "label": "Last name"},
            {"key": "email",              "label": "Email",             "hideEmpty": True},
            {"key": "postcode",           "label": "Postcode",          "required": True},
        ],
        "outcomes": {
            "QUALIFIED_PENDING":       ("Qualified — advisor callback booked", "good"),
            "TRANSFERRED":             ("Qualified — transferred to an advisor", "good"),
            "CALLBACK_BOOKED":         ("Callback booked for a set time", "good"),
            "NOT_QUALIFIED_PREMIUM":   ("Not qualified — premium below threshold", "bad"),
            "NOT_QUALIFIED_NO_POLICY": ("Not qualified — no private cover", "bad"),
            "NOT_QUALIFIED_AGE":       ("Not qualified — under 25", "bad"),
            "NOT_DECISION_MAKER":      ("Not the decision maker", "bad"),
            "NOT_INTERESTED":          ("Not interested", "neutral"),
            "DNC":                     ("Do-not-call registered", "neutral"),
            "VOICEMAIL":               ("Voicemail detected", "neutral"),
            "MACHINE_DETECTED":        ("Reached a machine", "neutral"),
            "NO_OUTCOME":              ("Ended without a decision", "neutral"),
            "DROPPED":                 ("Ended before the conversation started", "neutral"),
        },
    },

    # ═══════════════════════════════════════════════════════════════════════════
    "roof": {
        "name": "Roof & Insulation Assessment",
        "market": "United Kingdom",
        "voice": "British English",
        "goal": "Adviser callback booked",
        "tagline": "Offers homeowners a free roof and insulation check, confirms they own "
                   "the property, and books an adviser callback.",
        "persona": "You own your home in the UK. You don't know exactly how old the roof "
                   "is, and you haven't had it checked recently.",
        "agent": "Emma",
        "company": "Building Regulatory Services",
        "script": [
            {"agent": "Hello, my name's {agent}, calling from {company}. We're offering "
                      "homeowners a free roof and insulation assessment. Am I speaking with "
                      "the property owner?",
             "you": "Yes, I own the house.", "fields": ["homeowner"]},
            {"agent": "There's no cost and no obligation at all. Is that something you'd "
                      "be interested in?",
             "you": "Yes, go on then.", "fields": ["interested"]},
            {"agent": "Do you know roughly how old the roof is?",
             "you": "Not sure. Maybe twenty years.", "fields": ["roof_age"],
             "tip": "“I'm not sure” is a fine answer. It won't press you."},
            {"agent": "Have you had the roof or insulation checked recently?",
             "you": "No, never.", "fields": ["checked_recently"]},
            {"agent": "Would you be happy to receive a callback from one of our advisers?",
             "you": "Yes, that's fine.", "flag": "consent"},
            {"agent": "Could I take your name, please?",
             "you": "Margaret Jones.", "fields": ["first_name"]},
            {"agent": "Could I confirm the property address?",
             "you": "14 Elm Road, Bristol, BS7 8QX.", "fields": ["address"],
             "tip": "It reads the address back so you can correct it."},
            {"agent": "Reads back the number you verified and asks if it's the best "
                      "one for the adviser.",
             "you": "Yes, that's the one.", "fields": ["callback_number"],
             "tip": "On the demo it confirms the number you verified instead of asking "
                    "for digits. Give it a different one and it saves that instead."},
        ],
        "try": [
            {"say": "I'm renting.",
             "expect": "It thanks you and ends politely. Only property owners qualify, so "
                       "it doesn't pitch."},
            {"say": "Are you from the council?",
             "expect": "It says plainly that it's a private company."},
            {"say": "How much would the work cost?",
             "expect": "It won't quote or assess anything. That's the adviser's job."},
            {"say": "I need to talk to my partner first.",
             "expect": "It offers a callback at a time you're both free."},
            {"say": "Please take me off your list.",
             "expect": "It apologises, registers do-not-call and ends the call."},
        ],
        "fields": [
            {"key": "homeowner",        "label": "Property owner"},
            {"key": "interested",       "label": "Interested"},
            {"key": "roof_age",         "label": "Roof age"},
            {"key": "checked_recently", "label": "Checked recently"},
            {"key": "first_name",       "label": "First name",       "required": True},
            {"key": "last_name",        "label": "Last name"},
            {"key": "address",          "label": "Property address", "required": True},
            {"key": "callback_number",  "label": "Callback number",  "required": True},
        ],
        "outcomes": {
            "CALLBACK_AGREED":     ("Adviser callback agreed", "good"),
            "CALLBACK_BOOKED":     ("Callback booked for a set time", "good"),
            "CALLBACK_INCOMPLETE": ("Agreed to a callback — some details missing", "warn"),
            "NOT_HOMEOWNER":       ("Not the property owner", "bad"),
            "NOT_INTERESTED":      ("Not interested", "neutral"),
            "WRONG_NUMBER":        ("Wrong number", "neutral"),
            "DNC":                 ("Do-not-call registered", "neutral"),
            "VOICEMAIL":           ("Voicemail detected", "neutral"),
            "MACHINE_DETECTED":    ("Reached a machine", "neutral"),
            "NO_OUTCOME":          ("Ended without a decision", "neutral"),
            "DROPPED":             ("Ended before the conversation started", "neutral"),
        },
    },

    # ═══════════════════════════════════════════════════════════════════════════
    "finance": {
        "name": "Personal & Business Loans",
        "market": "Australia",
        "voice": "Australian English",
        "goal": "Information email + adviser follow-up",
        "tagline": "Qualifies personal and business loan enquiries, explains how a broker "
                   "works, and captures the email to send the information to.",
        "persona": "You run a café in Australia that's been trading for about four years, "
                   "and you're after around $30,000 for a fit-out.",
        "agent": "Sophie",
        "company": "Quick Funding",
        "script": [
            {"agent": "Hi, my name's {agent}, calling from {company}. We compare loan "
                      "options from multiple lenders. Are you looking at any finance at the "
                      "moment, like a personal or business loan?",
             "you": "Yes, actually I am.", "fields": ["looking_for_finance"]},
            {"agent": "Is that for a personal requirement, or finance for a business?",
             "you": "It's for my business.", "fields": ["loan_purpose"],
             "tip": "Say “personal” instead to try the personal-loan path."},
            {"agent": "Roughly how much are you looking for?",
             "you": "Around thirty thousand.", "fields": ["amount"]},

            {"agent": "Is this for an existing business, or to start or buy one?",
             "you": "An existing business. It's a café.", "fields": ["business_stage"],
             "when": {"loan_purpose": "business"}, "suggested": True},
            {"agent": "How long has the business been operating?",
             "you": "About four years.", "fields": ["business_age"],
             "when": {"loan_purpose": "business"}, "suggested": True},
            {"agent": "Roughly what's the monthly turnover?",
             "you": "Around sixty thousand a month.", "fields": ["monthly_turnover"],
             "when": {"loan_purpose": "business"}, "suggested": True},

            {"agent": "What would the loan be used for?",
             "you": "Consolidating a couple of credit cards.", "fields": ["loan_use"],
             "when": {"loan_purpose": "personal"}},
            {"agent": "Are you working at the moment? Full-time, part-time or "
                      "self-employed?",
             "you": "Full-time.", "fields": ["employment_status"],
             "when": {"loan_purpose": "personal"}},
            {"agent": "Roughly how much do you receive per week or fortnight, after tax?",
             "you": "About fourteen hundred a fortnight.", "fields": ["income_amount"],
             "when": {"loan_purpose": "personal"}},

            {"agent": "Do you have any loans, credit cards or other commitments at the "
                      "moment?",
             "you": "Just a car loan.", "fields": ["liabilities"]},
            {"agent": "I can email you our company details and the information you need. "
                      "Would you like me to send that through?",
             "you": "Yes, please.", "flag": "consent"},
            {"agent": "Can I grab your first name?",
             "you": "Daniel.", "fields": ["first_name"]},
            {"agent": "What's the best email address for you?",
             "you": "dan at acme dot com", "fields": ["email"],
             "tip": "Say it the way you would on the phone. It reads the address back to "
                    "confirm."},
            {"agent": "Reads back the number you verified and asks if it's the best "
                      "one to use.",
             "you": "Yes, that's right.", "fields": ["contact_number"],
             "tip": "Give it a different number instead and it saves that one."},
        ],
        "try": [
            {"say": "What's your interest rate?",
             "expect": "It won't give a rate or a range. It explains that rates depend on "
                       "your profile."},
            {"say": "Will this affect my credit score?",
             "expect": "It gives the approved soft-check wording once, and nothing more."},
            {"say": "Do you need my bank details?",
             "expect": "It declines. No banking or ID details are taken on the call."},
            {"say": "Mumble your email address twice.",
             "expect": "After two tries it offers an adviser call instead of guessing an "
                       "address."},
            {"say": "Please take me off your list.",
             "expect": "It apologises, registers do-not-call and ends the call."},
        ],
        "fields": [
            {"key": "looking_for_finance", "label": "Looking for finance"},
            {"key": "loan_purpose",        "label": "Loan type",          "required": True},
            {"key": "amount",              "label": "Amount",             "required": True},
            {"key": "business_stage",      "label": "Business stage",
             "when": {"loan_purpose": "business"}},
            {"key": "business_age",        "label": "Trading for",
             "when": {"loan_purpose": "business"}},
            {"key": "monthly_turnover",    "label": "Monthly turnover",
             "when": {"loan_purpose": "business"}},
            {"key": "loan_use",            "label": "Loan purpose",
             "when": {"loan_purpose": "personal"}},
            {"key": "employment_status",   "label": "Employment",
             "when": {"loan_purpose": "personal"}},
            {"key": "income_amount",       "label": "Income after tax",
             "when": {"loan_purpose": "personal"}},
            {"key": "income_frequency",    "label": "Paid",
             "when": {"loan_purpose": "personal"}},
            {"key": "liabilities",         "label": "Existing commitments"},
            {"key": "first_name",          "label": "First name",         "required": True},
            {"key": "last_name",           "label": "Last name",          "hideEmpty": True},
            {"key": "email",               "label": "Email"},
            {"key": "contact_number",      "label": "Contact number",     "required": True},
        ],
        "outcomes": {
            "INFO_REQUESTED":     ("Information email agreed — address captured", "good"),
            "QUALIFIED_CALLBACK": ("Qualified — adviser to call (no usable email)", "good"),
            "CALLBACK_BOOKED":    ("Callback booked for a set time", "good"),
            "INFO_INCOMPLETE":    ("Agreed — some details missing", "warn"),
            "NOT_LOOKING":        ("Not looking for finance", "bad"),
            "NOT_INTERESTED":     ("Not interested", "neutral"),
            "WRONG_NUMBER":       ("Wrong number", "neutral"),
            "DNC":                ("Do-not-call registered", "neutral"),
            "VOICEMAIL":          ("Voicemail detected", "neutral"),
            "MACHINE_DETECTED":   ("Reached a machine", "neutral"),
            "NO_OUTCOME":         ("Ended without a decision", "neutral"),
            "DROPPED":            ("Ended before the conversation started", "neutral"),
        },
    },

    # ═══════════════════════════════════════════════════════════════════════════
    "movers": {
        "name": "Moving Services Booking",
        "market": "Australia",
        "voice": "Australian English",
        "goal": "Move booked, deposit pending",
        "tagline": "Follows up an online moving inquiry: confirms the job, offers arrival "
                   "windows, quotes from the rate card and books the move.",
        "persona": "You asked online for a quote to move your two-bedroom unit from {pickup} "
                   "to {dropoff} on {move_date}. You have a queen bed, a sofa, a dining "
                   "table and two wardrobes, and there's a lift at the pickup.",
        "agent": "Jack",
        "company": "EasyMove Removals",
        "consent_chip": ["Agreed to book", "Not booked yet"],
        "demo_attrs": _movers_attrs,
        "script": [
            {"agent": "Hi, it's {agent} from {company}, following up your online moving "
                      "inquiry. Is now a good time to talk about your move?",
             "you": "Yes, now's fine."},
            {"agent": "I understand you're planning to move from {pickup} to {dropoff}. Is "
                      "that correct?",
             "you": "Yes, that's right.", "fields": ["details_confirmed"]},
            {"agent": "And are you still planning to move on {move_date}?",
             "you": "Yes, still that day.", "fields": ["date_confirmed"]},
            {"agent": "Is it a one-bedroom, two-bedroom, three-bedroom or larger move?",
             "you": "A two-bedroom unit.", "fields": ["property_size"]},
            {"agent": "What types of items will you be moving?",
             "you": "Furniture, boxes and a fridge.", "fields": ["items"]},
            {"agent": "Roughly how many beds, sofas, tables and cupboards?",
             "you": "A queen bed, a sofa, a dining table and two wardrobes.",
             "fields": ["furniture"]},
            {"agent": "Any large or heavy items, like a piano, pool table or safe?",
             "you": "No, nothing like that.", "fields": ["heavy_items"],
             "tip": "Say “a piano” instead: it flags special handling and never "
                    "invents a price for it."},
            {"agent": "Anything fragile that needs special handling?",
             "you": "The TV and some glassware.", "fields": ["fragile_items"]},
            {"agent": "Are both places on the ground floor? Any stairs, lifts or tight "
                      "spots?",
             "you": "Second floor with a lift at pickup, ground floor at the new place.",
             "fields": ["access"]},
            {"agent": "Will there be a driveway or parking for the truck?",
             "you": "Yes, there's a driveway.", "fields": ["parking"]},
            {"agent": "Here are the arrival windows we have free that day. Which suits you "
                      "better?",
             "you": "The morning one, please.", "fields": ["arrival_window"],
             "tip": "The windows come from the schedule. It won't make one up."},
            {"agent": "Gives the crew, truck, hourly rate, minimum and estimate. Would you "
                      "like to go ahead and book?",
             "you": "Yes, let's book it.", "flag": "consent",
             "tip": "Every figure comes from the rate card and is worked out in code, not "
                    "by the AI."},
            {"agent": "Could I take your full name?",
             "you": "Alex Taylor.", "fields": ["full_name"]},
            {"agent": "Reads back the number you verified and asks you to confirm it.",
             "you": "Yes, that's the best one.", "fields": ["contact_number"]},
            {"agent": "And your email address, for the confirmation?",
             "you": "alex at acme dot com", "fields": ["email"]},
            {"agent": "What's the full pickup address?",
             "you": "12 Church Street, Parramatta.", "fields": ["pickup_address"]},
            {"agent": "And the delivery address?",
             "you": "5 Oxford Street, Bondi Junction.", "fields": ["delivery_address"]},
            {"agent": "Explains the deposit, then reads the whole booking back. Is "
                      "everything correct?",
             "you": "Yes, that's all correct.", "flag": "done"},
        ],
        "try": [
            {"say": "Actually, there's a piano too.",
             "expect": "It notes special handling for the office to confirm, and quotes "
                       "nothing extra."},
            {"say": "Can I pay the deposit by card now?",
             "expect": "It won't take card details on the call. Payment details come with "
                       "the confirmation."},
            {"say": "Can we make it the Friday before instead?",
             "expect": "It saves the new date and checks the arrival windows again."},
            {"say": "Can you do it any cheaper?",
             "expect": "No discounts. It offers you time to think about it instead."},
            {"say": "I only wanted a price for now.",
             "expect": "It gives you the quote and records it as not booked yet."},
        ],
        "fields": [
            {"key": "details_confirmed", "label": "Route confirmed"},
            {"key": "pickup_area",       "label": "Moving from"},
            {"key": "dropoff_area",      "label": "Moving to"},
            {"key": "date_confirmed",    "label": "Date confirmed"},
            {"key": "move_date",         "label": "Moving date",       "required": True},
            {"key": "property_size",     "label": "Size of move",      "required": True},
            {"key": "items",             "label": "Items"},
            {"key": "furniture",         "label": "Furniture"},
            {"key": "heavy_items",       "label": "Heavy items"},
            {"key": "fragile_items",     "label": "Fragile items"},
            {"key": "access",            "label": "Access"},
            {"key": "parking",           "label": "Truck parking"},
            {"key": "notes",             "label": "Notes",             "hideEmpty": True},
            {"key": "arrival_window",    "label": "Arrival window",    "required": True},
            {"key": "full_name",         "label": "Full name",         "required": True},
            {"key": "contact_number",    "label": "Contact number",    "required": True},
            {"key": "email",             "label": "Email"},
            {"key": "pickup_address",    "label": "Pickup address",    "required": True},
            {"key": "delivery_address",  "label": "Delivery address",  "required": True},
        ],
        "outcomes": {
            "BOOKING_PENDING_DEPOSIT": ("Booked — awaiting deposit", "good"),
            "CALLBACK_BOOKED":         ("Callback booked for a set time", "good"),
            "BOOKING_INCOMPLETE":      ("Agreed to book — details missing", "warn"),
            "QUOTE_ONLY":              ("Quote given — not booked yet", "warn"),
            "NOT_MOVING":              ("No longer moving", "bad"),
            "NOT_INTERESTED":          ("Not interested", "neutral"),
            "WRONG_PERSON":            ("Wrong person", "neutral"),
            "DNC":                     ("Do-not-call registered", "neutral"),
            "VOICEMAIL":               ("Voicemail detected", "neutral"),
            "MACHINE_DETECTED":        ("Reached a machine", "neutral"),
            "NO_OUTCOME":              ("Ended without a decision", "neutral"),
            "DROPPED":                 ("Ended before the conversation started", "neutral"),
        },
    },

    # ═══════════════════════════════════════════════════════════════════════════
    # Demo-only from here: no telephony service, agent files in /opt/livekit/web-demo/bots
    # ═══════════════════════════════════════════════════════════════════════════
    "mortgage": {
        "name": "Mortgage Broker Leads",
        "market": "Australia",
        "voice": "Australian English",
        "goal": "Introductory email agreed",
        "tagline": "Calls mortgage brokers to offer refinance and purchase leads, handles "
                   "the upfront-payment question, and sends an introductory pack.",
        "persona": "You're a mortgage broker in Sydney who handles home loans and "
                   "refinancing in NSW and Queensland. You're open to more clients, but "
                   "wary of paying upfront.",
        "agent": "Ethan",
        "company": "Keystone Lead Partners",
        "consent_chip": ["Agreed to the intro email", "No agreement yet"],
        "script": [
            {"agent": "Hi, it's {agent} from {company}. We provide property finance leads "
                      "to mortgage brokers. Are you interested in receiving more qualified "
                      "mortgage clients?",
             "you": "Maybe. What exactly do you provide?", "fields": ["interested"]},
            {"agent": "Explains the prepared applications, then asks a few quick questions. "
                      "Do you handle home loans, refinancing, investment or business lending?",
             "you": "Mostly home loans and refinancing.", "fields": ["loan_types"]},
            {"agent": "Which states or territories do you serve?",
             "you": "New South Wales and Queensland.", "fields": ["states"]},
            {"agent": "Would you prefer individual opportunities, or a monthly campaign?",
             "you": "Individual ones to start with.", "fields": ["preference"]},
            {"agent": "Roughly what loan volume can your team handle each month?",
             "you": "About ten a month.", "fields": ["monthly_capacity"]},
            {"agent": "Explains the price per 100 thousand dollars of loan value.",
             "you": "Why do you charge upfront?", "fields": ["concerns"],
             "tip": "Also ask what a six hundred and fifty thousand dollar loan would cost. "
                    "The price is worked out in code, not by the AI."},
            {"agent": "We could start with a single opportunity. Can I send you an "
                      "introductory email with our details, a sample agreement, pricing and "
                      "a client scenario?",
             "you": "Yes, send it through.", "flag": "consent"},
            {"agent": "Could I confirm your full name?",
             "you": "Alex Taylor.", "fields": ["first_name"]},
            {"agent": "And your company name?",
             "you": "Acme Finance.", "fields": ["company_name"]},
            {"agent": "What's the best email address for you?",
             "you": "alex at acme dot com", "fields": ["email"]},
            {"agent": "Reads back the number you verified and asks you to confirm it.",
             "you": "Yes, that's my WhatsApp.", "fields": ["whatsapp"]},
        ],
        "try": [
            {"say": "How do I know the client is genuine?",
             "expect": "It offers a discussion with the client first, where privacy allows, "
                       "and the written terms."},
            {"say": "Can you guarantee the loan settles?",
             "expect": "No guarantees, ever. It says it depends on the individual client."},
            {"say": "Do you have a client in my area right now?",
             "expect": "It won't share any client's details on the call. The scenario comes "
                       "in writing."},
            {"say": "Can you do it cheaper if I take twenty?",
             "expect": "No discounts on the call. Monthly pricing is given in writing."},
            {"say": "I'm not actually a broker.",
             "expect": "It apologises and ends the call politely."},
        ],
        "fields": [
            {"key": "interested",       "label": "Wants more clients"},
            {"key": "loan_types",       "label": "Loan types"},
            {"key": "states",           "label": "States served"},
            {"key": "preference",       "label": "Prefers"},
            {"key": "monthly_capacity", "label": "Monthly capacity"},
            {"key": "concerns",         "label": "Concerns raised"},
            {"key": "first_name",       "label": "First name",   "required": True},
            {"key": "last_name",        "label": "Last name",    "hideEmpty": True},
            {"key": "company_name",     "label": "Company",      "required": True},
            {"key": "email",            "label": "Email",        "required": True},
            {"key": "whatsapp",         "label": "WhatsApp"},
        ],
        "outcomes": {
            "INTRO_PACK_AGREED": ("Introductory email agreed", "good"),
            "CALLBACK_BOOKED":   ("Callback booked for a set time", "good"),
            "INTRO_INCOMPLETE":  ("Agreed — some details missing", "warn"),
            "NOT_A_BROKER":      ("Not a mortgage broker", "bad"),
            "NOT_INTERESTED":    ("Not interested", "neutral"),
            "WRONG_PERSON":      ("Wrong person", "neutral"),
            "DNC":               ("Do-not-call registered", "neutral"),
            "VOICEMAIL":         ("Voicemail detected", "neutral"),
            "MACHINE_DETECTED":  ("Reached a machine", "neutral"),
            "NO_OUTCOME":        ("Ended without a decision", "neutral"),
            "DROPPED":           ("Ended before the conversation started", "neutral"),
        },
    },

    # ═══════════════════════════════════════════════════════════════════════════
    "energy": {
        "name": "Electricity Credit Assistance",
        "market": "Australia",
        "voice": "Australian English",
        "goal": "Pointed to official channels",
        "tagline": "Helps households find out where to check for energy rebates and "
                   "credits, and never asks for card, bank or account details.",
        "persona": "You rent your home in Australia, get your electricity bill by email, "
                   "and have a pension card. You haven't heard about any rebate.",
        "agent": "Grace",
        "company": "ClearWatt Energy Assist",
        "consent_chip": None,
        "script": [
            {"agent": "Hi, it's {agent} from {company}, about electricity account "
                      "assistance and possible energy rebates. We'll never ask for your card "
                      "number, PIN, password or banking details. Is now a good time?",
             "you": "Yes, okay."},
            {"agent": "Have you received any information about an electricity rebate or "
                      "energy assistance payment recently?",
             "you": "No, I haven't.", "fields": ["received_info"]},
            {"agent": "Do you get your electricity bill sent to you directly, or is it paid "
                      "automatically?",
             "you": "It comes by email.", "fields": ["bill_delivery"]},
            {"agent": "Who is your electricity provider?",
             "you": "Origin.", "fields": ["provider"]},
            {"agent": "Are you a homeowner, or a tenant?",
             "you": "I rent.", "fields": ["homeowner_or_tenant"]},
            {"agent": "Do you currently receive any pension or concession benefits?",
             "you": "Yes, I have a pension card.", "fields": ["concession"],
             "tip": "It only ever wants a yes or no. It never asks for the card number."},
            {"agent": "The safest way to check is directly with your provider, on the "
                      "number printed on your bill, or on the government energy website.",
             "you": "Okay, I'll check that. Thank you.", "flag": "done"},
        ],
        "try": [
            {"say": "Start reading out a card number.",
             "expect": "The number is removed before the AI ever sees it, and it stops you "
                       "kindly."},
            {"say": "I can't see very well, I can't check right now.",
             "expect": "It stops straight away and ends the call kindly, with no pressure."},
            {"say": "Are you from the government?",
             "expect": "No. It says it's an independent company, not your provider and not "
                       "a government agency."},
            {"say": "How much is the rebate? Am I eligible?",
             "expect": "It won't give an amount or say you're eligible. It points you to "
                       "your provider."},
            {"say": "Can you just pay it into my account?",
             "expect": "It never handles money, and won't take any account details."},
        ],
        "fields": [
            {"key": "received_info",       "label": "Heard about a rebate"},
            {"key": "bill_delivery",       "label": "Gets the bill by"},
            {"key": "provider",            "label": "Provider"},
            {"key": "homeowner_or_tenant", "label": "Homeowner or tenant"},
            {"key": "concession",          "label": "Pension / concession"},
        ],
        "outcomes": {
            "GUIDANCE_GIVEN":     ("Pointed to official channels", "good"),
            "CALLBACK_BOOKED":    ("Callback booked for a set time", "good"),
            "UNABLE_TO_CONTINUE": ("Couldn't continue — ended kindly", "neutral"),
            "NOT_INTERESTED":     ("Not interested", "neutral"),
            "WRONG_PERSON":       ("Wrong person", "neutral"),
            "DNC":                ("Do-not-call registered", "neutral"),
            "VOICEMAIL":          ("Voicemail detected", "neutral"),
            "MACHINE_DETECTED":   ("Reached a machine", "neutral"),
            "NO_OUTCOME":         ("Ended without a decision", "neutral"),
            "DROPPED":            ("Ended before the conversation started", "neutral"),
        },
    },
    # ═══════════════════════════════════════════════════════════════════════════
    "warranty": {
        "name": "Appliance Cover",
        "market": "United Kingdom",
        "voice": "British English",
        "goal": "Policy details sent in writing",
        "tagline": "Offers optional repair cover for a household appliance, checks the "
                   "appliance and its age, and sends the policy in writing — it never "
                   "takes a payment.",
        "persona": "You own a {appliance} that's about three years old and working fine. "
                   "You weren't expecting the call, and you're a little wary of it.",
        "agent": "Hannah",
        "company": "Brightwell Home Cover",
        "consent_chip": ["Agreed to the written details", "Nothing agreed yet"],
        "demo_attrs": _warranty_attrs,
        "script": [
            {"agent": "Hi, it's {agent} from {company}, calling about optional repair and "
                      "warranty cover for your {appliance}. Do you have a couple of minutes?",
             "you": "Yes, go on."},
            {"agent": "Just to confirm, the appliance is your {appliance} — is that right?",
             "you": "Yes, that's right.", "fields": ["appliance_confirmed"]},
            {"agent": "And roughly how old is it?",
             "you": "About three years.", "fields": ["appliance_age"],
             "tip": "Say “about twelve years” instead and watch it refuse to quote a "
                    "price — the age limit is checked in code."},
            {"agent": "Is it working normally at the moment, or are you having any problems "
                      "with it?",
             "you": "It's working fine.", "fields": ["working_normally"],
             "tip": "Mention a fault instead and the sale stops there: an existing fault "
                    "isn't covered by a new policy, and it says so plainly."},
            {"agent": "Checks the plan for an appliance that age, then explains what the "
                      "cover includes.",
             "you": "Right, okay.",
             "tip": "Eligibility is decided from the age by the code, not the AI. It never "
                    "tells you that you're covered — the written terms do that."},
            {"agent": "Gives the price and the cover period, and says there's nothing beyond "
                      "what the policy states.",
             "you": "How much is it again?",
             "tip": "Every figure comes from the plan settings. The AI can't round it, "
                    "discount it or invent an offer."},
            {"agent": "Before you decide anything, would you like me to send the details in "
                      "writing?",
             "you": "Yes, please send them.", "flag": "consent",
             "tip": "This is the only thing the call closes on. Nothing is sold and nothing "
                    "is charged."},
            {"agent": "Could I take your first name?",
             "you": "Alex Taylor.", "fields": ["first_name"]},
            {"agent": "Reads back the number you verified and asks you to confirm it.",
             "you": "Yes, that's the one.", "fields": ["contact_number"]},
            {"agent": "And an email address to send the details to?",
             "you": "alex at acme dot com", "fields": ["email"]},
            {"agent": "Confirms the details are on their way, and that there's nothing to "
                      "pay today.",
             "you": "Thanks, I'll have a read.", "flag": "done"},
        ],
        "try": [
            {"say": "Can I just pay for it now, by card?",
             "expect": "It won't take a payment or card details on the call at all — the "
                       "policy goes out first and payment happens from there."},
            {"say": "Actually it's been leaking for about a week.",
             "expect": "The sale stops. It says an existing fault generally isn't covered "
                       "by a new policy, and records it."},
            {"say": "It must be twelve years old by now.",
             "expect": "No price. It says cover for an appliance that age has to be "
                       "checked, and offers the details in writing."},
            {"say": "Are you calling from the manufacturer?",
             "expect": "No. It gives the company name and offers everything in writing so "
                       "you can check independently."},
            {"say": "I think I already have a warranty on it.",
             "expect": "It tells you to check what you already have first, and doesn't push."},
            {"say": "I'd want to ask my husband first.",
             "expect": "That's fine by it — it offers to send the details so you can read "
                       "them together."},
        ],
        "fields": [
            {"key": "appliance",           "label": "Appliance"},
            {"key": "appliance_confirmed", "label": "Appliance confirmed"},
            {"key": "appliance_age",       "label": "Age"},
            {"key": "working_normally",    "label": "Working normally"},
            {"key": "existing_fault",      "label": "Existing fault"},
            {"key": "already_covered",     "label": "Already covered", "hideEmpty": True},
            {"key": "interest",            "label": "Interest",        "hideEmpty": True},
            {"key": "discuss_with",        "label": "Decides with",    "hideEmpty": True},
            {"key": "notes",               "label": "Notes",           "hideEmpty": True},
            {"key": "first_name",          "label": "First name",      "required": True},
            {"key": "last_name",           "label": "Last name",       "hideEmpty": True},
            {"key": "contact_number",      "label": "Contact number",  "required": True},
            {"key": "email",               "label": "Email"},
        ],
        "outcomes": {
            "INFO_REQUESTED":   ("Written details agreed — sending", "good"),
            "CALLBACK_BOOKED":  ("Callback booked for a set time", "good"),
            "INFO_INCOMPLETE":  ("Agreed — contact details missing", "warn"),
            "EXISTING_FAULT":   ("Existing fault — not sold", "warn"),
            "NOT_ELIGIBLE":     ("Age — cover to be checked", "warn"),
            "THINKING":         ("Wants to consider it", "warn"),
            "ALREADY_COVERED":  ("Already has cover", "neutral"),
            "NOT_INTERESTED":   ("Not interested", "neutral"),
            "WRONG_PERSON":     ("Wrong person", "neutral"),
            "DNC":              ("Do-not-call registered", "neutral"),
            "VOICEMAIL":        ("Voicemail detected", "neutral"),
            "MACHINE_DETECTED": ("Reached a machine", "neutral"),
            "NO_OUTCOME":       ("Ended without a decision", "neutral"),
            "DROPPED":          ("Ended before the conversation started", "neutral"),
        },
    },
}


def enabled_keys() -> list[str]:
    """DEMO_BOTS, in dropdown order. Unknown keys are dropped with a warning rather than
    breaking the page; an empty or unset value means every bot in the catalog."""
    raw = os.getenv("DEMO_BOTS", "").strip()
    if not raw:
        return list(BOT_CATALOG)
    keys = []
    for k in (p.strip().lower() for p in raw.split(",")):
        if not k:
            continue
        if k not in BOT_CATALOG:
            log.warning("DEMO_BOTS names unknown bot %r — ignoring it. Known: %s",
                        k, ", ".join(BOT_CATALOG))
            continue
        if k not in keys:
            keys.append(k)
    if not keys:
        log.error("DEMO_BOTS=%r matched no known bot — showing all of them instead.", raw)
        return list(BOT_CATALOG)
    return keys


def demo_attrs(key: str) -> dict:
    """Stand-in campaign data for this bot's demo call, sent to the bot as dialer.<name>.
    Empty for bots that don't need any."""
    make = (BOT_CATALOG.get(key) or {}).get("demo_attrs")
    if not callable(make):
        return {}
    try:
        return {str(k): str(v) for k, v in make().items()}
    except Exception as e:
        log.error("demo_attrs for %s failed: %s", key, e)
        return {}


def _fill(obj, values: dict):
    """Replace {name} placeholders for the given names only. {agent} and {company} are
    left for the page, which fills them from what the bot reports live."""
    if isinstance(obj, str):
        for k, v in values.items():
            obj = obj.replace("{" + k + "}", v)
        return obj
    if isinstance(obj, list):
        return [_fill(x, values) for x in obj]
    if isinstance(obj, dict):
        return {k: _fill(v, values) for k, v in obj.items()}
    return obj


def _names_from_env(key: str) -> dict:
    """AGENT_NAME / COMPANY_NAME from that bot's .env, if readable. Nothing else is read
    from the file — it holds API keys this process has no business with."""
    path = os.getenv(f"DEMO_BOT_ENV_{key.upper()}", BOT_ENV_DEFAULTS.get(key, ""))
    if not path or not os.path.isfile(path):
        return {}
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(path)
    except Exception as e:
        log.info("could not read %s for bot %s names: %s", path, key, e)
        return {}
    out = {}
    if (vals.get("AGENT_NAME") or "").strip():
        out["agent"] = vals["AGENT_NAME"].strip()
    if (vals.get("COMPANY_NAME") or "").strip():
        out["company"] = vals["COMPANY_NAME"].strip()
    return out


def public_catalog() -> list[dict]:
    """What /api/config sends the page: enabled bots only, in order, JSON-shaped."""
    out = []
    for key in enabled_keys():
        bot = BOT_CATALOG[key]
        entry = {k: v for k, v in bot.items() if k not in ("outcomes", "demo_attrs")}
        entry = _fill(entry, demo_attrs(key))
        entry.update(_names_from_env(key))
        entry["key"] = key
        entry["outcomes"] = {code: {"label": lbl, "tone": tone}
                             for code, (lbl, tone) in bot["outcomes"].items()}
        out.append(entry)
    return out
