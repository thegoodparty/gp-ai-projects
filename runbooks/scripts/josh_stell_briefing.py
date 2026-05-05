"""
Elected Official Briefing — Josh Stell, Village Trustee, Minooka IL
Generated: March 2026
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

# White-text styles for dark-background table headers (see runbook ReportLab rules)
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

# Issue score tier styles (pre-defined per runbook rules — never create inside loop)
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
        [Paragraph("📊  GOOD PARTY POLLING OPPORTUNITY", POLL_TITLE)],
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
    """rows_data: list of (rank, issue_name, score_float, tier_str, bg_color)"""
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
    """rows_data: list of (label, value) tuples"""
    data = [[Paragraph(r, TH_WHITE_BOLD), Paragraph(v, TH_WHITE)] for r, v in rows_data[:1]]
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
    # Navy banner
    title_data = [[
        Paragraph("ELECTED OFFICIAL BRIEFING", TITLE_SUB),
    ]]
    title_banner = Table(title_data, colWidths=[7.5 * inch])
    title_banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 28),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 20),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 20),
    ]))

    name_data = [[Paragraph("Josh Stell", TITLE_NAME)]]
    name_banner = Table(name_data, colWidths=[7.5 * inch])
    name_banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 20),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 20),
    ]))

    role_data = [[
        Paragraph("Village Trustee  ·  Village of Minooka, Illinois", TITLE_SUB),
    ]]
    role_banner = Table(role_data, colWidths=[7.5 * inch])
    role_banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 28),
        ("LEFTPADDING",   (0, 0), (-1, -1), 20),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 20),
    ]))

    story += [title_banner, name_banner, role_banner, Spacer(1, 0.3 * inch)]
    story.append(Paragraph("March 2026  ·  Prepared by Good Party", CAPTION))
    story.append(Spacer(1, 0.5 * inch))

    story.append(body(
        "This briefing is designed to help you hit the ground running as a newly elected Village Trustee "
        "in Minooka. It gives you the context, constituent data, and specific action opportunities you need "
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

    story.append(body(
        "<b>The Situation</b>"
    ))
    story.append(body(
        "You're stepping into a Village Board that is financially healthy and managing the most consequential "
        "development decisions in Minooka's history — all at once. Voter data from 10,433 registered constituents "
        "shows a fiscally conservative community (fiscal conservatism score: 62.1) that places the highest "
        "priority on public safety (58.3) and economic stewardship (56.8). You're inheriting active litigation "
        "against Canadian National Railroad, a transformative $28M/year data center deal, a $20 million water "
        "infrastructure commitment, and the beginning of a new budget cycle — all requiring your attention in "
        "the first 90 days."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story.append(body("<b>Your First 90 Days: Top 4 Focus Areas</b>"))
    story.append(body(
        "First, <b>get deeply briefed on the CN Railroad litigation</b> — this is the highest-profile public issue "
        "with depositions recently concluded and the facility projected operational by 2027. Residents know "
        "where you stood during the campaign; now you need to understand the legal strategy and what advocacy "
        "role the board can play. Second, <b>understand the FY2027 budget framework</b>: budget meetings occurred "
        "March 2–10, 2026, right around your swearing-in. The $20M water infrastructure commitment and a "
        "potential new municipal building will define capital spending for a decade — you need to understand "
        "those numbers before any early votes. Third, <b>engage seriously with the Equinix data center "
        "annexation agreement</b>: the projected $28M/year in tax revenue (utility + property) could "
        "fundamentally reshape village finances, and the terms of the agreement will matter enormously. "
        "Fourth, <b>establish yourself as a transparent, responsive trustee</b> — your constituents are "
        "fiscally conservative and skeptical of government overreach; regular communication on spending "
        "decisions, the tax levy rationale, and development terms will build the trust you'll need for "
        "larger future decisions."
    ))

    story.append(PageBreak())

    # ── SECTION 2: LESSONS LEARNED ────────────────────────────────────────
    story += section_header("Lessons Learned From Recent History", 2)

    lessons = [
        (
            "1. Communicate the property tax levy in context, not just percentages.",
            "The December 2025 truth-in-taxation hearing drew ~20 residents concerned about the "
            "12% levy increase. Village Administrator Duffy had to explain that because of EAV adjustments, "
            "homeowners at $250K–$300K would actually see lower total taxes. The lesson: lead with the "
            "real-dollar impact on homeowners, not the percentage headline, or you'll spend the meeting "
            "correcting misperceptions."
        ),
        (
            "2. Local authority over truck routing is legally complex — stay coordinated.",
            "Minooka's legal challenge against CN Railroad invoked federal preemption arguments. "
            "The village had to retain two separate law firms and carefully frame its weight-limit "
            "ordinances to avoid federal preemption. Any new ordinances in this area must go through "
            "legal counsel. Acting independently without that coordination could undermine the litigation."
        ),
        (
            "3. Economic development decisions require long-term infrastructure planning.",
            "The initial Equinix water concern (6 million gallons/day) nearly created a conflict with "
            "the Grand Prairie Water Commission transition. Equinix ultimately agreed to air cooling, "
            "but only after extensive negotiation. Every major development agreement going forward should "
            "explicitly address water, sewer, and infrastructure capacity before annexation approval."
        ),
        (
            "4. Solar farm location matters more than solar farm opposition.",
            "The board successfully distinguished between solar projects by location: approving those near "
            "railroad tracks (unsuitable for other uses) while opposing Bright Star near the I-80 interchange "
            "reserved for heavy industrial. Barry Thompson's dissent on Water Lily shows this framework isn't "
            "universally shared. Having a clear, map-based policy rationale protects you from "
            "appearing arbitrary on any individual vote."
        ),
        (
            "5. Major capital decisions require early stakeholder engagement.",
            "The space needs analysis for a new village hall/police facility was only authorized in June 2025 — "
            "years after the current converted strip-mall facility was renovated. This decision will involve "
            "significant public dollars and constituent trust. Engaging residents and Police Chief Meyer "
            "early in the process will be essential to build the mandate for whatever is proposed."
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
            "Request a one-on-one briefing from Village Administrator Dan Duffy",
            "Dan Duffy (dan.duffy@minooka.com, ext. 3173) is your primary operational resource. A dedicated "
            "briefing will give you the full picture on CN litigation status, Equinix timeline milestones, "
            "FY2027 budget constraints, and any pending contracts. This is the single most valuable action "
            "you can take in your first week."
        ),
        (
            "Champion transparent communication on the CN litigation",
            "Residents voted for you partly because of your public opposition to CN truck traffic. Schedule "
            "or support a public update on where the litigation stands — depositions concluded recently, "
            "construction continues, and the facility is projected operational by 2027. Regular updates "
            "maintain constituent trust and demonstrate you're actively tracking the issue."
        ),
        (
            "Review the Equinix annexation agreement terms before any vote",
            "The $28M/year in projected tax revenue (utility + property) represents a transformational "
            "deal. Request the full annexation agreement from the village attorney (Spesia & Taylor, "
            "815-726-4311) and ask for a staff briefing specifically on infrastructure obligations, "
            "utility commitments, and phasing milestones. Your informed vote here will define your fiscal "
            "credibility on the board."
        ),
        (
            "Request a committee assignment aligned with your priorities",
            "Minooka's standing committees (Finance, Ordinance & Building, Public Safety, Economic "
            "Development) meet the second Wednesday of each month before the full board. This is where "
            "policy is shaped. Finance and Public Safety committees align directly with your campaign "
            "priorities and constituent data."
        ),
        (
            "Introduce yourself to key department heads in person",
            "Brief one-on-ones with Police Chief Justin Meyer (justin.meyer@minooka.com), Finance Director "
            "Austin Haacke, and Public Works Superintendent Ryan Anderson will give you operational "
            "context that no briefing document can replicate. These relationships also signal to staff "
            "that you're engaged and accessible."
        ),
    ]

    for i, (title, desc) in enumerate(qw, start=1):
        story.append(body(f"<b>{i}. {title}</b>"))
        story.append(body(desc))
        story.append(Spacer(1, 0.06 * inch))

    story += polling_callout(
        issue="CN Litigation Update & Constituent Sentiment",
        timing="Within first 60 days — before summer 2026 construction ramps up",
        questions=[
            "How concerned are you about CN truck traffic through downtown Minooka?",
            "Do you support the village's legal challenge against Canadian National Railroad?",
            "What specific impact on your neighborhood/business are you most worried about?",
        ],
        why="Your constituents are likely split on urgency and specifics. Polling validates the village's "
            "legal investment, identifies which neighborhoods feel most at risk, and gives you constituent "
            "mandate data to share with legal counsel and the board.",
        action="Contact Good Party to set up a community poll on CN traffic in April 2026."
    )

    story.append(PageBreak())

    # ── SECTION 4: CONSTITUENTS' TOP ISSUES ───────────────────────────────
    story += section_header("Your Constituents' Top Issues", 4)

    story.append(body(
        "The following data reflects Haystaq voter intelligence scores for 10,433 registered voters "
        "in Minooka, Illinois. Scores are 0–100; higher scores indicate stronger constituent "
        "support or prioritization. Only issues within local/state government authority are included."
    ))

    story.append(Spacer(1, 0.12 * inch))
    story += sub_header("Demographics at a Glance")
    story.append(demographics_table([
        ("Total Registered Voters", "10,638"),
        ("Average Age", "48.1 years"),
        ("Gender: Female", "5,548  (52%)"),
        ("Gender: Male", "5,083  (48%)"),
    ]))

    story.append(Spacer(1, 0.14 * inch))
    story += sub_header("Top Issues by Priority (Local/State Authority Only)")

    issue_rows = [
        (1,  "Public Safety / Keeping Community Safe",    58.3, "Moderate", TIER3_BG),
        (2,  "Police Trust & Confidence",                 58.7, "Moderate", TIER3_BG),
        (3,  "Tax Policy (Tax Cuts Support)",              57.2, "Moderate", TIER3_BG),
        (4,  "Local Economic Development",                56.8, "Moderate", TIER3_BG),
        (5,  "Environmental Priorities",                  47.0, "Below 50", BELOW_BG),
        (6,  "Public Transit Access",                     40.4, "Below 50", BELOW_BG),
        (7,  "Affordable Housing (Government Role)",      38.8, "Below 50", BELOW_BG),
    ]
    story.append(issues_table(issue_rows))
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "Tier Guide: Strong = 60–74 (green)  ·  Moderate = 50–59 (amber)  ·  Below 50 = lower priority. "
        "Fiscal conservatism ideology score: 62.1 (Strong). Conservative ideology index: 59.5 vs. Liberal: 40.5.",
        CAPTION
    ))
    story.append(Spacer(1, 0.08 * inch))

    story += polling_callout(
        issue="Public Safety & Police Funding Priorities",
        timing="Before FY2027 budget discussions (April–June 2026)",
        questions=[
            "How would you rate the Minooka Police Department's performance in your neighborhood?",
            "If the village had additional budget, what public safety investment should come first: "
            "more officers, technology/equipment, or community programs?",
            "Do you feel safe in Minooka's downtown and main commercial areas?",
        ],
        why="The 'keep safe' score of 58.3 shows public safety matters to constituents but the direction "
            "isn't definitive. Before the next budget cycle, polling clarifies whether residents want "
            "staffing, equipment, or programs — informing your vote on police funding.",
        action="Contact Good Party to set up a public safety priorities poll before the April 2026 board meeting."
    )

    story.append(PageBreak())

    # ── SECTION 5: WHAT TO WATCH ───────────────────────────────────────────
    story += section_header("What to Watch & Prepare For", 5)

    story += sub_header("Issues Requiring Immediate Attention")
    story.append(bullet(
        "<b>FY2027 Budget Cycle (Active Now):</b> Budget meetings occurred March 2–10, 2026, right "
        "around your swearing-in. The annual budget process will lead to an April/May 2026 adoption. "
        "Request all March meeting materials from the village clerk and review before the April board meeting."
    ))
    story.append(bullet(
        "<b>OneEnergy Solar Farm Vote (Pending):</b> The public hearing closed in February 2026; a final "
        "vote is expected at an upcoming meeting. Review the project location against the village's solar "
        "designation map and understand why Barry Thompson dissented on the similar Water Lily approval. "
        "Know your position before walking into that vote."
    ))
    story.append(bullet(
        "<b>Grand Prairie Water Commission Timeline:</b> The $20M commitment is locked in and the Lake "
        "Michigan pipeline is under construction with 2030 delivery. Water rates will roughly double "
        "by 2030. Residents will have questions. Understand the rate schedule, project milestones, "
        "and communication plan before this becomes a heated constituent issue."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story += sub_header("Political Dynamics to Understand")
    story.append(bullet(
        "<b>Barry Thompson's Dissent Pattern:</b> Thompson was the sole 'no' vote on the Water Lily Solar "
        "Farm approval — the only documented split vote in recent board history. Understanding his "
        "reasoning on that vote will tell you a lot about his general policy framework and help you "
        "anticipate where future disagreements may surface."
    ))
    story.append(bullet(
        "<b>Terry Houchens' 28-Year Legacy:</b> You replaced a trustee who served nearly three decades. "
        "The board honored him publicly in May 2025. Institutional knowledge that lived with Houchens "
        "now needs to be rebuilt. Dennis Martin and Ray Mason are your best resources for understanding "
        "decisions made before recent meeting records."
    ))
    story.append(bullet(
        "<b>Village President Offerman's Development-Forward Posture:</b> Offerman's April 2025 State "
        "of the Village address highlighted growth, Equinix, and Surf Internet as wins. The board tone "
        "is pragmatically pro-development. Your fiscally conservative perspective will be welcome — "
        "just frame it around sound agreements and long-term financial health, not opposition to growth."
    ))

    story.append(Spacer(1, 0.1 * inch))
    story += sub_header("Upcoming Challenges or Decisions")
    story.append(bullet(
        "<b>New Municipal Building / Police Facility:</b> The June 2025 RFQ for a space needs analysis "
        "will eventually yield a capital proposal. Given Minooka's current financial health, this "
        "could move faster than expected. Understand the cost range, financing options, and community "
        "sentiment before a capital bond question reaches the board."
    ))
    story.append(bullet(
        "<b>Equinix Phasing & Infrastructure Agreements:</b> Land clearing starts end of 2025; main "
        "construction in 2027; commissioning by 2031. The annexation agreement terms — especially "
        "infrastructure obligations and utility tax collection mechanisms — will govern the village's "
        "relationship with its most significant taxpayer for decades."
    ))
    story.append(bullet(
        "<b>CN Litigation Resolution or Escalation:</b> Depositions concluded recently. The next "
        "phase could involve motions, hearings, or settlement discussions. Federal preemption arguments "
        "create real legal risk. The board may face a decision about settlement terms vs. continued "
        "litigation costs — a significant financial and political decision."
    ))

    story += polling_callout(
        issue="Water Rate Increases — Constituent Readiness",
        timing="Before the first public communication on rate increases (Summer 2026)",
        questions=[
            "Have you heard about Minooka's plan to transition to Lake Michigan water by 2030?",
            "Are you aware that water rates are projected to increase significantly to fund this transition?",
            "What is most important to you in how the village communicates about water rate changes?",
        ],
        why="The rate transition from $5.25 to ~$13.00/1,000 gallons will be a significant shock for "
            "many residents. Polling reveals whether residents are already aware, their tolerance level, "
            "and what communication approach will build rather than erode trust.",
        action="Contact Good Party to set up a water infrastructure poll before the Summer 2026 rate "
               "communication rollout."
    )

    story.append(PageBreak())

    # ── SECTION 6: UNDERSTANDING THE ROLE ─────────────────────────────────
    story += section_header("Understanding This Role", 6)

    story += sub_header("What Is Expected of This Role")
    story.append(body(
        "Minooka operates under a <b>council-manager hybrid model</b>: the Village Board of Trustees "
        "sets policy, approves the budget, and authorizes major contracts — while Village Administrator "
        "Dan Duffy handles day-to-day operations. Your role is <b>legislative and strategic</b>, not "
        "operational. You vote on ordinances, resolutions, and the annual budget. You ask questions, "
        "represent constituent interests, and hold the administration accountable through the budget "
        "and policy process."
    ))
    story.append(body(
        "The Board meets the <b>fourth Tuesday of each month at 6:30 PM</b> at Village Hall, "
        "121 E. McEvilly Road. Committee meetings occur the second Wednesday. Agenda materials are "
        "published on the village website (minooka.com) before each meeting."
    ))
    story.append(body(
        "As a trustee, you have access to all staff reports, contract documents, and legal briefings. "
        "Use this access proactively — asking for documents before meetings rather than during them "
        "signals preparation and builds administrative respect."
    ))

    story.append(Spacer(1, 0.08 * inch))
    story += sub_header("What Is Outside the Scope of This Role")
    story.append(body(
        "You are not an ombudsman, project manager, or department supervisor. When constituents bring "
        "individual service complaints (a pothole, a billing dispute, a code issue), your role is to "
        "connect them to the right staff member — not to personally investigate or resolve. Direct "
        "constituent service inquiries to Village Clerk Orsola Filus (orsola.evola@minooka.com) or "
        "the relevant department head."
    ))
    story.append(body(
        "Avoid directing staff to take specific operational actions without going through the Village "
        "Administrator. The council-manager structure depends on clear lines of authority — bypassing "
        "it, even with good intentions, creates accountability confusion and can create legal exposure."
    ))

    story.append(Spacer(1, 0.08 * inch))
    story += sub_header("How to Represent Your Community")
    story.append(body(
        "Minooka is a <b>fiscally conservative, public-safety-oriented community</b> (see Section 4 "
        "data). But vocal residents at meetings may not represent the broader constituency. Use Haystaq "
        "data and tools like Good Party polling to understand what the full community actually prioritizes "
        "— not just who shows up to public comment."
    ))
    story.append(body(
        "Transparency is your strongest tool for building constituent trust. When you vote yes or no "
        "on major items (tax levy, development deals, litigation spending), communicate your reasoning "
        "publicly — through social posts, the village newsletter, or constituent emails. The community "
        "elected you because they believed you'd be a responsible steward. Show them regularly that "
        "you are."
    ))

    story.append(PageBreak())

    # ── SECTION 7: TOP 3 BUDGET DISCUSSIONS ───────────────────────────────
    story += section_header("Top 3 Budget Discussions (Last 6 Months)", 7)

    # Budget 1
    story += sub_header("1. 2025 Property Tax Levy Approval — December 2025")
    story.append(body(
        "The Village Board held its first-ever truth-in-taxation public hearing (required when levies "
        "exceed prior year by 5%+) and approved a <b>$3.5 million total levy</b> — approximately "
        "$440,000 (12%) higher than 2024. About 20 residents attended; roughly 10 asked questions. "
        "(<a href='https://www.wcsjnews.com/news/local/minooka-trustees-approve-2025-tax-levy/article_616eb960-e0b8-4336-b23a-97e7472c3acd.html' color='blue'>"
        "WCSJ News: Minooka Trustees Approve 2025 Tax Levy</a>)"
    ))
    for b_item in [
        "Levy funds two new police officers, a new snowplow, and increased pension obligations.",
        "Village Administrator Duffy noted that the village represents only <b>8% of total property tax bills</b> — "
        "the rest goes to schools, fire, library, etc.",
        "For a $250K–$300K home, total taxes could actually <b>drop $12–$14</b> due to EAV adjustments, "
        "despite the 12% village levy increase — a nuance Duffy had to explain at the hearing.",
        "You publicly supported an approach allowing a rate reduction while permitting modest growth "
        "— a position you'll want to revisit as FY2027 discussions begin.",
    ]:
        story.append(bullet(b_item))
    story.append(body(
        "<b>Outcome:</b> Levy approved. First truth-in-taxation hearing conducted successfully."
    ))
    story.append(body(
        "<b>Why This Matters:</b> The FY2027 budget cycle begins now (March 2026 meetings). Water rate "
        "increases and potential capital spending for a new municipal building will add fiscal pressure. "
        "Your approach to the levy will be a defining vote for your first term."
    ))

    story.append(Spacer(1, 0.12 * inch))
    hr_story = hr()
    story.append(hr_story)

    # Budget 2
    story += sub_header("2. FY2026 Annual Audit Results — February 2026")
    story.append(body(
        "Auditors Lauterbach & Associates presented the FY2026 audit results at the "
        "<b>February 26, 2026</b> board meeting. "
        "(<a href='https://www.wcsjnews.com/news/local/minooka-hears-annual-audit-results/article_a6e6ff1e-f9c3-11ef-b927-f3c491dffb3f.html' color='blue'>"
        "WCSJ News: Minooka Hears Annual Audit Results</a>)"
    ))
    for b_item in [
        "<b>Clean audit — no findings</b> for the second consecutive year.",
        "Sales tax revenues are up; General Fund balance improved.",
        "Sales tax directly funds police salaries, public works salaries, and equipment — not discretionary.",
        "Total village operating budget: approximately <b>$11 million</b>. "
        "(<a href='https://www.minooka.com/our-government/finance-department/budget-and-finances/' color='blue'>"
        "Budget & Finances | minooka.com</a>)",
    ]:
        story.append(bullet(b_item))
    story.append(body(
        "<b>Outcome:</b> Clean audit accepted. No corrective actions required."
    ))
    story.append(body(
        "<b>Why This Matters:</b> Two consecutive clean audits demonstrate sound fiscal management — "
        "a key talking point when the community questions the levy increase or future capital spending."
    ))

    story.append(Spacer(1, 0.12 * inch))
    story.append(hr())

    # Budget 3
    story += sub_header("3. Grand Prairie Water Commission — $20M Capital Commitment")
    story.append(body(
        "The village committed <b>$20 million</b> as its share of the Grand Prairie Water Commission's "
        "$1.5 billion Lake Michigan pipeline, projected operational by 2030. "
        "(<a href='https://www.wspynews.com/news/wcsjnews/village-of-minooka-s-20m-stake-in-bringing-lake-michigan-water-to-town/article_f855ab56-b1f9-5bd7-a83b-ad2b26ae363a.html' color='blue'>"
        "WSPY News: Village of Minooka's $20M Stake</a>)"
    ))
    for b_item in [
        "Minooka's existing deep aquifer wells are projected to become inadequate by 2030.",
        "Water rates will increase from <b>$5.25 to ~$13.00 per 1,000 gallons</b> by 2030.",
        "The Equinix data center's initial proposal to use 6M gallons/day raised alarm about capacity "
        "during the transition — Equinix subsequently agreed to <b>air cooling</b>, eliminating the concern.",
        "Commission construction has broken ground; Minooka's $20M commitment is locked in.",
    ]:
        story.append(bullet(b_item))
    story.append(body(
        "<b>Outcome:</b> Commitment made; construction underway. Equinix water conflict resolved."
    ))
    story.append(body(
        "<b>Why This Matters:</b> The doubling of water rates will be the most visible fee increase "
        "for residents in years. Proactive, transparent communication about why it's necessary — "
        "and what residents get in return (reliable clean water for 20+ years) — is essential."
    ))

    story.append(PageBreak())

    # ── SECTION 8: TOP 3 NON-BUDGET POLICY DISCUSSIONS ────────────────────
    story += section_header("Top 3 Non-Budget Policy Discussions (Last 6 Months)", 8)

    # Policy 1
    story += sub_header("1. Canadian National Railroad Logistics Hub — Active Litigation")
    story.append(body(
        "Wisconsin Central (a CN subsidiary) is building a <b>900-acre intermodal facility</b> in "
        "neighboring Channahon. Truck traffic is projected to route through Minooka, potentially "
        "adding up to <b>9,691 heavy truck trips per day</b> through the village's main commercial corridor "
        "to reach I-80. "
        "(<a href='https://www.minooka.com/news/posts/minooka-voices-concerns-over-canadian-national-hub-and-heavy-truck-traffic/' color='blue'>"
        "Village Official Statement</a>  ·  "
        "<a href='https://www.chicagotribune.com/2024/06/22/minooka-pushes-back-on-massive-rail-project-it-says-will-flood-village-with-truck-traffic/' color='blue'>"
        "Chicago Tribune</a>)"
    ))
    for b_item in [
        "Village position: NOT opposed to the CN project — only wants trucks routed to an I-80 entrance "
        "5.5 miles southwest, bypassing downtown Minooka.",
        "Village retained <b>Spesia & Taylor and Roetzel & Andress</b> for a declaratory judgment action; "
        "amended village code to impose weight limits on McLindon Road and related roads.",
        "CN threatened to invoke federal preemption under the Interstate Commerce Commission Termination "
        "Act (1995). This remains the primary legal risk.",
        "<b>Depositions recently concluded</b>. CN facility projected operational by 2027.",
    ]:
        story.append(bullet(b_item))
    story.append(body(
        "<b>Outcome:</b> Litigation ongoing. No settlement reached. Construction proceeding."
    ))
    story.append(body(
        "<b>Why This Matters:</b> You campaigned on this issue. Residents expect you to stay engaged. "
        "Request a full litigation briefing from Spesia & Taylor and ask the board to discuss "
        "communication strategy with constituents."
    ))

    story.append(Spacer(1, 0.12 * inch))
    story.append(hr())

    # Policy 2
    story += sub_header("2. Equinix Data Center — Transformative Economic Development")
    story.append(body(
        "Equinix has proposed a multi-phase data center campus on approximately <b>300 acres</b> north "
        "of Minooka at the northeast corner of Holt and Ridge Road. The village created a new "
        "<b>Data Center Zoning District</b> in late 2024 to accommodate this project. "
        "(<a href='https://patch.com/illinois/channahon-minooka/equinix-proposes-billion-dollar-data-center-investment-minooka-nodx' color='blue'>"
        "Patch.com: Equinix Proposes Billion-Dollar Data Center</a>)"
    ))
    for b_item in [
        "Projected revenue: <b>$8M/year in utility taxes + $20M/year in property taxes</b> for the village, "
        "plus 100–200 permanent jobs and hundreds of indirect positions.",
        "Equinix agreed to use <b>air-cooled computers</b>, eliminating the 6M gallon/day water concern "
        "raised during the Grand Prairie Water Commission transition planning.",
        "Over <b>100 residents</b> attended the public open house. January 23, 2025 board meeting included "
        "extensive trustee questions on setbacks, noise, and air quality compliance.",
        "Timeline: land clearing end of 2025; main construction 2027; commissioning by 2031.",
    ]:
        story.append(bullet(b_item))
    story.append(body(
        "<b>Outcome:</b> Data Center Zoning District adopted. Annexation agreement negotiations ongoing."
    ))
    story.append(body(
        "<b>Why This Matters:</b> This is the most consequential economic development deal in Minooka's "
        "recent history. The annexation agreement terms will govern the village's relationship with "
        "its largest future taxpayer for decades — understand the details before it comes to a vote."
    ))

    story.append(Spacer(1, 0.12 * inch))
    story.append(hr())

    # Policy 3
    story += sub_header("3. Solar Farm Policy Framework — Series of Approvals & One Split Vote")
    story.append(body(
        "The board handled multiple solar farm applications in late 2025 and early 2026, developing "
        "a de facto location policy while producing Minooka's first notable split vote in recent history. "
        "(<a href='https://now.solar/2026/03/02/minooka-board-approves-final-plat-for-solar-farm-project-one-trustee-votes-no-wcsj-news/' color='blue'>"
        "Solar Now: Board Approves Water Lily, One Trustee Votes No</a>  ·  "
        "<a href='https://www.wcsjnews.com/news/local/minooka-village-board-opposes-solar-farm-project/article_72e35cb4-afcc-42b7-a334-2ff62dd9133b.html' color='blue'>"
        "WCSJ: Board Opposes Bright Star</a>)"
    ))
    for b_item in [
        "<b>Greenwood Solar (approved):</b> 39 acres near railroad tracks. Revenue: $12K–$15K/year. "
        "Minimal opposition; location deemed unsuitable for industrial/residential.",
        "<b>Bright Star Renewables (opposed by resolution):</b> Board blocked 37-acre project near the "
        "Brisbin/I-80 interchange, reserving that corridor for heavy industrial use.",
        "<b>Water Lily Solar (approved, split vote):</b> 5 MW, 9-acre project near Wilde/Ridge Road. "
        "<b>Barry Thompson was the sole 'no' vote</b> — first documented split in recent board history.",
        "<b>OneEnergy Solar (pending):</b> Public hearing held February 2026; vote expected at upcoming meeting.",
    ]:
        story.append(bullet(b_item))
    story.append(body(
        "<b>Outcome:</b> Location-based framework emerged; map of preferred solar zones in use. County "
        "retains final approval authority under state law."
    ))
    story.append(body(
        "<b>Why This Matters:</b> The OneEnergy vote is likely your first contested policy decision. "
        "Review the solar designation map and Thompson's reasoning before voting. Consistency with "
        "the established framework is important for legal defensibility."
    ))

    story.append(PageBreak())

    # ── SECTION 9: APPENDIX ────────────────────────────────────────────────
    story += section_header("Appendix: Key Resources", 9)

    story.append(body("<b>Village of Minooka — Contact & Reference</b>"))
    story.append(body(
        "121 E. McEvilly Road, Minooka, IL 60447  ·  (815) 467-2151  ·  info@minooka.com"
    ))

    resources = [
        ("Official Website",           "https://www.minooka.com"),
        ("Agendas & Minutes",           "https://www.minooka.com/our-government/agendas-minutes/"),
        ("Budget & Finances",           "https://www.minooka.com/our-government/finance-department/budget-and-finances/"),
        ("Village Board page",          "https://www.minooka.com/our-government/village-board/"),
        ("Contact Us (all staff)",      "https://www.minooka.com/our-government/contact-us/"),
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
        ("Village President",  "Ric Offerman",     "ric.offerman@minooka.com",     "(815) 467-2151 ext. 3172"),
        ("Village Administrator", "Daniel Duffy",  "dan.duffy@minooka.com",        "ext. 3173"),
        ("Finance Director",   "Austin Haacke",    "austin.haacke@minooka.com",    "ext. 3175"),
        ("Police Chief",       "Justin Meyer",     "justin.meyer@minooka.com",     "ext. 3177"),
        ("Public Works Supt.", "Ryan Anderson",    "ryan.anderson@minooka.com",    "ext. 2303"),
        ("Village Clerk",      "Orsola Filus",     "orsola.evola@minooka.com",     "ext. 3176"),
        ("Village Attorney",   "Spesia & Taylor",  "—",                            "(815) 726-4311"),
        ("Village Engineer",   "Robinson Eng.",    "—",                            "(815) 806-0300"),
    ]
    contacts_data = [
        [Paragraph(h, TH_WHITE_BOLD) for h in ["Role", "Name", "Email", "Phone"]]
    ] + [
        [Paragraph(r, TD_LEFT), Paragraph(n, TD_BOLD_LEFT),
         Paragraph(e, TD_LEFT), Paragraph(p, TD_CENTER)]
        for r, n, e, p in contacts
    ]
    contacts_table = Table(contacts_data,
                           colWidths=[1.6 * inch, 1.5 * inch, 2.6 * inch, 1.8 * inch])
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
        [Paragraph("Total Operating Budget", TD_BOLD_LEFT), Paragraph("~$11 million", TD_LEFT)],
        [Paragraph("Property Tax Levy (2025)", TD_BOLD_LEFT), Paragraph("$3.5 million (12% increase)", TD_LEFT)],
        [Paragraph("Current Water Rate", TD_BOLD_LEFT), Paragraph("$5.25 / 1,000 gallons", TD_LEFT)],
        [Paragraph("Projected 2030 Water Rate", TD_BOLD_LEFT), Paragraph("~$13.00 / 1,000 gallons", TD_LEFT)],
        [Paragraph("Water Commission Commitment", TD_BOLD_LEFT), Paragraph("$20 million (Minooka share)", TD_LEFT)],
        [Paragraph("Equinix Projected Revenue/yr", TD_BOLD_LEFT), Paragraph("$8M utility tax + $20M property tax", TD_LEFT)],
        [Paragraph("Board Meeting Schedule", TD_BOLD_LEFT), Paragraph("4th Tuesday, 6:30 PM — Village Hall", TD_LEFT)],
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
        "Prepared by Good Party  ·  goodparty.org  ·  March 2026  ·  "
        "Questions? Contact your Good Party representative.",
        CAPTION
    ))

    doc.build(story)
    print(f"✅ Briefing saved to: {output_path}")


if __name__ == "__main__":
    output = "/Users/kaylee/gp-ai-projects/runbooks/briefings/josh_stell_minooka_trustee_briefing.pdf"
    build_briefing(output)
