from __future__ import annotations
import logging
import time
import re
import datetime as dt
from urllib.parse import unquote
from flask import Flask, jsonify, request
from flask import Flask, jsonify, request, url_for, send_from_directory

import config
from db import SessionLocal, init_db, create_job, add_event, Job, JobTarget, JobEvent, get_group_phones, commit_with_retry

# ---------------------------------------------------------------------
# LOGGING / GENERAL SETTINGS
# ---------------------------------------------------------------------
log = logging.getLogger("whatsapp")

# For security in request/job IDs: filter out characters other than letters, numbers, underscores
_REQ_ID_RE = re.compile(r"[^A-Za-z0-9_]+")


# ---------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------
def _ready_profile_count(shared_state) -> int:
    """Returns the number of profiles that are 'ready' in the shared state."""
    return sum(1 for p in shared_state.get("profiles", {}).values() if p.get("ready"))


def _gen_request_id() -> str:
    """Generates a unique identifier for job/request: e.g. req_20250915_134455_ABC123"""
    import datetime, random, string
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rnd = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"req_{ts}_{rnd}"


def _parse_targets_and_messages(target: str, message: str):
    """
    Parses target and message information.
    - If comma-separated like '9053...,9054...' then multi-phone
    - If single phone then single_phone
    - Otherwise treated as group name (group)
    - 'message' may come URL-encoded; decoded with unquote
    - Message can also be comma-separated (m1,m2,...) -> sequential distribution in multi-phone
    """
    t = target.strip()
    msg_raw = unquote(message).strip()

    if "," in t:  # Multi-phone sending
        phones = [s.strip() for s in t.split(",") if s.strip()]
        messages = [s.strip() for s in msg_raw.split(",")] if "," in msg_raw else [msg_raw]
        return {"mode": "multi_phone", "phones": phones, "messages": messages}

    # '+9053...' or '9053...' pattern for single phone (may have spaces)
    if t.replace("+", "").replace(" ", "").isdigit():
        return {"mode": "single_phone", "phones": [t], "messages": [msg_raw]}

    # Remaining: group name
    return {"mode": "group", "group": t, "messages": [msg_raw]}


def _to_local_iso(d: dt.datetime | None) -> str | None:
    """
    Converts naive/UTC datetime from DB to config.APP_TZ and returns ISO-8601 string.
    - Returns None if input is None.
    """
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(config.APP_TZ).isoformat()


def _serialize_job(session, job: Job):
    """
    Converts Job object to readable JSON for API response.
    Content:
      - timeline: events (chronological order)
      - targets: target phones and their statuses
      - general job fields: status, profile, flags, timestamps etc.
    """
    # Get job events (timeline) in chronological order
    events = (
        session.query(JobEvent)
        .filter(JobEvent.job_id == job.id)
        .order_by(JobEvent.ts.asc(), JobEvent.id.asc())
        .all()
    )
    timeline = [
        {
            "ts": _to_local_iso(e.ts),     # ISO date/time in local timezone
            "event": e.kind,               # e.g: 'job_created', 'job_recovered'
            "detail": e.detail or "",
        }
        for e in events
    ]

    # Target phones and their current statuses
    targets = (
        session.query(JobTarget)
        .filter(JobTarget.job_id == job.id)
        .order_by(JobTarget.ord.asc())
        .all()
    )
    targets_serialized = [
        {
            "phone": t.phone,                      # target phone number
            "status": t.status,                    # pending/running/sent/failed/canceled
            "error": t.error,                      # last error message if any
            "updated_at": _to_local_iso(t.updated_at),
        }
        for t in targets
    ]

    # Aggregate output
    return {
        "id": job.id,
        "status": job.status,                      # queued/running/paused/canceled/failed/...
        "profile": job.profile,                    # profile executing the job (if any)
        "paused": job.paused,
        "canceled": job.canceled,
        "error": job.error,
        "created_at": _to_local_iso(job.created_at),
        "updated_at": _to_local_iso(job.updated_at),
        "target_type": job.target_type,            # single_phone/multi_phone/group
        "raw_target": job.raw_target,              # raw target string from user
        "timeline": timeline,                      # event list
        "targets": targets_serialized,             # target list
        "timezone": str(config.APP_TZ),            # reference timezone
    }


# ---------------------------------------------------------------------
# FLASK APPLICATION
# ---------------------------------------------------------------------
from flask import Flask, jsonify, request, url_for, send_from_directory
# ... other imports (config, SessionLocal, Job, JobTarget, etc.)

def create_app(shared_state) -> Flask:
    app = Flask(__name__)
    init_db()

    # ---------------- LIVE METRICS JSON ----------------
    @app.get("/metrics")
    def metrics():
        """
        Live status data:
        - wa_ready (bool)
        - prof_total, prof_ready
        - queued_jobs, running_jobs, pending_targets
        - profiles: {key, ready, path?, pid?, last_seen?, windows?: [...]}
        windows (optional): {title?, url?, wa_ready?, last_seen?}
        """
        wa_ready = bool(shared_state.get("wa_ready"))
        profiles = shared_state.get("profiles", {}) or {}

        # shared_state["profiles"] expected minimal structure:
        # {
        #   "profile_01": {"ready": True, "path": "...", "pid": 1234, "last_seen": "...",
        #                  "windows": [{"title":"WhatsApp Web","url":"...","wa_ready":True,"last_seen":"..."}]},
        #   ...
        # }

        prof_total = len(profiles)
        prof_ready = sum(1 for p in profiles.values() if p.get("ready"))

        with SessionLocal() as s:
            queued_jobs = (
                s.query(Job)
                 .filter(Job.status == "queued", Job.canceled == False, Job.paused == False)
                 .count()
            )
            running_jobs = s.query(Job).filter(Job.status == "running").count()
            pending_targets = (
                s.query(JobTarget)
                 .join(Job, Job.id == JobTarget.job_id)
                 .filter(Job.status == "queued", Job.canceled == False, Job.paused == False)
                 .filter(JobTarget.status == "pending")
                 .count()
            )

        # Normalize profiles
        profiles_list = []
        for key, p in profiles.items():
            profiles_list.append({
                "key": key,
                "ready": bool(p.get("ready")),
                "path": p.get("path"),
                "pid": p.get("pid"),
                "last_seen": p.get("last_seen"),
                "windows": p.get("windows") or []
            })

        return jsonify({
            "wa_ready": wa_ready,
            "prof_total": prof_total,
            "prof_ready": prof_ready,
            "queued_jobs": queued_jobs,
            "running_jobs": running_jobs,
            "pending_targets": pending_targets,
            "profiles": profiles_list
        })

    # ---------------- ROOT: WhatsApp Bot Control Panel ----------------
    @app.get("/")
    def home():
        """Ana sayfa - WhatsApp Bot kontrol paneli"""
        return send_from_directory('.', 'index.html')

    @app.get("/panel")
    def panel():
        """WhatsApp Bot kontrol paneli - alternatif route"""
        return send_from_directory('.', 'index.html')

    # ---------------- API DOCS ----------------
    @app.get("/docs")
    def docs():
        base = request.host_url.rstrip("/")
        tz = str(config.APP_TZ)
        css_url = url_for('static', filename='wpbot.css')
        js_url = url_for('static', filename='wpbot.js')

        html = \
            f"""<!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>WPBot API Docs</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <link href="{css_url}" rel="stylesheet">
    </head>
    <body>
    <header class="site-header">
      <div class="container header-row">
        <div class="brand">WPBot API</div>
        <div class="tz-badge">{tz}</div>
      </div>
    </header>

    <main class="container">
    <!-- LIVE METRICS (will be rendered dynamically) -->
    <section class="card card--metrics">
      <h1 class="card-title">Status</h1>
      <div id="live-metrics" class="metrics-shell">
        <!-- JS will fill this with /metrics data -->
        <div class="skeleton">
          <div class="sk-line"></div>
          <div class="sk-line"></div>
          <div class="sk-line short"></div>
        </div>
      </div>
      <div class="hint">Metrics are live. Documentation examples below are static.</div>
    </section>

      <!-- STATIC DOCUMENTATION -->
      <section class="card">
        <h2 class="card-title">Health</h2>
    <pre><code>GET {base}/health
    → 200 OK
    {{
      "status": "ok"
    }}

    GET {base}/ready
    → 200 OK
    {{
      "wa_ready": true
    }}</code></pre>
      </section>

      <section class="card">
        <h2 class="card-title">Send</h2>
        <div class="bullets">
          <ul>
            <li>Single phone: <code>GET {base}/send/&lt;phone&gt;/&lt;message&gt;</code></li>
            <li>Multi phone: <code>GET {base}/send/&lt;phone1,phone2,...&gt;/&lt;message | m1,m2,...&gt;</code></li>
            <li>Group name: <code>GET {base}/send/&lt;groupName&gt;/&lt;message&gt;</code></li>
          </ul>
        </div>

        <details open class="ex">
          <summary>Real example — Single phone (successful)</summary>
    <pre><code>GET {base}/send/+90555550123/Test1

    → 202 Accepted
    {{
      "accepted": 1,
      "queued": 1,
      "request_id": "req_20250917_132111_C2C3R6",
      "running": 0,
      "status_url": "/status/req_20250917_132111_C2C3R6"
    }}

    GET {base}/status/req_20250917_132111_C2C3R6

    → 200 OK
    {{
      "canceled": false,
      "created_at": "2025-09-17T13:21:11+03:00",
      "error": null,
      "id": "req_20250917_132111_C2C3R6",
      "paused": false,
      "profile": "profile_01",
      "raw_target": "+90555550123",
      "status": "done",
      "target_type": "single_phone",
      "targets": [
        {{
          "error": null,
          "phone": "+90555550123",
          "status": "sent",
          "updated_at": "2025-09-17T13:21:19+03:00"
        }}
      ],
      "timeline": [
        {{ "detail": "Queued 1 target(s)", "event": "job_queued",   "ts": "2025-09-17T13:21:11+03:00" }},
        {{ "detail": "Using profile profile_01", "event": "job_started", "ts": "2025-09-17T13:21:12+03:00" }},
        {{ "detail": "+90555550123", "event": "target_sent", "ts": "2025-09-17T13:21:19+03:00" }},
        {{ "detail": "All targets sent", "event": "job_completed", "ts": "2025-09-17T13:21:19+03:00" }}
      ],
      "timezone": "UTC+03:00",
      "updated_at": "2025-09-17T13:21:19+03:00"
    }}</code></pre>
        </details>

        <details class="ex">
          <summary>Real example — Multi phone (partial failure → partial_failure)</summary>
    <pre><code>GET {base}/send/5310000000,5370000000/this%20is%20a%20test%20message

    → 202 Accepted
    {{
      "accepted": 1,
      "queued": 1,
      "request_id": "req_20250917_132454_AEU62N",
      "running": 0,
      "status_url": "/status/req_20250917_132454_AEU62N"
    }}

    GET {base}/status/req_20250917_132454_AEU62N

    → 200 OK
    {{
      "canceled": false,
      "created_at": "2025-09-17T13:24:54+03:00",
      "error": "partial_failure",
      "id": "req_20250917_132454_AEU62N",
      "paused": false,
      "profile": "profile_01",
      "raw_target": "5310000000,5370000000",
      "status": "failed",
      "target_type": "multi_phone",
      "targets": [
        {{ "error": null, "phone": "5310000000", "status": "sent",    "updated_at": "2025-09-17T13:25:03+03:00" }},
        {{ "error": null, "phone": "5370000000", "status": "pending", "updated_at": "2025-09-17T13:24:54+03:00" }}
      ],
      "timeline": [
        {{ "detail": "Queued 2 target(s)", "event": "job_queued",  "ts": "2025-09-17T13:24:54+03:00" }},
        {{ "detail": "Using profile profile_01", "event": "job_started", "ts": "2025-09-17T13:24:56+03:00" }},
        {{ "detail": "5310000000", "event": "target_sent", "ts": "2025-09-17T13:25:03+03:00" }},
        {{ "detail": "Paused: profile not ready", "event": "job_paused", "ts": "2025-09-17T13:25:03+03:00" }},
        {{ "detail": "Partial failure", "event": "job_failed", "ts": "2025-09-17T13:25:03+03:00" }}
      ],
      "timezone": "UTC+03:00",
      "updated_at": "2025-09-17T13:25:03+03:00"
    }}</code></pre>
          <p class="hint">Note: If a target remains <code>pending</code>, you can re-queue it with <code>/retry/&lt;id&gt;</code>.</p>
        </details>

        <details class="ex">
          <summary>Real example — Multi phone (successful, mixed formats)</summary>
    <pre><code>GET {base}/send/5310000000,+905370000000/this%20is%20a%20test%20message

    → 200 OK
    {{
      "canceled": false,
      "created_at": "2025-09-17T13:27:49+03:00",
      "error": null,
      "id": "req_20250917_132749_ZDBIPE",
      "paused": false,
      "profile": "profile_01",
      "raw_target": "5310000000,+905370000000",
      "status": "done",
      "target_type": "multi_phone",
      "targets": [
        {{ "error": null, "phone": "5310000000",      "status": "sent", "updated_at": "2025-09-17T13:28:02+03:00" }},
        {{ "error": null, "phone": "+905370000000",   "status": "sent", "updated_at": "2025-09-17T13:28:08+03:00" }}
      ],
      "timeline": [
        {{ "detail": "Queued 2 target(s)", "event": "job_queued", "ts": "2025-09-17T13:27:49+03:00" }},
        {{ "detail": "Using profile profile_01", "event": "job_started", "ts": "2025-09-17T13:27:56+03:00" }},
        {{ "detail": "5310000000", "event": "target_sent", "ts": "2025-09-17T13:28:02+03:00" }},
        {{ "detail": "+905370000000", "event": "target_sent", "ts": "2025-09-17T13:28:08+03:00" }},
        {{ "detail": "All targets sent", "event": "job_completed", "ts": "2025-09-17T13:28:08+03:00" }}
      ],
      "timezone": "UTC+03:00",
      "updated_at": "2025-09-17T13:28:08+03:00"
    }}</code></pre>
        </details>

        <details class="ex">
          <summary>Real example — Group name (successful)</summary>
    <pre><code>GET {base}/send/Friends/this%20is%20a%20test%20message

    → 200 OK
    {{
      "canceled": false,
      "created_at": "2025-09-17T13:35:05+03:00",
      "error": null,
      "id": "req_20250917_133504_9NSS53",
      "paused": false,
      "profile": "profile_01",
      "raw_target": "Friends",
      "status": "done",
      "target_type": "group",
      "targets": [
        {{ "error": null, "phone": "+905540000000", "status": "sent", "updated_at": "2025-09-17T13:35:12+03:00" }},
        {{ "error": null, "phone": "+905550000000", "status": "sent", "updated_at": "2025-09-17T13:35:18+03:00" }}
      ],
      "timeline": [
        {{ "detail": "Queued 2 target(s)", "event": "job_queued", "ts": "2025-09-17T13:35:05+03:00" }},
        {{ "detail": "Using profile profile_01", "event": "job_started", "ts": "2025-09-17T13:35:06+03:00" }},
        {{ "detail": "+905540000000", "event": "target_sent", "ts": "2025-09-17T13:35:12+03:00" }},
        {{ "detail": "+905550000000", "event": "target_sent", "ts": "2025-09-17T13:35:18+03:00" }},
        {{ "detail": "All targets sent", "event": "job_completed", "ts": "2025-09-17T13:35:18+03:00" }}
      ],
      "timezone": "UTC+03:00",
      "updated_at": "2025-09-17T13:35:18+03:00"
    }}</code></pre>
        </details>

        <details class="ex">
          <summary>Error — Group not found</summary>
    <pre><code>GET {base}/send/SalesTeam/Meeting%20at%2015:00
    → 404 Not Found
    {{
      "error": "group_not_found",
      "group": "SalesTeam"
    }}</code></pre>
        </details>
      </section>

      <section class="card">
        <h2 class="card-title">Status (general format)</h2>
    <pre><code>GET {base}/status/&lt;request_id&gt;
    → 200 OK
    {{
      "id": "req_20250915_134455_ABC123",
      "status": "queued",
      "profile": null,
      "paused": false,
      "canceled": false,
      "error": null,
      "created_at": "2025-09-15T13:44:55+03:00",
      "updated_at": "2025-09-15T13:44:55+03:00",
      "target_type": "single_phone",
      "raw_target": "905312345678",
      "timezone": "{tz}",
      "timeline": [
        {{
          "ts": "2025-09-15T13:44:55+03:00",
          "event": "job_created",
          "detail": ""
        }}
      ],
      "targets": [
        {{
          "phone": "+905312345678",
          "status": "pending",
          "error": null,
          "updated_at": "2025-09-15T13:44:55+03:00"
        }}
      ]
    }}

    # Not found
    → 404 Not Found
    {{ "error": "not_found" }}</code></pre>
      </section>

      <section class="card">
        <h2 class="card-title">Control</h2>

        <details class="ex" open>
          <summary>Pause</summary>
    <pre><code>GET {base}/pause/&lt;request_id&gt;
    → 200 OK
    {{
      "id": "req_20250915_134455_ABC123",
      "status": "paused",
      "paused": true,
      "canceled": false,
      "timeline": [
        {{ "ts":"2025-09-15T13:44:55+03:00", "event":"job_created", "detail":"" }},
        {{ "ts":"2025-09-15T13:46:12+03:00", "event":"job_paused", "detail":"Pause requested" }}
      ],
      "targets": [
        {{ "phone":"+905312345678", "status":"pending", "error": null, "updated_at":"2025-09-15T13:46:12+03:00" }}
      ]
    }}

    # Not found
    → 404 Not Found
    {{ "error": "not_found" }}</code></pre>
        </details>

        <details class="ex">
          <summary>Resume</summary>
    <pre><code>GET {base}/resume/&lt;request_id&gt;
    → 200 OK
    {{
      "id": "req_20250915_134455_ABC123",
      "status": "queued",
      "paused": false,
      "timeline": [
        {{ "ts":"2025-09-15T13:44:55+03:00", "event":"job_created", "detail":"" }},
        {{ "ts":"2025-09-15T13:46:12+03:00", "event":"job_paused", "detail":"Pause requested" }},
        {{ "ts":"2025-09-15T13:47:02+03:00", "event":"job_resumed", "detail":"Resume requested" }}
      ]
    }}</code></pre>
        </details>

        <details class="ex">
          <summary>Cancel</summary>
    <pre><code>GET {base}/cancel/&lt;request_id&gt;
    → 200 OK
    {{
      "id": "req_20250915_134455_ABC123",
      "status": "canceled",
      "canceled": true,
      "timeline": [
        {{ "ts":"2025-09-15T13:44:55+03:00", "event":"job_created", "detail":"" }},
        {{ "ts":"2025-09-15T13:48:33+03:00", "event":"job_canceled", "detail":"Cancellation requested" }}
      ],
      "targets": [
        {{ "phone":"+905312345678", "status":"canceled", "error": null, "updated_at":"2025-09-15T13:48:33+03:00" }}
      ]
    }}</code></pre>
        </details>

        <details class="ex">
          <summary>Retry</summary>
    <pre><code>GET {base}/retry/&lt;request_id&gt;
    → 200 OK
    {{
      "id": "req_20250915_134455_ABC123",
      "status": "queued",
      "paused": false,
      "canceled": false,
      "error": null,
      "timeline": [
        {{ "ts":"2025-09-15T13:44:55+03:00", "event":"job_created", "detail":"" }},
        {{ "ts":"2025-09-15T13:49:10+03:00", "event":"job_retried", "detail":"Reset 1 target(s) to pending" }}
      ],
      "targets": [
        {{ "phone":"+905312345678", "status":"pending", "error": null, "updated_at":"2025-09-15T13:49:10+03:00" }}
      ]
    }}</code></pre>
        </details>

        <details class="ex">
          <summary>Recover (bulk recovery)</summary>
    <pre><code>GET {base}/recover
    → 200 OK
    {{
      "ok": true,
      "updated_jobs": 3,
      "reset_targets": 12,
      "message": "All paused/failed/canceled/queued jobs set to queued; non-sent targets reset to pending."
    }}</code></pre>
        </details>
      </section>

      <section class="card">
        <h2 class="card-title">Notes</h2>
        <ul class="notes">
          <li>If group is not found in <code>/send/&lt;group&gt;/...</code> call, returns <code>404 group_not_found</code>.</li>
          <li><code>/status/&lt;id&gt;</code> responses are shown in <code>{tz}</code> timezone as ISO-8601.</li>
          <li><code>request_id</code> format: <code>req_YYYYMMDD_HHMMSS_RANDOM</code>.</li>
          <li><em>Privacy:</em> Real examples on this page contain masked numbers.</li>
        </ul>
      </section>

    </main>

    <footer class="site-footer">
      <div class="container">WPBot • Flask • Selenium • SQL (Jobs &amp; Targets)</div>
    </footer>

    <script src="{js_url}"></script>
    </body>
    </html>"""
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}



    # ---------------- Simple JSON health endpoints ----------------
    @app.get("/health")
    def health():
        """Returns simple 'ok' response to test if service is alive."""
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        """Is WhatsApp automation layer ready? (returns via shared_state)"""
        wa_ready = bool(shared_state.get("wa_ready"))
        return jsonify({"wa_ready": wa_ready})

    # ---------------- Job status display ----------------
    @app.get("/status/<job_id>")
    def status(job_id: str):
        """
        Returns current status as JSON for given job ID.
        - timeline: event list
        - targets: individual target statuses
        404: if job not found
        """
        safe_id = _REQ_ID_RE.sub("", job_id)  # Clean ID (security)
        with SessionLocal() as s:
            job = s.get(Job, safe_id)
            if not job:
                return jsonify({"error": "not_found"}), 404
            return jsonify(_serialize_job(s, job))

    # ---------------- Message sending queue ----------------
    @app.get("/send/<path:target>/<path:message>")
    def send(target: str, message: str):
        """
        Saves send request to DB as 'job'; worker will pick it up and process.
        - target: phone(s) or group name
        - message: may be URL-encoded
        202 Accepted: request received and queued
        404: group name given but group not found
        """
        parsed = _parse_targets_and_messages(target, message)
        job_id = _gen_request_id()
        with SessionLocal() as s:
            if parsed["mode"] == "group":
                # Group name -> get group phones from DB
                phones = get_group_phones(s, parsed["group"])
                if not phones:
                    return jsonify({"error": "group_not_found", "group": parsed["group"]}), 404
                messages = parsed["messages"]
                job = create_job(s, job_id, "group", target, message, phones, messages)
            elif parsed["mode"] == "single_phone":
                job = create_job(s, job_id, "single_phone", target, message, parsed["phones"], parsed["messages"])
            else:  # multi_phone
                job = create_job(s, job_id, "multi_phone", target, message, parsed["phones"], parsed["messages"])
            s.commit()

        # Worker side will pick up this job and execute it (asynchronous execution)
        return jsonify({
            "accepted": 1,                 # request accepted
            "queued": 1,                   # queued
            "running": 0,                  # this request not running now (worker will pick it up)
            "request_id": job_id,          # unique ID for tracking
            "status_url": f"/status/{job_id}"
        }), 202

    # ---------------- Control endpoints: cancel/pause/resume/retry ----------------
    @app.get("/cancel/<job_id>")
    def cancel(job_id: str):
        """
        Cancels job.
        - If queued/paused, job status becomes 'canceled'
        - pending/running targets are marked 'canceled'
        - worker will not execute this job anymore
        """
        safe_id = _REQ_ID_RE.sub("", job_id)
        with SessionLocal() as s:
            job = s.get(Job, safe_id)
            if not job:
                return jsonify({"error": "not_found"}), 404
            job.canceled = True
            # If queued/paused, job is also set to canceled status
            job.status = "canceled" if job.status in ("queued", "paused") else job.status
            add_event(s, job, "job_canceled", "Cancellation requested")
            for t in job.targets:
                if t.status in ("pending", "running"):
                    t.status = "canceled"
            s.commit()
            return jsonify(_serialize_job(s, job))

    @app.get("/pause/<job_id>")
    def pause(job_id: str):
        """
        Pauses job (status='paused').
        Note: Depending on design, running subtargets may complete; new ones won't be picked up.
        """
        safe_id = _REQ_ID_RE.sub("", job_id)
        with SessionLocal() as s:
            job = s.get(Job, safe_id)
            if not job:
                return jsonify({"error": "not_found"}), 404
            job.paused = True
            job.status = "paused"
            add_event(s, job, "job_paused", "Pause requested")
            s.commit()
            return jsonify(_serialize_job(s, job))

    @app.get("/resume/<job_id>")
    def resume(job_id: str):
        """
        Re-queues paused or 'failed' job.
        - paused=False
        - status becomes 'queued' (if paused/failed)
        """
        safe_id = _REQ_ID_RE.sub("", job_id)
        with SessionLocal() as s:
            job = s.get(Job, safe_id)
            if not job:
                return jsonify({"error": "not_found"}), 404
            job.paused = False
            if job.status in ("paused", "failed"):
                job.status = "queued"
            add_event(s, job, "job_resumed", "Resume requested")
            s.commit()
            return jsonify(_serialize_job(s, job))

    @app.get("/retry/<job_id>")
    def retry(job_id: str):
        """
        Pulls 'failed' or 'canceled' targets back to 'pending' and re-queues job.
        - job.canceled=False, job.paused=False, job.status='queued'
        - job.error cleared
        """
        safe_id = _REQ_ID_RE.sub("", job_id)
        with SessionLocal() as s:
            job = s.get(Job, safe_id)
            if not job:
                return jsonify({"error": "not_found"}), 404
            reset_count = 0
            for t in job.targets:
                if t.status in ("failed", "canceled"):
                    t.status = "pending"
                    t.error = None
                    reset_count += 1
            job.canceled = False
            job.paused = False
            job.status = "queued"
            job.error = None
            add_event(s, job, "job_retried", f"Reset {reset_count} target(s) to pending")
            s.commit()
            return jsonify(_serialize_job(s, job))

    # ---------------- Bulk recovery: recover_all ----------------
    @app.get("/recover")
    def recover_all():
        """
        Bulk recovery for server/worker restarts.
        - All jobs with status in (paused, failed, canceled, queued) -> 'queued'
        - paused/canceled/job.error reset
        - targets: status in (failed, canceled, pending, running) -> 'pending'
        - 'sent' targets left untouched
        Purpose: "Whatever happened, continue safely from where left off".
        """
        updated_jobs = 0
        reset_targets = 0
        with SessionLocal() as s:
            jobs = (
                s.query(Job)
                 .filter(Job.status.in_(("paused", "failed", "canceled", "queued")))
                 .all()
            )
            for job in jobs:
                job.canceled = False
                job.paused = False
                job.status = "queued"
                job.error = None

                job_reset = 0
                for t in job.targets:
                    if t.status in ("failed", "canceled", "pending", "running"):
                        t.status = "pending"
                        t.error = None
                        job_reset += 1
                add_event(s, job, "job_recovered", f"Recovered by /recover; reset {job_reset} target(s)")
                updated_jobs += 1
                reset_targets += job_reset

            s.commit()

        return jsonify({
            "ok": True,
            "updated_jobs": updated_jobs,     # how many jobs recovered
            "reset_targets": reset_targets,   # how many targets reset
            "message": "All paused/failed/canceled/queued jobs set to queued; non-sent targets reset to pending."
        })

    return app
