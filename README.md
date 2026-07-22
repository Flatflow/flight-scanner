# London ⇄ Melbourne Flight Scanner

Finds business/economy return fares between London's airports and Melbourne, and
publishes a filterable, sortable fare report as a free website via GitHub Pages.

**Live site:** see the "Pages" link in this repo's sidebar (or Settings → Pages)
once the first scan has run.

## How it works

A [scheduled GitHub Action](.github/workflows/scan-and-publish.yml) runs every
Monday (and can be triggered manually from the **Actions** tab → *Scan flights
and publish* → **Run workflow**). It searches Google Flights for a broad sweep
of dates/trip-lengths/cabins, then publishes the result as a static page —
no server to run, nothing to pay for.

The published page itself is fully interactive: filter by airline tier, seat
class, route, trip length, price, and departure date, and sort any column —
all instantly, in the browser, no reloading.

Because the report only reflects whatever was actually searched, if you want
a wider spread of dates/trip-lengths than the current default, edit the
`python flight_scanner.py ...` command in the workflow file and re-run it
(or wait for the next scheduled run).

## Running it yourself, locally

```bash
pip install -r requirements.txt
python flight_scanner.py --help
```

See `flight_scanner.py --help` for the full set of options (dates, trip
length, seat class, direction, origins/destination, etc).

## Notes and caveats

- This scrapes Google Flights' public search results rather than using an
  official API, so it can break if Google changes their site, and is a
  point-in-time snapshot rather than a live quote.
- Airline quality tiers (S–D) reflect business-class reputation specifically
  (Skytrax ratings + business-class award history) — treat them as a rough
  proxy if comparing economy or first class.
- A round trip's price isn't always the same both ways; see the report's
  footer for how "direction" affects what's shown.
