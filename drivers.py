from __future__ import annotations
import logging
import os
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

log = logging.getLogger(__name__)

def make_chrome(chrome_user_data_dir: str, headless: bool) -> webdriver.Chrome:
    """
    Launch a Chrome WebDriver (Selenium).
    - With Selenium 4.6+, Selenium Manager auto-manages ChromeDriver.
    - A persistent user profile (user-data-dir) is used so QR login stays cached between runs.
    - Headless mode can be enabled for servers/CI.
    """

    opts = Options()

    # Ensure a stable user profile directory (e.g., Browser/profile_01)
    # Login session cookies and storage will persist here.
    user_data_dir = os.path.abspath(chrome_user_data_dir)
    os.makedirs(user_data_dir, exist_ok=True)
    opts.add_argument(f"--user-data-dir={user_data_dir}")

    # Recommended stable Chrome flags
    opts.add_argument("--disable-gpu")             # disable GPU acceleration
    opts.add_argument("--no-sandbox")              # required in some containerized environments
    opts.add_argument("--disable-dev-shm-usage")   # mitigate shared memory issues (Linux)
    opts.add_argument("--window-size=1280,900")    # default window size

    # Headless mode (Chrome 109+ style)
    if headless:
        opts.add_argument("--headless=new")

    log.info("Launching Chrome (headless=%s, profile=%s)", headless, user_data_dir)

    # Start Chrome WebDriver → Selenium Manager will download/manage the driver
    driver = webdriver.Chrome(options=opts)

    # Reasonable page load timeout (seconds)
    driver.set_page_load_timeout(60)

    return driver
