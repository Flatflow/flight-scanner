#!/usr/bin/env python3
"""
London <-> Melbourne fare scanner.

Searches Google Flights (via the unofficial `fast-flights` scraper) across every
London airport, a range of departure dates, trip lengths (nights), one or more
seat classes, and either direction of travel, to find the cheapest return fares.

This hits Google's public flight-search page repeatedly, impersonating a browser.
It is unofficial and can break if Google changes their site, and can get you
rate-limited if you search too aggressively -- keep an eye on --delay and
--max-requests.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from primp import Client
from selectolax.lexbor import LexborHTMLParser

from fast_flights import FlightQuery, Passengers, Query, create_query
from fast_flights.exceptions import FlightsNotFound
from fast_flights.model import (
    Airport,
    CarbonEmission,
    Flights,
    SimpleDatetime,
    SingleFlight,
)
from fast_flights.parser import ResultList

GOOGLE_FLIGHTS_URL = "https://www.google.com/travel/flights"

# Google shows an EU/UK cookie-consent interstitial instead of results unless a
# prior-consent cookie is present. Pre-seeding this well-known "already
# consented" SOCS cookie skips that wall so the actual flights page loads.
SOCS_COOKIE = "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiA_LyaBg"


def fetch_html(query: Query, proxy: Optional[str] = None) -> str:
    client = Client(
        impersonate="chrome_145",
        impersonate_os="macos",
        referer=True,
        proxy=proxy,
        cookie_store=True,
    )
    client.set_cookies("https://www.google.com", {"SOCS": SOCS_COOKIE})
    res = client.get(GOOGLE_FLIGHTS_URL, params=query.params())
    return res.text


def _decode_time(arr: Optional[list]) -> tuple[int, int]:
    """Google's payload trims trailing zero entries and uses `null` for a
    leading zero (e.g. `[3]` -> 3:00, `[None, 5]` -> 0:05, `[8, 40]` -> 8:40)."""
    arr = arr or []
    h = arr[0] if len(arr) > 0 and arr[0] is not None else 0
    m = arr[1] if len(arr) > 1 and arr[1] is not None else 0
    return (h, m)


def parse_flights(html: str) -> ResultList:
    """Re-implementation of fast_flights.parser.parse that tolerates itineraries
    with no price data. The upstream parser (fast-flights 3.0.2) raises an
    unhandled IndexError on those instead of skipping them, which happens
    routinely for business-class searches (some itineraries show "price
    unavailable")."""
    parser = LexborHTMLParser(html)
    script = parser.css_first(r"script.ds\:1")
    if script is None:
        raise FlightsNotFound("no flights found; results script missing (possibly blocked or consent wall)")

    js = script.text()
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise FlightsNotFound("no flights found; received error")

    payload = json.loads(data)

    flights = ResultList()
    items_container = payload[3] if len(payload) > 3 else None
    items = items_container[0] if items_container else None
    if not items:
        return flights

    for k in items:
        flight = k[0]
        price_info = k[1][0] if k[1] else []
        if len(price_info) < 2:
            continue  # price unavailable for this itinerary; skip it

        price = price_info[1]
        typ = flight[0]
        airlines = flight[1]

        sg_flights = []
        for single_flight in flight[2]:
            from_airport = Airport(code=single_flight[3], name=single_flight[4])
            to_airport = Airport(code=single_flight[6], name=single_flight[5])
            departure = SimpleDatetime(date=single_flight[20], time=_decode_time(single_flight[8]))
            arrival = SimpleDatetime(date=single_flight[21], time=_decode_time(single_flight[10]))
            sg_flights.append(
                SingleFlight(
                    from_airport=from_airport,
                    to_airport=to_airport,
                    departure=departure,
                    arrival=arrival,
                    duration=single_flight[11],
                    plane_type=single_flight[17],
                )
            )

        try:
            extras = flight[22]
            carbon = CarbonEmission(typical_on_route=extras[8], emission=extras[7])
        except (IndexError, TypeError):
            carbon = CarbonEmission(typical_on_route=0, emission=0)

        flights.append(Flights(type=typ, price=price, airlines=airlines, flights=sg_flights, carbon=carbon))

    return flights

LONDON_AIRPORTS = ["LHR", "LGW", "STN", "LTN", "LCY"]
DEFAULT_DESTINATION = "MEL"

AIRPORT_NAMES = {
    "LHR": "London Heathrow",
    "LGW": "London Gatwick",
    "STN": "London Stansted",
    "LTN": "London Luton",
    "LCY": "London City",
    "MEL": "Melbourne",
}

SEAT_LABELS = {
    "economy": "Economy",
    "premium-economy": "Premium Economy",
    "business": "Business",
    "first": "First",
}

# Airline quality tiers for business class, S (best) down to D.
#
# Methodology: Skytrax's 2026 airline star ratings (skytraxratings.com) set the
# floor -- 5-star, 4-star, or 3-star-and-below. Within the large 4-star bucket,
# airlines are split further using Skytrax/AirlineRatings' "World's Best
# Business Class" award placements and current hard-product reviews (seat type,
# fleet consistency), since a 4-star overall rating covers everything from
# Emirates' A380 suites to a regional carrier's angled-flat recliners.
#
#   S: The recognized elite of business class -- Skytrax 5-star and/or a
#      perennial top-6 in business-class-specific rankings.
#   A: Skytrax 5-star carriers not in the elite six, or 4-star carriers with a
#      top-10 business class award placement.
#   B: Skytrax 4-star, reliable modern lie-flat product, no major complaints.
#   C: Skytrax 4-star but the business product is dated, inconsistent across
#      the fleet, or the carrier is mid-transition.
#   D: Skytrax 3-star or below.
#
# Keys are lowercased and match the display names Google Flights actually
# returns (which are inconsistent -- "THAI" not "Thai Airways", "SWISS" not
# "Swiss International Air Lines" -- so common short forms are included too).
AIRLINE_TIERS: dict[str, tuple[str, str]] = {
    # S -- the recognized elite of business class
    "qatar airways": ("S", "Skytrax 5-star; Qsuite named world's best business class seat 5 years running"),
    "singapore airlines": ("S", "Skytrax 5-star; consistently top-3 business class worldwide"),
    "cathay pacific": ("S", "Skytrax 5-star; new Aria Suite, The Pier lounge"),
    "cathay pacific airways": ("S", "Skytrax 5-star; new Aria Suite, The Pier lounge"),
    "ana": ("S", "Skytrax 5-star; 'The Room' suite, best-in-class Japanese service"),
    "ana all nippon airways": ("S", "Skytrax 5-star; 'The Room' suite, best-in-class Japanese service"),
    "all nippon airways": ("S", "Skytrax 5-star; 'The Room' suite, best-in-class Japanese service"),
    "japan airlines": ("S", "Skytrax 5-star; Omotenashi service, award-winning business dining"),
    "jal": ("S", "Skytrax 5-star; Omotenashi service, award-winning business dining"),
    "emirates": ("S", "Skytrax 4-star, but A380 business/first consistently ranked top 5 worldwide"),

    # A -- Skytrax 5-star (not in the elite six) or top-10 business class awards
    "korean air": ("A", "Skytrax 5-star"),
    "eva air": ("A", "Skytrax 5-star"),
    "asiana airlines": ("A", "Skytrax 5-star"),
    "asiana": ("A", "Skytrax 5-star"),
    "hainan airlines": ("A", "Skytrax 5-star"),
    "starlux airlines": ("A", "Skytrax 5-star, newest carrier on the list"),
    "starlux": ("A", "Skytrax 5-star, newest carrier on the list"),
    "etihad": ("A", "Skytrax 4-star; Business Studios routinely top-10 for business class"),
    "etihad airways": ("A", "Skytrax 4-star; Business Studios routinely top-10 for business class"),
    "british airways": ("A", "Skytrax 4-star; new Club Suite with closing door, top-10 business class"),
    "ba": ("A", "Skytrax 4-star; new Club Suite with closing door, top-10 business class"),
    "turkish airlines": ("A", "Skytrax 4-star; top-10 business class, extensive network"),
    "turkish": ("A", "Skytrax 4-star; top-10 business class, extensive network"),
    "air france": ("A", "Skytrax 4-star; Michelin-starred business class dining, top-10"),

    # B -- Skytrax 4-star, reliable modern product
    "swiss": ("B", "Skytrax 4-star; solid modern lie-flat product"),
    "swiss international air lines": ("B", "Skytrax 4-star; solid modern lie-flat product"),
    "lufthansa": ("B", "Skytrax 4-star; consistent long-haul business product"),
    "klm": ("B", "Skytrax 4-star; consistent, no-frills-premium reputation"),
    "klm royal dutch airlines": ("B", "Skytrax 4-star; consistent, no-frills-premium reputation"),
    "virgin atlantic": ("B", "Skytrax 4-star; Upper Class well-liked, strong soft product"),
    "virgin australia": ("B", "Skytrax 4-star; good regional business product"),
    "qantas": ("B", "Skytrax 4-star; solid product, modernising fleet"),
    "qantas airways": ("B", "Skytrax 4-star; solid product, modernising fleet"),
    "thai": ("B", "Skytrax 4-star; comfortable but dated on older aircraft"),
    "thai airways": ("B", "Skytrax 4-star; comfortable but dated on older aircraft"),
    "malaysia airlines": ("B", "Skytrax 4-star; good value, solid regional product"),
    "finnair": ("B", "Skytrax 4-star; efficient, well-regarded Nordic carrier"),
    "iberia": ("B", "Skytrax 4-star"),
    "austrian airlines": ("B", "Skytrax 4-star"),
    "china airlines": ("B", "Skytrax 4-star"),
    "garuda indonesia": ("B", "Skytrax 4-star"),
    "oman air": ("B", "Skytrax 4-star"),
    "saudia": ("B", "Skytrax 4-star"),
    "saudi arabian airlines": ("B", "Skytrax 4-star"),
    "royal brunei airlines": ("B", "Skytrax 4-star"),
    "royal brunei": ("B", "Skytrax 4-star"),
    "gulf air": ("B", "Skytrax 4-star"),
    "ethiopian airlines": ("B", "Skytrax 4-star"),
    "south african airways": ("B", "Skytrax 4-star"),
    "aer lingus": ("B", "Skytrax 4-star"),
    "air canada": ("B", "Skytrax 4-star"),
    "air new zealand": ("B", "Skytrax 4-star"),

    # C -- Skytrax 4-star, but the business product is dated or inconsistent
    "air india": ("C", "Skytrax 4-star; mid-retrofit -- new A350/787 cabins are good, older ones lag"),
    "vietnam airlines": ("C", "Skytrax 4-star; strong service, hard product behind the market leaders"),
    "china southern": ("C", "Skytrax 4-star; dated 2-2-2 cabin widely criticised vs. regional peers"),
    "china southern airlines": ("C", "Skytrax 4-star; dated 2-2-2 cabin widely criticised vs. regional peers"),
    "philippine airlines": ("C", "Skytrax 4-star; good value, older widebody business product"),
    "condor": ("C", "Skytrax 4-star; lie-flat on new A330-900neo, but older A330-200s are angled-flat"),
    "condor airlines": ("C", "Skytrax 4-star; lie-flat on new A330-900neo, but older A330-200s are angled-flat"),
    "royal air maroc": ("C", "Skytrax 4-star"),
    "bangkok airways": ("C", "Skytrax 4-star"),

    # D -- Skytrax 3-star or below
    "china eastern": ("D", "Skytrax 3-star"),
    "china eastern airlines": ("D", "Skytrax 3-star"),
    "srilankan": ("D", "Skytrax 3-star"),
    "srilankan airlines": ("D", "Skytrax 3-star"),
    "shenzhen": ("D", "Skytrax 3-star; cabin product and service below international standard"),
    "shenzhen airlines": ("D", "Skytrax 3-star; cabin product and service below international standard"),
    "airasia x": ("D", "Skytrax 3-star low-cost carrier; its Premium Flatbed is well-reviewed for a budget carrier, but isn't full-service business class"),
}

TIER_RANK = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, "?": 5}


def lookup_tier(airline_name: str) -> tuple[str, str]:
    return AIRLINE_TIERS.get(airline_name.strip().lower(), ("?", "Not yet rated -- verify manually"))


def trip_tier(airlines_str: str) -> tuple[str, str, str]:
    """Grade an itinerary by its weakest-linked airline (worst of any carrier
    operating a leg), since one rough segment sets the experience for the trip."""
    names = [a.strip() for a in airlines_str.split(";") if a.strip()]
    worst: Optional[tuple[str, str, str]] = None
    for name in names:
        tier, note = lookup_tier(name)
        if worst is None or TIER_RANK[tier] > TIER_RANK[worst[0]]:
            worst = (tier, name, note)
    if worst is None:
        return ("?", "", "No airline data")
    return worst


@dataclass
class SearchResult:
    """One priced itinerary.

    `price` is the total round-trip fare (all passengers) for this exact
    outbound+return date pair -- verified against separate one-way fares to
    confirm it's a genuine discounted round-trip total, not a one-way price.
    Only the outbound leg's routing is available from this data source; Google
    resolves the specific return-leg flights in a later step of its own UI that
    this scraper doesn't follow.
    """

    origin: str
    destination: str
    departure_date: date
    return_date: date
    nights: int
    seat: str
    price: int
    currency: str
    airlines: str
    outbound_route: str
    outbound_stops: int
    outbound_duration_min: int
    tier: str
    tier_airline: str
    tier_note: str


def to_datetime(sd: SimpleDatetime) -> datetime:
    y, m, d = sd.date
    h, mi = sd.time
    return datetime(y, m, d, h, mi)


def fmt_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}m"


@dataclass
class MonthStat:
    label: str
    year: int
    month: int
    count: int
    min_price: int
    median_price: int


def monthly_seasonality(results: list[SearchResult]) -> list[MonthStat]:
    """Group itineraries by departure month to surface seasonal price patterns.

    This reflects only the dates actually searched -- it's evidence from this
    scan's sample, not a claim about the whole year unless the search covered it.
    """
    buckets: dict[tuple[int, int], list[int]] = {}
    for r in results:
        key = (r.departure_date.year, r.departure_date.month)
        buckets.setdefault(key, []).append(r.price)

    stats = []
    for (year, month), prices in sorted(buckets.items()):
        stats.append(
            MonthStat(
                label=date(year, month, 1).strftime("%b %Y"),
                year=year,
                month=month,
                count=len(prices),
                min_price=min(prices),
                median_price=int(statistics.median(prices)),
            )
        )
    return stats


TIER_LABELS = {
    "S": "S — Elite",
    "A": "A — Excellent",
    "B": "B — Solid",
    "C": "C — Mixed",
    "D": "D — Weak",
    "?": "? — Unrated",
}


def generate_html_report(results: list[SearchResult], args: argparse.Namespace) -> str:
    html_path = args.html_output
    if html_path is None:
        base, _ext = os.path.splitext(args.output)
        html_path = base + ".html"

    prices = [r.price for r in results]
    tier_counts = {t: 0 for t in TIER_RANK}
    for r in results:
        tier_counts[r.tier] += 1

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    nights_range = sorted({r.nights for r in results})
    date_span = (
        f"{min(r.departure_date for r in results).isoformat()} to "
        f"{max(r.departure_date for r in results).isoformat()}"
    )
    currency = results[0].currency

    origins_arg = sorted({o.strip().upper() for o in args.origins.split(",") if o.strip()})
    destinations_arg = sorted({d.strip().upper() for d in args.destination.split(",") if d.strip()})
    arrow = {"outbound": "→", "return": "←", "both": "⇄"}[args.direction]

    def side_label(codes: list[str], known_group: list[str], group_name: str) -> str:
        if sorted(codes) == sorted(known_group):
            return group_name
        if len(codes) == 1:
            return AIRPORT_NAMES.get(codes[0], codes[0])
        return "/".join(codes)

    origin_label = side_label(origins_arg, LONDON_AIRPORTS, "London")
    dest_label = side_label(destinations_arg, [DEFAULT_DESTINATION], "Melbourne")
    page_title = f"{origin_label} {arrow} {dest_label}"

    seat_classes_present = sorted({r.seat for r in results})
    routes_present = sorted({(r.origin, r.destination) for r in results})
    seat_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    for r in results:
        seat_counts[r.seat] = seat_counts.get(r.seat, 0) + 1
        route_counts[f"{r.origin}-{r.destination}"] = route_counts.get(f"{r.origin}-{r.destination}", 0) + 1

    subtitle = (
        f"{len(results)} priced itineraries · {html.escape(', '.join(SEAT_LABELS.get(s, s) for s in seat_classes_present))}"
        f" · {min(nights_range)}–{max(nights_range)} night trips · generated {generated_at}"
    )

    stat_cards = f"""
      <div class="stat-card">
        <span class="stat-label">Cheapest fare</span>
        <span class="stat-value">{min(prices):,} <small>{html.escape(currency)}</small></span>
        <span class="stat-sub">of {len(results)} itineraries scanned</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">Median fare</span>
        <span class="stat-value">{int(statistics.median(prices)):,} <small>{html.escape(currency)}</small></span>
        <span class="stat-sub">price range {min(prices):,}–{max(prices):,}</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">Routes searched</span>
        <span class="stat-value">{len(routes_present)}</span>
        <span class="stat-sub">{html.escape(", ".join(f"{o}→{d}" for o, d in routes_present))}</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">Trip lengths sampled</span>
        <span class="stat-value">{min(nights_range)}–{max(nights_range)}<small> nights</small></span>
        <span class="stat-sub">departures {html.escape(date_span)}</span>
      </div>
    """

    tier_chips = "".join(
        f'<button type="button" class="legend-chip tier-{t}" data-tier-filter="{t}" aria-pressed="true">'
        f'<span class="chip-dot"></span>{html.escape(TIER_LABELS[t])} '
        f'<span class="legend-count">{tier_counts[t]}</span></button>'
        for t in ["S", "A", "B", "C", "D", "?"]
        if tier_counts[t] > 0
    )

    class_filter_row = ""
    if len(seat_counts) > 1:
        class_chips = "".join(
            f'<button type="button" class="legend-chip chip-neutral" data-seat-filter="{html.escape(s)}" aria-pressed="true">'
            f'{html.escape(SEAT_LABELS.get(s, s))} <span class="legend-count">{seat_counts[s]}</span></button>'
            for s in seat_classes_present
        )
        class_filter_row = f'<div class="filter-row">{class_chips}</div>'

    route_filter_row = ""
    if len(route_counts) > 1:
        route_chips = "".join(
            f'<button type="button" class="legend-chip chip-neutral" data-route-filter="{html.escape(key)}" aria-pressed="true">'
            f'{html.escape(key.replace("-", " → "))} <span class="legend-count">{route_counts[key]}</span></button>'
            for key in sorted(route_counts)
        )
        route_filter_row = f'<div class="filter-row">{route_chips}</div>'

    nights_counts: dict[int, int] = {}
    for r in results:
        nights_counts[r.nights] = nights_counts.get(r.nights, 0) + 1
    nights_filter_row = ""
    if len(nights_counts) > 1:
        nights_chips = "".join(
            f'<button type="button" class="legend-chip chip-neutral" data-nights-filter="{n}" aria-pressed="true">'
            f'{n} nights <span class="legend-count">{nights_counts[n]}</span></button>'
            for n in sorted(nights_counts)
        )
        nights_filter_row = f'<div class="filter-row">{nights_chips}</div>'

    min_price, max_price = min(prices), max(prices)
    min_date = min(r.departure_date for r in results).isoformat()
    max_date = max(r.departure_date for r in results).isoformat()

    monthly = monthly_seasonality(results)
    seasonality_section = ""
    if len(monthly) >= 2:
        cheapest_month = min(m.min_price for m in monthly)
        scale_max = max(m.min_price for m in monthly)
        month_rows = []
        for m in monthly:
            pct = round((m.min_price / scale_max) * 100) if scale_max else 0
            is_cheapest = m.min_price == cheapest_month
            month_rows.append(f"""
          <div class="month-row{' cheapest' if is_cheapest else ''}">
            <span class="month-label">{html.escape(m.label)}</span>
            <div class="bar-track"><div class="bar-fill" style="width: {pct}%"></div></div>
            <span class="month-price">{m.min_price:,} <span class="cur">{html.escape(currency)}</span>
              <span class="muted">(median {m.median_price:,}, n={m.count})</span></span>
          </div>""")
        seasonality_section = f"""
  <section class="seasonality">
    <h2>Price by departure month</h2>
    <p class="muted">Cheapest fare found per month, from the dates this search actually scanned -- narrow evidence
      for the months covered, not a forecast for months not searched. Run a wider --departure-months sweep to fill
      in more of the year.</p>
    <div class="month-bars">{"".join(month_rows)}
    </div>
  </section>"""

    rows = []
    for r in results:
        tier_title = html.escape(f"{r.tier_airline}: {r.tier_note}" if r.tier_airline else r.tier_note)
        route_key = f"{r.origin}-{r.destination}"
        rows.append(f"""
        <tr data-price="{r.price}" data-tier="{TIER_RANK[r.tier]}" data-tier-letter="{html.escape(r.tier)}"
            data-nights="{r.nights}" data-date="{r.departure_date.isoformat()}" data-stops="{r.outbound_stops}"
            data-seat="{html.escape(r.seat)}" data-route="{html.escape(route_key)}">
          <td><span class="tier-badge tier-{r.tier}" title="{tier_title}">{html.escape(r.tier)}</span></td>
          <td class="mono num">{r.price:,} <span class="cur">{html.escape(r.currency)}</span></td>
          <td>{html.escape(r.origin)} <span class="muted">→ {html.escape(r.destination)}</span></td>
          <td>{html.escape(SEAT_LABELS.get(r.seat, r.seat))}</td>
          <td class="mono">{r.departure_date.isoformat()}</td>
          <td class="mono">{r.return_date.isoformat()}</td>
          <td class="mono num">{r.nights}</td>
          <td class="mono num">{r.outbound_stops}</td>
          <td class="mono route">{html.escape(r.outbound_route)}</td>
          <td>{html.escape(r.airlines)}</td>
        </tr>""")

    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(page_title)} — Fare Scan</title>
<style>
  :root {{
    --ground: #F5F1E9;
    --surface: #FFFFFF;
    --surface-2: #EFE8D8;
    --ink: #1B2129;
    --ink-dim: #5B6672;
    --line: #DED6C0;
    --accent: #1F6F5C;
    --tier-S: #B8860B;
    --tier-A: #2E8B57;
    --tier-B: #3A6EA5;
    --tier-C: #C1652E;
    --tier-D: #B23A3A;
    --tier-unknown: #8A8F98;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --ground: #10161C;
      --surface: #161D24;
      --surface-2: #1C232B;
      --ink: #EDEAE2;
      --ink-dim: #9AA3AC;
      --line: #2A323B;
      --accent: #5FC1AA;
      --tier-S: #E0AC4A;
      --tier-A: #4CAF7D;
      --tier-B: #5A93CB;
      --tier-C: #D97F45;
      --tier-D: #D15C5C;
      --tier-unknown: #9096A0;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--ground);
    color: var(--ink);
    font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    line-height: 1.45;
  }}
  .mono {{
    font-family: ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace;
    font-variant-numeric: tabular-nums;
  }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 2.5rem 1.5rem 4rem; }}
  header.masthead {{
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    margin-bottom: 2rem;
  }}
  h1 {{
    font-family: Georgia, "Iowan Old Style", "Times New Roman", serif;
    font-size: clamp(1.8rem, 3vw, 2.6rem);
    font-weight: 600;
    margin: 0;
    text-wrap: balance;
  }}
  .subtitle {{ color: var(--ink-dim); font-size: 0.95rem; }}
  .stat-strip {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1px;
    background: var(--line);
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 1.75rem;
  }}
  .stat-card {{
    background: var(--surface);
    padding: 1.1rem 1.3rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }}
  .stat-label {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-dim); }}
  .stat-value {{ font-family: ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace; font-size: 1.5rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .stat-value small {{ font-size: 0.85rem; font-weight: 400; color: var(--ink-dim); }}
  .stat-sub {{ font-size: 0.8rem; color: var(--ink-dim); }}
  .filter-bar {{
    display: flex;
    flex-direction: column;
    gap: 0.85rem;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 1.1rem 1.3rem;
    margin-bottom: 1.75rem;
  }}
  .filter-row {{ display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }}
  .range-filters {{ gap: 1.1rem; }}
  .range-filters label {{
    display: inline-flex;
    flex-direction: column;
    gap: 0.25rem;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--ink-dim);
  }}
  .range-filters input {{
    font: inherit;
    font-family: ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace;
    font-variant-numeric: tabular-nums;
    text-transform: none;
    letter-spacing: normal;
    color: var(--ink);
    background: var(--ground);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 0.35rem 0.5rem;
    width: 9.5rem;
  }}
  .reset-btn {{
    appearance: none;
    font: inherit;
    font-size: 0.8rem;
    color: var(--accent);
    background: none;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 0.4rem 0.75rem;
    cursor: pointer;
    margin-left: auto;
  }}
  .reset-btn:hover {{ border-color: var(--accent); }}
  .filter-status {{ margin: 0; font-size: 0.8rem; color: var(--ink-dim); }}
  .legend-chip {{
    appearance: none;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font: inherit;
    font-size: 0.8rem;
    padding: 0.3rem 0.65rem;
    border-radius: 999px;
    background: var(--surface-2);
    border: 1px solid var(--line);
    cursor: pointer;
  }}
  .chip-dot {{ width: 0.55rem; height: 0.55rem; border-radius: 50%; background: currentColor; }}
  .legend-count {{ color: var(--ink-dim); font-family: ui-monospace, monospace; }}
  .legend-chip.tier-S {{ color: var(--tier-S); }}
  .legend-chip.tier-A {{ color: var(--tier-A); }}
  .legend-chip.tier-B {{ color: var(--tier-B); }}
  .legend-chip.tier-C {{ color: var(--tier-C); }}
  .legend-chip.tier-D {{ color: var(--tier-D); }}
  .legend-chip.tier-\\? {{ color: var(--tier-unknown); }}
  .legend-chip.chip-neutral {{ color: var(--accent); }}
  .legend-chip.inactive {{ opacity: 0.35; }}
  .legend-chip:focus-visible, .reset-btn:focus-visible, .range-filters input:focus-visible, thead th:focus-visible {{
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }}
  .seasonality {{
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--surface);
    padding: 1.1rem 1.3rem 1.3rem;
    margin-bottom: 1.75rem;
  }}
  .seasonality h2 {{ font-size: 1rem; margin: 0 0 0.2rem; }}
  .seasonality > .muted {{ margin: 0 0 1rem; }}
  .month-bars {{ display: flex; flex-direction: column; gap: 0.5rem; }}
  .month-row {{
    display: grid;
    grid-template-columns: 5.5rem 1fr auto;
    align-items: center;
    gap: 0.75rem;
  }}
  .month-label {{ font-size: 0.85rem; color: var(--ink-dim); }}
  .month-row.cheapest .month-label {{ color: var(--ink); font-weight: 600; }}
  .bar-track {{ background: var(--surface-2); border-radius: 4px; height: 0.6rem; overflow: hidden; }}
  .bar-fill {{ background: var(--accent); height: 100%; border-radius: 4px; }}
  .month-row.cheapest .bar-fill {{ background: var(--tier-S); }}
  .month-price {{
    font-family: ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace;
    font-variant-numeric: tabular-nums;
    font-size: 0.85rem;
    white-space: nowrap;
  }}
  .table-scroll {{
    overflow-x: auto;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--surface);
  }}
  table {{ border-collapse: collapse; width: 100%; min-width: 820px; }}
  thead th {{
    position: sticky;
    top: 0;
    background: var(--surface-2);
    text-align: left;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--ink-dim);
    padding: 0.7rem 0.9rem;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
  }}
  thead th:hover {{ color: var(--ink); }}
  thead th.sorted::after {{ content: " \\2193"; color: var(--accent); }}
  thead th.sorted.asc::after {{ content: " \\2191"; color: var(--accent); }}
  tbody td {{
    padding: 0.65rem 0.9rem;
    border-bottom: 1px solid var(--line);
    font-size: 0.9rem;
    vertical-align: top;
  }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover {{ background: var(--surface-2); }}
  .num {{ text-align: right; }}
  .muted {{ color: var(--ink-dim); font-size: 0.82rem; }}
  .cur {{ color: var(--ink-dim); font-size: 0.78rem; }}
  .route {{ font-size: 0.85rem; }}
  .tier-badge {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.7rem;
    height: 1.7rem;
    border-radius: 6px;
    font-weight: 700;
    font-size: 0.85rem;
    color: #fff;
    cursor: help;
  }}
  .tier-S {{ background: var(--tier-S); }}
  .tier-A {{ background: var(--tier-A); }}
  .tier-B {{ background: var(--tier-B); }}
  .tier-C {{ background: var(--tier-C); }}
  .tier-D {{ background: var(--tier-D); }}
  .tier-\\? {{ background: var(--tier-unknown); }}
  footer {{
    margin-top: 2rem;
    font-size: 0.8rem;
    color: var(--ink-dim);
    border-top: 1px solid var(--line);
    padding-top: 1.25rem;
  }}
  footer p {{ margin: 0.4rem 0; max-width: 70ch; }}
  @media (prefers-reduced-motion: no-preference) {{
    .wrap {{ animation: fade-in 0.4s ease-out; }}
  }}
  @keyframes fade-in {{ from {{ opacity: 0; transform: translateY(4px); }} to {{ opacity: 1; transform: none; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header class="masthead">
    <h1>{html.escape(page_title)}</h1>
    <div class="subtitle">{subtitle}</div>
  </header>

  <div class="stat-strip">{stat_cards}</div>

  <div class="filter-bar" role="group" aria-label="Filter results">
    <div class="filter-row tier-filters">{tier_chips}</div>
    {class_filter_row}
    {route_filter_row}
    {nights_filter_row}
    <div class="filter-row range-filters">
      <label>Min price
        <input type="number" id="filter-min-price" inputmode="numeric" placeholder="{min_price:,}" min="{min_price}" max="{max_price}">
      </label>
      <label>Max price
        <input type="number" id="filter-max-price" inputmode="numeric" placeholder="{max_price:,}" min="{min_price}" max="{max_price}">
      </label>
      <label>Depart from
        <input type="date" id="filter-from-date" min="{min_date}" max="{max_date}" value="{min_date}">
      </label>
      <label>Depart to
        <input type="date" id="filter-to-date" min="{min_date}" max="{max_date}" value="{max_date}">
      </label>
      <button type="button" id="filter-reset" class="reset-btn">Reset filters</button>
    </div>
    <p class="filter-status" id="filter-status" aria-live="polite">Showing {len(results)} of {len(results)} itineraries</p>
  </div>
{seasonality_section}
  <div class="table-scroll">
    <table id="results-table">
      <thead>
        <tr>
          <th data-key="tier">Tier</th>
          <th data-key="price" class="sorted asc">Price</th>
          <th>Route</th>
          <th>Class</th>
          <th data-key="date">Depart</th>
          <th>Return</th>
          <th data-key="nights">Nights</th>
          <th data-key="stops">Stops</th>
          <th>Outbound route</th>
          <th>Airlines</th>
        </tr>
      </thead>
      <tbody>{"".join(rows)}
      </tbody>
    </table>
  </div>

  <footer>
    <p><strong>Price:</strong> total round-trip fare for this exact outbound/return date pair (verified against
      separate one-way fares to confirm it's a genuine discounted round-trip total). Only the outbound leg's routing
      is shown in detail — the specific return flight isn't resolvable from this data source, only its
      contribution to the total price.</p>
    <p><strong>Airline tier:</strong> graded S (best) to D, using Skytrax's 2026 star ratings as a floor, split
      further by Skytrax/AirlineRatings "World's Best Business Class" award placements and current hard-product
      reviews. An itinerary is graded by its weakest-linked carrier, since one rough segment sets the experience for
      the whole trip. Hover a tier badge for the airline and reasoning. "?" means the carrier hasn't been researched
      yet. The tier reflects each airline's <em>business class</em> reputation specifically -- if this report
      includes economy, premium economy, or first class fares, treat the tier as a rough proxy for the airline's
      general quality rather than a grade of that specific cabin.</p>
    <p><strong>Route direction:</strong> a round trip's total price isn't always symmetric -- airlines can file
      different fares depending on which end of the trip is the point of sale -- so results may include the same
      route searched from both directions. The Route column and origin/destination order in each row show which
      airport was queried as the outbound departure.</p>
    <p>Sourced by scraping Google Flights' public search results (unofficial, no API key) — prices can move
      quickly and this is a point-in-time snapshot, not a live quote.</p>
  </footer>
</div>
<script>
(function() {{
  var table = document.getElementById("results-table");
  var tbody = table.querySelector("tbody");
  var headers = table.querySelectorAll("th[data-key]");
  var state = {{ key: "price", asc: true }};

  function sortRows(key, asc) {{
    var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
    rows.sort(function(a, b) {{
      var av = a.dataset[key], bv = b.dataset[key];
      var an = parseFloat(av), bn = parseFloat(bv);
      var cmp = (isNaN(an) || isNaN(bn)) ? String(av).localeCompare(String(bv)) : an - bn;
      return asc ? cmp : -cmp;
    }});
    rows.forEach(function(r) {{ tbody.appendChild(r); }});
  }}

  headers.forEach(function(th) {{
    th.addEventListener("click", function() {{
      var key = th.dataset.key;
      var asc = state.key === key ? !state.asc : true;
      state = {{ key: key, asc: asc }};
      headers.forEach(function(h) {{ h.classList.remove("sorted", "asc"); }});
      th.classList.add("sorted");
      if (asc) th.classList.add("asc");
      sortRows(key, asc);
    }});
  }});

  // -- filters: tier/class/route toggles, price range, departure date range --
  var allRows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
  var minPriceInput = document.getElementById("filter-min-price");
  var maxPriceInput = document.getElementById("filter-max-price");
  var fromDateInput = document.getElementById("filter-from-date");
  var toDateInput = document.getElementById("filter-to-date");
  var statusEl = document.getElementById("filter-status");
  var resetBtn = document.getElementById("filter-reset");
  var defaultFromDate = fromDateInput.value;
  var defaultToDate = toDateInput.value;

  // Each facet toggles a row's dataset[rowKey] membership. A facet with no
  // buttons on the page (e.g. only one seat class was searched, so no class
  // chips were rendered) is left unrestricted -- every row passes it.
  var facetConfigs = [
    {{ rowKey: "tierLetter", selector: "[data-tier-filter]", btnKey: "tierFilter" }},
    {{ rowKey: "seat", selector: "[data-seat-filter]", btnKey: "seatFilter" }},
    {{ rowKey: "route", selector: "[data-route-filter]", btnKey: "routeFilter" }},
    {{ rowKey: "nights", selector: "[data-nights-filter]", btnKey: "nightsFilter" }}
  ];
  var facetActive = {{}};

  facetConfigs.forEach(function(cfg) {{
    var buttons = Array.prototype.slice.call(document.querySelectorAll(cfg.selector));
    if (buttons.length === 0) {{
      facetActive[cfg.rowKey] = null;
      return;
    }}
    var active = {{}};
    buttons.forEach(function(b) {{ active[b.dataset[cfg.btnKey]] = true; }});
    facetActive[cfg.rowKey] = active;
    buttons.forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        var v = btn.dataset[cfg.btnKey];
        var nowActive = !active[v];
        active[v] = nowActive;
        btn.setAttribute("aria-pressed", String(nowActive));
        btn.classList.toggle("inactive", !nowActive);
        applyFilters();
      }});
    }});
  }});

  function passesFacets(row) {{
    return facetConfigs.every(function(cfg) {{
      var active = facetActive[cfg.rowKey];
      return active === null || !!active[row.dataset[cfg.rowKey]];
    }});
  }}

  function applyFilters() {{
    var minP = minPriceInput.value === "" ? -Infinity : parseFloat(minPriceInput.value);
    var maxP = maxPriceInput.value === "" ? Infinity : parseFloat(maxPriceInput.value);
    var fromD = fromDateInput.value || null;
    var toD = toDateInput.value || null;
    var visible = 0;

    allRows.forEach(function(r) {{
      var price = parseFloat(r.dataset.price);
      var d = r.dataset.date;
      var ok = passesFacets(r)
        && price >= minP && price <= maxP
        && (!fromD || d >= fromD)
        && (!toD || d <= toD);
      r.style.display = ok ? "" : "none";
      if (ok) visible++;
    }});

    statusEl.textContent = "Showing " + visible + " of " + allRows.length + " itineraries";
  }}

  [minPriceInput, maxPriceInput, fromDateInput, toDateInput].forEach(function(el) {{
    el.addEventListener("input", applyFilters);
  }});

  resetBtn.addEventListener("click", function() {{
    minPriceInput.value = "";
    maxPriceInput.value = "";
    fromDateInput.value = defaultFromDate;
    toDateInput.value = defaultToDate;
    facetConfigs.forEach(function(cfg) {{
      var active = facetActive[cfg.rowKey];
      if (active === null) return;
      Array.prototype.slice.call(document.querySelectorAll(cfg.selector)).forEach(function(btn) {{
        active[btn.dataset[cfg.btnKey]] = true;
        btn.setAttribute("aria-pressed", "true");
        btn.classList.remove("inactive");
      }});
    }});
    applyFilters();
  }});
}})();
</script>
</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(document)

    return html_path


def daterange(start: date, end: date, step_days: int):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=step_days)


def build_nights_list(min_nights: int, max_nights: int, step: int) -> list[int]:
    nights = list(range(min_nights, max_nights + 1, step))
    if nights[-1] != max_nights:
        nights.append(max_nights)
    return nights


def build_route_pairs(origins: list[str], destinations: list[str], direction: str) -> list[tuple[str, str]]:
    """(first-leg-departure, first-leg-arrival) pairs to search.

    A round trip's total price isn't always the same both ways -- airlines can
    file directionally asymmetric fares -- so 'direction' controls which airport
    is queried as the departure point: 'outbound' departs from --origins first
    (e.g. London first), 'return' departs from --destination first (e.g.
    Melbourne first), 'both' runs every combination of the two.
    """
    pairs: list[tuple[str, str]] = []
    if direction in ("outbound", "both"):
        pairs.extend((o, d) for o in origins for d in destinations)
    if direction in ("return", "both"):
        pairs.extend((d, o) for o in origins for d in destinations)
    return pairs


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def parse_departure_months(spec: str, step_days: int) -> list[date]:
    dates: list[date] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        year_str, month_str = token.split("-")
        month_start, month_end = month_bounds(int(year_str), int(month_str))
        dates.extend(daterange(month_start, month_end, step_days))
    return dates


def search_one(
    origin: str,
    destination: str,
    departure: date,
    ret: date,
    seat: str,
    adults: int,
    currency: str,
    max_stops: Optional[int],
) -> list[SearchResult]:
    query = create_query(
        flights=[
            FlightQuery(date=departure.isoformat(), from_airport=origin, to_airport=destination),
            FlightQuery(date=ret.isoformat(), from_airport=destination, to_airport=origin),
        ],
        seat=seat,
        trip="round-trip",
        passengers=Passengers(adults=adults),
        currency=currency,
        max_stops=max_stops,
    )

    results = parse_flights(fetch_html(query))

    out: list[SearchResult] = []
    nights = (ret - departure).days
    for flight in results:
        legs = flight.flights
        if not legs:
            continue

        outbound_duration = int(
            (to_datetime(legs[-1].arrival) - to_datetime(legs[0].departure)).total_seconds() // 60
        )
        route = "-".join([legs[0].from_airport.code] + [leg.to_airport.code for leg in legs])
        airlines_str = "; ".join(flight.airlines)
        tier, tier_airline, tier_note = trip_tier(airlines_str)

        out.append(
            SearchResult(
                origin=origin,
                destination=destination,
                departure_date=departure,
                return_date=ret,
                nights=nights,
                seat=seat,
                price=flight.price,
                currency=currency,
                airlines=airlines_str,
                outbound_route=route,
                outbound_stops=len(legs) - 1,
                outbound_duration_min=outbound_duration,
                tier=tier,
                tier_airline=tier_airline,
                tier_note=tier_note,
            )
        )
    return out


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Find the cheapest return flights between London airports and Melbourne, across seat classes and either direction.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--origins",
        default=",".join(LONDON_AIRPORTS),
        help="Comma-separated London airport IATA codes to search.",
    )
    p.add_argument(
        "--destination",
        default=DEFAULT_DESTINATION,
        help="Comma-separated destination airport IATA code(s), e.g. 'MEL' or 'MEL,SYD'.",
    )
    p.add_argument(
        "--direction",
        default="outbound",
        choices=["outbound", "return", "both"],
        help=(
            "Which airport is queried as the first-leg departure. 'outbound' departs from --origins "
            "first (e.g. London first); 'return' departs from --destination first (e.g. Melbourne "
            "first) -- useful since round-trip fares aren't always symmetric; 'both' runs every "
            "combination and tags each result so you can compare."
        ),
    )
    p.add_argument(
        "--start-date",
        default=(date.today() + timedelta(days=14)).isoformat(),
        help="Earliest departure date to search (YYYY-MM-DD). Ignored if --departure-months is set.",
    )
    p.add_argument(
        "--end-date",
        default=(date.today() + timedelta(days=14 + 90)).isoformat(),
        help="Latest departure date to search (YYYY-MM-DD). Ignored if --departure-months is set.",
    )
    p.add_argument(
        "--departure-months",
        default=None,
        help=(
            "Comma-separated YYYY-MM months to sample departures from (e.g. '2026-09,2026-10'). "
            "Lets you target specific, non-contiguous months (handy for comparing shoulder seasons) "
            "without scanning everything in between. Overrides --start-date/--end-date."
        ),
    )
    p.add_argument(
        "--departure-step",
        type=int,
        default=1,
        help="Sample departure dates every N days across the search window (or within each --departure-months month).",
    )
    p.add_argument("--min-nights", type=int, default=30, help="Minimum trip length in nights. Ignored if --nights is set.")
    p.add_argument("--max-nights", type=int, default=40, help="Maximum trip length in nights. Ignored if --nights is set.")
    p.add_argument(
        "--nights-step",
        type=int,
        default=5,
        help="Sample trip lengths every N nights between --min-nights and --max-nights. Ignored if --nights is set.",
    )
    p.add_argument(
        "--nights",
        default=None,
        help="Comma-separated explicit trip lengths in nights (e.g. '30,35,45'). Overrides --min/--max/--nights-step.",
    )
    p.add_argument("--adults", type=int, default=1, help="Number of adult passengers.")
    p.add_argument("--currency", default="GBP", help="Currency code for prices (e.g. GBP, AUD, USD).")
    p.add_argument(
        "--seat",
        default="business",
        help=(
            "Comma-separated seat classes to search: economy, premium-economy, business, first "
            "(e.g. 'business,first' to compare cabins in one run). Note: airline tiers are graded on "
            "business class reputation and may not reflect the airline's economy/premium product."
        ),
    )
    p.add_argument("--max-stops", type=int, default=None, help="Maximum stops per direction (optional filter).")
    p.add_argument("--delay", type=float, default=0.5, help="Seconds to wait between requests (be polite to Google).")
    p.add_argument("--top", type=int, default=15, help="Number of cheapest results to print at the end.")
    p.add_argument("--output", default="business_class_results.csv", help="CSV file to write all results to (raw data).")
    p.add_argument(
        "--html-output",
        default=None,
        help="HTML file to write the styled report to (default: same name as --output, with .html).",
    )
    p.add_argument(
        "--max-requests",
        type=int,
        default=2000,
        help="Safety cap on total searches to run in one go. Increase deliberately for a wider sweep.",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    origins = [o.strip().upper() for o in args.origins.split(",") if o.strip()]
    destinations = [d.strip().upper() for d in args.destination.split(",") if d.strip()]

    seat_classes = [s.strip().lower() for s in args.seat.split(",") if s.strip()]
    invalid_seats = [s for s in seat_classes if s not in SEAT_LABELS]
    if invalid_seats:
        print(f"Unknown seat class(es): {', '.join(invalid_seats)}. Choose from: {', '.join(SEAT_LABELS)}.")
        return 1

    route_pairs = build_route_pairs(origins, destinations, args.direction)
    if not route_pairs:
        print("No routes to search -- check --origins/--destination.")
        return 1

    if args.departure_months:
        departure_dates = parse_departure_months(args.departure_months, args.departure_step)
        if not departure_dates:
            print(f"--departure-months '{args.departure_months}' produced no dates.")
            return 1
        window_desc = f"months {args.departure_months} (every {args.departure_step} days, {len(departure_dates)} dates)"
    else:
        start_date = date.fromisoformat(args.start_date)
        end_date = date.fromisoformat(args.end_date)
        departure_dates = list(daterange(start_date, end_date, args.departure_step))
        window_desc = f"{start_date} to {end_date} (every {args.departure_step} days, {len(departure_dates)} dates)"

    if args.nights:
        nights_list = sorted({int(n.strip()) for n in args.nights.split(",") if n.strip()})
    else:
        nights_list = build_nights_list(args.min_nights, args.max_nights, args.nights_step)

    jobs = [
        (origin, destination, dep, dep + timedelta(days=n), seat)
        for (origin, destination) in route_pairs
        for dep in departure_dates
        for n in nights_list
        for seat in seat_classes
    ]

    print(f"Routes: {', '.join(f'{o}->{d}' for o, d in route_pairs)} (direction: {args.direction})")
    print(f"Departure window: {window_desc}")
    print(f"Trip lengths (nights): {nights_list}")
    print(f"Seat classes: {', '.join(SEAT_LABELS[s] for s in seat_classes)} | Adults: {args.adults} | Currency: {args.currency}")
    print(f"Total searches to run: {len(jobs)}")

    if len(jobs) > args.max_requests:
        print(
            f"\nThis would run {len(jobs)} searches, above the safety cap of --max-requests={args.max_requests}.\n"
            "Narrow the search (fewer origins/destinations/dates/nights/seat classes) or pass a higher "
            "--max-requests to proceed."
        )
        return 1

    est_seconds = len(jobs) * args.delay
    print(f"Estimated minimum runtime: ~{est_seconds / 60:.1f} minutes (at --delay={args.delay}s)\n")

    all_results: list[SearchResult] = []
    errors = 0

    try:
        for i, (origin, destination, dep, ret, seat) in enumerate(jobs, start=1):
            label = f"[{i}/{len(jobs)}] {origin}->{destination} {seat} {dep} -> {ret} ({(ret - dep).days}n)"
            try:
                found = search_one(
                    origin=origin,
                    destination=destination,
                    departure=dep,
                    ret=ret,
                    seat=seat,
                    adults=args.adults,
                    currency=args.currency,
                    max_stops=args.max_stops,
                )
            except FlightsNotFound:
                print(f"{label}: no flights found")
                continue
            except Exception as exc:  # noqa: BLE001 - keep scanning past transient scrape/network errors
                errors += 1
                print(f"{label}: ERROR ({exc})")
                continue

            if not found:
                print(f"{label}: no results")
                continue

            cheapest = min(found, key=lambda r: r.price)
            print(f"{label}: {len(found)} options, cheapest {cheapest.price} {args.currency}")
            all_results.extend(found)

            if i < len(jobs):
                time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nInterrupted -- saving results collected so far.")

    if not all_results:
        print("\nNo results collected.")
        return 1

    all_results.sort(key=lambda r: r.price)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "origin",
                "origin_name",
                "destination",
                "destination_name",
                "seat_class",
                "departure_date",
                "return_date",
                "nights",
                "price_total_return",
                "currency",
                "airlines",
                "outbound_route",
                "outbound_stops",
                "outbound_duration",
                "airline_tier",
                "tier_driven_by",
                "tier_note",
            ]
        )
        for r in all_results:
            writer.writerow(
                [
                    r.origin,
                    AIRPORT_NAMES.get(r.origin, r.origin),
                    r.destination,
                    AIRPORT_NAMES.get(r.destination, r.destination),
                    SEAT_LABELS.get(r.seat, r.seat),
                    r.departure_date.isoformat(),
                    r.return_date.isoformat(),
                    r.nights,
                    r.price,
                    r.currency,
                    r.airlines,
                    r.outbound_route,
                    r.outbound_stops,
                    fmt_duration(r.outbound_duration_min),
                    r.tier,
                    r.tier_airline,
                    r.tier_note,
                ]
            )

    print(f"\nWrote {len(all_results)} results to {args.output}")
    if errors:
        print(f"({errors} searches errored out and were skipped)")
    print(
        "Note: 'price' is the total round-trip fare for this exact date pair. Only the "
        "outbound routing is shown in detail -- the specific return flight isn't resolvable "
        "from this data source, only its contribution to the total price."
    )

    top = all_results[: args.top]
    print(f"\nTop {len(top)} cheapest fares:\n")
    header = (
        f"{'Tier':<6}{'Route':<10}{'Class':<17}{'Depart':<12}{'Return':<12}{'Nights':<8}"
        f"{'Price':<12}{'Stops':<7}{'Out. route':<20}{'Airlines'}"
    )
    print(header)
    print("-" * len(header))
    for r in top:
        price_str = f"{r.price} {r.currency}"
        route_str = f"{r.origin}-{r.destination}"
        print(
            f"{r.tier:<6}{route_str:<10}{SEAT_LABELS.get(r.seat, r.seat):<17}"
            f"{r.departure_date.isoformat():<12}{r.return_date.isoformat():<12}"
            f"{r.nights:<8}{price_str:<12}{r.outbound_stops:<7}{r.outbound_route:<20}{r.airlines}"
        )

    monthly = monthly_seasonality(all_results)
    if len(monthly) >= 2:
        print("\nCheapest fare by departure month (from the dates actually scanned):\n")
        for m in monthly:
            print(f"  {m.label:<10}min {m.min_price:>7,} {args.currency}   median {m.median_price:>7,}   (n={m.count})")

    html_path = generate_html_report(all_results, args)
    print(f"\nWrote styled report to {html_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
