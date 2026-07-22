#!/usr/bin/env python3
"""
Flight Fare Finder - friendly desktop app for the London <-> Melbourne scanner.

No command line, no typed dates, no jargon: pick your options in the window,
click Search, and results open automatically in your web browser (the same
filterable/sortable report the website uses).
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import tkinter as tk
from tkinter import messagebox, ttk

from tkcalendar import DateEntry

from fast_flights.exceptions import FlightsNotFound
from flight_scanner import (
    AIRPORT_NAMES,
    DEFAULT_DESTINATION,
    LONDON_AIRPORTS,
    SEAT_LABELS,
    build_route_pairs,
    daterange,
    generate_html_report,
    search_one,
)

APP_TITLE = "Flight Fare Finder"
OUTPUT_DIR = Path.home() / "Documents" / "FlightFareFinder"

# Requests run a few at a time rather than strictly one-by-one, with a small
# per-request pacing delay on top -- a deliberately moderate middle ground.
# We've only actually validated Google tolerating ~1 request/second sustained
# (hundreds of requests, no blocks, including from a shared cloud IP); this is
# roughly a 5-6x speedup on that without going as far as "no pacing at all,"
# since a *blocked* home IP would be a worse outcome than a slower search.
CONCURRENT_REQUESTS = 3
SEARCH_DELAY_SECONDS = 0.5
DEFAULT_SEARCH_LIMIT = 180
# Above this many combinations, warn about the wait before starting rather
# than just launching into it -- the search limit is fully user-editable now,
# so a big date range + "every day" can add up to a genuinely long wait.
CONFIRM_THRESHOLD = 220

DIRECTION_CHOICES = [
    ("London first (normal)", "outbound"),
    ("Melbourne first", "return"),
    ("Check both ways", "both"),
]

SEAT_CHOICES = ["business", "premium-economy", "economy", "first"]


def pick_departure_dates(start: date, end: date, target_count: int) -> list[date]:
    span_days = (end - start).days
    if span_days <= 0:
        return [start]
    step = max(1, round(span_days / max(1, target_count - 1)))
    return list(daterange(start, end, step))


def pick_nights_list(min_nights: int, max_nights: int) -> list[int]:
    if min_nights >= max_nights:
        return [min_nights]
    if max_nights - min_nights <= 10:
        return sorted({min_nights, max_nights})
    mid = (min_nights + max_nights) // 2
    return sorted({min_nights, mid, max_nights})


class SearchCancelled(Exception):
    pass


def build_jobs(
    origins: list[str],
    destinations: list[str],
    direction: str,
    seat_classes: list[str],
    start_date: date,
    end_date: date,
    min_nights: int,
    max_nights: int,
    every_day: bool,
    search_limit: int,
) -> list[tuple]:
    """Pure, fast (no network) so it can be used both for a pre-flight count
    (to warn about a long wait before starting) and by the actual search."""
    route_pairs = build_route_pairs(origins, destinations, direction)
    nights_list = pick_nights_list(min_nights, max_nights)

    if every_day:
        departure_dates = list(daterange(start_date, end_date, 1))
    else:
        base = len(route_pairs) * len(seat_classes) * len(nights_list)
        target_dates = max(1, search_limit // max(1, base))
        departure_dates = pick_departure_dates(start_date, end_date, target_dates)

    jobs = [
        (origin, destination, dep, dep + timedelta(days=n), seat)
        for (origin, destination) in route_pairs
        for dep in departure_dates
        for n in nights_list
        for seat in seat_classes
    ]
    return jobs[:search_limit]


def _search_one_job(job: tuple) -> list:
    origin, destination, dep, ret, seat = job
    time.sleep(SEARCH_DELAY_SECONDS)  # each worker paces itself before its request
    try:
        return search_one(
            origin=origin,
            destination=destination,
            departure=dep,
            ret=ret,
            seat=seat,
            adults=1,
            currency="GBP",
            max_stops=None,
        )
    except FlightsNotFound:
        return []
    except Exception:
        return []  # keep going past a single bad search; not worth surfacing mid-run


def run_search(
    jobs: list[tuple],
    progress_queue: "queue.Queue",
    cancel_event: threading.Event,
    report_meta: dict,
) -> None:
    """Runs on a background thread; only ever talks back via progress_queue."""
    try:
        progress_queue.put(("total", len(jobs)))

        all_results = []
        completed = 0

        with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as executor:
            futures = {executor.submit(_search_one_job, job): job for job in jobs}
            try:
                for future in as_completed(futures):
                    if cancel_event.is_set():
                        for f in futures:
                            f.cancel()
                        raise SearchCancelled()

                    all_results.extend(future.result())
                    completed += 1
                    progress_queue.put(("progress", completed, len(jobs), len(all_results)))
            except SearchCancelled:
                executor.shutdown(wait=False, cancel_futures=True)
                raise

        if not all_results:
            progress_queue.put((
                "error",
                "No flights found for those options. Try a wider date range or a longer/shorter trip length.",
            ))
            return

        all_results.sort(key=lambda r: r.price)

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H%M")
        html_path = OUTPUT_DIR / f"results_{stamp}.html"

        fake_args = SimpleNamespace(
            html_output=str(html_path),
            output=str(html_path.with_suffix(".csv")),
            origins=",".join(report_meta["origins"]),
            destination=",".join(report_meta["destinations"]),
            direction=report_meta["direction"],
        )
        written_path = generate_html_report(all_results, fake_args)
        progress_queue.put(("done", written_path))

    except SearchCancelled:
        progress_queue.put(("cancelled", None))
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected as a friendly message
        traceback.print_exc()
        progress_queue.put(("error", f"Something went wrong: {exc}"))


class FlightFareFinderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.resizable(False, False)

        self.progress_queue: "queue.Queue" = queue.Queue()
        self.cancel_event = threading.Event()
        self.search_thread: threading.Thread | None = None

        self.origin_vars: dict[str, tk.BooleanVar] = {}
        self.seat_vars: dict[str, tk.BooleanVar] = {}
        self.direction_var = tk.StringVar(value="outbound")

        self._build_ui()

    # -- UI construction -----------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 8}

        header = ttk.Label(self.root, text="Find London ↔ Melbourne fares", font=("Segoe UI", 14, "bold"))
        header.grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 0))

        subheader = ttk.Label(
            self.root,
            text="Pick your options below and click Search. Results open in your web browser.",
            foreground="#555555",
        )
        subheader.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))

        # -- Airports & direction --
        airports_frame = ttk.LabelFrame(self.root, text="Which London airport(s)?")
        airports_frame.grid(row=2, column=0, sticky="nsew", **pad)
        for i, code in enumerate(LONDON_AIRPORTS):
            var = tk.BooleanVar(value=True)
            self.origin_vars[code] = var
            ttk.Checkbutton(airports_frame, text=AIRPORT_NAMES[code], variable=var).grid(
                row=i, column=0, sticky="w", padx=8, pady=2
            )

        direction_frame = ttk.LabelFrame(self.root, text="Which way first?")
        direction_frame.grid(row=2, column=1, sticky="nsew", **pad)
        for i, (label, value) in enumerate(DIRECTION_CHOICES):
            ttk.Radiobutton(direction_frame, text=label, value=value, variable=self.direction_var).grid(
                row=i, column=0, sticky="w", padx=8, pady=2
            )

        # -- Cabin class --
        class_frame = ttk.LabelFrame(self.root, text="Which cabin class(es)?")
        class_frame.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=12, pady=8)
        for i, seat in enumerate(SEAT_CHOICES):
            var = tk.BooleanVar(value=(seat == "business"))
            self.seat_vars[seat] = var
            ttk.Checkbutton(class_frame, text=SEAT_LABELS[seat], variable=var).grid(
                row=0, column=i, sticky="w", padx=12, pady=4
            )

        # -- Dates --
        dates_frame = ttk.LabelFrame(self.root, text="Departing between")
        dates_frame.grid(row=4, column=0, columnspan=2, sticky="nsew", padx=12, pady=8)
        today = date.today()
        ttk.Label(dates_frame, text="Earliest:").grid(row=0, column=0, padx=8, pady=6, sticky="e")
        self.start_date_entry = DateEntry(
            dates_frame, date_pattern="dd/mm/yyyy", mindate=today, year=today.year, month=today.month
        )
        self.start_date_entry.set_date(today + timedelta(days=14))
        self.start_date_entry.grid(row=0, column=1, padx=8, pady=6)

        ttk.Label(dates_frame, text="Latest:").grid(row=0, column=2, padx=8, pady=6, sticky="e")
        self.end_date_entry = DateEntry(
            dates_frame, date_pattern="dd/mm/yyyy", mindate=today, year=today.year, month=today.month
        )
        self.end_date_entry.set_date(today + timedelta(days=270))
        self.end_date_entry.grid(row=0, column=3, padx=8, pady=6)

        # -- Trip length --
        nights_frame = ttk.LabelFrame(self.root, text="How many nights away?")
        nights_frame.grid(row=5, column=0, columnspan=2, sticky="nsew", padx=12, pady=8)
        ttk.Label(nights_frame, text="Shortest:").grid(row=0, column=0, padx=8, pady=6, sticky="e")
        self.min_nights_spin = ttk.Spinbox(nights_frame, from_=1, to=180, width=6)
        self.min_nights_spin.set(30)
        self.min_nights_spin.grid(row=0, column=1, padx=8, pady=6)

        ttk.Label(nights_frame, text="Longest:").grid(row=0, column=2, padx=8, pady=6, sticky="e")
        self.max_nights_spin = ttk.Spinbox(nights_frame, from_=1, to=180, width=6)
        self.max_nights_spin.set(40)
        self.max_nights_spin.grid(row=0, column=3, padx=8, pady=6)

        # -- How thorough --
        thorough_frame = ttk.LabelFrame(self.root, text="How thorough?")
        thorough_frame.grid(row=6, column=0, columnspan=2, sticky="nsew", padx=12, pady=8)
        self.every_day_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            thorough_frame, text="Check every single day in range (slower)", variable=self.every_day_var
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 2))
        ttk.Label(thorough_frame, text="Search limit (higher = more thorough, slower):").grid(
            row=1, column=0, padx=8, pady=(2, 6), sticky="w"
        )
        self.search_limit_spin = ttk.Spinbox(thorough_frame, from_=1, to=999999, width=8)
        self.search_limit_spin.set(DEFAULT_SEARCH_LIMIT)
        self.search_limit_spin.grid(row=1, column=1, padx=8, pady=(2, 6), sticky="w")

        # -- Search button & progress --
        self.search_button = ttk.Button(self.root, text="Search Flights", command=self.on_search_clicked)
        self.search_button.grid(row=7, column=0, columnspan=2, pady=(12, 4))

        self.status_label = ttk.Label(self.root, text="", foreground="#555555")
        self.status_label.grid(row=8, column=0, columnspan=2, sticky="w", padx=12)

        self.progress_bar = ttk.Progressbar(self.root, orient="horizontal", length=460, mode="determinate")
        self.progress_bar.grid(row=9, column=0, columnspan=2, padx=12, pady=(0, 6))
        self.progress_bar.grid_remove()

        self.cancel_button = ttk.Button(self.root, text="Cancel search", command=self.on_cancel_clicked)
        self.cancel_button.grid(row=10, column=0, columnspan=2, pady=(0, 12))
        self.cancel_button.grid_remove()

    # -- Event handlers --------------------------------------------------

    def on_search_clicked(self) -> None:
        origins = [code for code, var in self.origin_vars.items() if var.get()]
        seat_classes = [seat for seat, var in self.seat_vars.items() if var.get()]

        if not origins:
            messagebox.showwarning(APP_TITLE, "Pick at least one departure airport.")
            return
        if not seat_classes:
            messagebox.showwarning(APP_TITLE, "Pick at least one cabin class.")
            return

        start_date = self.start_date_entry.get_date()
        end_date = self.end_date_entry.get_date()
        if end_date < start_date:
            messagebox.showwarning(APP_TITLE, "The latest departure date is before the earliest one.")
            return

        try:
            min_nights = int(self.min_nights_spin.get())
            max_nights = int(self.max_nights_spin.get())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Trip length needs to be a number of nights.")
            return
        if min_nights > max_nights:
            min_nights, max_nights = max_nights, min_nights

        try:
            search_limit = int(self.search_limit_spin.get())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Search limit needs to be a number.")
            return
        if search_limit < 1:
            messagebox.showwarning(APP_TITLE, "Search limit needs to be at least 1.")
            return

        destinations = [DEFAULT_DESTINATION]
        direction = self.direction_var.get()

        jobs = build_jobs(
            origins=origins,
            destinations=destinations,
            direction=direction,
            seat_classes=seat_classes,
            start_date=start_date,
            end_date=end_date,
            min_nights=min_nights,
            max_nights=max_nights,
            every_day=self.every_day_var.get(),
            search_limit=search_limit,
        )
        if not jobs:
            messagebox.showwarning(APP_TITLE, "That combination doesn't produce any searches -- widen your date range.")
            return

        if len(jobs) > CONFIRM_THRESHOLD:
            est_minutes = (len(jobs) / CONCURRENT_REQUESTS) * SEARCH_DELAY_SECONDS / 60
            proceed = messagebox.askyesno(
                APP_TITLE,
                f"This will check {len(jobs)} date/class combinations, which should take "
                f"roughly {est_minutes:.0f} minute(s) (plus real network time on top). Continue?",
            )
            if not proceed:
                return

        report_meta = {"origins": origins, "destinations": destinations, "direction": direction}

        self.cancel_event = threading.Event()
        self.progress_queue = queue.Queue()
        self.search_button.config(state="disabled")
        self.cancel_button.grid()
        self.cancel_button.config(state="normal")
        self.progress_bar.grid()
        self.progress_bar.config(value=0, maximum=max(len(jobs), 1))
        self.status_label.config(text="Starting search...")

        self.search_thread = threading.Thread(
            target=run_search, args=(jobs, self.progress_queue, self.cancel_event, report_meta), daemon=True
        )
        self.search_thread.start()
        self.root.after(100, self.poll_progress)

    def on_cancel_clicked(self) -> None:
        self.cancel_event.set()
        self.status_label.config(text="Cancelling...")
        self.cancel_button.config(state="disabled")

    def poll_progress(self) -> None:
        try:
            while True:
                message = self.progress_queue.get_nowait()
                kind = message[0]

                if kind == "total":
                    total = message[1]
                    self.progress_bar.config(maximum=max(total, 1))
                elif kind == "progress":
                    _, current, total, found_count = message
                    self.progress_bar.config(value=current)
                    self.status_label.config(
                        text=f"Checking {current} of {total}... found {found_count} fare(s) so far."
                    )
                elif kind == "done":
                    html_path = message[1]
                    self._finish(f"Done! Found results -- opening in your browser.")
                    webbrowser.open(html_path)
                    return
                elif kind == "cancelled":
                    self._finish("Search cancelled.")
                    return
                elif kind == "error":
                    self._finish("")
                    messagebox.showerror(APP_TITLE, message[1])
                    return
        except queue.Empty:
            pass

        self.root.after(100, self.poll_progress)

    def _finish(self, status_text: str) -> None:
        self.search_button.config(state="normal")
        self.cancel_button.config(state="normal")
        self.cancel_button.grid_remove()
        self.progress_bar.grid_remove()
        self.status_label.config(text=status_text)


def main() -> None:
    root = tk.Tk()
    FlightFareFinderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
