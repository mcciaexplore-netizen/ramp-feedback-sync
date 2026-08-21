"""
Builds digest_report.html from digest_data.json — a standalone report,
not part of the scrape pipeline. Run after digest_gather.py.
"""

import html
import json

MONTHS = ["May 2026", "June 2026", "July 2026", "August 2026"]

RATING_LABELS = {
    "excellent": "Excellent", "good": "Good", "neutral": "Neutral",
    "poor": "Poor", "yes": "Yes", "no": "No",
}


def normalize_ratings(dist: dict) -> dict:
    """Merge case variants ('no'/'NO') the raw scrape left un-normalized."""
    merged = {}
    for raw, count in dist.items():
        label = RATING_LABELS.get(raw.strip().lower(), raw.strip())
        merged[label] = merged.get(label, 0) + count
    return merged


def pct(numer, denom):
    if not denom:
        return None
    return round(100 * numer / denom)


def fmt_pct(p):
    return f"{p}%" if p is not None else "—"


def fmt_count(n):
    return f"{n:,}" if n is not None else "—"


def event_row(ev):
    enrolled, attended, feedback = ev["enrolled"], ev["attended"], ev["feedbackCount"]
    attend_rate = pct(attended, enrolled) if attended is not None and enrolled else None
    feedback_rate = pct(feedback, attended) if attended else None
    flag = ""
    if enrolled is None:
        flag = '<span class="badge badge-muted">no data</span>'
    elif attended == 0 and enrolled:
        flag = '<span class="badge badge-critical">0 attended</span>'
    elif feedback == 0 and attended:
        flag = '<span class="badge badge-warning">no feedback</span>'

    dup = ev["topDuplicateFeedback"]
    dup_note = ""
    if dup:
        dup_note = f'<div class="dup-note">{dup[0]["count"]}× identical response</div>'

    return f"""<tr>
      <td class="col-event">
        <div class="event-name">{html.escape(ev["eventName"])}</div>
        <div class="event-meta">{html.escape(ev["eventId"])} · {html.escape(ev["component"])} · {html.escape(ev["eventDate"].split(" ")[0])}</div>
      </td>
      <td class="num">{fmt_count(enrolled)}</td>
      <td class="num">{fmt_count(attended)}</td>
      <td class="num">{fmt_pct(attend_rate)}</td>
      <td class="num">{fmt_count(feedback)}</td>
      <td class="num">{fmt_pct(feedback_rate)}</td>
      <td class="col-flag">{flag}{dup_note}</td>
    </tr>"""


def bar_group(label, values, maxval):
    """One labeled group of 3 bars (Enrolled/Attended/Feedback) as a CSS chart."""
    keys = [("Enrolled", "series-1", values[0]), ("Attended", "series-2", values[1]), ("Feedback", "series-3", values[2])]
    bars = ""
    for name, cls, v in keys:
        w = round(100 * v / maxval) if maxval else 0
        bars += f"""
        <div class="bar-row">
          <span class="bar-label">{name}</span>
          <div class="bar-track"><div class="bar-fill {cls}" style="width:{w}%"></div></div>
          <span class="bar-value">{v:,}</span>
        </div>"""
    return f'<div class="bar-group"><div class="bar-group-title">{html.escape(label)}</div>{bars}</div>'


def month_section(month, m):
    events = sorted(m["events"], key=lambda e: e["eventDate"])
    ratings = normalize_ratings(m["ratingDistribution"])

    attend_rate = pct(m["totalAttended"], m["totalEnrolled"])
    feedback_rate = pct(m["totalFeedback"], m["totalAttended"])
    similar_rate = pct(m["totalSimilarFeedbackRespondents"], m["totalFeedback"])

    rows = "\n".join(event_row(ev) for ev in events)

    rating_html = ""
    if ratings:
        max_r = max(ratings.values())
        rating_bars = "".join(
            f'<div class="bar-row"><span class="bar-label">{html.escape(k)}</span>'
            f'<div class="bar-track"><div class="bar-fill series-4" style="width:{round(100*v/max_r)}%"></div></div>'
            f'<span class="bar-value">{v}</span></div>'
            for k, v in sorted(ratings.items(), key=lambda kv: -kv[1])
        )
        rating_html = f"""
        <div class="spotlight-block">
          <div class="spotlight-label">Word-scale ratings given (Excellent/Good/Yes/No forms only)</div>
          {rating_bars}
        </div>"""

    # top duplicate feedback across the month, for the spotlight callout
    all_dupes = [d for ev in events for d in ev["topDuplicateFeedback"]]
    top_dupe = max(all_dupes, key=lambda d: d["count"], default=None)
    top_dupe_html = ""
    if top_dupe:
        snippet = html.escape(top_dupe["text"][:220]) + ("…" if len(top_dupe["text"]) > 220 else "")
        top_dupe_html = f"""
        <div class="spotlight-block">
          <div class="spotlight-label">Most repeated response ({top_dupe["count"]}×)</div>
          <div class="dupe-quote">“{snippet}”</div>
        </div>"""

    max_val = max(m["totalEnrolled"], m["totalAttended"], m["totalFeedback"], 1)

    return f"""
  <section class="month-section" id="{month.replace(' ', '-')}">
    <div class="month-header">
      <h2>{month}</h2>
      <span class="month-count">{m["totalEvents"]} event{"s" if m["totalEvents"] != 1 else ""}</span>
    </div>

    <div class="stat-grid">
      <div class="stat-tile"><div class="stat-value">{m["totalEvents"]}</div><div class="stat-label">Events held</div></div>
      <div class="stat-tile"><div class="stat-value">{fmt_count(m["totalEnrolled"])}</div><div class="stat-label">Enrolled</div></div>
      <div class="stat-tile"><div class="stat-value">{fmt_count(m["totalAttended"])}</div><div class="stat-label">Attended</div><div class="stat-sub">{fmt_pct(attend_rate)} of enrolled</div></div>
      <div class="stat-tile"><div class="stat-value">{fmt_count(m["totalFeedback"])}</div><div class="stat-label">Gave feedback</div><div class="stat-sub">{fmt_pct(feedback_rate)} of attendees</div></div>
      <div class="stat-tile accent"><div class="stat-value">{fmt_count(m["totalSimilarFeedbackRespondents"])}</div><div class="stat-label">Gave matching feedback</div><div class="stat-sub">{fmt_pct(similar_rate)} of respondents</div></div>
    </div>

    <div class="chart-and-spotlight">
      {bar_group("Enrolled → Attended → Feedback", [m["totalEnrolled"], m["totalAttended"], m["totalFeedback"]], max_val)}
      <div class="spotlight">
        <div class="spotlight-title">Similar feedback</div>
        {top_dupe_html}
        {rating_html if rating_html else '<div class="spotlight-empty">No word-scale (Excellent/Good/Yes/No) ratings this month — forms used numeric scales instead.</div>'}
      </div>
    </div>

    <div class="table-wrap">
      <table>
        <thead><tr>
          <th class="col-event">Event</th>
          <th class="num">Enrolled</th>
          <th class="num">Attended</th>
          <th class="num">Attend %</th>
          <th class="num">Feedback</th>
          <th class="num">Feedback %</th>
          <th class="col-flag"></th>
        </tr></thead>
        <tbody>
        {rows}
        </tbody>
      </table>
    </div>
  </section>"""


def main():
    data = json.load(open("digest_data.json"))
    months_data = data["months"]

    total_events = sum(m["totalEvents"] for m in months_data.values())
    total_enrolled = sum(m["totalEnrolled"] for m in months_data.values())
    total_attended = sum(m["totalAttended"] for m in months_data.values())
    total_feedback = sum(m["totalFeedback"] for m in months_data.values())
    total_similar = sum(m["totalSimilarFeedbackRespondents"] for m in months_data.values())

    nav_pills = "".join(f'<a href="#{m.replace(" ", "-")}">{m.split(" ")[0]}</a>' for m in MONTHS)
    sections = "".join(month_section(m, months_data[m]) for m in MONTHS if m in months_data)

    error_note = ""
    if data.get("errors"):
        error_note = f'<p class="data-note">{len(data["errors"])} events could not be counted for enrolled/attended (server rate-limited during collection) — shown as “—” above.</p>'

    html_out = TEMPLATE.format(
        nav_pills=nav_pills,
        sections=sections,
        total_events=total_events,
        total_enrolled=f"{total_enrolled:,}",
        total_attended=f"{total_attended:,}",
        total_feedback=f"{total_feedback:,}",
        total_similar=f"{total_similar:,}",
        error_note=error_note,
    )

    with open("digest_report.html", "w") as f:
        f.write(html_out)
    print("Wrote digest_report.html")


TEMPLATE = """<!doctype html>
<title>MCCIA RAMP Feedback Digest</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Manrope:wght@700;800&family=Source+Sans+3:wght@400;600&family=IBM+Plex+Mono:wght@500;600&display=swap">
<style>
  :root {{
    --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
    --navy: #14304f; --navy-ink: #ffffff;
    --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-4: #eda100;
    --accent: #eb6834; --accent-wash: #fdeee6;
    --good: #0ca30c; --warning: #fab219; --critical: #d03b3b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
      --navy: #1c3a5e; --navy-ink: #ffffff;
      --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
      --accent: #d95926; --accent-wash: #2a1d15;
    }}
  }}
  :root[data-theme="dark"] {{
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --navy: #1c3a5e; --navy-ink: #ffffff;
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
    --accent: #d95926; --accent-wash: #2a1d15;
  }}

  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--page); color: var(--ink);
    font-family: "Source Sans 3", system-ui, -apple-system, sans-serif;
    font-size: 15px; line-height: 1.5;
  }}
  h1, h2, .stat-value, .bar-group-title {{ font-family: "Manrope", system-ui, sans-serif; }}
  .num, .stat-value, table td.num, table th.num {{ font-variant-numeric: tabular-nums; }}
  .mono {{ font-family: "IBM Plex Mono", monospace; }}

  header.top {{
    position: sticky; top: 0; z-index: 5; background: var(--navy); color: var(--navy-ink);
    padding: 20px 28px; display: flex; align-items: baseline; justify-content: space-between; gap: 16px;
    flex-wrap: wrap;
  }}
  header.top h1 {{ margin: 0; font-size: 1.25rem; font-weight: 800; letter-spacing: -0.01em; }}
  header.top .subtitle {{ font-size: 0.85rem; opacity: 0.75; font-weight: 400; font-family: "Source Sans 3", sans-serif; margin-top: 2px; }}
  nav.month-nav {{ display: flex; gap: 6px; }}
  nav.month-nav a {{
    color: var(--navy-ink); text-decoration: none; font-size: 0.85rem; font-weight: 600;
    padding: 6px 14px; border-radius: 999px; background: rgba(255,255,255,0.12);
  }}
  nav.month-nav a:hover {{ background: rgba(255,255,255,0.22); }}

  main {{ max-width: 980px; margin: 0 auto; padding: 32px 24px 80px; }}

  .hero-stats {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 40px; }}
  .hero-stats .stat-tile {{ background: var(--surface); }}

  .data-note {{ color: var(--muted); font-size: 0.85rem; margin: -24px 0 32px; }}

  .month-section {{ margin-bottom: 56px; scroll-margin-top: 90px; }}
  .month-header {{ display: flex; align-items: baseline; gap: 12px; border-bottom: 2px solid var(--ink); padding-bottom: 8px; margin-bottom: 18px; }}
  .month-header h2 {{ margin: 0; font-size: 1.5rem; font-weight: 800; }}
  .month-count {{ color: var(--muted); font-size: 0.9rem; }}

  .stat-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px; }}
  .stat-tile {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px;
  }}
  .stat-tile.accent {{ background: var(--accent-wash); border-color: var(--accent); }}
  .stat-value {{ font-size: 1.6rem; font-weight: 800; line-height: 1.1; font-variant-numeric: tabular-nums; }}
  .stat-label {{ color: var(--ink-2); font-size: 0.78rem; margin-top: 4px; }}
  .stat-sub {{ color: var(--muted); font-size: 0.72rem; margin-top: 2px; }}

  .chart-and-spotlight {{ display: grid; grid-template-columns: 1.3fr 1fr; gap: 16px; margin-bottom: 20px; }}
  .bar-group, .spotlight {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; }}
  .bar-group-title {{ font-size: 0.85rem; font-weight: 700; margin-bottom: 12px; }}
  .bar-row {{ display: grid; grid-template-columns: 70px 1fr 56px; align-items: center; gap: 10px; margin-bottom: 8px; }}
  .bar-label {{ font-size: 0.78rem; color: var(--ink-2); }}
  .bar-track {{ height: 10px; background: var(--grid); border-radius: 6px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 6px; }}
  .bar-fill.series-1 {{ background: var(--series-1); }}
  .bar-fill.series-2 {{ background: var(--series-2); }}
  .bar-fill.series-3 {{ background: var(--series-3); }}
  .bar-fill.series-4 {{ background: var(--series-4); }}
  .bar-value {{ font-size: 0.78rem; text-align: right; font-variant-numeric: tabular-nums; }}

  .spotlight-title {{ font-size: 0.85rem; font-weight: 700; margin-bottom: 10px; }}
  .spotlight-block {{ margin-bottom: 14px; }}
  .spotlight-block:last-child {{ margin-bottom: 0; }}
  .spotlight-label {{ font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 6px; }}
  .dupe-quote {{ font-size: 0.85rem; font-style: italic; color: var(--ink-2); line-height: 1.4; }}
  .spotlight-empty {{ font-size: 0.8rem; color: var(--muted); }}

  .table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  thead th {{
    text-align: left; padding: 10px 12px; background: var(--surface); border-bottom: 1px solid var(--border);
    color: var(--muted); font-weight: 600; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em;
    position: sticky; top: 0;
  }}
  th.num, td.num {{ text-align: right; }}
  tbody tr {{ border-bottom: 1px solid var(--grid); }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:hover {{ background: var(--accent-wash); }}
  td {{ padding: 9px 12px; vertical-align: top; }}
  .event-name {{ font-weight: 600; }}
  .event-meta {{ color: var(--muted); font-size: 0.72rem; margin-top: 2px; }}
  .col-event {{ min-width: 260px; }}
  .col-flag {{ min-width: 110px; }}
  .dup-note {{ font-size: 0.68rem; color: var(--accent); margin-top: 3px; }}

  .badge {{
    display: inline-block; font-size: 0.68rem; font-weight: 600; padding: 2px 8px; border-radius: 999px;
  }}
  .badge-critical {{ background: rgba(208,59,59,0.14); color: var(--critical); }}
  .badge-warning {{ background: rgba(250,178,25,0.18); color: #9a6a00; }}
  .badge-muted {{ background: var(--grid); color: var(--muted); }}

  @media (max-width: 720px) {{
    .stat-grid, .hero-stats {{ grid-template-columns: repeat(2, 1fr); }}
    .chart-and-spotlight {{ grid-template-columns: 1fr; }}
    header.top {{ flex-direction: column; align-items: flex-start; }}
  }}
</style>

<header class="top">
  <div>
    <h1>MCCIA RAMP Feedback Digest</h1>
    <div class="subtitle">Mahratta Chamber of Commerce, Industries &amp; Agriculture · May–August 2026 · Industry Association scope</div>
  </div>
  <nav class="month-nav">{nav_pills}</nav>
</header>

<main>
  <div class="hero-stats">
    <div class="stat-tile"><div class="stat-value">{total_events}</div><div class="stat-label">Events, 4 months</div></div>
    <div class="stat-tile"><div class="stat-value">{total_enrolled}</div><div class="stat-label">Total enrolled</div></div>
    <div class="stat-tile"><div class="stat-value">{total_attended}</div><div class="stat-label">Total attended</div></div>
    <div class="stat-tile"><div class="stat-value">{total_feedback}</div><div class="stat-label">Feedback given</div></div>
    <div class="stat-tile accent"><div class="stat-value">{total_similar}</div><div class="stat-label">Matching feedback</div></div>
  </div>
  {error_note}
  {sections}
</main>
"""

if __name__ == "__main__":
    main()
