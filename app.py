import math
import os
import json
import time
import threading
from flask import Flask, Response, flash, redirect, render_template, request, url_for
from cloudera_ps.EmbeddedRegistryUtil import EmbeddedRegistryUtil
from cloudera_ps.util import (
    init_logging,
    load_registry_config,
    load_registry_image_list,
    logger,
    save_registry_config,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.join(BASE_DIR, "cloudera_ps")
REGCONF_PATH = os.path.join(BASE_DIR, ".regconf")
LOG_PATH = os.path.join(BASE_DIR, "app.log")
IMAGES_PER_PAGE = 15

init_logging(LOG_PATH)

app = Flask(
    __name__,
    template_folder=os.path.join(PACKAGE_DIR, "templates"),
    static_folder=os.path.join(PACKAGE_DIR, "static"),
)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "embedded-registry-image-manager")


def _config_complete(url, user, password):
    return bool(url and user and password)


def _load_config():
    return load_registry_config(REGCONF_PATH)


def _prepare_image_rows(raw_images):
    rows = []
    for item in raw_images:
        image_path = item.get("image", "")
        tags = item.get("tags") or []
        rows.append(
            {
                "image": image_path,
                "tag_count": len(tags),
                "latest_tag": max(tags) if tags else "—",
            }
        )
    rows.sort(key=lambda row: row["image"].lower())
    return rows


def _paginate(items, page, per_page):
    total_items = len(items)
    total_pages = max(1, math.ceil(total_items / per_page)) if total_items else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], page, total_pages, total_items


# ---------------------------------------------------------------------------
# Singleton load-state — shared across all SSE clients
# ---------------------------------------------------------------------------

class _LoadState:
    """Thread-safe singleton that holds the state of the single background load."""

    def __init__(self):
        self._lock = threading.Lock()
        self.status = "idle"   # idle | running | complete | error
        self.total = 0
        self.done = 0
        self.current_image = ""
        self.rows = []
        self.error_message = ""

    # ------------------------------------------------------------------
    # Writers — called only from the background worker thread
    # ------------------------------------------------------------------

    def set_running(self, total):
        with self._lock:
            self.status = "running"
            self.total = total
            self.done = 0
            self.current_image = ""
            self.rows = []
            self.error_message = ""

    def set_progress(self, done, image):
        with self._lock:
            self.done = done
            self.current_image = image

    def set_complete(self, rows):
        with self._lock:
            self.status = "complete"
            self.rows = rows

    def set_error(self, message):
        with self._lock:
            self.status = "error"
            self.error_message = message

    def reset(self):
        with self._lock:
            self.status = "idle"

    # ------------------------------------------------------------------
    # Reader — safe snapshot for SSE clients
    # ------------------------------------------------------------------

    def snapshot(self):
        with self._lock:
            return {
                "status": self.status,
                "total": self.total,
                "done": self.done,
                "current_image": self.current_image,
                "rows": self.rows,
                "error_message": self.error_message,
            }


_load_state = _LoadState()
_load_lock = threading.Lock()   # guards starting a new worker thread


def _run_worker(registry_url, registry_user, registry_password):
    """Background thread: fetches catalog + tags, updates _load_state throughout."""
    import requests as _req

    try:
        # --- Phase 1: enumerate all repositories ---
        img_list = []
        seen = set()
        page_size = 1000
        reg = EmbeddedRegistryUtil(registry_url, registry_user, registry_password)
        next_url = f"{reg._registry_url}{reg._CATALOG}".replace("$page", str(page_size))
        while next_url:
            resp = _req.get(
                next_url,
                auth=(reg._registry_user, reg._registry_password),
                timeout=30, verify=False,
            )
            resp.raise_for_status()
            for repo in resp.json().get("repositories", []):
                if isinstance(repo, str) and repo not in seen:
                    seen.add(repo)
                    img_list.append(repo)
            next_link = resp.links.get("next", {}).get("url")
            if next_link and next_link.startswith("/"):
                next_link = f"{reg._registry_url}{next_link}"
            next_url = next_link

        _load_state.set_running(len(img_list))
        logger.info("Worker: %d repositories found", len(img_list))

        # --- Phase 2: fetch tags per image ---
        repositories = []
        for idx, image in enumerate(img_list, 1):
            try:
                tags = reg._get_tags(image)
            except Exception as exc:
                logger.error("Failed to get tags for '%s': %s", image, exc)
                tags = []
            repositories.append({"image": image, "tags": tags})
            _load_state.set_progress(idx, image)

        rows = _prepare_image_rows(repositories)
        _load_state.set_complete(rows)
        logger.info("Worker: complete, %d rows prepared", len(rows))

    except Exception as exc:
        logger.error("Worker error: %s", exc)
        _load_state.set_error(str(exc))


def _ensure_worker_running(registry_url, registry_user, registry_password):
    """Start a new worker only if one isn't already running or complete."""
    with _load_lock:
        snap = _load_state.snapshot()
        if snap["status"] in ("idle", "error"):
            _load_state.reset()
            _load_state.status = "running"   # mark early so no second thread races in
            t = threading.Thread(
                target=_run_worker,
                args=(registry_url, registry_user, registry_password),
                daemon=True,
            )
            t.start()
            logger.info("New background worker thread started")
        else:
            logger.info("Worker already %s — attaching SSE client", snap["status"])


def _sse_stream():
    """
    Generator for a single SSE client.  Polls _load_state and emits events.
    All clients share the same _load_state, so they all see identical progress.
    """
    sent_total = False

    while True:
        snap = _load_state.snapshot()

        # Emit 'total' once we know how many images there are
        if not sent_total and snap["total"] > 0:
            yield f"data: {json.dumps({'type': 'total', 'total': snap['total']})}\n\n"
            sent_total = True

        if snap["status"] == "running":
            yield f"data: {json.dumps({'type': 'progress', 'done': snap['done'], 'total': snap['total'], 'image': snap['current_image']})}\n\n"
            time.sleep(0.3)

        elif snap["status"] == "complete":
            # Make sure 'total' was sent before 'complete'
            if not sent_total:
                yield f"data: {json.dumps({'type': 'total', 'total': snap['total']})}\n\n"
            yield f"data: {json.dumps({'type': 'complete', 'rows': snap['rows']})}\n\n"
            break

        elif snap["status"] == "error":
            yield f"data: {json.dumps({'type': 'error', 'message': snap['error_message']})}\n\n"
            break

        else:
            # Still in the brief window before the worker calls set_running()
            time.sleep(0.1)


@app.route("/api/load-images")
def api_load_images():
    registry_url, registry_user, registry_password = _load_config()
    if not _config_complete(registry_url, registry_user, registry_password):
        payload = json.dumps({"type": "error", "message": "Registry not configured."})
        return Response(f"data: {payload}\n\n", mimetype="text/event-stream")

    _ensure_worker_running(registry_url, registry_user, registry_password)
    
    return Response(
        _sse_stream(),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.route("/api/refresh")
def api_refresh():
    """Force-reset load state and start a fresh background worker."""
    registry_url, registry_user, registry_password = _load_config()
    if not _config_complete(registry_url, registry_user, registry_password):
        payload = json.dumps({"type": "error", "message": "Registry not configured."})
        return Response(f"data: {payload}\n\n", mimetype="text/event-stream")

    with _load_lock:
        _load_state.reset()
        _load_state.status = "running"
        t = threading.Thread(
            target=_run_worker,
            args=(registry_url, registry_user, registry_password),
            daemon=True,
        )
        t.start()
        logger.info("Manual refresh — new background worker thread started")

    return Response(
        _sse_stream(),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.route("/")
def images():
    registry_url, registry_user, registry_password = _load_config()
    if not _config_complete(registry_url, registry_user, registry_password):
        flash("Configure registry credentials before viewing images.", "warning")
        return redirect(url_for("settings"))

    logger.info("Images page requested — will stream via SSE")

    return render_template(
        "images.html",
        active_tab="images",
        images_per_page=IMAGES_PER_PAGE,
    )


@app.route("/settings", methods=["GET", "POST"])
def settings():
    registry_url, registry_user, registry_password = _load_config()
    has_existing_config = _config_complete(
        registry_url, registry_user, registry_password
    )

    if request.method == "POST":
        registry_url = request.form.get("registry_url", "").strip()
        registry_user = request.form.get("registry_user", "").strip()
        password_input = request.form.get("registry_password", "")

        if not registry_url or not registry_user:
            flash("Registry URL and username are required.", "error")
        elif not password_input and not has_existing_config:
            flash("Password is required for a new configuration.", "error")
        else:
            password_to_save = (
                password_input if password_input else registry_password
            )
            try:
                EmbeddedRegistryUtil(registry_url, registry_user, password_to_save)
                save_registry_config(
                    registry_url, registry_user, password_to_save, REGCONF_PATH
                )
                flash("Registry settings saved successfully.", "success")
                #return redirect(url_for("images"))
            except ValueError as exc:
                flash(str(exc), "error")

    return render_template(
        "settings.html",
        active_tab="settings",
        registry_url=registry_url or "",
        registry_user=registry_user or "",
        has_existing_config=has_existing_config,
    )


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
