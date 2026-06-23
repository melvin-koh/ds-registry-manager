import math
import os

from flask import Flask, flash, redirect, render_template, request, url_for

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


@app.route("/")
def images():
    registry_url, registry_user, registry_password = _load_config()
    if not _config_complete(registry_url, registry_user, registry_password):
        flash("Configure registry credentials before viewing images.", "warning")
        return redirect(url_for("settings"))

    page = request.args.get("page", 1, type=int)
    rows = []
    error = None

    logger.info("Images page requested (page=%d)", page)
    try:
        raw_images = load_registry_image_list(
            registry_url, registry_user, registry_password
        )
        rows = _prepare_image_rows(raw_images)
        logger.info(
            "Prepared %d image row(s) for display on page %d",
            len(rows),
            page,
        )
    except ValueError as exc:
        error = str(exc)
        logger.error("Error loading images for display: %s", error)
        flash(error, "error")

    page_rows, page, total_pages, total_items = _paginate(rows, page, IMAGES_PER_PAGE)

    return render_template(
        "images.html",
        active_tab="images",
        images=page_rows,
        page=page,
        total_pages=total_pages,
        total_items=total_items,
        per_page=IMAGES_PER_PAGE,
        error=error,
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
                return redirect(url_for("images"))
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
