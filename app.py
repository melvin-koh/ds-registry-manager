import math
import os
import json
import queue
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
# Background-load SSE endpoint
# ---------------------------------------------------------------------------

def _stream_image_load(registry_url, registry_user, registry_password):
    """
    Generator that runs load_registry_image_list() in a background thread,
    yielding SSE events with progress and final results.
    """
    q = queue.Queue()

    def worker():
        try:
            from cloudera_ps.EmbeddedRegistryUtil import EmbeddedRegistryUtil as _Util
            import requests as _req

            # Monkey-patch _get_tags so we can emit progress per image.
            original_list = _Util.list_images

            def _patched_list(self):
                # Re-implement list_images with per-image progress events.
                img_list = []
                seen = set()
                page = 1000
                next_url = f"{self._registry_url}{self._CATALOG}".replace("$page", str(page))
                while next_url:
                    resp = _req.get(
                        next_url,
                        auth=(self._registry_user, self._registry_password),
                        timeout=30, verify=False,
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    for repo in payload.get("repositories", []):
                        if isinstance(repo, str) and repo not in seen:
                            seen.add(repo)
                            img_list.append(repo)
                    next_link = resp.links.get("next", {}).get("url")
                    if next_link and next_link.startswith("/"):
                        next_link = f"{self._registry_url}{next_link}"
                    next_url = next_link

                total = len(img_list)
                q.put({"type": "total", "total": total})

                repositories = []
                for idx, image in enumerate(img_list, 1):
                    try:
                        tags = self._get_tags(image)
                    except Exception as exc:
                        logger.error("Failed to get tags for '%s': %s", image, exc)
                        tags = []
                    repositories.append({"image": image, "tags": tags})
                    q.put({"type": "progress", "done": idx, "total": total, "image": image})

                return repositories

            _Util.list_images = _patched_list
            try:
                reg = EmbeddedRegistryUtil(registry_url, registry_user, registry_password)
                images = reg.list_images()
            finally:
                _Util.list_images = original_list  # restore

            rows = _prepare_image_rows(images)
            q.put({"type": "complete", "rows": rows})
        except Exception as exc:
            q.put({"type": "error", "message": str(exc)})

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    while True:
        event = q.get()
        yield f"data: {json.dumps(event)}\n\n"
        if event["type"] in ("complete", "error"):
            break


@app.route("/api/load-images")
def api_load_images():
    registry_url, registry_user, registry_password = _load_config()
    if not _config_complete(registry_url, registry_user, registry_password):
        payload = json.dumps({"type": "error", "message": "Registry not configured."})
        return Response(f"data: {payload}\n\n", mimetype="text/event-stream")
    return Response(
        _stream_image_load(registry_url, registry_user, registry_password),
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
