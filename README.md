# London ⇄ Melbourne Flight Scanner

Finds business/economy return fares between London's airports and Melbourne.
Comes in two forms:

- **A desktop app** — pick your own dates, cabin class, and airports in a
  normal window, click Search, done. This is the one to use if you want a
  custom search (recommended for most people, especially non-technical family).
- **A free website** — a broad, pre-fetched sweep refreshed weekly, that you
  filter/sort in the browser. No download needed, but you can't type in an
  arbitrary new date on the spot.

## Desktop app (custom searches)

**[Download FlightFareFinder.exe](../../releases/latest/download/FlightFareFinder.exe)**
— double-click it, no installation, no Python, no command line.

Pick your departure airport(s), cabin class(es), which way round you're
flying, your date range, and roughly how many nights away, then click
**Search Flights**. It searches live (this can take anywhere from ~30 seconds
to a few minutes depending how broad your search is) and opens the results
in your web browser when done — the same filterable/sortable report the
website uses.

Windows only for now. The `.exe` is rebuilt automatically by
[a GitHub Action](.github/workflows/build-exe.yml) whenever the app's code
changes, so the download link always has the latest version.

## Website (browse a pre-fetched sweep)

**Live site:** see the "Pages" link in this repo's sidebar (or Settings → Pages).

A [scheduled GitHub Action](.github/workflows/scan-and-publish.yml) runs every
Monday (and can be triggered manually from the **Actions** tab → *Scan flights
and publish* → **Run workflow**), searching a broad sweep of dates/trip-lengths/
cabins and publishing the result as a static page — no server, nothing to pay
for. The page itself is interactive: filter by airline tier, seat class,
route, trip length, price, and departure date, and sort any column, all
instantly in the browser.

Because the page only reflects whatever was actually searched, if you want a
different spread of dates/trip-lengths than the current default, edit the
`python flight_scanner.py ...` command in that workflow file.

## Running it yourself, locally

```bash
pip install -r requirements.txt
python flight_scanner.py --help   # command-line version
python gui_app.py                 # desktop app, run from source
```

See `flight_scanner.py --help` for the full set of command-line options
(dates, trip length, seat class, direction, origins/destination, etc).

## Notes and caveats

- This scrapes Google Flights' public search results rather than using an
  official API, so it can break if Google changes their site, and is a
  point-in-time snapshot rather than a live quote.
- Airline quality tiers (S–D) reflect business-class reputation specifically
  (Skytrax ratings + business-class award history) — treat them as a rough
  proxy if comparing economy or first class.
- A round trip's price isn't always the same both ways; see the report's
  footer for how "direction" affects what's shown.
- The desktop app deliberately keeps a single live search modest in scope (it
  automatically samples within your chosen range rather than checking every
  possible date) so a search finishes in a reasonable time while you wait.
