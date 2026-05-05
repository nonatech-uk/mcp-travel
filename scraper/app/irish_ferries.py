"""
Irish Ferries passenger ferry sailings and prices.

Uses Playwright (headless Chromium + stealth) to drive the irishferries.com
booking form, which is protected by Imperva Incapsula WAF.

ROUTES
  IRLUK   Ireland → Britain   (Dublin → Holyhead, Rosslare → Pembroke)
  UKIRL   Britain → Ireland   (Holyhead → Dublin, Pembroke → Rosslare)
  IRLFRA  Ireland → France    (Rosslare → Cherbourg)
  FRAIRL  France → Ireland    (Cherbourg → Rosslare)

TRANSPORT TYPES
  "foot"        → F     on foot / cycling (default)
  "car"         → C     car
  "motorhome"   → M     campervan / motorhome
  "motorcycle"  → MCYV  motorcycle
  "van"         → V     van

VEHICLE DIMENSIONS  (shown after selecting a vehicle type; pass as vehicle_height)
  Car / Van:
    "standard"  → ACRV   Car/MPV/4x4 up to 1.9 m high   (default for car)
    "medium"    → BCRV   Car/MPV/4x4 up to 2.25 m high
    "high"      → CCRV   Car/MPV/4x4 over 2.25 m high
  Motorhome / Campervan:
    "standard"  → BDMV   Camper/Motorhome up to 2.25 m high  (default for motorhome)
    "high"      → CDMV   Camper/Motorhome over 2.25 m high
    "long"      → DCRV   Camper/Motorhome over 8 m long
  Pass the raw code (e.g. "BCRV") to override the default.

FARE TYPES
  Economy, Flexi, Flexi+

CURRENCY: EUR for Ireland-origin routes; GBP for Britain-origin routes.
"""

import re
import time
from typing import Optional

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

ROUTES = {
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

TRANSPORT_CODES = {
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

# Default dimension code per transport type (first/cheapest option)
_DEFAULT_DIMENSIONS = {
    "C":    "ACRV",   # Car up to 1.9m
    "M":    "BDMV",   # Motorhome up to 2.25m
    "V":    "ACRV",   # Van (uses same choices as car on most routes)
    "MCYV": None,     # Motorcycle has no dimension dropdown
    "F":    None,     # Foot — no dimension
}

# Human-readable height shortcuts → dimension code
_HEIGHT_SHORTCUTS = {
    # Car / Van
    "standard": "ACRV",
    "medium":   "BCRV",
    "high":     "CCRV",
    # Motorhome (override with these if motorhome)
    "long":     "DCRV",
}

# Motorhome-specific overrides for standard/high
_MOTORHOME_HEIGHT = {
    "standard": "BDMV",
    "medium":   "BDMV",
    "high":     "CDMV",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_route(route) -> str:
    key = str(route).lower().strip()
    if key in ROUTES:
        return ROUTES[key]
    raise ValueError(
        f"Unknown route {route!r}. "
        f"Use one of: {', '.join(sorted(set(ROUTES.values())))}"
    )


def _resolve_transport(transport) -> str:
    key = str(transport).lower().strip()
    if key in TRANSPORT_CODES:
        return TRANSPORT_CODES[key]
    # Already a valid code?
    if key.upper() in ("F", "C", "M", "MCYV", "ASCV", "G", "V"):
        return key.upper()
    raise ValueError(f"Unknown transport {transport!r}. Use: foot, car, motorhome, motorcycle, van")


def _parse_price(text: str) -> Optional[float]:
    """Parse '€43.50' or '£66.50' or 'No Availability' → float or None."""
    text = text.strip()
    if not text or "No Availability" in text or "no availability" in text.lower():
        return None
    m = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
    return float(m.group()) if m else None


def _navigate_calendar(page, target_year: int, target_month: int):
    """Navigate litepicker calendar to the target year/month."""
    MONTH_NUMS = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    for _ in range(24):
        header = page.evaluate(
            "document.querySelector('.litepicker .month-item-header').innerText.toUpperCase()"
        )
        # e.g. "JULY2026" or "JUL2026" or "JULY 2026"
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
    """Evaluate JS that returns [x, y] coords and mouse-click there."""
    coords = page.evaluate(js)
    if coords:
        page.mouse.click(coords[0], coords[1])
        time.sleep(0.3)
    return coords


def _fill_and_submit(page, route_code: str, date: str,
                     adults: int, children: int, transport_code: str,
                     dimension_code: Optional[str] = None):
    """
    Fill step-1 form and submit, landing on step-2.
    date must be YYYY-MM-DD.
    dimension_code: choices.js data-value for VehicleDimensions (e.g. "ACRV").
                    Required for car/motorhome/van; ignored for foot/motorcycle.
    """
    year, month, day = int(date[:4]), int(date[5:7]), int(date[8:10])

    # --- Route dropdown ---
    # Scope to the DepartureRouteGroup container to avoid hitting the
    # IrelandStation (ferry+rail) dropdown which shares the same classes.
    _bclick(page, """(() => {
        var b = document.getElementById('DepartureRouteGroup')
            .closest('.choices').querySelector('.choices__inner').getBoundingClientRect();
        return [b.x + b.width/2, b.y + b.height/2];
    })()""")
    route_clicked = _bclick(page, f"""(() => {{
        var container = document.getElementById('DepartureRouteGroup').closest('.choices');
        var opt = container.querySelector('.choices__item--choice[data-value="{route_code}"]');
        if (!opt) return null;
        opt.scrollIntoView({{block: 'nearest'}});
        var b = opt.getBoundingClientRect();
        return [b.x + b.width/2, b.y + b.height/2];
    }})()""")
    if not route_clicked:
        raise RuntimeError(f"Route option {route_code!r} not found in dropdown")

    # --- One-way toggle ---
    page.evaluate("document.getElementById('jsToggleOneWay').click()")
    time.sleep(0.3)

    # --- Departure date ---
    # Set directly via jQuery (more reliable than driving the litepicker calendar UI,
    # which behaves differently across routes and in headless mode).
    _MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    lp_display = f"{day:02d} {_MONTHS[month - 1]} {year}"
    page.evaluate(f"""(() => {{
        jQuery('#DepartureDate').val('{date}');
        var lp = document.getElementById('DepartureDateLitepicker');
        lp.value = '{lp_display}';
        lp.classList.remove('error');
        var errEl = document.getElementById('errorDepartureDate');
        if (errEl) errEl.classList.add('u-hidden');
    }})()""")
    time.sleep(0.3)

    # --- Adults ---
    for _ in range(adults):
        _bclick(page, """(() => {
            var b = document.getElementById('DeparturePassengers.Adults')
                .closest('.c-input-counter').querySelector('.c-input-counter__increase')
                .getBoundingClientRect();
            return [b.x + b.width/2, b.y + b.height/2];
        })()""")

    # --- Children ---
    for _ in range(children):
        _bclick(page, """(() => {
            var b = document.getElementById('DeparturePassengers.Children')
                .closest('.c-input-counter').querySelector('.c-input-counter__increase')
                .getBoundingClientRect();
            return [b.x + b.width/2, b.y + b.height/2];
        })()""")

    # --- Transport / Method of transport ---
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

    # --- Vehicle dimensions (required for car / motorhome / van) ---
    if dimension_code:
        time.sleep(0.5)  # let the dimensions dropdown populate
        dim_code_js = dimension_code.replace("'", "\\'")
        # Check if the dimension dropdown has the expected item
        dim_items = page.evaluate(f"""(() => {{
            return Array.from(document.querySelectorAll(
                '.choices__item--choice[data-value="{dim_code_js}"]'
            )).map(function(el) {{
                var b = el.getBoundingClientRect();
                return {{value: el.getAttribute('data-value'), visible: b.width > 0}};
            }});
        }})()""")

        # Open the VehicleDimensions choices dropdown
        _bclick(page, """(() => {
            var sel = document.querySelector("select[name='DepartureVehicle.VehicleDimensions']");
            if (!sel) return null;
            var wrapper = sel.closest('.choices');
            if (!wrapper) return null;
            wrapper.scrollIntoView({block:'center'});
            var inner = wrapper.querySelector('.choices__inner');
            if (!inner) return null;
            var b = inner.getBoundingClientRect();
            return [b.x + b.width/2, b.y + b.height/2];
        })()""")
        time.sleep(0.4)

        # Click the specific dimension option
        dim_clicked = _bclick(page, f"""(() => {{
            // The dropdown is now open — find our target item
            var sel = document.querySelector("select[name='DepartureVehicle.VehicleDimensions']");
            if (!sel) return null;
            var wrapper = sel.closest('.choices');
            if (!wrapper) return null;
            var opt = wrapper.querySelector('.choices__item--choice[data-value="{dim_code_js}"]');
            if (!opt) return null;
            opt.scrollIntoView({{block:'nearest'}});
            var b = opt.getBoundingClientRect();
            return [b.x + b.width/2, b.y + b.height/2];
        }})()""")
        if not dim_clicked:
            raise RuntimeError(
                f"Vehicle dimension option {dimension_code!r} not found in dropdown. "
                f"Available items: {dim_items}"
            )
        time.sleep(0.3)

    # --- Submit ---
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
    """
    Parse the sailing cards displayed for `date` (YYYY-MM-DD) on step-2.
    Returns list of sailing dicts.
    """
    return page.evaluate(f"""(() => {{
        // Find the active date item, or the one matching our date
        var dateItem =
            document.querySelector('.outward-departure-date-content.tns-slide-active') ||
            document.querySelector('.outward-departure-date-content[data-date="{date}"]');
        if (!dateItem) return [];

        var results = [];
        var cards = dateItem.querySelectorAll('.c-date-ticket__result');
        cards.forEach(function(card) {{
            var disabled = card.classList.contains('disabled');

            // Times and ports
            var times = card.querySelectorAll('.c-date-ticket__time');
            var ports  = card.querySelectorAll('.c-date-ticket__port');
            var dep_time = times[0] ? times[0].innerText.trim() : '';
            var arr_time = times[1] ? times[1].innerText.trim() : '';
            var dep_port = ports[0] ? ports[0].innerText.trim() : '';
            var arr_port = ports[1] ? ports[1].innerText.trim() : '';

            // Vessel name
            var boatEl = card.querySelector('.c-date-ticket__boat');
            var vessel = boatEl ? boatEl.innerText.trim() : '';

            // Fares
            var prices = {{}};
            var options = card.querySelectorAll('.c-date-ticket__option');
            options.forEach(function(opt) {{
                var typeEl = opt.querySelector('.c-date-ticket__option__type');
                var priceEl = opt.querySelector('.current-price');
                if (!typeEl || !priceEl) return;
                var fareType = typeEl.innerText.replace(/\\s+/g, ' ').trim();
                var priceText = priceEl.innerText.trim();
                prices[fareType] = priceText;
            }});

            if (dep_time) {{
                results.push({{
                    departure_port: dep_port,
                    arrival_port:   arr_port,
                    departure_time: dep_time,
                    arrival_time:   arr_time,
                    vessel:         vessel,
                    available:      !disabled,
                    prices:         prices,
                }});
            }}
        }});
        return results;
    }})()""")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_sailings(
    date: str,
    route,
    adults: int = 1,
    children: int = 0,
    transport: str = "foot",
    vehicle_height: str = "standard",
    headless: bool = True,
) -> list[dict]:
    """
    Get Irish Ferries sailing times and prices for a given date and route.

    Args:
        date:           Departure date YYYY-MM-DD.
        route:          Route code or name. Examples:
                          "IRLUK", "ireland-britain", "dublin-holyhead"
                          "UKIRL", "britain-ireland", "holyhead-dublin"
                          "IRLFRA", "ireland-france", "rosslare-cherbourg"
                          "FRAIRL", "france-ireland", "cherbourg-rosslare"
        adults:         Number of adult passengers (default 1).
        children:       Number of child passengers (default 0).
        transport:      Transport type: "foot" (default), "car", "motorhome",
                        "motorcycle", "van".
        vehicle_height: Vehicle height category — only used when transport is
                        "car", "motorhome", or "van":
                          "standard" (default) — car ≤1.9m / motorhome ≤2.25m
                          "medium"             — car ≤2.25m (SUV, tall estate)
                          "high"               — car >2.25m / motorhome >2.25m
                          "long"               — motorhome >8m long
                          or pass a raw code e.g. "ACRV", "BCRV", "BDMV"
        headless:       Run browser headlessly (default True).

    Returns:
        List of sailing dicts, each with:
            departure_port  - e.g. "Dublin", "Holyhead"
            arrival_port    - e.g. "Holyhead", "Dublin"
            departure_time  - HH:MM local time
            arrival_time    - HH:MM local time
            vessel          - ship name, e.g. "Ulysses", "James Joyce"
            available       - True if at least one fare is bookable
            prices          - dict of {fare_type: price_str}
                              e.g. {"Economy": "€43.50", "Flexi": "€49.50", "Flexi +": "€66.50"}
                              "No Availability" means sold out for that fare
            best_price      - lowest available numeric price, or None
            currency        - "EUR" or "GBP" depending on route direction

    Note:
        This function launches a headless Chromium browser. Typical execution
        time is 15-25 seconds.

    Raises:
        RuntimeError if unable to complete booking form or navigate to step-2.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth.stealth import Stealth

    route_code = _resolve_route(route)
    transport_code = _resolve_transport(transport)

    # Resolve vehicle dimension code
    dimension_code = _DEFAULT_DIMENSIONS.get(transport_code)  # None for foot/motorcycle
    if dimension_code is not None:
        # User may override via vehicle_height
        vh_key = vehicle_height.lower().strip()
        if transport_code == "M":
            dimension_code = _MOTORHOME_HEIGHT.get(vh_key, vh_key.upper() or dimension_code)
        else:
            dimension_code = _HEIGHT_SHORTCUTS.get(vh_key, vh_key.upper() or dimension_code)

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

            _fill_and_submit(
                page, route_code, date, adults, children, transport_code, dimension_code
            )

            raw_sailings = _parse_sailings_for_date(page, date)
        finally:
            browser.close()

    # Post-process: add best_price and currency
    currency = "GBP" if route_code in ("UKIRL", "UKFRA") else "EUR"
    result = []
    for s in raw_sailings:
        numeric_prices = {
            k: _parse_price(v)
            for k, v in s["prices"].items()
        }
        best = min((v for v in numeric_prices.values() if v is not None), default=None)
        result.append({
            **s,
            "best_price": best,
            "currency": currency,
        })
    return result


def find_sailings_week(
    start_date: str,
    route,
    adults: int = 1,
    children: int = 0,
    transport: str = "foot",
    vehicle_height: str = "standard",
) -> dict[str, list[dict]]:
    """
    Get Irish Ferries sailings for a week (7 days) starting from start_date.

    Returns a dict mapping date (YYYY-MM-DD) → list of sailings.
    The booking form always shows 7 days; call once and parse all 7 dates.

    Args:
        start_date:  Any date within the desired week (YYYY-MM-DD). The booking
                     form will show the week containing this date.
        route:       Route code or name (see get_sailings).
        adults:      Number of adult passengers.
        children:    Number of child passengers.
        transport:   Transport type.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth.stealth import Stealth

    route_code = _resolve_route(route)
    transport_code = _resolve_transport(transport)
    dimension_code = _DEFAULT_DIMENSIONS.get(transport_code)
    if dimension_code is not None:
        vh_key = vehicle_height.lower().strip()
        if transport_code == "M":
            dimension_code = _MOTORHOME_HEIGHT.get(vh_key, vh_key.upper() or dimension_code)
        else:
            dimension_code = _HEIGHT_SHORTCUTS.get(vh_key, vh_key.upper() or dimension_code)
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

            _fill_and_submit(
                page, route_code, start_date, adults, children, transport_code, dimension_code
            )

            # Get all 7 date labels shown in the carousel
            week_dates = page.evaluate("""(() => {
                return Array.from(
                    document.querySelectorAll('.outward-departure-date[data-date]')
                ).map(function(el) { return el.getAttribute('data-date'); });
            })()""")

            currency = "GBP" if route_code in ("UKIRL", "UKFRA") else "EUR"
            results = {}
            for d in week_dates:
                if not d:
                    continue
                # Click on this date to load its sailings
                _bclick(page, f"""(() => {{
                    var btn = document.querySelector('.outward-departure-date[data-date="{d}"]');
                    if (!btn) return null;
                    var b = btn.getBoundingClientRect();
                    return [b.x + b.width/2, b.y + b.height/2];
                }})()""")
                time.sleep(1.5)

                raw = _parse_sailings_for_date(page, d)
                day_sailings = []
                for s in raw:
                    prices = {k: _parse_price(v) for k, v in s["prices"].items()}
                    best = min((v for v in prices.values() if v is not None), default=None)
                    day_sailings.append({**s, "best_price": best, "currency": currency})
                results[d] = day_sailings

        finally:
            browser.close()

    return results


# ---------------------------------------------------------------------------
# CLI / test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    def show(sailings, date):
        print(f"\n  {date}:")
        if not sailings:
            print("    (no sailings)")
            return
        for s in sailings:
            avail = "✓" if s["available"] else "✗"
            prices = "  ".join(
                f"{k} {s['currency']}{v:.0f}" if v else f"{k} N/A"
                for k, v in {k: _parse_price(p) for k, p in s["prices"].items()}.items()
            )
            print(
                f"    {avail} {s['departure_time']} {s['departure_port']}"
                f" → {s['arrival_time']} {s['arrival_port']}"
                f"  [{s['vessel']}]  {prices}"
            )

    print("=== Dublin → Holyhead, 10 Jul 2026, 1 adult, foot ===")
    sailings = get_sailings("2026-07-10", "IRLUK", adults=1, transport="foot")
    show(sailings, "2026-07-10")

    print("\n=== Holyhead → Dublin, 15 Jul 2026, 1 adult, foot ===")
    sailings = get_sailings("2026-07-15", "UKIRL", adults=1, transport="foot")
    show(sailings, "2026-07-15")
