import base64
import json
import logging
import os
from typing import Dict, List

from cloudera_ps.EmbeddedRegistryUtil import EmbeddedRegistryUtil

LOG_FORMAT = "%(asctime)-15s %(levelname)-6s %(message)s"
logger = logging.getLogger("cloudera_ps")


def format_bytes(n: int) -> str:
    """Return a human-readable byte size string (e.g. '123.4 MB')."""
    if n <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def init_logging(logfile: str = "app.log") -> logging.Logger:
    """Configure application logging to the given log file."""
    handler = logging.FileHandler(logfile, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root_logger = logging.getLogger("cloudera_ps")
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    return root_logger


def load_registry_config(file_path: str = ".regconf") -> Dict[str, str]:
    """
    Load registry config from JSON file and decode the base64 password.
    """
    registry_url = None
    registry_user = None
    registry_password = None

    if os.path.isfile(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "registry_url" in data and data["registry_url"]:
                registry_url = data["registry_url"]
            if "registry_user" in data and data["registry_user"]:
                registry_user = data["registry_user"]
            if "registry_password" in data and data["registry_password"]:
                decoded_bytes = base64.b64decode(data["registry_password"])
                registry_password = decoded_bytes.decode("utf-8")

    return registry_url, registry_user, registry_password


def save_registry_config(url: str, user: str, password: str, file_path: str = ".regconf") -> None:
    """
    Save registry config to JSON file, encoding the password in base64.
    """
    data = { "registry_url": url, "registry_user": user, "registry_password": password }

    if "registry_password" in data and data["registry_password"]:
        encoded_bytes = base64.b64encode(data["registry_password"].encode("utf-8"))
        data["registry_password"] = encoded_bytes.decode("utf-8")

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_registry_image_list(registry_url, registry_user, registry_password) -> List:
    """
    Retreive image list from registry
    """
    logger.info("Starting image load from registry at %s", registry_url)
    try:
        reg = EmbeddedRegistryUtil(registry_url, registry_user, registry_password)
        images = reg.list_images()
        logger.info("Completed image load. Total images: %d", len(images))
        return images
    except ValueError as exc:
        logger.error("Failed to load images from registry: %s", exc)
        raise


def list_images_with_hash(manifest: dict) -> List[Dict[str, str]]:
    """
    List container images and their SHA-256 digests defined in a manifest.

    This will load the manifest JSON using ``_load_manifest`` and extract
    tuples of the form ``(<path>:<version>, <sha256>)``.

    Parameters
    ----------
    manifest : dict
        Dict of the manifest json content

    Returns
    -------
    list of dict
        Each dict is of the form::

            {
                "image": "cloudera/cdsw/web:2.0.51-b321",
                "hash": "sha256:9f155f2533267720f51d5e6971dbdb423e92a74fceabefb54f6454381f0c1dc5",
            }

    Raises
    ------
    ValueError
        If the manifest structure is invalid or required fields are missing.
    """

    images_section = manifest.get("images")
    if not images_section:
        return []

    if isinstance(images_section, dict):
        paths = images_section.get("paths", [])
    elif isinstance(images_section, list):
        paths = images_section
    else:
        raise ValueError("Manifest field 'images' must be either an object or a list.")

    if not isinstance(paths, list):
        raise ValueError("Manifest field 'images.paths' must be a list.")

    results: List[Dict[str, str]] = []
    for idx, entry in enumerate(paths):
        if not isinstance(entry, dict):
            raise ValueError(f"Manifest 'images[{idx}]' entry must be an object.")

        path = entry.get("path")
        version = entry.get("version")
        digest = entry.get("image_sha")

        if not isinstance(path, str) or not isinstance(version, str):
            raise ValueError(
                f"Manifest 'images[{idx}]' must contain string 'path' and 'version' fields."
            )

        if not isinstance(digest, str):
            raise ValueError(
                f"Manifest 'images[{idx}]' must contain string 'image_digest' or 'image_sha' field."
            )

        image_with_tag = f"{path}:{version}"
        results.append({"image": image_with_tag, "hash": digest})

    return results