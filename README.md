# ECS Embedded Registry Manager

A lightweight Flask web application for managing the images stored in the Docker Registry backing a **Cloudera AI Embedded Registry for ECS**. It provides a simple UI to browse repositories, inspect tag-level detail (digest, architecture, OS, size, created date), and delete unwanted image tags directly against the registry's Docker Registry v2 API.

## 1. Overview

ECS Registry Manager gives administrators a browser-based way to manage the contents of an embedded Docker Registry without needing to script calls against the registry API directly. It supports:

- **Registry configuration** — point the app at a registry URL and store credentials locally so they don't need to be re-entered every session.
- **Browsing repositories** — lists every repository (image path) in the registry catalog, with the number of tags and the latest tag for each, paginated and loaded progressively via a live progress indicator (server-sent events) so large registries don't block the UI.
- **Refreshing the catalog** — re-fetch the current state of the registry on demand rather than relying on a stale cache.
- **Inspecting image detail** — drill into a single repository to see every tag along with its manifest digest, architecture, OS, creation date, and compressed size.
- **Deleting image tags** — select one or more tags (on either the repository list or the image detail page) and delete their manifests from the registry in a single action.

This tool is intended for use alongside a **Cloudera AI Embedded Registry for ECS** deployment. Over time these registries accumulate old, unused image tags that consume disk space; ECS Registry Manager makes it easy to find and remove them.

## 2. How to Deploy

_Placeholder — deployment instructions will be added here._

This application is intended to be deployed as a **Cloudera AI Workbench Application**.

## 3. Settings

Before you can browse or manage images, you need to point the application at your registry:

1. Open the application and navigate to the **Settings** tab (you'll be redirected here automatically on first launch if no registry is configured).
2. Fill in the following fields:
   - **Registry URL** — the base URL of the Docker Registry, e.g. `https://<host>:5000`.
   - **Username** — the registry username.
   - **Password** — the registry password. On subsequent visits this field can be left blank to keep the currently saved password unchanged.
3. Click **Save Settings**.
4. On save, the application validates the credentials by making an authenticated request to the registry (`GET /v2/`). If authentication fails or the registry is unreachable, an error is shown and the configuration is not saved.
5. Once saved successfully, the credentials are stored locally in a `.regconf` file at the project root. The password is base64-encoded (not stored in clear text) before being written to disk.
6. Navigate to the **Images** tab to start browsing the registry.

## 4. Image Deletion

Deleting a tag from the **Images** or **Image Detail** page removes the corresponding manifest from the registry via the Docker Registry v2 `DELETE` API. This is a soft delete: the manifest reference is removed, but the underlying image layer blobs are **not** immediately freed from disk.

To actually reclaim disk space, the Docker Registry's built-in **garbage collection** must be run on the host/container serving the registry. Garbage collection scans the registry storage, identifies blobs that are no longer referenced by any manifest, and removes them.

Run garbage collection from within the registry container:

```bash
docker exec -it <registry-container> registry garbage-collect /etc/docker/registry/config.yml
```

> Replace `<registry-container>` with the name or ID of the running Docker Registry container, and adjust the config path if your deployment uses a different location.

**After garbage collection completes, restart the Docker registry** so it picks up the updated storage state:

```bash
docker restart <registry-container>
```

Deleted tags will continue to consume disk space until both steps above are completed.

## 5. FAQ

**Q: Why do some image paths show a tag count of `0`?**

A: A repository can appear in the registry catalog even after all of its tags have been deleted — the repository directory itself remains on disk even though it no longer references any manifests. DS Registry Manager will list this repository with a tag count of `0` since there are no tags left to display or delete through the API.

The Docker Registry v2 API does not provide a way to delete an empty repository entry itself — only individual tags/manifests. To fully remove an image with `0` tag count from the registry catalog, you need to **manually delete the repository's directory** from the registry's storage backend (e.g. under `/var/lib/registry/docker/registry/v2/repositories/<image-path>` for a filesystem-backed registry), then restart the registry as described above.
