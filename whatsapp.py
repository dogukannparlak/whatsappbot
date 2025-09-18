from __future__ import annotations
import logging
import time
import re
from typing import Iterable
from urllib.parse import quote_plus

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains

# Module-level logger
log = logging.getLogger("whatsapp")

# Optional E.164 phone validation (allows optional '+' and 6-15 digits)
_E164_RE = re.compile(r"^\+?\d{6,15}$")


class WhatsAppWebClient:
    """
    WhatsApp Web client helper:
      - open(): navigate to WhatsApp Web
      - wait_until_logged_in(): wait until QR login completes
      - ready(): quick readiness check
      - send_text_to_phone(): send a text message to a single number
      - send_bulk(): send messages to multiple numbers
    """

    def __init__(self, driver, base_url: str = "https://web.whatsapp.com/") -> None:
        # Selenium WebDriver instance is provided externally
        self.driver = driver
        # Normalize base URL to have a single trailing slash
        self.base_url = base_url.rstrip("/") + "/"

    def open(self) -> None:
        """Open WhatsApp Web home page."""
        log.info("Navigating to %s", self.base_url)
        self.driver.get(self.base_url)

    # -------------------- LOGIN / READY --------------------

    def is_logged_in_fast(self) -> bool:
        """
        Quick readiness check.
        If any of the following signals are visible, consider it logged in:
          - Search icon in the header: [data-icon='search-refreshed-thin']
          - Composer / send button / conversation header
        If QR is visible, it's not ready yet.
        """
        # If QR code is visible, login has not been completed
        try:
            if self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='qrcode'], canvas[aria-label*='QR']"):
                return False
        except Exception:
            # DOM can be transiently inaccessible; ignore and continue
            pass

        # Potential selectors indicating readiness
        selectors = [
            "[data-icon='search-refreshed-thin']",
            "footer [contenteditable='true'][role='textbox']",
            "[data-testid='conversation-compose-box-input']",
            "[data-icon='wds-ic-send-filled']",
            "header [data-testid='conversation-info-header']",
            "[data-testid='chat-list-search']",
            "[data-testid='chat-list']",
        ]
        for css in selectors:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, css)
                if el and el.is_displayed():
                    return True
            except Exception:
                continue
        return False

    def wait_until_logged_in(self, timeout_seconds: int = 120) -> bool:
        """
        Wait until the chat UI is ready (i.e., user is logged in).
        - If already logged in, return True immediately.
        - If timeout is exceeded, return False.
        """
        if self.is_logged_in_fast():
            log.info("WhatsApp already logged in (profile cache).")
            return True

        log.info("Waiting for login (scan QR if shown)… up to %ss", timeout_seconds)
        wait = WebDriverWait(self.driver, timeout_seconds, poll_frequency=1.0)
        try:
            wait.until(self._logged_in_condition)
            log.info("Login detected: chat UI ready.")
            return True
        except TimeoutException:
            log.warning("Login NOT completed within timeout.")
            return False

    def _logged_in_condition(self, driver) -> bool:
        """
        Condition function for WebDriverWait.
        - If QR is visible return False
        - Otherwise, return True when certain UI elements are visible
        """
        # Not ready if QR is visible
        try:
            if driver.find_elements(By.CSS_SELECTOR, "[data-testid='qrcode'], canvas[aria-label*='QR']"):
                return False
        except Exception:
            pass

        # Strong signals that UI is ready
        for css in (
            "[data-icon='search-refreshed-thin']",
            "footer [contenteditable='true'][role='textbox']",
            "[data-testid='conversation-compose-box-input']",
            "header [data-testid='conversation-info-header']",
            "[data-testid='chat-list']",
        ):
            try:
                el = driver.find_element(By.CSS_SELECTOR, css)
                if el and el.is_displayed():
                    return True
            except Exception:
                continue
        return False

    def ready(self) -> bool:
        """Return whether the client session appears ready."""
        return self.is_logged_in_fast()

    # -------------------- SEND ACTIONS --------------------

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """
        Normalize phone numbers:
        - Convert leading '00' to '+' (international form)
        - If no '+', strip non-digits
        """
        p = phone.strip().replace(" ", "")
        if p.startswith("00"):
            p = "+" + p[2:]
        if not p.startswith("+"):
            return re.sub(r"\D+", "", p)
        return p

    def _wait_chat_open(self, wait: WebDriverWait) -> None:
        """Ensure the chat UI is really open (header/composer present)."""
        wait.until(
            EC.any_of(
                EC.presence_of_element_located((By.CSS_SELECTOR, "header [data-testid='conversation-info-header']")),
                EC.presence_of_element_located((By.CSS_SELECTOR, "footer [contenteditable='true'][role='textbox']")),
                EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='conversation-compose-box-input']")),
            )
        )

    def _locate_composer(self):
        """
        Try multiple selectors to locate the message composer.
        Different DOM versions may require different selectors.
        """
        selectors = [
            "footer [contenteditable='true'][role='textbox']",
            "[data-testid='conversation-compose-box-input']",
            "div[contenteditable='true'][role='textbox']",
        ]
        for css in selectors:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, css)
                if el.is_displayed():
                    return el
            except Exception:
                continue
        return None

    def _type_via_js_and_dispatch(self, message: str) -> bool:
        """
        Type into the composer via JS and dispatch a React-style input event.
        Sometimes more reliable than send_keys.
        """
        try:
            script = r"""
                (function(msg){
                    let el = document.querySelector("footer [contenteditable='true'][role='textbox']")
                          || document.querySelector("[data-testid='conversation-compose-box-input']")
                          || document.querySelector("div[contenteditable='true'][role='textbox']");
                    if(!el) return false;
                    el.focus();

                    // Clear existing content
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    range.deleteContents();
                    el.innerHTML = "";

                    // Insert new text
                    el.appendChild(document.createTextNode(msg));

                    // Notify React there's an input change
                    const ev = new InputEvent("input", {bubbles: true});
                    el.dispatchEvent(ev);
                    return true;
                })(arguments[0]);
            """
            ok = self.driver.execute_script(script, message)
            return bool(ok)
        except Exception as e:
            log.debug("JS type/dispatch failed: %s", e)
            return False

    def _click_send_button(self, wait: WebDriverWait) -> bool:
        """
        Click the Send button.
        Try multiple selectors to tolerate DOM variations:
          - [data-icon='wds-ic-send-filled'] (new icon)
          - [aria-label='Gönder'] (TR)
          - [aria-label='Send'] (EN)
          - [data-testid='compose-btn-send'] (legacy)
        """
        selectors = [
            "[data-icon='wds-ic-send-filled']",
            "footer [aria-label='Gönder']",
            "footer [aria-label='Send']",
            "[data-testid='compose-btn-send']",
            "footer [data-icon='send']",
        ]
        for css in selectors:
            try:
                btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, css)))
                # .click() can be blocked by overlays; JS click is often more stable
                self.driver.execute_script("arguments[0].click();", btn)
                return True
            except Exception:
                continue
        return False

    def _press_enter(self) -> bool:
        """
        Try to send via ENTER key (depends on user's WhatsApp settings).
        Prefer composer-focused ENTER; fallback to a global ENTER.
        """
        try:
            comp = self._locate_composer()
            if comp:
                comp.click()
                comp.send_keys(Keys.ENTER)
                return True
        except Exception as e:
            log.debug("ENTER send failed (composer): %s", e)
        try:
            ActionChains(self.driver).send_keys(Keys.ENTER).perform()
            return True
        except Exception as e:
            log.debug("ENTER send failed (global): %s", e)
            return False

    def send_text_to_phone(self, phone: str, message: str, timeout_seconds: int = 30) -> dict:
        """
        Send a plain text message to a given phone number.
        Flow:
          1) Open chat via deep-link: /send/?phone=<PHONE>&text=<TEXT>...
          2) Fill the composer: via JS or send_keys
          3) Send: prefer send button; fallback to ENTER
          4) Wait 1s after each send (throttle)
        Returns dict:
          { "phone": "<num>", "ok": True/False, "error": <code or None> }
        """
        # Do not attempt to send if session is not ready
        if not self.ready():
            return {"phone": phone, "ok": False, "error": "not_logged_in"}

        # Normalize and lightly validate phone
        norm = self._normalize_phone(phone)
        if not _E164_RE.match(norm) and not norm.isdigit():
            return {"phone": phone, "ok": False, "error": "invalid_phone"}

        # Build deep-link (ensure there's only a single '?')
        url = f"{self.base_url}send/?phone={norm}&text={quote_plus(message)}&type=phone_number&app_absent=0"
        log.info("Opening chat for %s", norm)
        self.driver.get(url)

        wait = WebDriverWait(self.driver, timeout_seconds, poll_frequency=0.5)
        try:
            # Ensure chat UI is open
            self._wait_chat_open(wait)

            # Fill message into composer (try JS first; fallback to send_keys)
            wrote = self._type_via_js_and_dispatch(message)
            if not wrote:
                comp = self._locate_composer()
                if not comp:
                    return {"phone": phone, "ok": False, "error": "composer_not_found"}
                comp.click()
                # Try selecting existing content and overwriting
                try:
                    comp.send_keys(Keys.CONTROL, "a")
                except Exception:
                    pass
                comp.send_keys(message)

            # Prefer send button; fallback to ENTER
            sent = self._click_send_button(wait)
            if not sent:
                sent = self._press_enter()

            if not sent:
                return {"phone": phone, "ok": False, "error": "send_action_failed"}

            # Small delay to reduce collisions / rate limits
            time.sleep(1.0)
            return {"phone": phone, "ok": True, "error": None}

        except TimeoutException:
            # Opening chat timed out
            return {"phone": phone, "ok": False, "error": "open_timeout"}
        except Exception as e:
            # Log unexpected exceptions and return standard error code
            log.exception("Unexpected error while sending to %s: %s", norm, e)
            return {"phone": phone, "ok": False, "error": "unexpected_error"}

    def send_bulk(self, phones: Iterable[str], messages: list[str] | None = None) -> list[dict]:
        """
        Bulk send scenario.
        Rules:
          - If messages has a single element, everyone gets the same message
          - If messages is shorter than phones, the last message is reused
          - Each send is routed through send_text_to_phone (includes internal 1s throttle)
        Returns: list of send_text_to_phone results per phone.
        """
        results = []
        phones = list(phones)
        if not phones:
            return results

        # No message → return an error per phone
        if not messages:
            return [{"phone": p, "ok": False, "error": "no_message"} for p in phones]

        # Single message: broadcast the same
        if len(messages) == 1:
            msg = messages[0]
            for p in phones:
                results.append(self.send_text_to_phone(p, msg))
            return results

        # Multiple messages: match by index; fallback to last for overflow
        for idx, p in enumerate(phones):
            m = messages[idx] if idx < len(messages) else messages[-1]
            results.append(self.send_text_to_phone(p, m))
        return results
