# main.py
from __future__ import annotations
import logging
import threading
import time
import signal
import sys
import os
import webbrowser

from logger_setup import configure_logging
import config
from drivers import make_chrome
from whatsapp import WhatsAppWebClient
from api import create_app
from db import SessionLocal, init_db, Job, JobTarget, add_event, commit_with_retry

# Typical Selenium errors (window closed / session dropped, etc.)
from selenium.common.exceptions import WebDriverException, NoSuchWindowException, InvalidSessionIdException

# Module-level logger
log = logging.getLogger("whatsapp")


# In-process lock to prevent simultaneous job picking across all profiles
JOB_PICK_LOCK = threading.Lock()


def run_api(shared_state):
    """
    Starts the Flask API server.
    - shared_state: state shared with workers (readiness, profiles, etc.)
    """
    app = create_app(shared_state)

    # API sunucusunu başlattıktan sonra tarayıcıyı aç
    def open_browser():
        time.sleep(2)  # API'nin tamamen başlamasını bekle
        url = f"http://127.0.0.1:{config.API_PORT}/"
        try:
            log.info(f"WhatsApp Bot kontrol paneli açılıyor: {url}")
            webbrowser.open(url)
        except Exception as e:
            log.warning(f"Tarayıcı açılamadı: {e}")

    # Tarayıcı açma işlemini ayrı thread'de çalıştır
    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()

    app.run(host=config.API_HOST, port=config.API_PORT, threaded=True, use_reloader=False)


def ensure_dir(path: str) -> str:
    """Creates the directory (if missing) and returns its absolute path."""
    abs_path = os.path.abspath(path)
    os.makedirs(abs_path, exist_ok=True)
    return abs_path


def update_overall_ready(shared_state) -> None:
    """Sets the global 'wa_ready' flag to True if at least one profile is ready."""
    profiles = shared_state.get("profiles", {})
    shared_state["wa_ready"] = any(p.get("ready", False) for p in profiles.values())


# ---------- RESTART-SAFE RECOVERY ----------

def _recover_on_startup():
    """
    When the process restarts, reconcile half-finished work:
      - Set Job.status 'running' -> 'queued'
      - Set JobTarget.status 'running' -> 'pending'
    So the system continues from where it left off.
    """
    with SessionLocal() as s:
        jobs = s.query(Job).filter(Job.status == "running").all()
        for job in jobs:
            job.status = "queued"
            for t in job.targets:
                if t.status == "running":
                    t.status = "pending"
                    t.error = None
            add_event(s, job, "recovered_queued", "Recovered on startup")
        commit_with_retry(s)


# ---------- HELPER: Is the driver alive? ----------

def _driver_alive(driver) -> bool:
    """
    Simple check to determine whether the Chrome/Selenium session is still alive.
    - Are there window handles?
    - Is current_url accessible?
    """
    try:
        handles = driver.window_handles
        if not handles:
            return False
        _ = driver.current_url
        return True
    except (NoSuchWindowException, InvalidSessionIdException, WebDriverException):
        return False
    except Exception:
        return False


# ---------- PROFILE WORKER (each profile pulls the next job) ----------

def _pick_next_job_locked(session, profile_name: str) -> Job | None:
    """
    Selection is done with a global lock (JOB_PICK_LOCK) to prevent a job-grabbing race across all profiles.
    The selected job is marked as 'running' and assigned to the profile.
    """
    with JOB_PICK_LOCK:
        job = (
            session.query(Job)
            .filter(Job.status == "queued")
            .filter(Job.canceled == False)  # noqa: E712
            .filter(Job.paused == False)    # noqa: E712
            .order_by(Job.created_at.asc())
            .first()
        )
        if not job:
            return None
        job.status = "running"
        job.profile = profile_name
        add_event(session, job, "job_started", f"Using profile {profile_name}")
        commit_with_retry(session)
        return job


def run_profile_worker(profile_name: str, shared_state) -> None:
    """
    A single worker thread per profile:
      - Waits if the profile is not ready
      - If ready, takes the next job and processes its targets
      - Client/driver is obtained dynamically via shared_state (stay up-to-date after relaunch)
    """
    logger = logging.getLogger(f"whatsapp.worker.{profile_name}")
    init_db()  # idempotent, safe

    while True:
        try:
            # Is the profile ready?
            prof_info = shared_state.get("profiles", {}).get(profile_name, {})
            prof_ready = prof_info.get("ready", False)
            if not prof_ready:
                logger.info("Profile not ready; waiting login…")
                time.sleep(3)
                continue

            # Get the current client
            client: WhatsAppWebClient | None = prof_info.get("client")
            if client is None:
                logger.info("Client not available yet; waiting…")
                time.sleep(2)
                continue

            with SessionLocal() as s:
                # Take the next job
                job = _pick_next_job_locked(s, profile_name)
                if not job:
                    time.sleep(2)
                    continue

                # Process the job's targets in order
                targets = (
                    s.query(JobTarget)
                    .filter(JobTarget.job_id == job.id)
                    .order_by(JobTarget.ord.asc())
                    .all()
                )
                for t in targets:
                    # Before each target, re-check the latest state (for relaunch, etc.)
                    prof_info = shared_state.get("profiles", {}).get(profile_name, {})
                    client = prof_info.get("client")
                    if client is None:
                        # If the client disappeared, pause the job
                        job.status = "paused"
                        add_event(s, job, "job_paused", "Paused: client missing")
                        commit_with_retry(s)
                        break

                    s.refresh(job)

                    # If canceled, cancel pending/running targets
                    if job.canceled:
                        if t.status in ("pending", "running"):
                            t.status = "canceled"
                        commit_with_retry(s)
                        break

                    # If the profile is not ready or the job is paused, pause it
                    if job.paused or not prof_info.get("ready", False):
                        job.status = "paused"
                        add_event(s, job, "job_paused", "Paused: profile not ready")
                        commit_with_retry(s)
                        break

                    # Only deal with pending/running targets
                    if t.status not in ("pending", "running"):
                        continue

                    # Set the target to 'running' and send
                    t.status = "running"
                    commit_with_retry(s)

                    res = client.send_text_to_phone(t.phone, t.message)
                    if res.get("ok"):
                        t.status = "sent"
                        t.error = None
                        add_event(s, job, "target_sent", detail=t.phone)
                    else:
                        t.status = "failed"
                        t.error = res.get("error") or "unknown_error"
                        add_event(s, job, "target_failed", detail=f"{t.phone} :: {t.error}")
                    commit_with_retry(s)

                # Determine the final job status (if not paused/canceled)
                s.refresh(job)
                if not job.paused and not job.canceled:
                    trows = s.query(JobTarget).filter(JobTarget.job_id == job.id).all()
                    if all(tr.status == "sent" for tr in trows):
                        job.status = "done"
                        add_event(s, job, "job_completed", "All targets sent")
                    elif any(tr.status == "sent" for tr in trows):
                        job.status = "failed"
                        job.error = "partial_failure"
                        add_event(s, job, "job_failed", "Partial failure")
                    else:
                        job.status = "failed"
                        job.error = "all_failed"
                        add_event(s, job, "job_failed", "All failed")
                    commit_with_retry(s)

        except Exception as e:
            # Even if the worker crashes, keep looping and retry
            logger.exception("Profile worker crashed: %s", e)
            time.sleep(2)


# ---------- PROFILE LAUNCH / RELAUNCH WATCHER ----------

def _relaunch_driver(profile_name: str, profile_dir: str, shared_state) -> tuple[WhatsAppWebClient, bool]:
    """
    If the window is closed / session is dropped, restart the driver with the same profile folder.
    Return: (client, logged)
    """
    logger = logging.getLogger(f"whatsapp.{profile_name}")

    # Safely close the old driver (if any)
    try:
        old = shared_state.get("profiles", {}).get(profile_name, {}).get("client")
        if old is not None and getattr(old, "driver", None) is not None:
            try:
                old.driver.quit()
            except Exception:
                pass
    except Exception:
        pass

    # Create a new driver and client
    driver = make_chrome(profile_dir, config.CHROME_HEADLESS)
    client = WhatsAppWebClient(driver, base_url=config.WHATSAPP_URL)

    # Update shared_state
    shared_state.setdefault("profiles", {})
    shared_state["profiles"].setdefault(profile_name, {})
    shared_state["profiles"][profile_name]["client"] = client
    shared_state["profiles"][profile_name]["ready"] = False
    update_overall_ready(shared_state)

    # Open the page and wait for login
    client.open()
    logged = client.wait_until_logged_in(timeout_seconds=config.LOGIN_TIMEOUT_SECONDS)
    shared_state["profiles"][profile_name]["ready"] = logged
    update_overall_ready(shared_state)

    if logged:
        logger.info("Ready; waiting for API requests.")
    else:
        logger.info("Waiting for login (scan QR if shown)… up to %ss", config.LOGIN_TIMEOUT_SECONDS)

    return client, logged


def run_profile(profile_name: str, profile_dir: str, shared_state) -> None:
    """
    Manages a single Chrome profile:
      - Opens the first driver and waits for login
      - If the driver dies/loses reachability, relaunches it
      - The worker thread is started once; even if the client changes, the latest is used from shared_state
    """
    logger = logging.getLogger(f"whatsapp.{profile_name}")

    # First driver+client
    client, logged = _relaunch_driver(profile_name, profile_dir, shared_state)

    # Start the worker only once
    worker_started = False
    if logged and not worker_started:
        threading.Thread(
            target=run_profile_worker,
            args=(profile_name, shared_state),
            daemon=True,
            name=f"worker-{profile_name}",
        ).start()
        worker_started = True

    try:
        while True:
            # Is the driver still alive?
            if not _driver_alive(client.driver):
                logger.warning("Chrome window closed or session lost; relaunching driver…")
                client, logged_after = _relaunch_driver(profile_name, profile_dir, shared_state)
                if logged_after and not worker_started:
                    threading.Thread(
                        target=run_profile_worker,
                        args=(profile_name, shared_state),
                        daemon=True,
                        name=f"worker-{profile_name}",
                    ).start()
                    worker_started = True

            # Update readiness (also needed for API /ready)
            is_ready = client.ready()
            shared_state["profiles"][profile_name]["ready"] = is_ready
            update_overall_ready(shared_state)

            if is_ready:
                logger.info("Ready; waiting for API requests.")
            else:
                logger.info("Waiting for login (scan QR if shown)… up to %ss", config.LOGIN_TIMEOUT_SECONDS)

            # Loop period
            time.sleep(6)

    except KeyboardInterrupt:
        logger.info("Shutdown requested for %s", profile_name)
        raise
    except Exception as e:
        logger.exception("Profile '%s' crashed: %s", profile_name, e)
    finally:
        # Try to close the driver on exit
        try:
            client.driver.quit()
        except Exception:
            pass


# ---------- AUTOSCALER (initially INITIAL_PROFILES; +1 when capacity is exceeded) ----------

def _pending_target_count() -> int:
    """Returns the number of targets waiting in the queue (JobTarget.status=='pending')."""
    with SessionLocal() as s:
        q = (
            s.query(JobTarget)
            .join(Job, Job.id == JobTarget.job_id)
            .filter(Job.status == "queued")
            .filter(Job.canceled == False)  # noqa: E712
            .filter(Job.paused == False)    # noqa: E712
            .filter(JobTarget.status == "pending")
        )
        return q.count()

def _current_profile_count(shared_state) -> int:
    """How many profile threads are currently open?"""
    return len(shared_state.get("profiles", {}))

def _ready_profile_count(shared_state) -> int:
    """Number of ready (ready=True) profiles."""
    return sum(1 for p in shared_state.get("profiles", {}).values() if p.get("ready"))

def _spawn_profile(shared_state, index: int, base_dir: str):
    """Starts a new profile thread (run_profile)."""
    profile_name = f"profile_{index:02d}"
    profile_dir = ensure_dir(os.path.join(base_dir, profile_name))
    log.info("Starting %s at %s", profile_name, profile_dir)
    t = threading.Thread(
        target=run_profile,
        args=(profile_name, profile_dir, shared_state),
        daemon=False,  # keep the runner thread in the foreground
        name=f"runner-{profile_name}",
    )
    t.start()

def autoscaler(shared_state, base_dir: str):
    """
    Simple autoscaler:
      - Initially, there are INITIAL_PROFILES profiles open
      - Condition to open a new profile:
          * There is at least 1 ready profile
          * The number of pending targets EXCEEDS the ready capacity
      - At most 1 profile is added per cycle
      - Does not scale down
      - NO UPPER LIMIT (MAX_PROFILES removed)
    """
    logger = logging.getLogger("whatsapp.autoscaler")
    while True:
        try:
            pending = _pending_target_count()
            current = _current_profile_count(shared_state)
            ready = _ready_profile_count(shared_state)

            # Capacity: number of ready profiles × targets per profile
            ready_capacity = max(0, ready) * max(1, config.TASKS_PER_PROFILE)

            logger.info(
                "Autoscale check: pending=%s | current_profiles=%s | ready_profiles=%s | ready_capacity=%s",
                pending, current, ready, ready_capacity
            )

            if ready == 0:
                # Do not open a new profile without login
                pass
            elif pending <= ready_capacity:
                # Current capacity is sufficient
                pass
            else:
                # Capacity exceeded: no upper bound → open +1 profile
                idx = shared_state.setdefault("next_profile_number", current + 1)
                _spawn_profile(shared_state, idx, base_dir)
                shared_state["next_profile_number"] = idx + 1
                idx = shared_state["next_profile_number"]
                time.sleep(config.PROFILE_START_DELAY_SECONDS)

        except Exception as e:
            logger.exception("Autoscaler error: %s", e)

        # Check period
        time.sleep(max(1, config.SCALE_INTERVAL_SECONDS))


# ---------- MAIN ----------

def main():
    """
    Application entry point:
      - Configure logging
      - DB init + startup recovery
      - Shared state
      - Start API + initial profiles + autoscaler
      - Keep the main thread alive
    """
    configure_logging(log_dir=config.LOG_DIR, level=config.LOG_LEVEL)
    log.info("Starting WPBot: API + per-profile workers + autoscaler (self-healing Chrome)")

    init_db()
    _recover_on_startup()  # continue from where jobs left off after restart

    # Shared state (used by API and profiles)
    shared_state = {"wa_ready": False, "profiles": {}}

    # Run the API server in a separate daemon thread
    threading.Thread(target=run_api, args=(shared_state,), daemon=True, name="api").start()

    # Catch SIGINT/SIGTERM and exit gracefully
    def _sig_handler(sig, frame):
        log.info("Signal %s received, exiting…", sig)
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    # --- Initial startup: INITIAL PROFILES ---
    base = ensure_dir(config.BROWSER_ROOT_DIR)

    # The value from ENV is parsed by config.py. At least 1.
    initial = max(1, getattr(config, "INITIAL_PROFILES", 1))

    # Counter for the next spawn
    shared_state["next_profile_number"] = 1

    # Start profiles between 1..initial
    for idx in range(1, initial + 1):
        _spawn_profile(shared_state, idx, base)
        shared_state["next_profile_number"] = idx + 1
        # Small delay to avoid loading too much back-to-back
        time.sleep(max(0.1, config.PROFILE_START_DELAY_SECONDS / 5.0))

    # --- Autoscaling ---
    scaler_thread = threading.Thread(
        target=autoscaler, args=(shared_state, base), daemon=True, name="autoscaler"
    )
    scaler_thread.start()

    # Keep the main thread alive (can exit with Ctrl+C)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt: shutting down…")
        pass


if __name__ == "__main__":
    main()
