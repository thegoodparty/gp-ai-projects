"""
Elected Official Briefing — Robert Dearborn, Village President, Winnetka IL
Generated: May 2026
"""

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white, black
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    Table, TableStyle, HRFlowable
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

# ── Color Palette ──────────────────────────────────────────────────────────
NAVY       = HexColor("#1B3A6B")
LIGHT_BLUE = HexColor("#E8F0FA")
ACCENT     = HexColor("#2E6DB4")
GOLD       = HexColor("#F2A900")
TIER2_BG   = HexColor("#E8F5E9")   # green tint — Strong 60-74
TIER3_BG   = HexColor("#FFF8E1")   # amber tint — Moderate 50-59
BELOW_BG   = HexColor("#FAFAFA")   # near-white — Below 50
POLL_BG    = HexColor("#EEF4FF")   # light blue — polling callout
DARK_TEXT  = HexColor("#212121")
MID_TEXT   = HexColor("#555555")
LIGHT_GRAY = HexColor("#CCCCCC")

# ── Paragraph Styles ───────────────────────────────────────────────────────
def S(name, **kwargs):
    return ParagraphStyle(name, **kwargs)

TITLE_NAME = S("TITLE_NAME", fontSize=26, fontName="Helvetica-Bold",
               textColor=white, leading=32, alignment=TA_CENTER)
TITLE_SUB  = S("TITLE_SUB",  fontSize=13, fontName="Helvetica",
               textColor=HexColor("#D0DFF5"), leading=18, alignment=TA_CENTER)
TITLE_DATE = S("TITLE_DATE", fontSize=11, fontName="Helvetica",
               textColor=HexColor("#A0B8D8"), leading=16, alignment=TA_CENTER)

H1   = S("H1",   fontSize=16, fontName="Helvetica-Bold", textColor=NAVY,
         spaceBefore=18, spaceAfter=8, leading=20)
H2   = S("H2",   fontSize=12, fontName="Helvetica-Bold", textColor=ACCENT,
         spaceBefore=12, spaceAfter=6, leading=16)
BODY = S("BODY", fontSize=10, fontName="Helvetica", textColor=DARK_TEXT,
         leading=15, spaceAfter=6)
BODY_SM   = S("BODY_SM",  fontSize=9,  fontName="Helvetica",
              textColor=DARK_TEXT, leading=13)
BODY_BOLD = S("BODY_BOLD", fontSize=10, fontName="Helvetica-Bold",
              textColor=DARK_TEXT, leading=15)
CAPTION   = S("CAPTION",  fontSize=8,  fontName="Helvetica-Oblique",
              textColor=MID_TEXT, leading=11)
BULLET    = S("BULLET",   fontSize=10, fontName="Helvetica",
              textColor=DARK_TEXT, leading=14, leftIndent=14,
              firstLineIndent=-10, spaceAfter=5)
POLL_TITLE = S("POLL_TITLE", fontSize=10, fontName="Helvetica-Bold",
               textColor=ACCENT, leading=14)
POLL_BODY  = S("POLL_BODY",  fontSize=9,  fontName="Helvetica",
               textColor=DARK_TEXT, leading=13)
POLL_LABEL = S("POLL_LABEL", fontSize=9,  fontName="Helvetica-Bold",
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


# ── Helper builders ────────────────────────────────────────────────────────

def section_header(title, number=None):
    label = f"SECTION {number} — " if number else ""
    return [
        Spacer(1, 0.15 * inch),
        Table(
            [[Paragraph(f"{label}{title.upper()}", TH_WHITE_BOLD)]],
            colWidths=[7.5 * inch],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
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


def polling_callout(issue, timing, questions, why, action):
    rows = [
        [Paragraph("GOOD PARTY POLLING OPPORTUNITY", POLL_TITLE)],
        [Paragraph(f"<b>Issue:</b> {issue}", POLL_BODY)],
        [Paragraph(f"<b>Timing:</b> {timing}", POLL_BODY)],
        [Paragraph("<b>Key Questions:</b>", POLL_LABEL)],
    ]
    for q in questions:
        rows.append([Paragraph(f"  • {q}", POLL_BODY)])
    rows.append([Paragraph(f"<b>Why This Matters:</b> {why}", POLL_BODY)])
    rows.append([Paragraph(f"<b>Action:</b> {action}", POLL_BODY)])

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
    header = [
        Paragraph("Rank", TH_WHITE_BOLD),
        Paragraph("Issue", TH_WHITE_BOLD),
        Paragraph("Score", TH_WHITE_BOLD),
        Paragraph("Tier", TH_WHITE_BOLD),
    ]
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
            score_style, tier_style = SCORE_STRONG, TIER_STRONG
        elif tier == "Moderate":
            score_style, tier_style = SCORE_MODERATE, TIER_MODERATE
        else:
            score_style, tier_style = SCORE_BELOW, TIER_BELOW

        data.append([
            Paragraph(str(rank), TD_CENTER),
            Paragraph(issue, TD_LEFT),
            Paragraph(f"{score:.1f}", score_style),
            Paragraph(tier, tier_style),
        ])
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))

    t = Table(data, colWidths=[0.5 * inch, 3.8 * inch, 1.0 * inch, 2.2 * inch])
    t.setStyle(TableStyle(style_cmds))
    return t


def demographics_table(rows_data):
    data = [
        [Paragraph("Metric", TH_WHITE_BOLD), Paragraph("Value", TH_WHITE_BOLD)]
    ] + [
        [Paragraph(label, TD_BOLD_LEFT), Paragraph(value, TD_CENTER)]
        for label, value in rows_data
    ]
    t = Table(data, colWidths=[3.5 * inch, 4.0 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("BOX",           (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("INNERGRID",     (0, 0), (-1, -1), 0.25, LIGHT_GRAY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [white, LIGHT_BLUE]),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def hr():
    return HRFlowable(width="100%", thickness=0.5, color=LIGHT_GRAY,
                      spaceAfter=6, spaceBefore=6)


# ── Document assembly ──────────────────────────────────────────────────────

def build_briefing(output_path):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    story = []

    # ── TITLE PAGE ─────────────────────────────────────────────────────────
    title_data = [[Paragraph("ELECTED OFFICIAL BRIEFING", TITLE_SUB)]]
    title_banner = Table(title_data, colWidths=[7.5 * inch])
    title_banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 28),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 20),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 20),
    ]))

    name_data = [[Paragraph("Robert Dearborn", TITLE_NAME)]]
    name_banner = Table(name_data, colWidths=[7.5 * inch])
    name_banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 20),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 20),
    ]))

    role_data = [[Paragraph("Village President  ·  Village of Winnetka, Illinois", TITLE_SUB)]]
    role_banner = Table(role_data, colWidths=[7.5 * inch])
    role_banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 28),
        ("LEFTPADDING",   (0, 0), (-1, -1), 20),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 20),
    ]))

    story += [title_banner, name_banner, role_banner, Spacer(1, 0.3 * inch)]
    story.append(Paragraph("May 2026  ·  Prepared by Good Party", CAPTION))
    story.append(Spacer(1, 0.5 * inch))

    story.append(body(
        "This briefing is designed to help you hit the ground running as the newly elected Village President "
        "of Winnetka. It gives you the context, constituent data, and specific action opportunities you need "
        "to be effective from day one. Each section is one page or less for quick reference."
    ))

    story.append(Spacer(1, 0.3 * inch))
    toc_data = [
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
    toc_table = Table(
        [[Paragraph("#", TH_WHITE_BOLD), Paragraph("Section", TH_WHITE_BOLD), Paragraph("Page", TH_WHITE_BOLD)]]
        + [[Paragraph(r, TD_CENTER), Paragraph(t, TD_LEFT), Paragraph(p, TD_CENTER)]
           for r, t, p in toc_data],
        colWidths=[0.5 * inch, 6.0 * inch, 1.0 * inch],
    )
    toc_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("BOX",           (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("INNERGRID",     (0, 0), (-1, -1), 0.25, LIGHT_GRAY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [white, LIGHT_BLUE]),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story += [toc_table, PageBreak()]

    # ── SECTION 1: EXECUTIVE SUMMARY ──────────────────────────────────────
    story += section_header("Executive Summary — Transition Roadmap", 1)

    story.append(body("<b>The Situation</b>"))
    story.append(body(
        "You're stepping into the Village President role with eight years on the board already behind you — "
        "which means you know the docket cold, but it also means the community will hold you accountable for "
        "progress, not just preparation. Voter data from 10,313 Winnetka constituents shows a community that "
        "leans center-left (liberal ideology score: 59.1), prioritizes infrastructure investment (61.8), "
        "police trust (61.9), and environmental action (60.1). You're inheriting a record-high $116.2M capital "
        "budget, a new village manager six months into her tenure, an IHDA affordable housing compliance "
        "deadline of October 1, 2026, and a downtown that is mid-transformation with One Winnetka construction "
        "underway and a post office site awaiting a community vision."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story.append(body("<b>Your First 90 Days: Top 4 Focus Areas</b>"))
    story.append(body(
        "First, <b>anchor your relationship with Village Manager Kristin Kazenas</b> — she took the role just "
        "seven months ago (October 2025) and is navigating her first full budget cycle while managing the most "
        "capital-intensive program in village history. A strong working partnership on Willow Road, the post "
        "office site, and the affordable housing deadline will define execution quality across all of your "
        "priorities. Second, <b>launch post office site public engagement immediately</b>: demolition is "
        "scheduled for April 2026 and the community expects to shape the permanent vision for this highly "
        "visible site — delay on engagement creates frustration and rumors. Third, <b>get ahead of the IHDA "
        "affordable housing deadline (October 1, 2026)</b>: Winnetka's first submission was rejected as "
        "non-compliant; the revised August 2025 plan was adopted but Dearborn himself acknowledged it is "
        "'more the beginning, not the end.' Concrete, credible follow-through is needed to avoid state "
        "intervention. Fourth, <b>convene a downtown parking and retail viability study session</b>: retail "
        "shop owners formally requested help in March 2026. One Winnetka's lack of traditional retail and "
        "construction-driven traffic disruption are converging into a visible community frustration you "
        "are uniquely positioned to address early."
    ))

    story.append(PageBreak())

    # ── SECTION 2: LESSONS LEARNED ────────────────────────────────────────
    story += section_header("Lessons Learned From Recent History", 2)

    lessons = [
        (
            "1. Unanimous votes don't end contentious debates — you need an ongoing communication strategy.",
            "The IMEA energy contract renewal passed unanimously in June 2025, yet 11 residents spoke "
            "against it at the meeting and the Go Green Winnetka coalition remains active. Framing the "
            "vote as 'done' without tracking IMEA's renewable milestones (Bee Hollow solar, net-zero "
            "by 2050) will leave critics with no progress to point to. Proactive communication on "
            "IMEA's transition timeline is the only way to gradually satisfy that constituency."
        ),
        (
            "2. Development approval is not tenant mix control — set expectations earlier.",
            "The council approved One Winnetka with a stated vision for vibrant downtown retail. By "
            "February 2026, all six signed commercial tenants were service businesses — zero traditional "
            "retail. Trustee concerns came too late to change outcomes. Future development agreements "
            "with retail expectations should include more explicit tenant-type commitments or at minimum "
            "honest upfront discussion of what the market will actually deliver."
        ),
        (
            "3. Local ordinances affecting property rights invite federal litigation — budget for it.",
            "The lakefront bluff ordinance (adopted February 2024) was challenged in federal court by "
            "affected property owners within months. The case was dismissed without prejudice in October "
            "2025, but 'without prejudice' means a refiled suit remains possible. Any future "
            "ordinance touching private property rights — especially high-value lakefront parcels — "
            "should involve upfront legal cost projections and a communications plan for affected owners."
        ),
        (
            "4. Resident fee increases require context, not just totals.",
            "The FY2025 budget raised utility fees and the property tax levy, adding ~$467/year for a "
            "typical household. The FY2026 budget adds another ~$440/year. These numbers, presented in "
            "isolation, generate sticker shock. Leading with service delivery improvements, the "
            "infrastructure investment rationale, and the nine-year period without levy increases "
            "gives residents the context to accept — or at least understand — the cumulative increases."
        ),
        (
            "5. Community transitions require proactive succession management.",
            "Village Manager Rob Bahan retired after 15 years in September 2025. Deputy Manager "
            "Kristin Kazenas stepped up, but simultaneously the Community Development director (David "
            "Schoon) and Economic Development Coordinator (Liz Dechant) both departed in spring 2025. "
            "Three senior departures in rapid succession created institutional knowledge gaps at a "
            "moment of maximum project complexity. Managing staff continuity should be an explicit "
            "board priority going forward."
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
            "Launch the post office site public engagement process",
            "Demolition of the old post office building is planned for April 2026. The council already "
            "authorized a land use planning consultant in October 2025. Activating that engagement now — "
            "with clear questions about the permanent vision — demonstrates responsive leadership on "
            "Winnetka's highest-visibility redevelopment opportunity. A 'town square' concept has "
            "been mentioned publicly; polling or open houses will build a genuine mandate."
        ),
        (
            "Convene a downtown parking and retail viability study session",
            "Retail shop owners formally asked for help in March 2026. Village President Dearborn "
            "acknowledged the issue and signaled a study session. Scheduling it quickly — within "
            "30–60 days — turns that signal into action. Solutions could include time-limited parking "
            "enforcement, temporary construction mitigation measures, or business district support "
            "programs. Delaying into summer hurts the business community's peak season."
        ),
        (
            "Request a briefing from Village Manager Kazenas on affordable housing compliance",
            "The IHDA deadline is October 1, 2026. Winnetka's first submission was rejected; the "
            "revised plan was adopted in August 2025. A dedicated session with Kazenas and Director "
            "of Community Development Scott Mangum to map the specific steps required between now "
            "and October will surface gaps early. This also signals to staff that you're treating "
            "the deadline seriously — not just as a paperwork exercise."
        ),
        (
            "Champion lead service line replacement acceleration",
            "The village's rebate program has a 140-person waitlist — demand far exceeds current "
            "replacement capacity of 90–100 lines/year. Given constituent infrastructure scores (61.8) "
            "and climate/health concerns (61.8), announcing a review of whether the replacement pace "
            "can be accelerated — through expanded staffing, contractor capacity, or additional funding "
            "in a mid-year amendment — is a visible, tangible commitment to both health and "
            "infrastructure that residents can feel."
        ),
        (
            "Issue a public update on IMEA's renewable energy transition milestones",
            "The IMEA contract renewal was the most publicly contested decision of your first year. "
            "The Bee Hollow solar project is expected online in late 2026 — a concrete milestone. "
            "A proactive public update on IMEA's renewable transition timeline, shared through the "
            "village newsletter and website, demonstrates that the board is actively monitoring the "
            "contract's environmental commitments and not simply having made a decision and moved on."
        ),
    ]

    for i, (title, desc) in enumerate(qw, start=1):
        story.append(body(f"<b>{i}. {title}</b>"))
        story.append(body(desc))
        story.append(Spacer(1, 0.06 * inch))

    story += polling_callout(
        issue="Post Office Site — Community Vision",
        timing="Before consultant engagement begins (Summer 2026)",
        questions=[
            "What type of use would you most like to see on the former post office site?",
            "How important is it that the site includes public green space vs. commercial activity?",
            "Would you support a community performance stage / gathering plaza at this location?",
        ],
        why="The post office redevelopment will define downtown Winnetka for decades. Polling "
            "before the consultant engagement gives residents real input into the scope of the "
            "process and gives you data-backed direction rather than relying on vocal participants "
            "in public meetings.",
        action="Contact Good Party to set up a community poll on the post office site before the "
               "planning consultant engagement launches."
    )

    story.append(PageBreak())

    # ── SECTION 4: CONSTITUENTS' TOP ISSUES ───────────────────────────────
    story += section_header("Your Constituents' Top Issues", 4)

    story.append(body(
        "The following data reflects Haystaq voter intelligence scores for 10,313 registered voters "
        "in Winnetka matched across demographic and issue dimensions. Scores are 0–100; higher scores "
        "indicate stronger constituent support or prioritization. Only issues within local/state "
        "government authority are included."
    ))

    story.append(Spacer(1, 0.12 * inch))
    story += sub_header("Demographics at a Glance")
    story.append(demographics_table([
        ("Total Registered Voters", "10,656"),
        ("Average Age", "49.8 years"),
        ("Gender: Female", "5,523  (52%)"),
        ("Gender: Male", "5,133  (48%)"),
    ]))

    story.append(Spacer(1, 0.14 * inch))
    story += sub_header("Top Issues by Priority (Local/State Authority Only)")

    issue_rows = [
        (1,  "Police Trust & Confidence",                61.9, "Strong",   TIER2_BG),
        (2,  "Infrastructure Investment (Fund More)",    61.8, "Strong",   TIER2_BG),
        (3,  "Climate / Environmental Action",           61.8, "Strong",   TIER2_BG),
        (4,  "Environmental Priorities",                 60.1, "Strong",   TIER2_BG),
        (5,  "Tax Policy (Tax Cuts Support)",            58.8, "Moderate", TIER3_BG),
        (6,  "Public Transit Access",                    57.5, "Moderate", TIER3_BG),
        (7,  "School Funding (More Funding Support)",    51.8, "Moderate", TIER3_BG),
        (8,  "Local Economic Development",               51.5, "Moderate", TIER3_BG),
        (9,  "Helping People / Social Services",         49.5, "Below 50", BELOW_BG),
        (10, "Affordable Housing (Government Role)",     47.8, "Below 50", BELOW_BG),
    ]
    story.append(issues_table(issue_rows))
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "Tier Guide: Strong = 60–74 (green)  ·  Moderate = 50–59 (amber)  ·  Below 50 = lower priority. "
        "Ideology: Liberal 59.1 vs. Conservative 40.9. Fiscal: Conservatism 49.5 / Liberal 50.5 (balanced). "
        "Note: Keep Safe score 32.2 — crime is NOT a top concern for Winnetka constituents.",
        CAPTION
    ))
    story.append(Spacer(1, 0.08 * inch))

    story += polling_callout(
        issue="IMEA Energy Contract & Renewable Transition Priorities",
        timing="Before IMEA Bee Hollow solar project goes online (late 2026)",
        questions=[
            "How important is it that Winnetka's electricity comes from renewable sources?",
            "Are you satisfied with the village's progress toward reducing reliance on coal-generated power?",
            "What renewable energy milestones would most increase your confidence in the IMEA contract?",
        ],
        why="The climate believer score (61.8) and environment priority score (60.1) are both Strong — "
            "yet the board voted unanimously for a contract tied to a coal plant through 2055. Polling "
            "helps quantify exactly how urgent residents consider this issue and what milestones would "
            "satisfy the activist community vs. the broader constituency.",
        action="Contact Good Party to set up a community poll on energy and IMEA priorities before "
               "the Bee Hollow solar project goes online."
    )

    story.append(PageBreak())

    # ── SECTION 5: WHAT TO WATCH ───────────────────────────────────────────
    story += section_header("What to Watch & Prepare For", 5)

    story += sub_header("Issues Requiring Immediate Attention")
    story.append(bullet(
        "<b>IHDA Affordable Housing Deadline — October 1, 2026:</b> Winnetka's first submission to "
        "the Illinois Housing Development Authority was rejected as non-compliant. The revised August "
        "2025 plan was adopted, but Dearborn himself described it as 'more the beginning, not the "
        "end.' IHDA's October 1 deadline is firm — non-compliant municipalities face state intervention. "
        "Assign clear staff ownership and establish monthly check-ins with the Village Manager."
    ))
    story.append(bullet(
        "<b>Willow Road Reconstruction (2026 construction season):</b> The $18.4M project — the "
        "largest single capital item in the FY2026 budget — begins construction this year. Grant "
        "funding covers ~$10M. Expect significant traffic disruption and constituent complaints. "
        "Proactive communication on detour routes, project milestones, and completion timeline "
        "will be essential to manage community frustration."
    ))
    story.append(bullet(
        "<b>Downtown Retail and Parking — Pre-Summer Decision Needed:</b> Retail shop owners "
        "formally asked for help in March 2026. A study session was signaled. If that session "
        "doesn't happen before summer, you lose the ability to implement any solutions before "
        "peak retail season. Lincoln Avenue parking enforcement and construction traffic mitigation "
        "are the two most immediate levers."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story += sub_header("Political Dynamics to Understand")
    story.append(bullet(
        "<b>A New Council Majority Elected With You:</b> Trustees Albinson, Myers, and Orsic were "
        "all elected on April 1, 2025 — the same day as Dearborn. They serve through May 2027. "
        "Trustees Apatoff, Dalman, and Handler (holdovers) are up for election in April 2027. "
        "Understanding where each trustee stood on key recent votes — particularly IMEA and "
        "the lakefront bluff ordinance — will shape your coalition-building approach."
    ))
    story.append(bullet(
        "<b>Go Green Winnetka Coalition Remains Active:</b> The organized resident group opposing "
        "the IMEA coal contract renewal has not disbanded since the June 2025 vote. They represent "
        "a vocal, well-organized constituency aligned with your constituents' strong environmental "
        "scores. Engaging them proactively on IMEA milestone monitoring — rather than treating "
        "them as adversaries — is a more sustainable political relationship."
    ))
    story.append(bullet(
        "<b>New Village Manager Relationship Is Critical:</b> Kristin Kazenas replaced a "
        "15-year veteran manager in October 2025. She's highly capable and promoted from within, "
        "but she is navigating a record capital program, multiple staff vacancies (Community "
        "Development director David Schoon and Economic Development Coordinator Liz Dechant both "
        "departed in spring 2025), and a new council majority — all simultaneously. "
        "A strong, clear working relationship between you will be the deciding factor in execution quality."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story += sub_header("Upcoming Challenges or Decisions")
    story.append(bullet(
        "<b>Lakefront Bluff Litigation — Possible Refiling:</b> The federal lawsuit challenging "
        "Winnetka's bluff regulations was dismissed without prejudice in October 2025. The "
        "dismissal was on ripeness grounds, not the merits — meaning property owners could "
        "refile with a more developed factual record. The village should maintain litigation "
        "readiness and ensure the Village Attorney (Peter Friedman, Elrod Friedman LLP) has "
        "adequate budget and strategy in place."
    ))
    story.append(bullet(
        "<b>One Winnetka Completion & Tenant Mix (October 2026):</b> The development is on "
        "track for October 2026. With zero traditional retail among the first six commercial "
        "tenants, the council faces a credibility question — the development was championed "
        "as a downtown revitalization anchor. Communicate proactively about what the "
        "development does deliver (housing, parking, tax base) vs. the retail mix aspiration."
    ))
    story.append(bullet(
        "<b>April 2027 Elections — Early Positioning:</b> Three trustees are up for "
        "re-election in April 2027, and Dearborn himself is up for a second term. "
        "The affordable housing deadline, post office site outcome, and downtown vitality "
        "will all be live issues in that campaign. Delivering visible progress on these "
        "three in the next 12 months is both the right policy move and the strongest "
        "re-election foundation."
    ))

    story += polling_callout(
        issue="Downtown Parking & Retail Vitality",
        timing="Before the downtown study session (Summer/Fall 2026)",
        questions=[
            "How often do you shop at retail stores in downtown Winnetka (Lincoln Ave / Elm St)?",
            "What is the biggest barrier to shopping downtown more frequently?",
            "Would you support a dedicated short-term parking zone for retail customers on Lincoln Avenue?",
        ],
        why="Retail owners are vocal but may not represent the broader constituency's experience. "
            "Polling the full community on parking behavior gives you data to design a parking solution "
            "that actually works for residents — not just for the businesses asking.",
        action="Contact Good Party to set up a downtown retail and parking poll before the scheduled "
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
        "sets policy, adopts the budget, and approves major contracts and appointments. Day-to-day "
        "administration is handled by the appointed Village Manager (Kristin Kazenas). Your role is "
        "<b>legislative and executive leadership</b> — not operational management."
    ))
    story.append(body(
        "As Village President you also <b>appoint members to Winnetka's ten advisory boards and "
        "commissions</b> (confirmed by the full council) — including the Plan Commission, Zoning "
        "Board of Appeals, Design Review Board, and Environmental, Forestry, and Sustainability "
        "Commission. These appointments are significant policy levers that don't require a full "
        "council vote. The village schedules regular meetings on the 1st and 3rd Tuesdays at 7:00 PM "
        "at Village Hall, 510 Green Bay Road; study sessions occur on 2nd Tuesdays."
    ))
    story.append(body(
        "Your constituent email rdearborn@winnetka.org is public; the full council can also be "
        "reached at contactcouncil@winnetka.org. Use your communications manager (Josie Clark, "
        "847-716-3545) proactively — Winnetka residents are informed and engaged, and "
        "newsletter/website updates dramatically reduce reactive constituent calls."
    ))

    story.append(Spacer(1, 0.08 * inch))
    story += sub_header("What Is Outside the Scope of This Role")
    story.append(body(
        "You are not the manager of village operations. When constituents contact you about a service "
        "complaint — a pothole, a utility billing dispute, a code enforcement question — your role is "
        "to direct them to the appropriate department, not to personally investigate or intervene. "
        "Direct service requests to Village Manager Kazenas (847-716-3541) or the relevant department head."
    ))
    story.append(body(
        "Avoid directing village staff outside the chain of command. The council-manager structure "
        "depends on the council setting policy and the manager executing it. Individual trustee or "
        "president directives to department staff — even with good intentions — undermine accountability "
        "and create legal exposure. Route all operational directives through the Village Manager."
    ))

    story.append(Spacer(1, 0.08 * inch))
    story += sub_header("How to Represent Your Community")
    story.append(body(
        "Winnetka's Haystaq data shows a center-left, environmentally engaged, infrastructure-focused "
        "community — not a uniformly vocal constituency. Meeting attendees skew older, more established, "
        "and more activated on specific issues. Use constituent data and Good Party polling to ensure "
        "that decisions reflect the full community's priorities, not just who shows up on any given "
        "Tuesday. Transparency on financial decisions — especially the compounding annual fee increases "
        "— will build the trust needed for larger future asks like capital bond questions."
    ))

    story.append(PageBreak())

    # ── SECTION 7: TOP 3 BUDGET DISCUSSIONS ───────────────────────────────
    story += section_header("Top 3 Budget Discussions (Last 6 Months)", 7)

    # Budget 1
    story += sub_header("1. FY2026 Budget Adoption — December 2, 2025")
    story.append(body(
        "The Village Council unanimously adopted the FY2026 budget on December 2, 2025 — the most "
        "capital-intensive budget in village history. "
        "(<a href='https://www.therecordnorthshore.org/2025/12/04/winnetkas-26-budget-plans-for-record-capital-costs-440-more-in-fees-per-resident/' color='blue'>"
        "The Record: Winnetka's 2026 Budget</a>)"
    ))
    for b_item in [
        "Total budget: <b>$116.2 million</b> (+23.1% from 2025) — Operating $76.73M, Capital $39.48M (record high).",
        "Property tax levy increase: <b>2.33%</b> (0.88% new development + 1.45% inflation).",
        "Average resident fee increase: <b>~$440/year</b> across all utility services (+4.93%).",
        "New revenue: <b>1% home rule sales tax</b> (~$1.5M/year) designated for downtown improvements "
        "(Hubbard Woods streetscape: $10.96M; post office plaza: $8.39M).",
        "Staffing: <b>5 new full-time positions</b>, including 4 additional police officers. "
        "Largest capital line: Willow Road reconstruction at <b>$18.4 million</b>.",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Adopted unanimously December 2, 2025."))
    story.append(body(
        "<b>Why This Matters:</b> The cumulative resident impact is now ~$907/year above FY2024 levels "
        "($467 from 2025 + $440 from 2026). As the new Village President, you will be the public face "
        "of the next budget cycle — frame it clearly in terms of infrastructure investment and service "
        "delivery, or the compounding sticker shock will become a political liability."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story.append(hr())

    # Budget 2
    story += sub_header("2. 1% Home Rule Sales Tax — Adopted July 2025")
    story.append(body(
        "The council approved a new <b>1% home rule sales tax</b> (excluding groceries, prescriptions, "
        "and vehicles) and a separate grocery replacement tax, generating a combined new revenue stream "
        "for downtown capital improvement projects. "
        "(<a href='https://www.therecordnorthshore.org/2025/07/09/winnetka-eyes-1-sales-tax-and-1-grocery-tax/' color='blue'>"
        "The Record: Winnetka Eyes Sales Tax</a>)"
    ))
    for b_item in [
        "Expected yield: approximately <b>$1.5 million/year</b> from the 1% sales tax.",
        "Designated uses: Hubbard Woods Business District streetscape (<b>$10.96M</b> budgeted), "
        "post office plaza improvements (<b>$8.39M</b> budgeted).",
        "Grocery tax adopted to replace expiring Illinois state grocery tax, maintaining continuity.",
        "The sales tax applies only to businesses in Winnetka — it affects consumers shopping locally.",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Both taxes adopted and in effect for FY2026."))
    story.append(body(
        "<b>Why This Matters:</b> This is the funding mechanism for the Hubbard Woods streetscape and "
        "post office site improvements — both major priorities of your presidency. The sales tax revenue "
        "must be spent as designated; constituent questions about whether these projects are actually "
        "being executed will be valid and warranted."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story.append(hr())

    # Budget 3
    story += sub_header("3. FY2025 Budget — First Levy Increase in Nine Years")
    story.append(body(
        "The FY2025 budget (adopted under predecessor Village President Chris Rintz) broke a nine-year "
        "freeze on the property tax levy, raising it <b>3.4%</b> and adding ~$467/year per typical household "
        "across all fees. "
        "(<a href='https://www.therecordnorthshore.org/2024/11/21/winnetka-fee-and-tax-increases-means-hundreds-more-per-resident/' color='blue'>"
        "The Record: Fee and Tax Increases</a>)"
    ))
    for b_item in [
        "Property tax levy: <b>$16.29 million</b>, a 3.4% increase (+$85/year typical homeowner).",
        "Utility fee increases: electric +7.2%, water +10%, sewer +4.9%, refuse +3.5%, "
        "municipal services +5.4%.",
        "Net annual impact per household: approximately <b>$467</b>.",
        "Context cited: public safety pension obligations had grown substantially during the freeze period.",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Adopted; ended the nine-year levy freeze."))
    story.append(body(
        "<b>Why This Matters:</b> The two-year compounding increase ($467 + $440 = ~$907/year above 2024) "
        "sets up a challenging FY2027 conversation. Residents may not remember the nine-year freeze — "
        "they will remember two years of ~$400–$500 increases. Building the narrative around what those "
        "dollars fund is your most important communications task going into the next budget cycle."
    ))

    story.append(PageBreak())

    # ── SECTION 8: TOP 3 NON-BUDGET POLICY DISCUSSIONS ────────────────────
    story += section_header("Top 3 Non-Budget Policy Discussions (Last 6 Months)", 8)

    # Policy 1
    story += sub_header("1. IMEA Energy Contract Renewal — June 17, 2025")
    story.append(body(
        "The most contentious policy decision of Dearborn's first year as Village President. IMEA "
        "(Illinois Municipal Electric Agency) has supplied Winnetka's electricity since 1991. The "
        "existing contract runs through 2035; IMEA asked member communities to renew through 2055. "
        "After 14 months of review and 5 study sessions, the council voted unanimously to renew. "
        "(<a href='https://www.therecordnorthshore.org/2025/06/23/this-is-a-hard-issue-amid-ongoing-concerns-from-residents-winnetka-trustees-approve-controversial-extension-with-imea/' color='blue'>"
        "The Record: Winnetka Trustees Approve IMEA Extension</a>)"
    ))
    for b_item in [
        "<b>11 residents spoke against</b> at the June meeting; Go Green Winnetka coalition was a central voice.",
        "Central concern: IMEA's Prairie State Coal Plant is ranked among America's top 12 climate polluters; "
        "the 2055 contract was seen as locking in coal dependency.",
        "IMEA's counter: net-zero emissions commitment by 2050; <b>Bee Hollow 150-MW solar project</b> "
        "coming online late 2026.",
        "Winnetka context: village households use <b>2.4x more electricity per capita</b> than other "
        "Cook County households — amplifying both environmental impact and cost exposure.",
        "Dearborn publicly acknowledged: <i>\"This is a hard issue. This weighs very heavily...\"</i>",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Contract renewed unanimously through 2055."))
    story.append(body(
        "<b>Why This Matters:</b> The environmental scores in Section 4 (60–62 range) confirm this "
        "constituency cares about climate. The issue is not resolved politically. Monitoring IMEA "
        "milestone delivery — especially Bee Hollow — and reporting back to the community "
        "is your primary tool for managing this ongoing tension."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story.append(hr())

    # Policy 2
    story += sub_header("2. One Winnetka — Final Approval, Construction, and Tenant Concerns")
    story.append(body(
        "After more than a decade of debate, One Winnetka received final approval in January 2025 "
        "and broke ground in spring 2025. A four-story mixed-use building at Elm/Lincoln, the "
        "development includes 59 rental units, 20,955 sq ft commercial space, and 150 parking spaces. "
        "Target completion: October 2026. "
        "(<a href='https://www.therecordnorthshore.org/2026/02/19/trustees-concerned-over-one-winnetkas-lack-of-retail-parking/' color='blue'>"
        "The Record: Trustees Concerned Over Lack of Retail</a>  ·  "
        "<a href='https://www.therecordnorthshore.org/2025/01/22/one-winnetka-plans-april-groundbreaking-after-earning-final-approval/' color='blue'>"
        "The Record: Final Approval</a>)"
    ))
    for b_item in [
        "By February 2026, all <b>six signed commercial tenants</b> were service/dining businesses "
        "(fitness studio, medical aesthetics, financial services, restaurants) — <b>zero traditional retail</b>.",
        "Trustee Orsic: <i>\"Is there nothing more vibrant that we can put in this space?\"</i> — "
        "reflecting council frustration at the gap between the vision and market reality.",
        "Developer Murphy Development Group acknowledged failed efforts to recruit retail; the "
        "market for traditional retail in suburban downtown locations is nationally challenged.",
        "Construction itself is disrupting existing Lincoln Avenue businesses by reducing parking "
        "access and deterring shoppers during the build-out period.",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Under construction; completion targeted October 2026."))
    story.append(body(
        "<b>Why This Matters:</b> One Winnetka's completion will be a major moment in your first "
        "term. Managing the public narrative — focusing on the housing units, parking, and tax "
        "base it adds — rather than the retail gap will require proactive communications "
        "in advance of the October 2026 opening."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story.append(hr())

    # Policy 3
    story += sub_header("3. Lakefront Bluff Regulations and Federal Lawsuit Dismissal — October 2025")
    story.append(body(
        "In February 2024, the council adopted a lakefront bluff ordinance setting construction "
        "restrictions at the Army Corps of Engineers ordinary high-water mark (581.5 ft). The "
        "ordinance was partly triggered by the Ishbia family's proposal to combine four lakefront "
        "parcels. Property owners filed a federal taking lawsuit in May 2024. "
        "(<a href='https://www.therecordnorthshore.org/2025/10/14/judge-dismisses-winnetka-property-owners-lawsuit-challenging-lakefront-regulations/' color='blue'>"
        "The Record: Judge Dismisses Lawsuit</a>)"
    ))
    for b_item in [
        "Property owners claimed the ordinance caused <b>'tens of millions'</b> in lost property value — "
        "a federal constitutional taking.",
        "Judge LaShonda Hunt (N.D. Ill.) dismissed the suit <b>without prejudice</b> in October 2025 — "
        "finding the federal takings claim was not yet ripe for adjudication.",
        "'Without prejudice' means the suit can be refiled if the property owners develop a "
        "more concrete factual record of harm — this is not a final victory for the village.",
        "The ordinance remains in effect; the Ishbia parcel combination proposal status is unclear.",
    ]:
        story.append(bullet(b_item))
    story.append(body("<b>Outcome:</b> Lawsuit dismissed without prejudice; ordinance remains in effect."))
    story.append(body(
        "<b>Why This Matters:</b> A refiled federal suit is possible. Ensure Village Attorney "
        "Peter Friedman (Elrod Friedman LLP, 312-578-6566) has the litigation strategy and "
        "budget in place to respond if needed. Any new lakefront-adjacent ordinance or "
        "enforcement action should be reviewed by legal counsel for takings vulnerability."
    ))

    story.append(PageBreak())

    # ── SECTION 9: APPENDIX ────────────────────────────────────────────────
    story += section_header("Appendix: Key Resources", 9)

    story.append(body("<b>Village of Winnetka — Contact & Reference</b>"))
    story.append(body(
        "510 Green Bay Road, Winnetka, IL 60093  ·  (847) 501-6000  ·  info@winnetka.org"
    ))

    resources = [
        ("Official Website",          "https://www.villageofwinnetka.org"),
        ("Agendas & Minutes",          "https://www.villageofwinnetka.org/129/Agendas-Minutes"),
        ("Budget & Finance",           "https://www.villageofwinnetka.org/175/Finance"),
        ("Fiscal Transparency Portal", "https://www.villageofwinnetka.org/277/Fiscal-Transparency"),
        ("Boards & Commissions",       "https://www.villageofwinnetka.org/240/Boards-Commissions"),
        ("Post Office Redevelopment",  "https://www.villageofwinnetka.org/395/Post-Office-Site-Redevelopment"),
        ("Lead Service Line Program",  "https://www.villageofwinnetka.org/386/Lead-Service-Line-Replacement"),
        ("IMEA Information Page",      "https://www.villageofwinnetka.org/417/Illinois-Municipal-Electric-Agency-IMEA"),
    ]
    res_data = [
        [Paragraph("Resource", TH_WHITE_BOLD), Paragraph("URL", TH_WHITE_BOLD)]
    ] + [
        [Paragraph(label, TD_BOLD_LEFT), Paragraph(f'<a href="{url}" color="blue">{url}</a>', TD_LEFT)]
        for label, url in resources
    ]
    res_table = Table(res_data, colWidths=[2.2 * inch, 5.3 * inch])
    res_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("BOX",           (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("INNERGRID",     (0, 0), (-1, -1), 0.25, LIGHT_GRAY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [white, LIGHT_BLUE]),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story += [Spacer(1, 0.1 * inch), res_table, Spacer(1, 0.15 * inch)]

    story.append(body("<b>Key Staff Contacts</b>"))
    contacts = [
        ("Village President",      "Robert Dearborn",   "rdearborn@winnetka.org",     "(847) 716-3541"),
        ("Village Manager",        "Kristin Kazenas",   "—",                           "(847) 716-3541"),
        ("Deputy Village Manager", "Hannah Lipman",     "—",                           "(847) 716-3543"),
        ("CFO / Finance",          "Timothy Sloth, CPA","tsloth@winnetka.org",         "(847) 716-3513"),
        ("Police Chief",           "Brian O'Connell",   "—",                           "(847) 716-3400"),
        ("Dir. Public Works",      "Tom Powers",        "—",                           "(847) 716-3270"),
        ("Dir. Water & Electric",  "Nick Narhi",        "—",                           "(847) 716-3558"),
        ("Dir. Community Dev.",    "Scott Mangum",      "—",                           "(847) 716-3526"),
        ("Communications Mgr.",    "Josie Clark",       "—",                           "(847) 716-3545"),
        ("Economic Dev. Mgr.",     "Lauren Parisi",     "—",                           "(847) 716-3528"),
        ("Village Attorney",       "Peter M. Friedman", "—",                           "(312) 578-6566"),
    ]
    contacts_data = [
        [Paragraph(h, TH_WHITE_BOLD) for h in ["Role", "Name", "Email", "Phone"]]
    ] + [
        [Paragraph(r, TD_LEFT), Paragraph(n, TD_BOLD_LEFT),
         Paragraph(e, TD_LEFT), Paragraph(p, TD_CENTER)]
        for r, n, e, p in contacts
    ]
    contacts_table = Table(contacts_data,
                           colWidths=[1.6 * inch, 1.5 * inch, 2.3 * inch, 2.1 * inch])
    contacts_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("BOX",           (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("INNERGRID",     (0, 0), (-1, -1), 0.25, LIGHT_GRAY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [white, LIGHT_BLUE]),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story += [contacts_table, Spacer(1, 0.15 * inch)]

    story.append(body("<b>Key Financial Figures (FY2026)</b>"))
    fin_data = [
        [Paragraph("Item", TH_WHITE_BOLD), Paragraph("Amount / Detail", TH_WHITE_BOLD)],
        [Paragraph("Total Budget (FY2026)", TD_BOLD_LEFT), Paragraph("$116.2 million (+23.1% from 2025)", TD_LEFT)],
        [Paragraph("Operating Budget", TD_BOLD_LEFT), Paragraph("$76.73 million", TD_LEFT)],
        [Paragraph("Capital Budget (record)", TD_BOLD_LEFT), Paragraph("$39.48 million", TD_LEFT)],
        [Paragraph("Property Tax Levy Increase", TD_BOLD_LEFT), Paragraph("2.33% (FY2026)", TD_LEFT)],
        [Paragraph("Resident Fee Impact (FY2026)", TD_BOLD_LEFT), Paragraph("~$440/year additional (+4.93%)", TD_LEFT)],
        [Paragraph("2-Year Cumulative Impact", TD_BOLD_LEFT), Paragraph("~$907/year above FY2024 levels", TD_LEFT)],
        [Paragraph("New Sales Tax Revenue", TD_BOLD_LEFT), Paragraph("~$1.5M/year (1% home rule sales tax)", TD_LEFT)],
        [Paragraph("Willow Road Reconstruction", TD_BOLD_LEFT), Paragraph("$18.4M (largest single capital line)", TD_LEFT)],
        [Paragraph("Board Meeting Schedule", TD_BOLD_LEFT), Paragraph("1st & 3rd Tuesday, 7:00 PM — 510 Green Bay Rd", TD_LEFT)],
    ]
    fin_table = Table(fin_data, colWidths=[2.5 * inch, 5.0 * inch])
    fin_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("BOX",           (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("INNERGRID",     (0, 0), (-1, -1), 0.25, LIGHT_GRAY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [white, LIGHT_BLUE]),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(fin_table)

    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph(
        "Prepared by Good Party  ·  goodparty.org  ·  May 2026  ·  "
        "Questions? Contact your Good Party representative.",
        CAPTION
    ))

    doc.build(story)
    print(f"Briefing saved to: {output_path}")


if __name__ == "__main__":
    output = "/Users/kaylee/gp-ai-projects/runbooks/briefings/robert_dearborn_winnetka_president_briefing.pdf"
    build_briefing(output)
