#!/usr/bin/env python3
"""Browser fallback for joining a Discord server through an invite link.

The API path (scripts/burner_prime.py) accepts a plain invite but cannot pass
the gates Discord renders client-side: rules / membership screening, hCaptcha
and phone verification. This is the browser path for those cases, and its whole
job is to say which gate it hit: `joined` is returned only when the browser
really landed on /channels/, never on a hopeful selector.

    uv run python scripts/burner_join_browser.py --self-test        # no browser
    uv run python scripts/burner_join_browser.py --invite https://discord.gg/abc \
        --profile .burner --screenshot-dir /tmp/shots

Cookies, tokens and storage state are never logged or printed: stdout carries
only the invite, the result, the guild name and the screenshot path. Nothing is
exported here; a caller wanting the session must write it itself, mode 0600.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any

# Selector map, one entry per recognisable thing on the page. "guild_name" is
# keyed to the heading and to the post-join URL rather than a widget class:
# Discord hashes its class names, they churn, and a stale class is exactly how a
# fallback ends up claiming a join that never happened.
SELECTORS: dict[str, list[str]] = {
    "join_button": ["button:has-text('Accept Invite')", "button:has-text('Join')"],
    "rules_accept": [
        "button:has-text('I have read and agree')",
        "button:has-text('Submit')",
        "button:has-text('Get started')",
    ],
    "captcha": [
        "iframe[src*='hcaptcha']",
        "text=Verify you are human",
        "iframe[title*='challenge']",
    ],
    "invalid": ["text=Invite Invalid", "text=Invite Expired", "text=invalid invite"],
    "already_member": ["text=You are already a member", "text=already a member"],
    "phone": ["text=phone number", "text=Verify by phone", "text=Add a phone number"],
    "guild_name": ["[class*='guildName']", "h1", "[role='heading']"],
    "spinner": ["[class*='spinner']", "svg[class*='spinner']", "text=Wumpus"],
    "error_banner": ["text=Something went wrong", "text=Banned from this server"],
}

RESULTS = (
    "joined",
    "already_member",
    "invite_invalid",
    "captcha_required",
    "phone_required",
    "rules_gate",
    "timeout",
    "error",
)
# Gate selector key -> result it proves, polled in this order. "joined" needs
# the URL and "timeout" the clock on top of their selector, so both are checked
# outside this table; self_test() proves the three sources together cover all of
# RESULTS, which is what keeps the names here honest.
SIGNAL_TO_RESULT = {
    "invalid": "invite_invalid",
    "already_member": "already_member",
    "captcha": "captcha_required",
    "phone": "phone_required",
    "error_banner": "error",
    "rules_accept": "rules_gate",
}
RETURN_KEYS = ("invite", "result", "detail", "guild", "screenshot")
FATAL = ("timeout", "error")  # the only results that exit non-zero
POLL_MS = 400


def _visible(page: Any, selector: str) -> bool:
    try:
        element = page.query_selector(selector)
        return bool(element and element.is_visible())
    except Exception:  # a detached page must not swallow a gate
        return False


def _seen(page: Any, key: str) -> bool:
    return any(_visible(page, selector) for selector in SELECTORS[key])


def _guild_name(page: Any) -> str | None:
    """Best-effort guild name from the invite card or the joined header."""
    for selector in SELECTORS["guild_name"]:
        try:
            element = page.query_selector(selector)
            text = " ".join((element.inner_text() if element else "").split())
        except Exception:
            continue
        if 0 < len(text) <= 100:
            return text
    return None


def _joined(page: Any) -> bool:
    """Success is the post-join URL, nothing softer."""
    return "/channels/" in (page.url or "")


def join(
    invite_url: str,
    profile_dir: str | None = None,
    headless: bool = True,
    timeout_s: int = 90,
    screenshot_dir: str | None = None,
) -> dict[str, Any]:
    """Join ``invite_url`` in a browser and report the outcome.

    Returns {"invite", "result", "detail", "guild", "screenshot"} and prints it
    as exactly one JSON line; ``result`` is one of RESULTS.
    """
    out: dict[str, Any] = dict.fromkeys(RETURN_KEYS)
    out["invite"], out["result"], out["detail"] = invite_url, "error", ""
    deadline = time.monotonic() + max(1, int(timeout_s))
    clicked = False

    def finish(result: str, detail: str, page: Any | None = None) -> dict[str, Any]:
        """Record and emit one outcome line; screenshot it when asked."""
        out["result"], out["detail"] = result, detail
        if page is not None and screenshot_dir:
            try:
                target = Path(screenshot_dir)
                target.mkdir(parents=True, exist_ok=True)
                path = target / f"join-{result}.png"
                page.screenshot(path=str(path), timeout=15_000)
                out["screenshot"] = str(path)
            except Exception:  # a missing screenshot is not fatal
                pass
        print(json.dumps(out), flush=True)
        return out

    try:
        # Lazy, and inside the guard: --self-test stays browserless, and a
        # missing playwright is reported as an error line rather than a traceback.
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            if profile_dir:
                context = pw.chromium.launch_persistent_context(profile_dir, headless=headless)
            else:
                context = pw.chromium.launch(headless=headless).new_context()
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(15_000)
                try:
                    page.goto(
                        invite_url,
                        wait_until="domcontentloaded",
                        timeout=max(1, int(timeout_s)) * 1_000,
                    )
                except PWTimeout:
                    return finish("timeout", "invite page never loaded", page)
                # Bounded poll: every wait inside shares the deadline, so a gate
                # that never resolves ends in a timeout instead of hanging.
                while time.monotonic() < deadline:
                    out["guild"] = _guild_name(page) or out["guild"]
                    for key, result in SIGNAL_TO_RESULT.items():
                        if _seen(page, key):
                            return finish(result, f"{key} prompt", page)
                    if _joined(page):
                        return finish("joined", "landed on /channels/", page)
                    if not clicked and _seen(page, "join_button"):
                        clicked = True  # once only, even if the click throws
                        try:
                            page.click(SELECTORS["join_button"][0], timeout=10_000)
                        except Exception:
                            pass
                    page.wait_for_timeout(POLL_MS)
                return finish("timeout", f"no outcome in {timeout_s}s", page)
            finally:
                context.close()  # closing the context stops its browser too
    except Exception as exc:  # report, never raise at the caller
        return finish("error", f"{type(exc).__name__}: {exc}")


def self_test() -> int:
    """No browser: every result is recognisable and the contract is well formed."""
    covered = set(SIGNAL_TO_RESULT.values()) | {"joined", "timeout"}
    assert covered == set(RESULTS), f"uncovered results: {set(RESULTS) - covered}"
    for key in (*SIGNAL_TO_RESULT, "guild_name", "spinner"):
        selectors = SELECTORS.get(key) or []
        assert selectors, f"{key} has no selectors"
        assert all(isinstance(s, str) and s for s in selectors), f"{key} has a bad selector"
    sig = inspect.signature(join)
    names = ["invite_url", "profile_dir", "headless", "timeout_s", "screenshot_dir"]
    assert list(sig.parameters) == names, f"join signature drifted: {list(sig.parameters)}"
    assert sig.parameters["headless"].default is True
    assert sig.parameters["timeout_s"].default == 90
    assert "URL" in (_joined.__doc__ or "")  # joined still keyed to the URL
    print(json.dumps(sorted(SELECTORS)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--invite", help="invite URL to join")
    parser.add_argument("--profile", help="persistent browser profile directory")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--timeout", type=int, default=90, help="seconds")
    parser.add_argument("--screenshot-dir", help="where outcome screenshots go")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.invite:
        parser.error("--invite is required unless --self-test is given")
    out = join(
        args.invite,
        profile_dir=args.profile,
        headless=args.headless,
        timeout_s=args.timeout,
        screenshot_dir=args.screenshot_dir,
    )
    return 1 if out["result"] in FATAL else 0


if __name__ == "__main__":
    sys.exit(main())
