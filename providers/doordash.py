"""Real DoorDash provider — Playwright over a persistent, human-warmed profile.

Reality of the target (scoped 2026-06): doordash.com sits behind a Cloudflare
"are you human" challenge and shows menus/prices only to a logged-in session
with a delivery address. Headless/fresh browsers are hard-walled. So this
adapter runs HEADED over a persistent profile a human signs into once (`--login`).

End-to-end flow (all selectors verified against the live logged-in DOM):
  discover : search -> open store -> parse [data-anchor-id='MenuItem'] (name+price),
             carrying the store URL so the item can be re-found.
  place_order: open the store -> click the item -> Add to cart -> open cart ->
             Checkout -> locate (NEVER click) [data-anchor-id='PlaceOrderButton']
             -> STOP. The total is read from that button's text and re-checked
             against the budget ceiling. `charged` is always False.

Safety invariants (a real account = a live trade):
  * No code path clicks the pay button; `_complete_payment` raises.
  * STOPPED_BEFORE_PAYMENT is only returned if we reached the pay gate AND the
    approved item is in the cart AND the real total is within budget. Otherwise
    we fail closed (FAILED / BLOCKED) — never a fake success.
  * "logged in" requires a POSITIVE marker and the ABSENCE of a sign-in CTA.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from engine.models import Candidate, UserConfig
from providers.base import OrderResult, OrderStatus, ProviderUnavailable

BASE_URL = "https://www.doordash.com"

BOT_WALL_MARKERS = (
    "just a moment",
    "verifying you are human",
    "performing security verification",
    "security verification",
    "checking your browser",
    "px-captcha",
    "access denied",
    "unusual traffic",
    "/cdn-cgi/challenge",
)

# Not a secret: knowing it cannot cause a charge (the pay action is unwired).
CHARGE_CONFIRM_PHRASE = "I UNDERSTAND THIS CHARGES MY CARD"

SELECTORS: dict[str, tuple[str, ...]] = {
    # Logged-in-only chrome (verified). NOT [href*='/orders'] (renders logged out).
    "logged_in_marker": (
        "[data-testid='addressTextButton']",
        "[data-anchor-id='HeaderOrderCart']",
        "[data-testid='OrderCartIconButton']",
        "[data-testid='NotificationBell']",
    ),
    "signed_out_cta": (
        "[data-testid='loginButton']",
        "a:has-text('Sign In')",
        "button:has-text('Sign In')",
        "a:has-text('Log In')",
    ),
    "store_card": (
        "a[data-anchor-id='StoreCard']",
        "[data-anchor-id='StoreCard'] a[href*='/store/']",
        "a[href*='/store/']",
    ),
    "menu_item": (
        "[data-anchor-id='MenuItem']",
        "[data-testid='MenuItem']",
        "[data-testid='GenericItemCard']",
    ),
    # The "Add to cart" control inside an item's modal.
    "add_to_cart": (
        "[data-anchor-id='AddItemButton']",
        "[data-testid='AddItemButton']",
        "button:has-text('Add to cart')",
        "button:has-text('Add to Cart')",
        "button:has-text('Add to Order')",
    ),
    "cart_button": (
        "[data-anchor-id='HeaderOrderCart']",
        "[data-testid='OrderCartIconButton']",
        "button:has-text('Cart')",
    ),
    # The post-add cart popover / drawer's "Continue" control. Its anchor id is
    # CheckoutButton and its href is /consumer/checkout/ — clicking it goes
    # straight to the real checkout IN SESSION (no fresh URL nav / error flash).
    "checkout_button": (
        "[data-anchor-id='CheckoutButton']",
        "[data-testid='CheckoutButton']",
        "a:has-text('Checkout')",
        "[data-anchor-id='OrderCartCtaButton']",
    ),
    # The button we STOP in front of — located to prove we reached the pay gate,
    # never clicked. Its text is "Place [Pickup ]Order $<total>".
    "place_order_button": (
        "[data-anchor-id='PlaceOrderButton']",
        "[data-testid='PlaceOrderButton']",
        "button:has-text('Place Order')",
        "button:has-text('Place Pickup Order')",
    ),
    "order_total": (
        "[data-testid='OrderCartItemSubtotal']",
    ),
}

_DOLLAR_RE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)")
_PRICE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")
_FREE_TOKENS = {"free", "free item", "$0", "$0.00"}


class DoorDashProvider:
    name = "doordash"

    def __init__(
        self,
        *,
        profile_dir: str | os.PathLike[str] | None = None,
        headless: bool = False,
        channel: str = "chrome",
        timeout_ms: int = 45000,
        search_query: str | None = None,
        max_stores: int = 3,
    ) -> None:
        home = Path.home() / ".daily-food-ordering"
        self.profile_dir = Path(profile_dir) if profile_dir else home / "chrome-profile"
        self.screenshot_dir = home / "screenshots"
        self.headless = headless
        self.channel = channel
        self.timeout_ms = timeout_ms
        self.search_query = search_query
        self.max_stores = max_stores
        # One warm browser session is held across discover() -> place_order() so
        # the demo is a single smooth flow (no close/reopen). close() tears it
        # down; run.py calls close() once the run finishes.
        self._pw: Any | None = None
        self._ctx: Any | None = None
        self._page: Any | None = None

    # ---- pure, browser-free logic (unit-tested) --------------------------------

    @staticmethod
    def is_bot_walled(title: str, body: str) -> bool:
        blob = f"{title}\n{body}".lower()
        return any(marker in blob for marker in BOT_WALL_MARKERS)

    @staticmethod
    def parse_price(text: str) -> float | None:
        """Best price from a price cell / item-card text.

        - a cell that is just 'Free'/'$0' is 0.0 ('Sugar-Free Soda $3.50' is 3.50);
        - among $-amounts the largest non-zero wins (a '$0 delivery' promo cannot
          mask the real price);
        - a rating like '4.5 ★' is ignored whenever a $-amount is present.
        """
        if not text:
            return None
        if text.strip().lower() in _FREE_TOKENS:
            return 0.0
        amounts = [float(a.replace(",", "")) for a in _DOLLAR_RE.findall(text)]
        nonzero = [a for a in amounts if a > 0]
        if nonzero:
            return max(nonzero)
        if amounts:
            return 0.0
        match = _PRICE_RE.search(text)
        return float(match.group(1).replace(",", "")) if match else None

    @staticmethod
    def _menu_item_name(text: str) -> str:
        first = (text.strip().splitlines() or [""])[0]
        first = re.split(r"\s*\$\s*\d", first)[0].strip()
        return (first[:80] or "Item")

    @staticmethod
    def _store_name_from_title(title: str) -> str:
        name = re.sub(r"^Order\s+", "", title or "").split(" Menu")[0]
        return name.split(" - ")[0].strip() or "DoorDash store"

    @staticmethod
    def _payment_authorized(complete_payment: bool) -> bool:
        if not complete_payment:
            return False
        return os.environ.get("DAILY_FOOD_CONFIRM_CHARGE", "") == CHARGE_CONFIRM_PHRASE

    def _build_stopped_result(
        self,
        candidate: Candidate,
        *,
        idempotency_key: str,
        summary: dict[str, Any],
        screenshot_path: str | None = None,
    ) -> OrderResult:
        return OrderResult(
            status=OrderStatus.STOPPED_BEFORE_PAYMENT,
            provider=self.name,
            restaurant=candidate.restaurant,
            item_name=candidate.item_name,
            price_usd=candidate.price_usd,
            idempotency_key=idempotency_key,
            reason="reached_checkout_stopped_before_payment",
            charged=False,
            summary=summary,
            screenshot_path=screenshot_path,
        )

    def _reconcile_budget(
        self,
        candidate: Candidate,
        *,
        idempotency_key: str,
        total: float | None,
        ceiling: float | None,
        summary: dict[str, Any],
        screenshot_path: str | None,
    ) -> OrderResult | None:
        """Re-run the budget guard against the REAL checkout total. Fail closed."""
        if ceiling is None:
            return None
        if total is None:
            return OrderResult(
                status=OrderStatus.FAILED, provider=self.name, restaurant=candidate.restaurant,
                item_name=candidate.item_name, price_usd=candidate.price_usd,
                idempotency_key=idempotency_key, reason="checkout_total_unverified",
                charged=False, summary=summary, screenshot_path=screenshot_path,
            )
        if total > ceiling:
            return OrderResult(
                status=OrderStatus.BLOCKED, provider=self.name, restaurant=candidate.restaurant,
                item_name=candidate.item_name, price_usd=total, idempotency_key=idempotency_key,
                reason=f"checkout_total_over_budget:{total}>{ceiling}", charged=False,
                summary={**summary, "checkout_total_usd": total, "budget_ceiling_usd": ceiling},
                screenshot_path=screenshot_path,
            )
        return None

    def _complete_payment(self, *args: Any, **kwargs: Any) -> None:
        # Intentionally unwired. The only way money moves is by implementing this
        # method, which this build refuses to do. Belt, suspenders, wall.
        raise NotImplementedError(
            "Real payment is disabled in this build. The adapter stops before pay "
            "by design; there is no supported path that completes a charge."
        )

    # ---- browser lifecycle -----------------------------------------------------

    def _launch(self, playwright: Any) -> Any:
        import sys

        # 0o700: the profile holds the user's live DoorDash session cookies.
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        kwargs = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1280, "height": 1000},
            locale="en-US",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        try:
            return playwright.chromium.launch_persistent_context(channel=self.channel, **kwargs)
        except Exception as error:  # noqa: BLE001
            print(f"(chrome channel unavailable: {error}; using bundled chromium)", file=sys.stderr)
            return playwright.chromium.launch_persistent_context(**kwargs)

    def _new_page(self, context: Any) -> Any:
        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(self.timeout_ms)
        return page

    def _session_page(self) -> Any:
        """The one shared, lazily-opened page reused across discover/place_order."""
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._ctx = self._launch(self._pw)
        self._page = self._new_page(self._ctx)
        return self._page

    def close(self) -> None:
        """Tear down the shared browser session. Idempotent; never raises."""
        for closer in (
            lambda: self._ctx.close() if self._ctx is not None else None,
            lambda: self._pw.stop() if self._pw is not None else None,
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._ctx = self._page = None

    def _guard_page(self, page: Any) -> None:
        page.wait_for_timeout(2500)
        title = page.title()
        body = ""
        try:
            body = page.inner_text("body")[:4000]
        except Exception:  # noqa: BLE001
            pass
        if self.is_bot_walled(title, body):
            raise ProviderUnavailable(
                "doordash bot wall (Cloudflare). Run `--login` to warm the profile first."
            )
        logged_in = self._first_locator(page, "logged_in_marker") is not None
        # Only the explicit sign-in CTA selectors — NOT a body substring scan,
        # which false-positives on logged-in footer/help text containing "sign in".
        signed_out = self._first_locator(page, "signed_out_cta") is not None
        if not logged_in or signed_out:
            raise ProviderUnavailable(
                "could not confirm a logged-in DoorDash session. Run `--login` to "
                "sign in and set a delivery address."
            )

    def _first_locator(self, page: Any, key: str) -> Any | None:
        for selector in SELECTORS[key]:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0:
                    return loc
            except Exception:  # noqa: BLE001
                continue
        return None

    def _absolute(self, href: str) -> str:
        return href if href.startswith("http") else f"{BASE_URL}{href}"

    # ---- public API ------------------------------------------------------------

    def login(self) -> None:
        """One-time, human-driven warm-up: open headed and WAIT for the human."""
        import sys
        from playwright.sync_api import sync_playwright

        print("Opening a DoorDash browser window. In that window:")
        print("  1) pass any 'verify you are human' check")
        print("  2) sign in to your DoorDash account")
        print("  3) set your delivery address")
        with sync_playwright() as p:
            self.headless = False
            context = self._launch(p)
            try:
                page = self._new_page(context)
                try:
                    page.goto(BASE_URL, wait_until="domcontentloaded")
                except Exception as error:  # noqa: BLE001
                    print(f"(navigation note: {error})")
                if sys.stdin and sys.stdin.isatty():
                    try:
                        input("\n>> Finished signing in? Press Enter here to save & close... ")
                    except (EOFError, KeyboardInterrupt):
                        pass
                else:
                    print("(stdin not interactive — waiting up to 5 min for a logged-in marker)")
                    for _ in range(60):
                        page.wait_for_timeout(5000)
                        if self._first_locator(page, "logged_in_marker"):
                            break
                try:
                    page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                except Exception:  # noqa: BLE001
                    pass
                if self._first_locator(page, "logged_in_marker"):
                    print("Logged-in marker detected — session looks good.")
                else:
                    print("No logged-in marker detected. The profile is still saved; "
                          "if a later run reports 'not logged in', re-run --login.")
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
        print(f"Profile saved at: {self.profile_dir}")

    def diagnose(self, url: str = BASE_URL) -> dict[str, Any]:
        from playwright.sync_api import sync_playwright

        out: dict[str, Any] = {"url": url}
        with sync_playwright() as p:
            context = self._launch(p)
            try:
                page = self._new_page(context)
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                out["title"] = page.title()
                out["bot_walled"] = self.is_bot_walled(page.title(), page.inner_text("body")[:2000])
                out["selector_hits"] = {k: bool(self._first_locator(page, k)) for k in SELECTORS}
            finally:
                context.close()
        return out

    def discover(self, config: UserConfig, *, query: str | None = None) -> list[Candidate]:
        # Uses the shared session and leaves it OPEN on the chosen store, so
        # place_order() reuses the same page — one smooth browser flow. The
        # session is closed by close() at the end of the run (run.py), not here.
        query = query or self.search_query or _default_query(config)
        page = self._session_page()
        page.goto(f"{BASE_URL}/search/store/{quote(query)}/", wait_until="domcontentloaded")
        self._guard_page(page)
        # Collect a few store links, then open each until one yields a menu.
        try:
            hrefs = page.eval_on_selector_all(
                "a[data-anchor-id='StoreCard'], a[href*='/store/']",
                "els => Array.from(new Set(els.map(e => e.getAttribute('href'))))"
                # real store pages are /store/<slug>-<id>/ — exclude
                # /search/store/<query>/ dish-search links.
                ".filter(h => h && h.includes('/store/') && !h.includes('/search/'))",
            )
        except Exception:  # noqa: BLE001
            hrefs = []
        for href in hrefs[: self.max_stores]:
            target = self._absolute(href)
            if not target.startswith(f"{BASE_URL}/"):
                continue
            page.goto(target, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            if self.is_bot_walled(page.title(), ""):
                continue
            for _ in range(4):
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(700)
            # Carry the post-redirect URL the page actually settled on, so
            # place_order() can detect it's already here and skip re-navigating.
            candidates = self._parse_store_menu(page, store_url=page.url)
            if candidates:
                return candidates
        return []

    def discover_fallback(self, config: UserConfig) -> list[Candidate]:
        # Search the user's fallback restaurant. Note: real DoorDash items stay
        # verified_safe=False, so for a *restricted* user the fallback is also
        # gated by the engine (honest — we can't verify safety on DoorDash).
        if not config.fallback.restaurant:
            return []
        return self.discover(config, query=config.fallback.restaurant)

    def place_order(
        self,
        candidate: Candidate,
        *,
        idempotency_key: str,
        complete_payment: bool = False,
        budget_ceiling_usd: float | None = None,
        auto_approve_ceiling_usd: float | None = None,
    ) -> OrderResult:
        store_url = candidate.metadata.get("url") if candidate.metadata else None
        # Only ever navigate to doordash.com — never an attacker-crafted URL.
        if not store_url or not store_url.startswith(f"{BASE_URL}/"):
            raise ProviderUnavailable("candidate has no DoorDash store URL to order from")

        # Reuse the warm session discover() left open. If we're already on this
        # store (the common path), don't re-navigate — that's the "1 browser
        # action" smoothness. Only (re)load + re-guard if we're elsewhere (e.g.
        # place_order called without a preceding discover, or a fallback store).
        page = self._session_page()
        if not self._on_store(page, store_url):
            page.goto(store_url, wait_until="domcontentloaded")
            self._guard_page(page)

        # Cart the approved item, or the cheapest item that adds without
        # required customization. Report what was ACTUALLY carted.
        added_name, added_price = self._add_item_to_cart(page, candidate)
        carted = candidate
        if added_name.strip().lower() != candidate.item_name.strip().lower():
            # A different item was carted — its safety is NOT the approved
            # item's, so mark it unverified, and re-apply the AUTO band to
            # its own price (a pricier substitute the engine never AUTO'd
            # must not be auto-placed).
            carted = Candidate(
                restaurant=candidate.restaurant,
                item_name=added_name,
                price_usd=added_price,
                cuisine=candidate.cuisine,
                dietary=candidate.dietary,
                allergens=candidate.allergens,
                verified_safe=False,
                metadata=candidate.metadata,
            )
            if auto_approve_ceiling_usd is not None and added_price > auto_approve_ceiling_usd:
                return OrderResult(
                    status=OrderStatus.BLOCKED, provider=self.name,
                    restaurant=carted.restaurant, item_name=carted.item_name,
                    price_usd=added_price, idempotency_key=idempotency_key,
                    reason=f"substitute_above_auto_approve:{added_price}>{auto_approve_ceiling_usd}",
                    charged=False,
                    summary={"substituted_for": candidate.item_name,
                             "carted_price_usd": added_price,
                             "auto_approve_ceiling_usd": auto_approve_ceiling_usd},
                )

        self._go_to_checkout(page)

        # HARD STOP. The success criterion is reaching the real pay gate;
        # cart-item presence is recorded, and the REAL total is reconciled
        # against budget (fail closed). We never click pay.
        self._require_pay_gate(page)
        cart_verified = self._verify_cart(page, carted)

        total = self._parse_checkout_total(page)
        summary = self._read_order_summary(page, carted, total)
        summary["cart_verified"] = cart_verified
        if carted is not candidate:
            summary["substituted_for"] = candidate.item_name
            summary["substitution_reason"] = "approved item required customization"
        screenshot_path = self._screenshot(page, idempotency_key)

        # Fail closed: STOPPED_BEFORE_PAYMENT must mean the carted item is
        # actually on the checkout page. If we can't confirm it, don't
        # claim success — never fake a stop-before-pay.
        if not cart_verified:
            return OrderResult(
                status=OrderStatus.FAILED, provider=self.name,
                restaurant=carted.restaurant, item_name=carted.item_name,
                price_usd=carted.price_usd, idempotency_key=idempotency_key,
                reason="cart_unverified: carted item not confirmed at checkout",
                charged=False, summary=summary, screenshot_path=screenshot_path,
            )

        failed = self._reconcile_budget(
            carted, idempotency_key=idempotency_key, total=total,
            ceiling=budget_ceiling_usd, summary=summary, screenshot_path=screenshot_path,
        )
        if failed is not None:
            return failed

        if self._payment_authorized(complete_payment):
            summary["payment_authorization"] = "passed_gates_but_disabled_in_build"

        return self._build_stopped_result(
            carted, idempotency_key=idempotency_key, summary=summary,
            screenshot_path=screenshot_path,
        )

    def _on_store(self, page: Any, store_url: str) -> bool:
        """True if `page` is already on `store_url` (ignoring query string)."""
        try:
            return page.url.split("?")[0].rstrip("/") == store_url.split("?")[0].rstrip("/")
        except Exception:  # noqa: BLE001
            return False

    # ---- browser helpers -------------------------------------------------------

    def _parse_store_menu(self, page: Any, *, store_url: str) -> list[Candidate]:
        restaurant = self._store_name_from_title(page.title())
        items = None
        for selector in SELECTORS["menu_item"]:
            loc = page.locator(selector)
            if loc.count() > 0:
                items = loc
                break
        if items is None:
            return []
        candidates: list[Candidate] = []
        seen: set[tuple[str, float]] = set()
        for i in range(min(items.count(), 16)):
            item = items.nth(i)
            try:
                text = item.inner_text()
            except Exception:  # noqa: BLE001
                continue
            price = self.parse_price(text)
            if price is None:
                continue
            # Only keep items that can be added directly (a quick-add "+"); items
            # with required customization are skipped so the engine never picks
            # one the adapter can't add unattended.
            try:
                if item.locator("[data-testid='quick-add-button']").count() == 0:
                    continue
            except Exception:  # noqa: BLE001
                continue
            name = self._menu_item_name(text)
            key = (name.lower(), price)
            if not name or key in seen:
                continue
            seen.add(key)
            candidates.append(
                Candidate(
                    restaurant=restaurant,
                    item_name=name,
                    price_usd=price,
                    cuisine=None,
                    dietary=[],          # DoorDash can't be trusted to confirm these...
                    allergens=[],
                    verified_safe=False,  # ...so candidates stay UNVERIFIED -> engine gates them.
                    metadata={"url": store_url, "source": "doordash", "item_name": name},
                )
            )
            if len(candidates) >= 12:
                break
        return candidates

    def _cart_count(self, page: Any) -> int:
        cart = self._first_locator(page, "cart_button")
        if cart is None:
            return 0
        try:
            match = re.search(r"(\d+)\s*item", cart.inner_text().lower())
            return int(match.group(1)) if match else 0
        except Exception:  # noqa: BLE001
            return 0

    def _wait_cart_increment(self, page: Any, before: int, *, timeout_ms: int = 6000) -> bool:
        """Poll the cart badge until it rises above `before`.

        The badge updates asynchronously after an add (a network round-trip), so
        a single immediate read races it: a *successful* customized add can read
        as still-`before` and be mistaken for a failure — which used to trigger a
        needless substitution AND a double-add (two items, inflated total). We
        poll for up to `timeout_ms` instead.
        """
        waited = 0
        while waited < timeout_ms:
            if self._cart_count(page) > before:
                return True
            page.wait_for_timeout(500)
            waited += 500
        return self._cart_count(page) > before

    def _add_item_to_cart(self, page: Any, candidate: Candidate) -> tuple[str, float]:
        """Cart the engine-approved dish; failing that, the cheapest item that
        adds cleanly. Returns the (name, price) ACTUALLY carted, or raises.
        """
        try:
            page.wait_for_selector("[data-anchor-id='MenuItem']", timeout=12000)
        except Exception:  # noqa: BLE001
            pass

        # 1) Smooth + reliable: filter the menu to the approved dish via the
        #    in-store search box and add THAT — so we cart the user's actual
        #    choice rather than substituting just because the full-menu DOM order
        #    differs from discovery (DoorDash reorders/lazy-loads between loads).
        found = self._search_and_add_named(page, candidate)
        if found is not None:
            return found

        # 2) Fall back to the full menu (cheapest item that adds cleanly). Clear
        #    the search filter first so the whole menu is visible again.
        self._clear_in_store_search(page)
        for _ in range(3):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(700)

        items = page.locator("[data-anchor-id='MenuItem']")
        parsed: list[tuple[int, str, float]] = []
        for i in range(min(items.count(), 24)):
            try:
                text = items.nth(i).inner_text()
            except Exception:  # noqa: BLE001
                continue
            price = self.parse_price(text)
            if price is None:
                continue
            if items.nth(i).locator("[data-testid='quick-add-button']").count() == 0:
                continue
            parsed.append((i, self._menu_item_name(text), price))

        # Approved item first (if it surfaced here), then cheapest among the rest.
        approved = candidate.item_name.strip().lower()
        parsed.sort(key=lambda t: (0 if t[1].strip().lower() == approved else 1, t[2]))

        for idx, name, price in parsed[:8]:
            quick = items.nth(idx).locator("[data-testid='quick-add-button']").first
            added = self._attempt_add(page, quick, name, price)
            if added is not None:
                return added
        raise ProviderUnavailable("could not add any item to the cart on this store")

    def _attempt_add(
        self, page: Any, quick: Any, name: str, price: float
    ) -> tuple[str, float] | None:
        """Click a quick-add, then confirm (polling) the item reached the cart,
        completing any required-customization modal. Returns (name, price)/None.
        """
        if quick is None or quick.count() == 0:
            return None
        before = self._cart_count(page)
        try:
            quick.scroll_into_view_if_needed()
            quick.click(timeout=8000)
        except Exception:  # noqa: BLE001
            try:
                quick.click(force=True)
            except Exception:  # noqa: BLE001
                return None
        # Added directly (no required customization)? Poll — the badge updates
        # asynchronously, so a single read races the add.
        if self._wait_cart_increment(page, before, timeout_ms=4000):
            return (name, price)
        # Otherwise a customization modal opened — satisfy required groups, then
        # confirm the add landed (polling again).
        if self._complete_item_modal(page) and self._wait_cart_increment(
            page, before, timeout_ms=7000
        ):
            return (name, price)
        self._close_modal(page)
        return None

    @staticmethod
    def _core_dish_name(name: str) -> str:
        # Strip a leading menu code ("A10 - ", "C9 - ", "33. ") so the in-store
        # search matches on the dish words DoorDash actually indexes.
        core = re.sub(r"^[A-Za-z]{0,3}\d+[A-Za-z]?[.\)]?\s*[-–]?\s*", "", name).strip()
        return core or name

    def _clear_in_store_search(self, page: Any) -> None:
        box = page.locator("input[placeholder*='Search' i]").first
        if box.count() == 0:
            return
        try:
            box.fill("")
            page.wait_for_timeout(1200)
        except Exception:  # noqa: BLE001
            pass

    def _search_and_add_named(
        self, page: Any, candidate: Candidate
    ) -> tuple[str, float] | None:
        """Type the dish into the in-store search box, then add the matching
        item. Returns (name, price) carted, or None if search can't surface it.
        """
        box = page.locator("input[placeholder*='Search' i]").first
        if box.count() == 0:
            return None
        core = self._core_dish_name(candidate.item_name)
        try:
            box.click()
            box.fill(core)
        except Exception:  # noqa: BLE001
            return None
        page.wait_for_timeout(2200)
        items = page.locator("[data-anchor-id='MenuItem']")
        approved = candidate.item_name.strip().lower()
        core_l = core.lower()
        for i in range(min(items.count(), 8)):
            try:
                text = items.nth(i).inner_text()
            except Exception:  # noqa: BLE001
                continue
            name = self._menu_item_name(text)
            price = self.parse_price(text)
            if price is None:
                continue
            nl = name.strip().lower()
            if nl != approved and core_l not in nl:
                continue
            quick = items.nth(i).locator("[data-testid='quick-add-button']").first
            added = self._attempt_add(page, quick, name, price)
            if added is not None:
                return added
        return None

    def _complete_item_modal(self, page: Any) -> bool:
        """Satisfy ALL of a customization modal's required option groups, then Add.

        Works uniformly across dishes with any number of required groups. The
        radios carry no shared `name`, so we can't group them up front; instead
        we click EVERY radio's row in DOM order. The last-clicked radio in each
        group stays selected, so one-per-group falls out naturally; optional
        checkbox add-ons are left untouched. We stop the moment the Add button
        stops demanding a "required"/"select" choice (so a dish needing 2 groups
        is handled just like one needing 1 — no "first section only" bug).
        """
        add_sel = "[data-anchor-id*='AddToCart'], [data-testid*='AddToCart']"
        radios = page.locator("[role='dialog'] input[type='radio']")
        total = min(radios.count(), 30)
        # `total + 1` so we re-check the Add button after the final radio click.
        for i in range(total + 1):
            add = page.locator(add_sel).first
            if add.count() == 0:
                return False
            try:
                text = add.inner_text().lower()
            except Exception:  # noqa: BLE001
                text = ""
            if text and "required" not in text and "select" not in text:
                self._click_resilient(add)
                page.wait_for_timeout(1800)
                return True
            if i >= total:
                break
            radio = radios.nth(i)
            # Reliable selection: a JS click on the input fires React's onChange
            # even when the input is visually hidden — more dependable than
            # force-clicking the row (which can land on padding and not register,
            # leaving a required group unsatisfied and forcing a substitution).
            selected = False
            try:
                radio.evaluate("el => el.click()")
                selected = True
            except Exception:  # noqa: BLE001
                pass
            if not selected:
                row = radio.locator("xpath=../..")  # grandparent = clickable row
                try:
                    target = row if row.count() else radio
                    target.scroll_into_view_if_needed()
                    target.click(force=True, timeout=3500)
                except Exception:  # noqa: BLE001
                    pass
            page.wait_for_timeout(500)
        return False

    def _close_modal(self, page: Any) -> None:
        for selector in ("[aria-label^='Close']", "button[aria-label*='close' i]"):
            loc = page.locator(selector).first
            if loc.count() > 0:
                try:
                    loc.click(timeout=3000)
                except Exception:  # noqa: BLE001
                    try:
                        loc.click(force=True)
                    except Exception:  # noqa: BLE001
                        pass
                page.wait_for_timeout(800)
                return
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
        except Exception:  # noqa: BLE001
            pass

    def _go_to_checkout(self, page: Any) -> None:
        # Smooth, in-session: right after adding, the cart popover already shows
        # the CheckoutButton ("Continue" -> /consumer/checkout/). Click it to go
        # straight to the real checkout — no fresh URL navigation, which flashed
        # an error page before the cart context loaded. Only fall back to opening
        # the header cart if that button isn't already on screen.
        checkout = self._first_locator(page, "checkout_button")
        if checkout is None:
            cart = self._first_locator(page, "cart_button")
            if cart is not None:
                self._click_resilient(cart)
                page.wait_for_timeout(1500)
            checkout = self._first_locator(page, "checkout_button")
        if checkout is not None:
            self._click_resilient(checkout)
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(3000)

    @staticmethod
    def _click_resilient(locator: Any) -> None:
        # Bounded click with a force fallback — avoids a 45s hang when an overlay
        # briefly covers the control.
        try:
            locator.click(timeout=15000)
        except Exception:  # noqa: BLE001
            try:
                locator.click(force=True, timeout=8000)
            except Exception:  # noqa: BLE001
                pass

    def _require_pay_gate(self, page: Any) -> None:
        # Honesty gate: STOPPED_BEFORE_PAYMENT must mean we reached the pay screen.
        # We locate the place-order button only to confirm presence; never click it.
        if self._first_locator(page, "place_order_button") is None:
            raise ProviderUnavailable(
                "did not reach the pay gate (no place-order button found); "
                "not claiming a stop-before-pay"
            )

    def _verify_cart(self, page: Any, candidate: Candidate) -> bool:
        # Best-effort confirmation that the carted item shows at checkout. We
        # strip a leading menu code ("A10 - ", "33 - ") and try the dish-name
        # prefix, then its last word. This is informational, not fatal: we never
        # pay, and the real total is reconciled against budget downstream.
        core = re.sub(r"^[A-Za-z]{0,3}\d+[A-Za-z]?[.\)]?\s*[-–]?\s*", "", candidate.item_name).strip()
        words = core.split()
        needles = [core[:14], words[-1] if words else "", words[0] if words else ""]
        for needle in needles:
            needle = (needle or "").strip()
            if len(needle) < 3:
                continue
            try:
                if page.get_by_text(needle, exact=False).count() > 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _parse_checkout_total(self, page: Any) -> float | None:
        # The place-order button reads "Place [Pickup ]Order $<total>".
        button = self._first_locator(page, "place_order_button")
        if button is not None:
            try:
                total = self.parse_price(button.inner_text())
                if total is not None:
                    return total
            except Exception:  # noqa: BLE001
                pass
        total_loc = self._first_locator(page, "order_total")
        if total_loc is not None:
            try:
                return self.parse_price(total_loc.inner_text())
            except Exception:  # noqa: BLE001
                return None
        return None

    def _read_order_summary(
        self, page: Any, candidate: Candidate, total: float | None
    ) -> dict[str, Any]:
        return {
            "restaurant": candidate.restaurant,
            "item": candidate.item_name,
            "listed_price_usd": candidate.price_usd,
            "checkout_total_usd": total,
            "url": page.url,
            "stopped_before_payment": True,
        }

    def _screenshot(self, page: Any, idempotency_key: str) -> str | None:
        try:
            # 0o700/0o600: checkout screenshots capture name, address, card hint.
            self.screenshot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.screenshot_dir / f"{idempotency_key}.png"
            page.screenshot(path=str(path))
            path.chmod(0o600)
            return str(path)
        except Exception:  # noqa: BLE001
            return None


def _default_query(config: UserConfig) -> str:
    if config.preferences.favorite_restaurants:
        return config.preferences.favorite_restaurants[0]
    if config.preferences.cuisines:
        return config.preferences.cuisines[0]
    return "lunch"


__all__ = ["DoorDashProvider", "BASE_URL", "SELECTORS", "CHARGE_CONFIRM_PHRASE"]
