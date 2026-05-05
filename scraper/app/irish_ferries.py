"""Irish Ferries passenger ferry sailings and prices.

Uses Playwright (headless Chromium + stealth) to drive irishferries.com,
which is protected by Imperva Incapsula WAF — no public REST API exists.

ROUTES
  IRLUK   Ireland → Britain   (Dublin → Holyhead, Rosslare → Pembroke)
  UKIRL   Britain → Ireland   (Holyhead → Dublin, Pembroke → Rosslare)
  IRLFRA  Ireland → France    (Rosslare → Cherbourg)
  FRAIRL  France → Ireland    (Cherbourg → Rosslare)

Synchronous Playwright API. Wrap calls in `asyncio.to_thread(...)` from
async code, or run inside a FastAPI sync endpoint (FastAPI runs sync
handlers in its threadpool automatically).
"""

import re
import time
from typing import Any, Optional


ROUTES: dict[str, str] = {
    "dublin-holyhead":       "IRLUK",
    "rosslare-pembroke":     "IRLUK",
    "ireland-britain":       "IRLUK",
    "irluk":                 "IRLUK",

    "holyhead-dublin":       "UKIRL",
    "pembroke-rosslare":     "UKIRL",
    "britain-ireland":       "UKIRL",
    "ukirl":                 "UKIRL",

    "rosslare-cherbourg":    "IRLFRA",
    "ireland-france":        "IRLFRA",
    "irlfra":                "IRLFRA",

    "cherbourg-rosslare":    "FRAIRL",
    "france-ireland":        "FRAIRL",
    "frairl":                "FRAIRL",
}

TRANSPORT_CODES: dict[str, str] = {
    "foot":       "F",
    "f":          "F",
    "car":        "C",
    "c":          "C",
    "motorhome":  "M",
    "campervan":  "M",
    "m":          "M",
    "motorcycle": "MCYV",
    "mcyv":       "MCYV",
    "van":        "V",
    "v":          "V",
}


def resolve_route(route: str) -> str:
    key = str(route).lower().strip()
    if key in ROUTES:
        return ROUTES[key]
    raise ValueError(
        f"Unknown route {route!r}. Use one of: {', '.join(sorted(set(ROUTES.values())))}"
    )


def resolve_transport(transport: str) -> str:
    key = str(transport).lower().strip()
    if key in TRANSPORT_CODES:
        return TRANSPORT_CODES[key]
    if key.upper() in ("F", "C", "M", "MCYV", "ASCV", "G", "V"):
        return key.upper()
    raise ValueError(
        f"Unknown transport {transport!r}. Use: foot, car, motorhome, motorcycle, van"
    )


def _parse_price(text: str) -> Optional[float]:
    """Parse '€43.50' or '£66.50' or 'No Availability' → float or None."""
    text = text.strip()
    if not text or "no availability" in text.lower():
        return None
    m = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
    return float(m.group()) if m else None


def _navigate_calendar(page, target_year: int, target_month: int):
    MONTH_NUMS = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    for _ in range(24):
        header = page.evaluate(
            "document.querySelector('.litepicker .month-item-header').innerText.toUpperCase()"
        )
        parts = re.findall(r"[A-Z]+|\d+", header)
        if len(parts) >= 2:
            mon_str = parts[0][:3]
            yr = int(parts[-1])
            mon = MONTH_NUMS.get(mon_str, 0)
            if yr == target_year and mon == target_month:
                break
            if yr > target_year or (yr == target_year and mon > target_month):
                btn_sel = ".litepicker .button-previous-month"
            else:
                btn_sel = ".litepicker .button-next-month"
        else:
            btn_sel = ".litepicker .button-next-month"

        coords = page.evaluate(f"""(() => {{
            var btn = document.querySelector('{btn_sel}');
            if (!btn) return null;
            var b = btn.getBoundingClientRect();
            return [b.x + b.width/2, b.y + b.height/2];
        }})()""")
        if coords:
            page.mouse.click(coords[0], coords[1])
            time.sleep(0.2)


def _bclick(page, js: str):
    coords = page.evaluate(js)
    if coords:
        page.mouse.click(coords[0], coords[1])
        time.sleep(0.3)
    return coords


def _fill_and_submit(page, route_code: str, date: str,
                     adults: int, children: int, transport_code: str):
    year, month, day = int(date[:4]), int(date[5:7]), int(date[8:10])

    # Route dropdown
    _bclick(page, """(() => {
        var b = document.getElementById('DepartureRouteGroup')
            .closest('.choices').querySelector('.choices__inner').getBoundingClientRect();
        return [b.x + b.width/2, b.y + b.height/2];
    })()""")
    route_clicked = _bclick(page, f"""(() => {{
        var opt = document.querySelector(
            '.choices__item--choice[data-value="{route_code}"]');
        if (!opt) return null;
        var b = opt.getBoundingClientRect();
        return [b.x + b.width/2, b.y + b.height/2];
    }})()""")
    if not route_clicked:
        raise RuntimeError(f"Route option {route_code!r} not found in dropdown")

    # One-way toggle
    page.evaluate("document.getElementById('jsToggleOneWay').click()")
    time.sleep(0.3)

    # Departure date via litepicker
    page.click("#DepartureDateLitepicker")
    time.sleep(0.8)
    _navigate_calendar(page, year, month)
    day_clicked = _bclick(page, f"""(() => {{
        var days = Array.from(document.querySelectorAll('.litepicker .day-item'));
        var d = days.find(function(el) {{
            return el.innerText.trim() === '{day}' && !el.classList.contains('is-locked');
        }});
        if (!d) return null;
        var b = d.getBoundingClientRect();
        return [b.x + b.width/2, b.y + b.height/2];
    }})()""")
    if not day_clicked:
        raise RuntimeError(f"Day {day} not available in calendar (locked/sold out)")

    # Adults
    for _ in range(adults):
        _bclick(page, """(() => {
            var b = document.getElementById('DeparturePassengers.Adults')
                .closest('.c-input-counter').querySelector('.c-input-counter__increase')
                .getBoundingClientRect();
            return [b.x + b.width/2, b.y + b.height/2];
        })()""")

    # Children
    for _ in range(children):
        _bclick(page, """(() => {
            var b = document.getElementById('DeparturePassengers.Children')
                .closest('.c-input-counter').querySelector('.c-input-counter__increase')
                .getBoundingClientRect();
            return [b.x + b.width/2, b.y + b.height/2];
        })()""")

    # Transport / Method of transport
    page.evaluate("""
        document.querySelector("select[name='DepartureVehicle.MethodOfTransport']")
            .closest('.choices').scrollIntoView({block:'center'})
    """)
    time.sleep(0.4)
    _bclick(page, """(() => {
        var b = document.querySelector("select[name='DepartureVehicle.MethodOfTransport']")
            .closest('.choices').querySelector('.choices__inner').getBoundingClientRect();
        return [b.x + b.width/2, b.y + b.height/2];
    })()""")
    time.sleep(0.4)
    transport_code_js = transport_code.replace("'", "\\'")
    mot_clicked = _bclick(page, f"""(() => {{
        var opt = document.querySelector(
            "#choices--DepartureVehicleMethodOfTransport-item-choice-1 ~ *[data-value='{transport_code_js}']"
        ) || document.querySelector(
            ".choices__item--choice[data-value='{transport_code_js}']"
        );
        if (!opt) return null;
        opt.scrollIntoView({{block:'center'}});
        var b = opt.getBoundingClientRect();
        return [b.x + b.width/2, b.y + b.height/2];
    }})()""")
    if not mot_clicked:
        raise RuntimeError(f"Transport option {transport_code!r} not found")

    # Submit
    page.evaluate("""
        Array.from(document.querySelectorAll('button[name="action:Search"]'))
            .find(function(b) { return b.offsetParent !== null; })
            .scrollIntoView({block:'center'})
    """)
    time.sleep(0.4)
    _bclick(page, """(() => {
        var btn = Array.from(document.querySelectorAll('button[name="action:Search"]'))
            .find(function(b) { return b.offsetParent !== null; });
        var bx = btn.getBoundingClientRect();
        return [bx.x + bx.width/2, bx.y + bx.height/2];
    })()""")
    page.wait_for_url("**/step-2/**", timeout=15000)
    try:
        page.wait_for_selector(".c-spinner", state="hidden", timeout=15000)
    except Exception:
        pass
    time.sleep(1.5)


def _parse_sailings_for_date(page, date: str) -> list[dict]:
    return page.evaluate(f"""(() => {{
        var dateItem =
            document.querySelector('.outward-departure-date-content.tns-slide-active') ||
            document.querySelector('.outward-departure-date-content[data-date="{date}"]');
        if (!dateItem) return [];

        var results = [];
        var cards = dateItem.querySelectorAll('.c-date-ticket__result');
        cards.forEach(function(card) {{
            var disabled = card.classList.contains('disabled');
            var times = card.querySelectorAll('.c-date-ticket__time');
            var ports  = card.querySelectorAll('.c-date-ticket__port');
            var dep_time = times[0] ? times[0].innerText.trim() : '';
            var arr_time = times[1] ? times[1].innerText.trim() : '';
            var dep_port = ports[0] ? ports[0].innerText.trim() : '';
            var arr_port = ports[1] ? ports[1].innerText.trim() : '';
            var boatEl = card.querySelector('.c-date-ticket__boat');
            var vessel = boatEl ? boatEl.innerText.trim() : '';

            var prices = {{}};
            var options = card.querySelectorAll('.c-date-ticket__option');
            options.forEach(function(opt) {{
                var typeEl = opt.querySelector('.c-date-ticket__option__type');
                var priceEl = opt.querySelector('.current-price');
                if (!typeEl || !priceEl) return;
                var fareType = typeEl.innerText.replace(/\\s+/g, ' ').trim();
                prices[fareType] = priceEl.innerText.trim();
            }});

            if (dep_time) {{
                results.push({{
                    departure_port: dep_port,
                    arrival_port:   arr_port,
                    departure:      dep_time,
                    arrival:        arr_time,
                    ship:           vessel,
                    available:      !disabled,
                    prices:         prices,
                }});
            }}
        }});
        return results;
    }})()""")


def get_sailings(
    date: str,
    route: str,
    adults: int = 1,
    children: int = 0,
    transport: str = "foot",
    headless: bool = True,
) -> list[dict[str, Any]]:
    """Sailings + prices for a single date.

    Returns sailing dicts with keys: departure_port, arrival_port, departure,
    arrival, ship, available, prices (raw map), best_price (numeric), currency.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth.stealth import Stealth

    route_code = resolve_route(route)
    transport_code = resolve_transport(transport)

    stealth = Stealth()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="en-GB",
            )
            page = context.new_page()
            stealth.apply_stealth_sync(page)

            page.goto(
                "https://www.irishferries.com/uk-en/booking/step-1/",
                timeout=30000,
            )
            time.sleep(2)

            _fill_and_submit(page, route_code, date, adults, children, transport_code)
            raw_sailings = _parse_sailings_for_date(page, date)
        finally:
            browser.close()

    currency = "GBP" if route_code in ("UKIRL", "UKFRA") else "EUR"
    out: list[dict[str, Any]] = []
    for s in raw_sailings:
        numeric = {k: _parse_price(v) for k, v in s["prices"].items()}
        best = min((v for v in numeric.values() if v is not None), default=None)
        out.append({**s, "best_price": best, "currency": currency})
    return out


def get_sailings_week(
    start_date: str,
    route: str,
    adults: int = 1,
    children: int = 0,
    transport: str = "foot",
) -> dict[str, list[dict[str, Any]]]:
    """Sailings for the 7-day carousel containing start_date.

    Single browser session, single form submission, parses all 7 dates.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth.stealth import Stealth

    route_code = resolve_route(route)
    transport_code = resolve_transport(transport)
    stealth = Stealth()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="en-GB",
            )
            page = context.new_page()
            stealth.apply_stealth_sync(page)

            page.goto(
                "https://www.irishferries.com/uk-en/booking/step-1/",
                timeout=30000,
            )
            time.sleep(2)

            _fill_and_submit(page, route_code, start_date, adults, children, transport_code)

            week_dates = page.evaluate("""(() => {
                return Array.from(
                    document.querySelectorAll('.outward-departure-date[data-date]')
                ).map(function(el) { return el.getAttribute('data-date'); });
            })()""")

            currency = "GBP" if route_code in ("UKIRL", "UKFRA") else "EUR"
            results: dict[str, list[dict[str, Any]]] = {}
            for d in week_dates:
                if not d:
                    continue
                _bclick(page, f"""(() => {{
                    var btn = document.querySelector('.outward-departure-date[data-date="{d}"]');
                    if (!btn) return null;
                    var b = btn.getBoundingClientRect();
                    return [b.x + b.width/2, b.y + b.height/2];
                }})()""")
                time.sleep(1.5)

                raw = _parse_sailings_for_date(page, d)
                day_sailings: list[dict[str, Any]] = []
                for s in raw:
                    numeric = {k: _parse_price(v) for k, v in s["prices"].items()}
                    best = min((v for v in numeric.values() if v is not None), default=None)
                    day_sailings.append({**s, "best_price": best, "currency": currency})
                results[d] = day_sailings
        finally:
            browser.close()

    return results
