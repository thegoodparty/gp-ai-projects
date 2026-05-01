"""
Elected Official Briefing — Robert Dearborn, Village President, Winnetka IL
V2 Format — Generated: May 2026
"""

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    Table, TableStyle, HRFlowable
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER

# ── Color Palette ──────────────────────────────────────────────────────────
NAVY       = HexColor("#1B3A6B")
LIGHT_BLUE = HexColor("#E8F0FA")
ACCENT     = HexColor("#2E6DB4")
TIER2_BG   = HexColor("#E8F5E9")
TIER3_BG   = HexColor("#FFF8E1")
BELOW_BG   = HexColor("#FAFAFA")
POLL_BG    = HexColor("#EEF4FF")
DARK_TEXT  = HexColor("#212121")
MID_TEXT   = HexColor("#555555")
LIGHT_GRAY = HexColor("#CCCCCC")

# ── Paragraph Styles ───────────────────────────────────────────────────────
def S(name, **kwargs):
    return ParagraphStyle(name, **kwargs)

TITLE_NAME    = S("TITLE_NAME",    fontSize=26, fontName="Helvetica-Bold",
                  textColor=white, leading=32, alignment=TA_CENTER)
TITLE_SUB     = S("TITLE_SUB",     fontSize=13, fontName="Helvetica",
                  textColor=HexColor("#D0DFF5"), leading=18, alignment=TA_CENTER)
H2            = S("H2",            fontSize=12, fontName="Helvetica-Bold",
                  textColor=ACCENT, spaceBefore=12, spaceAfter=6, leading=16)
BODY          = S("BODY",          fontSize=10, fontName="Helvetica",
                  textColor=DARK_TEXT, leading=15, spaceAfter=6)
CAPTION       = S("CAPTION",       fontSize=8,  fontName="Helvetica-Oblique",
                  textColor=MID_TEXT, leading=11)
BULLET        = S("BULLET",        fontSize=10, fontName="Helvetica",
                  textColor=DARK_TEXT, leading=14, leftIndent=14,
                  firstLineIndent=-10, spaceAfter=5)
POLL_TITLE    = S("POLL_TITLE",    fontSize=10, fontName="Helvetica-Bold",
                  textColor=ACCENT, leading=14)
POLL_BODY     = S("POLL_BODY",     fontSize=9,  fontName="Helvetica",
                  textColor=DARK_TEXT, leading=13)
POLL_LABEL    = S("POLL_LABEL",    fontSize=9,  fontName="Helvetica-Bold",
                  textColor=ACCENT, leading=13)
TH_WHITE      = S("TH_WHITE",      fontSize=9,  fontName="Helvetica",
                  textColor=white, leading=12, alignment=TA_CENTER)
TH_WHITE_BOLD = S("TH_WHITE_BOLD", fontSize=9,  fontName="Helvetica-Bold",
                  textColor=white, leading=12, alignment=TA_CENTER)
TD_CENTER     = S("TD_CENTER",     fontSize=9,  fontName="Helvetica",
                  textColor=DARK_TEXT, leading=12, alignment=TA_CENTER)
TD_LEFT       = S("TD_LEFT",       fontSize=9,  fontName="Helvetica",
                  textColor=DARK_TEXT, leading=12, alignment=TA_LEFT)
TD_BOLD_LEFT  = S("TD_BOLD_LEFT",  fontSize=9,  fontName="Helvetica-Bold",
                  textColor=DARK_TEXT, leading=12, alignment=TA_LEFT)

SCORE_STRONG   = S("SCORE_STRONG",   fontSize=9, fontName="Helvetica-Bold",
                   textColor=HexColor("#2E7D32"), leading=12, alignment=TA_CENTER)
SCORE_MODERATE = S("SCORE_MODERATE", fontSize=9, fontName="Helvetica-Bold",
                   textColor=HexColor("#F57F17"), leading=12, alignment=TA_CENTER)
SCORE_BELOW    = S("SCORE_BELOW",    fontSize=9, fontName="Helvetica",
                   textColor=MID_TEXT, leading=12, alignment=TA_CENTER)
TIER_STRONG    = S("TIER_STRONG",    fontSize=8, fontName="Helvetica-Bold",
                   textColor=HexColor("#2E7D32"), leading=11, alignment=TA_CENTER)
TIER_MODERATE  = S("TIER_MODERATE",  fontSize=8, fontName="Helvetica-Bold",
                   textColor=HexColor("#F57F17"), leading=11, alignment=TA_CENTER)
TIER_BELOW     = S("TIER_BELOW",     fontSize=8, fontName="Helvetica",
                   textColor=MID_TEXT, leading=11, alignment=TA_CENTER)


# ── Helpers ────────────────────────────────────────────────────────────────

def section_header(title, number=None):
    label = f"SECTION {number} — " if number else ""
    return [
        Spacer(1, 0.15 * inch),
        Table(
            [[Paragraph(f"{label}{title.upper()}", TH_WHITE_BOLD)]],
            colWidths=[7.5 * inch],
            style=TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
                ("TOPPADDING",    (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING",   (0, 0), (-1, -1), 12),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 12),
            ])
        ),
        Spacer(1, 0.1 * inch),
    ]


def sub_header(text):
    return [Paragraph(text, H2), Spacer(1, 0.04 * inch)]


def body(text):
    return Paragraph(text, BODY)


def bullet(text):
    return Paragraph(f"• {text}", BULLET)


def hr():
    return HRFlowable(width="100%", thickness=0.5, color=LIGHT_GRAY,
                      spaceAfter=6, spaceBefore=6)


def polling_callout(issue, timing, questions, why, action):
    rows = [
        [Paragraph("GOOD PARTY POLLING OPPORTUNITY", POLL_TITLE)],
        [Paragraph(f"<b>Issue:</b> {issue}", POLL_BODY)],
        [Paragraph(f"<b>Timing:</b> {timing}", POLL_BODY)],
        [Paragraph("<b>Key Questions:</b>", POLL_LABEL)],
    ] + [[Paragraph(f"  • {q}", POLL_BODY)] for q in questions] + [
        [Paragraph(f"<b>Why This Matters:</b> {why}", POLL_BODY)],
        [Paragraph(f"<b>Action:</b> {action}", POLL_BODY)],
    ]
    t = Table(rows, colWidths=[7.1 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), POLL_BG),
        ("BOX",           (0, 0), (-1, -1), 1, ACCENT),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
    ]))
    return [Spacer(1, 0.1 * inch), t, Spacer(1, 0.1 * inch)]


def issues_table(rows_data):
    header = [Paragraph(h, TH_WHITE_BOLD)
              for h in ["Rank", "Issue", "Score", "Tier"]]
    data = [header]
    style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("BOX",           (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("INNERGRID",     (0, 0), (-1, -1), 0.25, LIGHT_GRAY),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]
    for i, (rank, issue, score, tier, bg) in enumerate(rows_data, start=1):
        if tier == "Strong":
            ss, ts = SCORE_STRONG, TIER_STRONG
        elif tier == "Moderate":
            ss, ts = SCORE_MODERATE, TIER_MODERATE
        else:
            ss, ts = SCORE_BELOW, TIER_BELOW
        data.append([
            Paragraph(str(rank), TD_CENTER),
            Paragraph(issue, TD_LEFT),
            Paragraph(f"{score:.1f}", ss),
            Paragraph(tier, ts),
        ])
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))
    t = Table(data, colWidths=[0.5 * inch, 3.8 * inch, 1.0 * inch, 2.2 * inch])
    t.setStyle(TableStyle(style_cmds))
    return t


def demographics_table(rows_data):
    data = [[Paragraph("Metric", TH_WHITE_BOLD), Paragraph("Value", TH_WHITE_BOLD)]] + [
        [Paragraph(label, TD_BOLD_LEFT), Paragraph(value, TD_CENTER)]
        for label, value in rows_data
    ]
    t = Table(data, colWidths=[3.5 * inch, 4.0 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0), NAVY),
        ("BOX",            (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("INNERGRID",      (0, 0), (-1, -1), 0.25, LIGHT_GRAY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, LIGHT_BLUE]),
        ("TOPPADDING",     (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
        ("LEFTPADDING",    (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 8),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


# ── Document ───────────────────────────────────────────────────────────────

def build_briefing(output_path):
    doc = SimpleDocTemplate(
        output_path, pagesize=letter,
        leftMargin=0.75*inch, rightMargin=0.75*inch,
        topMargin=0.75*inch,  bottomMargin=0.75*inch,
    )
    story = []

    # ── TITLE PAGE ─────────────────────────────────────────────────────────
    def banner(content_rows, top_pad=8, bot_pad=8):
        t = Table(content_rows, colWidths=[7.5 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
            ("TOPPADDING",    (0, 0), (-1, -1), top_pad),
            ("BOTTOMPADDING", (0, 0), (-1, -1), bot_pad),
            ("LEFTPADDING",   (0, 0), (-1, -1), 20),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 20),
        ]))
        return t

    story += [
        banner([[Paragraph("ELECTED OFFICIAL BRIEFING", TITLE_SUB)]], top_pad=28, bot_pad=8),
        banner([[Paragraph("Robert Dearborn", TITLE_NAME)]]),
        banner([[Paragraph("Village President  ·  Village of Winnetka, Illinois", TITLE_SUB)]], top_pad=4, bot_pad=28),
        Spacer(1, 0.3 * inch),
        Paragraph("May 2026  ·  Prepared by Good Party", CAPTION),
        Spacer(1, 0.5 * inch),
        body(
            "This briefing is designed to help you hit the ground running as the newly elected Village "
            "President of Winnetka. It gives you the context, constituent data, and specific action "
            "opportunities you need to be effective from day one."
        ),
        Spacer(1, 0.3 * inch),
    ]

    toc_rows = [
        ["1", "Executive Summary", "2"],
        ["2", "Lessons Learned From Recent History", "3"],
        ["3", "Quick Wins", "4"],
        ["4", "Your Constituents' Top Issues", "5"],
        ["5", "What to Watch & Prepare For", "6"],
        ["6", "Understanding This Role", "7"],
        ["7", "Top 3 Budget Discussions", "8"],
        ["8", "Top 3 Non-Budget Policy Discussions", "9"],
        ["9", "Appendix: Key Resources", "10"],
    ]
    toc = Table(
        [[Paragraph(h, TH_WHITE_BOLD) for h in ["#", "Section", "Page"]]] +
        [[Paragraph(r, TD_CENTER), Paragraph(t, TD_LEFT), Paragraph(p, TD_CENTER)]
         for r, t, p in toc_rows],
        colWidths=[0.5*inch, 6.0*inch, 1.0*inch],
    )
    toc.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0), NAVY),
        ("BOX",            (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("INNERGRID",      (0, 0), (-1, -1), 0.25, LIGHT_GRAY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, LIGHT_BLUE]),
        ("TOPPADDING",     (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
        ("LEFTPADDING",    (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 8),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story += [toc, PageBreak()]

    # ── SECTION 1: EXECUTIVE SUMMARY ──────────────────────────────────────
    story += section_header("Executive Summary — Transition Roadmap", 1)
    story.append(body("<b>The Situation</b>"))
    story.append(body(
        "You're stepping into the Village President role with eight years on the board already behind "
        "you — which means you know the docket cold, but the community will hold you accountable for "
        "progress, not just preparation. Voter data from 10,313 Winnetka constituents shows a "
        "center-left community (liberal ideology: 59.1) that prioritizes police trust (61.9), "
        "infrastructure investment (61.8), and climate and environmental action (61.8 / 60.1 — both "
        "Strong tier). You're inheriting a record $116.2M capital budget, a village manager six months "
        "into her tenure, an IHDA affordable housing compliance deadline of October 1, 2026, and a "
        "downtown mid-transformation with One Winnetka under construction and the post office site "
        "awaiting a community vision."
    ))
    story.append(Spacer(1, 0.1 * inch))
    story.append(body("<b>Your First 90 Days: Top 4 Focus Areas</b>"))
    story.append(body(
        "First, <b>anchor your working relationship with Village Manager Kristin Kazenas</b> — she "
        "took the role in October 2025 and is managing Winnetka's most capital-intensive program in "
        "history while navigating two senior staff vacancies (Community Development director and "
        "Economic Development Coordinator both departed spring 2025). A strong partnership here "
        "determines execution quality across every other priority. Second, <b>launch post office site "
        "public engagement now</b>: demolition is planned for April 2026 and the community expects "
        "to shape the permanent vision — the planning consultant was authorized in October 2025 and "
        "must be activated. Third, <b>own the IHDA affordable housing deadline (October 1, 2026)</b>: "
        "Winnetka's first submission was rejected; the August 2025 plan was adopted but you yourself "
        "called it 'more the beginning, not the end' — concrete follow-through is required to avoid "
        "state intervention. Fourth, <b>convene a downtown parking study session before summer</b>: "
        "retail shop owners formally asked for help in March 2026, and One Winnetka construction is "
        "actively deterring shoppers — every week without action is lost retail season."
    ))
    story.append(PageBreak())

    # ── SECTION 2: LESSONS LEARNED ────────────────────────────────────────
    story += section_header("Lessons Learned From Recent History", 2)
    lessons = [
        (
            "1. Unanimous votes don't end contentious debates — you need an ongoing communications strategy.",
            "The IMEA energy contract renewal passed unanimously in June 2025, yet 11 residents spoke "
            "against it at that meeting and Go Green Winnetka remains active. With constituent climate "
            "scores in the Strong tier (61.8), treating the vote as 'done' without tracking IMEA's "
            "renewable milestones will keep criticism alive. Proactive updates on the Bee Hollow solar "
            "project (expected online late 2026) are your primary tool for managing this ongoing tension."
        ),
        (
            "2. Development approval is not tenant mix control — set expectations earlier.",
            "The council approved One Winnetka with a vision for vibrant downtown retail. By February "
            "2026, all six signed commercial tenants were service businesses — zero traditional retail. "
            "Future development agreements with retail expectations should include more explicit "
            "commitments up front, or honest early discussion of what the market will actually deliver "
            "so the council isn't surprised when the building fills."
        ),
        (
            "3. Local ordinances affecting property rights invite federal litigation — budget for it.",
            "The lakefront bluff ordinance (February 2024) was challenged in federal court within months "
            "of adoption. The case was dismissed without prejudice in October 2025 — meaning it can be "
            "refiled with a stronger factual record. Any future ordinance touching high-value private "
            "property should include upfront legal cost projections and a communications plan for "
            "affected owners before adoption."
        ),
        (
            "4. Compounding fee increases require narrative, not just numbers.",
            "FY2025 added ~$467/year per household; FY2026 adds another ~$440. Residents won't remember "
            "the nine-year levy freeze that preceded these increases — they will remember two straight "
            "years of ~$450 increases. Leading every budget conversation with what those dollars fund "
            "(Willow Road, lead line replacement, police staffing) is essential to maintaining trust "
            "going into FY2027 discussions."
        ),
        (
            "5. Staff transitions at the top create institutional knowledge gaps — manage proactively.",
            "Three senior departures in rapid succession (Village Manager Bahan after 15 years, "
            "Community Development Director Schoon, Economic Development Coordinator Dechant — all "
            "in 2025) created gaps at a moment of maximum project complexity. Tracking which "
            "institutional knowledge lives with which staff member — and ensuring it's documented — "
            "should be an explicit board priority, not something discovered after the fact."
        ),
    ]
    for title, desc in lessons:
        story.append(body(f"<b>{title}</b>"))
        story.append(body(desc))
        story.append(Spacer(1, 0.06 * inch))
    story.append(PageBreak())

    # ── SECTION 3: QUICK WINS ──────────────────────────────────────────────
    story += section_header("Quick Wins (First 90 Days)", 3)
    qw = [
        (
            "Activate the post office site planning consultant and schedule a public open house",
            "The consultant was authorized in October 2025 but the engagement needs to be formally "
            "launched. Scheduling a public open house within 60 days demonstrates momentum on "
            "Winnetka's highest-visibility redevelopment opportunity and gives residents the input "
            "process they've been expecting. A 'town square' concept has been publicly floated — "
            "polling or open houses will turn community aspiration into a data-backed mandate."
        ),
        (
            "Schedule a downtown parking and retail viability study session",
            "Retail shop owners asked for help formally in March 2026. Scheduling the study session "
            "before Memorial Day captures the pre-summer window to implement any near-term solutions "
            "(time-limited parking zones, construction mitigation signage) before peak retail season. "
            "Delay past summer means another lost season for Lincoln Avenue merchants."
        ),
        (
            "Get a dedicated IHDA compliance briefing from the Village Manager and Community Development",
            "The October 1, 2026 deadline is five months away. A focused session with Kazenas and "
            "Director Scott Mangum to map exactly what steps remain — and who owns each one — will "
            "surface any gaps while there is still time to close them. This also signals to staff "
            "that the deadline is a real governance priority, not a paperwork exercise."
        ),
        (
            "Issue a public update on IMEA's renewable energy transition milestones",
            "The IMEA contract renewal was the most contested decision of your first year. The Bee "
            "Hollow 150-MW solar project comes online in late 2026 — a concrete, visible milestone. "
            "A proactive update through the village newsletter and website shows the community you "
            "are actively monitoring what was promised, not simply moving on after the vote."
        ),
        (
            "Review lead service line replacement pace and announce a waitlist reduction goal",
            "The rebate program has a 140-person waitlist against a replacement rate of 90–100 lines "
            "per year. With constituent infrastructure scores in the Strong tier (61.8), announcing "
            "a review of whether the pace can be accelerated — even modestly — is a visible, tangible "
            "commitment to the community's top data-validated priority."
        ),
    ]
    for i, (title, desc) in enumerate(qw, start=1):
        story.append(body(f"<b>{i}. {title}</b>"))
        story.append(body(desc))
        story.append(Spacer(1, 0.06 * inch))

    story += polling_callout(
        issue="Post Office Site — Community Vision",
        timing="Before planning consultant engagement launches (June 2026)",
        questions=[
            "What type of use would you most like to see on the former post office site?",
            "How important is it that the site includes public green space vs. commercial activity?",
            "Would you support a community performance stage / public gathering plaza?",
        ],
        why="The post office redevelopment will define downtown Winnetka for decades. Polling "
            "before the consultant kicks off gives residents genuine input and gives you "
            "data-backed direction rather than relying on who shows up to a single public meeting.",
        action="Contact Good Party to set up a community poll on the post office site before "
               "the planning consultant engagement launches."
    )
    story.append(PageBreak())

    # ── SECTION 4: CONSTITUENTS' TOP ISSUES ───────────────────────────────
    story += section_header("Your Constituents' Top Issues", 4)
    story.append(body(
        "The following data reflects Haystaq voter intelligence scores for 10,313 registered voters "
        "in Winnetka matched to issue and behavioral dimensions. Scores are 0–100; higher scores "
        "indicate stronger constituent support or prioritization. Only issues within local/state "
        "government authority are included."
    ))
    story.append(Spacer(1, 0.12 * inch))
    story += sub_header("Demographics at a Glance")
    story.append(demographics_table([
        ("Total Registered Voters", "10,656"),
        ("Average Age",             "49.8 years"),
        ("Gender: Female",          "5,523  (52%)"),
        ("Gender: Male",            "5,133  (48%)"),
    ]))
    story.append(Spacer(1, 0.14 * inch))
    story += sub_header("Top Issues by Priority (Local/State Authority Only)")
    issue_rows = [
        (1,  "Police Trust & Confidence",             61.9, "Strong",   TIER2_BG),
        (2,  "Infrastructure Investment",             61.8, "Strong",   TIER2_BG),
        (3,  "Climate Action / Clean Energy",         61.8, "Strong",   TIER2_BG),
        (4,  "Environmental Priorities",              60.1, "Strong",   TIER2_BG),
        (5,  "Tax Policy (Tax Cuts Support)",         58.8, "Moderate", TIER3_BG),
        (6,  "Public Transit Access",                 57.5, "Moderate", TIER3_BG),
        (7,  "School Funding (Support More)",         51.8, "Moderate", TIER3_BG),
        (8,  "Local Economic Development",            51.5, "Moderate", TIER3_BG),
        (9,  "Helping People / Social Services",      49.5, "Below 50", BELOW_BG),
        (10, "Affordable Housing (Government Role)",  47.8, "Below 50", BELOW_BG),
    ]
    story.append(issues_table(issue_rows))
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "Tier Guide: Strong = 60–74 (green)  ·  Moderate = 50–59 (amber)  ·  Below 50 = lower priority.  "
        "Ideology: Liberal 59.1 vs. Conservative 40.9. Fiscal: balanced (conserv. 49.5 / liberal 50.5).  "
        "Note: Keep Safe score 32.2 and Violent Crime Worried 20.1 — crime is not a top concern for "
        "Winnetka constituents.",
        CAPTION
    ))
    story.append(Spacer(1, 0.08 * inch))

    story += polling_callout(
        issue="IMEA Renewable Energy Transition — Community Expectations",
        timing="Before IMEA Bee Hollow solar project goes online (late 2026)",
        questions=[
            "How important is it that Winnetka's electricity comes from renewable sources?",
            "Are you satisfied with the village's progress toward reducing reliance on coal-generated power?",
            "What renewable energy milestones would most increase your confidence in the IMEA contract?",
        ],
        why="Climate believer (61.8) and environment priority (60.1) are both Strong — yet the "
            "board unanimously renewed a contract tied to a coal plant through 2055. Polling "
            "quantifies exactly how urgent residents consider this and what milestones would "
            "satisfy the broader constituency, not just the most vocal activists.",
        action="Contact Good Party to set up a community energy priorities poll before the "
               "Bee Hollow solar project comes online."
    )
    story.append(PageBreak())

    # ── SECTION 5: WHAT TO WATCH ───────────────────────────────────────────
    story += section_header("What to Watch & Prepare For", 5)

    story += sub_header("Issues Requiring Immediate Attention")
    story.append(bullet(
        "<b>IHDA Affordable Housing Deadline — October 1, 2026:</b> Winnetka's first IHDA submission "
        "was rejected as non-compliant. The revised August 2025 plan was adopted but remains a starting "
        "point. The deadline is firm — non-compliant municipalities face state intervention. Assign "
        "clear staff ownership and establish monthly check-ins with the Village Manager now."
    ))
    story.append(bullet(
        "<b>Willow Road Reconstruction — 2026 Construction Season:</b> The $18.4M project (largest "
        "single capital item in the FY2026 budget) begins construction this year. Expect significant "
        "traffic disruption and constituent complaints. Proactive communications on detour routes, "
        "milestones, and completion timeline are essential. The village secured ~$10M in grants — "
        "communicate that context so residents understand the fiscal stewardship."
    ))
    story.append(bullet(
        "<b>Downtown Retail and Parking — Decision Needed Before Summer:</b> Retail shop owners "
        "formally requested help in March 2026. If the study session doesn't happen before Memorial "
        "Day, no solutions can be implemented before peak retail season. Lincoln Avenue parking "
        "enforcement and construction traffic mitigation are the two most immediate levers available."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story += sub_header("Political Dynamics to Understand")
    story.append(bullet(
        "<b>New Council Majority Elected With You:</b> Trustees Albinson, Myers, and Orsic were "
        "all elected April 1, 2025 — the same day as Dearborn — and serve through May 2027. "
        "Trustees Apatoff, Dalman, and Handler (holdovers) are up for election in April 2027. "
        "Understanding where each trustee stood on IMEA and the lakefront bluff ordinance will "
        "help you anticipate coalition dynamics on future contested votes."
    ))
    story.append(bullet(
        "<b>Go Green Winnetka Coalition Remains Active:</b> This organized resident group "
        "opposed the IMEA coal contract renewal and has not disbanded. They represent a vocal, "
        "well-organized constituency that aligns with your community's Strong-tier environmental "
        "scores. Engaging them proactively on IMEA milestone monitoring is a more sustainable "
        "political posture than treating them as ongoing adversaries."
    ))
    story.append(bullet(
        "<b>Village Manager Transition Is a Leadership Variable:</b> Kristin Kazenas replaced "
        "a 15-year veteran in October 2025. She is highly capable and promoted from within, but "
        "is simultaneously managing a record capital program, two senior staff vacancies, and a "
        "new council majority. The quality of your working relationship will be the single biggest "
        "determinant of execution quality across all major 2026 priorities."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story += sub_header("Upcoming Challenges or Decisions")
    story.append(bullet(
        "<b>Lakefront Bluff Litigation — Possible Refiling:</b> The federal lawsuit was dismissed "
        "without prejudice in October 2025 — on ripeness grounds, not the merits. Property owners "
        "can refile with a more developed factual record. Ensure Village Attorney Peter Friedman "
        "(Elrod Friedman LLP, 312-578-6566) has litigation readiness budget and strategy in place."
    ))
    story.append(bullet(
        "<b>One Winnetka Completion & Narrative Management (October 2026):</b> With zero "
        "traditional retail among the first six commercial tenants, the development will open "
        "against a credibility gap. Proactive communication before October — focusing on the "
        "59 housing units, 150 parking spaces, and tax base added — is essential to shaping "
        "the opening narrative rather than reacting to disappointment."
    ))
    story.append(bullet(
        "<b>April 2027 Elections:</b> Three trustees are up for re-election and Dearborn himself "
        "faces reelection in 2027. The affordable housing deadline, post office site outcome, and "
        "downtown vitality will all be live issues in that campaign. Delivering visible progress "
        "on these three in the next 12 months is both the right policy move and the strongest "
        "foundation for re-election."
    ))

    story += polling_callout(
        issue="Downtown Parking & Retail Vitality",
        timing="Before the downtown study session (May/June 2026)",
        questions=[
            "How often do you shop at retail stores in downtown Winnetka?",
            "What is the biggest barrier to shopping downtown more frequently?",
            "Would you support a dedicated short-term parking zone for retail customers on Lincoln Ave?",
        ],
        why="Retail owners are vocal but may not represent the broader community's experience. "
            "Polling the full constituency on parking behavior gives you data to design a solution "
            "that works for residents — not just the businesses asking for help.",
        action="Contact Good Party to set up a downtown retail and parking poll before the "
               "study session."
    )
    story.append(PageBreak())

    # ── SECTION 6: UNDERSTANDING THE ROLE ─────────────────────────────────
    story += section_header("Understanding This Role", 6)

    story += sub_header("What Is Expected of This Role")
    story.append(body(
        "Winnetka operates under a <b>council-manager form of government</b> — one of the oldest in "
        "Illinois, in place for over 100 years. The Village President chairs the seven-member Village "
        "Council, sets the meeting agenda, and is the chief elected official. The council collectively "
        "sets policy, adopts the budget, and approves major contracts. Day-to-day administration is "
        "handled by the appointed Village Manager (Kristin Kazenas). Your role is <b>legislative "
        "and executive leadership</b> — not operational management."
    ))
    story.append(body(
        "As Village President you also <b>appoint members to Winnetka's ten advisory boards and "
        "commissions</b> — including the Plan Commission, Zoning Board of Appeals, Design Review "
        "Board, and Environmental, Forestry, and Sustainability Commission — confirmed by the full "
        "council. These appointments are significant policy levers. The village holds regular meetings "
        "on the 1st and 3rd Tuesdays at 7:00 PM at Village Hall, 510 Green Bay Road; study sessions "
        "on 2nd Tuesdays."
    ))

    story.append(Spacer(1, 0.08 * inch))
    story += sub_header("What Is Outside the Scope of This Role")
    story.append(body(
        "You are not the manager of village operations. When constituents contact you about a service "
        "complaint — a pothole, a utility billing dispute, a code enforcement question — your role is "
        "to connect them to the appropriate department, not to personally investigate or resolve. "
        "Direct service requests to Village Manager Kazenas (847-716-3541) or the relevant "
        "department head."
    ))
    story.append(body(
        "Avoid directing village staff outside the chain of command. Individual trustee or president "
        "directives to department staff — even well-intentioned ones — undermine the "
        "council-manager accountability structure and can create legal exposure. Route all "
        "operational directives through the Village Manager."
    ))

    story.append(Spacer(1, 0.08 * inch))
    story += sub_header("How to Represent Your Community")
    story.append(body(
        "Winnetka's Haystaq data shows a center-left, environmentally engaged, infrastructure-focused "
        "community. Meeting attendees skew older and more activated on specific issues. Use constituent "
        "data and Good Party polling to ensure decisions reflect the full community's priorities — not "
        "just who shows up on any given Tuesday. Transparency on the compounding annual fee increases "
        "(~$907 above FY2024 over two years) is your most important trust-building tool going into "
        "the FY2027 budget cycle."
    ))
    story.append(PageBreak())

    # ── SECTION 7: TOP 3 BUDGET DISCUSSIONS ───────────────────────────────
    story += section_header("Top 3 Budget Discussions (Last 6 Months)", 7)

    story += sub_header("1. FY2026 Budget Adoption — December 2, 2025")
    story.append(body(
        "The Village Council unanimously adopted the FY2026 budget on December 2, 2025 — the most "
        "capital-intensive in village history. "
        "(<a href='https://www.therecordnorthshore.org/2025/12/04/winnetkas-26-budget-plans-for-record-capital-costs-440-more-in-fees-per-resident/' color='blue'>"
        "The Record, Dec 4 2025</a>)"
    ))
    for b_item in [
        "Total budget: <b>$116.2M</b> (+23.1%) — Operating $76.73M, Capital $39.48M (record high).",
        "Property tax levy increase: <b>2.33%</b> (0.88% new development + 1.45% inflation).",
        "Average resident fee increase: <b>~$440/year</b> across all utility services (+4.93%).",
        "Five new full-time positions funded, including <b>4 additional police officers</b>.",
        "Largest capital line: <b>Willow Road reconstruction at $18.4M</b> (~$10M covered by grants).",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Adopted unanimously December 2, 2025."))
    story.append(body(
        "<b>Why This Matters:</b> Combined with FY2025's ~$467/year increase, residents are now "
        "~$907/year above FY2024 levels. FY2027 budget discussions begin in fall 2026 — frame every "
        "spending decision in terms of what it delivers, not just what it costs."
    ))
    story.append(Spacer(1, 0.1 * inch))
    story.append(hr())

    story += sub_header("2. 1% Home Rule Sales Tax — Adopted July 2025")
    story.append(body(
        "The council approved a new 1% home rule sales tax (excluding groceries, prescriptions, "
        "vehicles) plus a grocery replacement tax, creating a new downtown capital improvement "
        "revenue stream. "
        "(<a href='https://www.therecordnorthshore.org/2025/07/09/winnetka-eyes-1-sales-tax-and-1-grocery-tax/' color='blue'>"
        "The Record, Jul 9 2025</a>)"
    ))
    for b_item in [
        "Expected yield: approximately <b>$1.5M/year</b> from the 1% sales tax.",
        "Designated uses: Hubbard Woods streetscape (<b>$10.96M</b> budgeted) and post office "
        "plaza improvements (<b>$8.39M</b> budgeted).",
        "Grocery tax adopted to replace expiring Illinois state grocery tax.",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Both taxes adopted; in effect for FY2026."))
    story.append(body(
        "<b>Why This Matters:</b> This is the funding mechanism for two of your highest-priority "
        "capital projects. Residents will expect to see these projects advance — the post office "
        "plaza in particular is now connected to a dedicated tax stream."
    ))
    story.append(Spacer(1, 0.1 * inch))
    story.append(hr())

    story += sub_header("3. FY2025 Budget — First Levy Increase in Nine Years")
    story.append(body(
        "The FY2025 budget (adopted under predecessor Village President Chris Rintz) broke a "
        "nine-year property tax levy freeze, adding ~$467/year per typical household across "
        "all fees. "
        "(<a href='https://www.therecordnorthshore.org/2024/11/21/winnetka-fee-and-tax-increases-means-hundreds-more-per-resident/' color='blue'>"
        "The Record, Nov 21 2024</a>)"
    ))
    for b_item in [
        "Property tax levy: <b>$16.29M</b>, a 3.4% increase (+$85/year typical homeowner).",
        "Utility fee increases: electric +7.2%, water +10%, sewer +4.9%, refuse +3.5%.",
        "Context: public safety pension obligations had grown substantially during the nine-year freeze.",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Adopted; ended the nine-year levy freeze."))
    story.append(body(
        "<b>Why This Matters:</b> The two-year run of large increases sets a difficult context "
        "for FY2027. Proactively building the narrative around what these dollars fund — and "
        "whether the rate of increase can moderate — will define your first budget cycle."
    ))
    story.append(PageBreak())

    # ── SECTION 8: TOP 3 NON-BUDGET POLICY DISCUSSIONS ────────────────────
    story += section_header("Top 3 Non-Budget Policy Discussions (Last 6 Months)", 8)

    story += sub_header("1. IMEA Energy Contract Renewal — June 17, 2025")
    story.append(body(
        "The most publicly contested decision of Dearborn's presidency. After 14 months of review "
        "and 5 study sessions, the council voted unanimously to renew Winnetka's IMEA power contract "
        "through 2055. "
        "(<a href='https://www.therecordnorthshore.org/2025/06/23/this-is-a-hard-issue-amid-ongoing-concerns-from-residents-winnetka-trustees-approve-controversial-extension-with-imea/' color='blue'>"
        "The Record, Jun 23 2025</a>)"
    ))
    for b_item in [
        "<b>11 residents spoke against</b> at the meeting; Go Green Winnetka coalition was central.",
        "Core concern: IMEA's Prairie State Coal Plant is among America's top 12 climate polluters; "
        "critics argued the 2055 renewal locks in coal dependency.",
        "IMEA commitments: net-zero by 2050; <b>Bee Hollow 150-MW solar</b> project online late 2026.",
        "Winnetka households use <b>2.4x more electricity per capita</b> than other Cook County "
        "households — amplifying both the environmental stakes and cost exposure.",
        "Dearborn on the vote: <i>\"This is a hard issue. This weighs very heavily...\"</i>",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Contract renewed unanimously through 2055."))
    story.append(body(
        "<b>Why This Matters:</b> Constituent climate scores (61.8) confirm this community cares "
        "deeply. The issue is not politically resolved. Monitoring IMEA's milestones and reporting "
        "back publicly is your primary ongoing management tool."
    ))
    story.append(Spacer(1, 0.1 * inch))
    story.append(hr())

    story += sub_header("2. One Winnetka — Completion Approaching, Retail Gap Emerging")
    story.append(body(
        "After more than a decade of debate, One Winnetka broke ground in spring 2025. The "
        "four-story mixed-use development (59 rental units, 20,955 sq ft commercial, 150 parking "
        "spaces) is on track to complete October 2026. "
        "(<a href='https://www.therecordnorthshore.org/2026/02/19/trustees-concerned-over-one-winnetkas-lack-of-retail-parking/' color='blue'>"
        "The Record, Feb 19 2026</a>)"
    ))
    for b_item in [
        "By February 2026, all <b>six signed commercial tenants</b> were service/dining businesses — "
        "<b>zero traditional retail</b>.",
        "Trustee Orsic: <i>\"Is there nothing more vibrant that we can put in this space?\"</i>",
        "Construction is simultaneously deterring existing Lincoln Avenue shoppers by reducing "
        "parking access during the build-out.",
        "Developer Murphy Development Group acknowledged failed efforts to recruit retail tenants.",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Under construction; completion targeted October 2026."))
    story.append(body(
        "<b>Why This Matters:</b> The October 2026 opening is a major first-term moment. Shaping "
        "the narrative proactively — housing units added, parking provided, tax base created — "
        "is more effective than reacting to the retail gap disappointment after opening."
    ))
    story.append(Spacer(1, 0.1 * inch))
    story.append(hr())

    story += sub_header("3. Lakefront Bluff Regulations — Federal Lawsuit Dismissed Without Prejudice")
    story.append(body(
        "In February 2024 the council adopted a bluff ordinance restricting construction near the "
        "lakefront (demarcation at 581.5 ft OHWM). Property owners filed a federal takings lawsuit "
        "in May 2024; Judge LaShonda Hunt (N.D. Ill.) dismissed it without prejudice in October 2025. "
        "(<a href='https://www.therecordnorthshore.org/2025/10/14/judge-dismisses-winnetka-property-owners-lawsuit-challenging-lakefront-regulations/' color='blue'>"
        "The Record, Oct 14 2025</a>)"
    ))
    for b_item in [
        "Property owners claimed the ordinance caused <b>'tens of millions'</b> in lost property value.",
        "Dismissal was on <b>ripeness grounds only</b> — the merits were not ruled on; refiling is possible.",
        "The ordinance remains in effect. The Ishbia family parcel combination proposal that partly "
        "triggered the ordinance remains unresolved.",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Lawsuit dismissed without prejudice; ordinance in effect."))
    story.append(body(
        "<b>Why This Matters:</b> A refiled suit is possible. Ensure Village Attorney Peter Friedman "
        "(Elrod Friedman LLP, 312-578-6566) has strategy and budget in place to respond. Any future "
        "ordinance touching lakefront property rights should be reviewed for takings vulnerability "
        "before adoption."
    ))
    story.append(PageBreak())

    # ── SECTION 9: APPENDIX ────────────────────────────────────────────────
    story += section_header("Appendix: Key Resources", 9)

    story.append(body("<b>Village of Winnetka — Contact & Reference</b>"))
    story.append(body("510 Green Bay Road, Winnetka, IL 60093  ·  (847) 501-6000  ·  info@winnetka.org"))

    resources = [
        ("Official Website",          "https://www.villageofwinnetka.org"),
        ("Agendas & Minutes",          "https://www.villageofwinnetka.org/129/Agendas-Minutes"),
        ("Budget & Finance",           "https://www.villageofwinnetka.org/175/Finance"),
        ("Fiscal Transparency Portal", "https://www.villageofwinnetka.org/277/Fiscal-Transparency"),
        ("Post Office Redevelopment",  "https://www.villageofwinnetka.org/395/Post-Office-Site-Redevelopment"),
        ("Lead Service Line Program",  "https://www.villageofwinnetka.org/386/Lead-Service-Line-Replacement"),
        ("IMEA Information Page",      "https://www.villageofwinnetka.org/417/Illinois-Municipal-Electric-Agency-IMEA"),
        ("Boards & Commissions",       "https://www.villageofwinnetka.org/240/Boards-Commissions"),
    ]
    res_data = [[Paragraph("Resource", TH_WHITE_BOLD), Paragraph("URL", TH_WHITE_BOLD)]] + [
        [Paragraph(label, TD_BOLD_LEFT),
         Paragraph(f'<a href="{url}" color="blue">{url}</a>', TD_LEFT)]
        for label, url in resources
    ]
    res_table = Table(res_data, colWidths=[2.2*inch, 5.3*inch])
    res_table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0), NAVY),
        ("BOX",            (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("INNERGRID",      (0, 0), (-1, -1), 0.25, LIGHT_GRAY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, LIGHT_BLUE]),
        ("TOPPADDING",     (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
        ("LEFTPADDING",    (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 8),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story += [Spacer(1, 0.1*inch), res_table, Spacer(1, 0.15*inch)]

    story.append(body("<b>Key Staff Contacts</b>"))
    contacts = [
        ("Village President",      "Robert Dearborn",   "rdearborn@winnetka.org",  "(847) 716-3541"),
        ("Village Manager",        "Kristin Kazenas",   "—",                       "(847) 716-3541"),
        ("Deputy Village Manager", "Hannah Lipman",     "—",                       "(847) 716-3543"),
        ("CFO / Finance",          "Timothy Sloth, CPA","tsloth@winnetka.org",     "(847) 716-3513"),
        ("Police Chief",           "Brian O'Connell",   "—",                       "(847) 716-3400"),
        ("Dir. Public Works",      "Tom Powers",        "—",                       "(847) 716-3270"),
        ("Dir. Water & Electric",  "Nick Narhi",        "—",                       "(847) 716-3558"),
        ("Dir. Community Dev.",    "Scott Mangum",      "—",                       "(847) 716-3526"),
        ("Communications Mgr.",    "Josie Clark",       "—",                       "(847) 716-3545"),
        ("Village Attorney",       "Peter M. Friedman", "—",                       "(312) 578-6566"),
    ]
    contacts_data = [[Paragraph(h, TH_WHITE_BOLD)
                      for h in ["Role", "Name", "Email", "Phone"]]] + [
        [Paragraph(r, TD_LEFT), Paragraph(n, TD_BOLD_LEFT),
         Paragraph(e, TD_LEFT), Paragraph(p, TD_CENTER)]
        for r, n, e, p in contacts
    ]
    contacts_table = Table(contacts_data,
                           colWidths=[1.6*inch, 1.5*inch, 2.3*inch, 2.1*inch])
    contacts_table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0), NAVY),
        ("BOX",            (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("INNERGRID",      (0, 0), (-1, -1), 0.25, LIGHT_GRAY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, LIGHT_BLUE]),
        ("TOPPADDING",     (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 4),
        ("LEFTPADDING",    (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 6),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story += [contacts_table, Spacer(1, 0.15*inch)]

    story.append(body("<b>Key Financial Figures (FY2026)</b>"))
    fin_data = [
        [Paragraph("Item", TH_WHITE_BOLD), Paragraph("Amount / Detail", TH_WHITE_BOLD)],
        [Paragraph("Total Budget (FY2026)", TD_BOLD_LEFT),        Paragraph("$116.2M (+23.1% from 2025)", TD_LEFT)],
        [Paragraph("Capital Budget (record)", TD_BOLD_LEFT),      Paragraph("$39.48M", TD_LEFT)],
        [Paragraph("Operating Budget", TD_BOLD_LEFT),             Paragraph("$76.73M", TD_LEFT)],
        [Paragraph("Tax Levy Increase (FY2026)", TD_BOLD_LEFT),   Paragraph("2.33%", TD_LEFT)],
        [Paragraph("Resident Fee Impact (FY2026)", TD_BOLD_LEFT), Paragraph("~$440/year additional", TD_LEFT)],
        [Paragraph("2-Year Cumulative Impact", TD_BOLD_LEFT),     Paragraph("~$907/year above FY2024", TD_LEFT)],
        [Paragraph("Sales Tax Revenue (new)", TD_BOLD_LEFT),      Paragraph("~$1.5M/year (1% home rule)", TD_LEFT)],
        [Paragraph("Willow Road Reconstruction", TD_BOLD_LEFT),   Paragraph("$18.4M (~$10M grant-funded)", TD_LEFT)],
        [Paragraph("Board Meeting Schedule", TD_BOLD_LEFT),       Paragraph("1st & 3rd Tuesday, 7:00 PM — 510 Green Bay Rd", TD_LEFT)],
    ]
    fin_table = Table(fin_data, colWidths=[2.5*inch, 5.0*inch])
    fin_table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0), NAVY),
        ("BOX",            (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("INNERGRID",      (0, 0), (-1, -1), 0.25, LIGHT_GRAY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, LIGHT_BLUE]),
        ("TOPPADDING",     (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 4),
        ("LEFTPADDING",    (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 6),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(fin_table)

    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph(
        "Prepared by Good Party  ·  goodparty.org  ·  May 2026  ·  "
        "Questions? Contact your Good Party representative.",
        CAPTION
    ))

    doc.build(story)
    print(f"Briefing saved to: {output_path}")


if __name__ == "__main__":
    output = "/Users/kaylee/gp-ai-projects/runbooks/briefings/robert_dearborn_winnetka_president_briefing_v2.pdf"
    build_briefing(output)
