import json
import logging
from typing import Any, Dict, List, Optional

import requests, warnings

logger = logging.getLogger("cloudera_ps")

# Suppress the specific warning message
warnings.filterwarnings("ignore", message="Unverified HTTPS request is being made to host")

class EmbeddedRegistryUtil:
    _CATALOG = f"/v2/_catalog?n=$page"
    _IMAGETAG = f"/v2/$image/tags/list"
    _MANIFEST = f"/v2/$image/manifests/$tag"

    def __init__(self, registry_url: str, registry_user: str, registry_password: str):
        self._registry_url = registry_url
        self._registry_user = registry_user
        self._registry_password = registry_password

        # Validate credentials immediately by performing an authenticated GET.
        try:
            response = requests.get(
                f"{self._registry_url}/v2/",
                auth=(self._registry_user, self._registry_password),
                timeout=30,
                verify=False,
            )
        except requests.RequestException as exc:
            raise ValueError(f"Failed to connect to registry at '{self._registry_url}': {exc}") from exc

        # Explicitly treat authentication errors as failures, even if not raised above.
        if response.status_code in (401, 403):
            raise ValueError(
                f"Authentication failed for registry at '{self._registry_url}' "
                f"with user '{self._registry_user}'. HTTP {response.status_code}"
            )

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise ValueError(
                f"Registry at '{self._registry_url}' is not reachable or returned an error: {exc}"
            ) from exc


    def _get_tags(self, imagepath: str) -> List[str]:
        """
        Get all available tags for an image from the Docker Registry.
        """
        endpoint = self._IMAGETAG.replace("$image", imagepath.strip("/"))
        url = f"{self._registry_url}{endpoint}"

        try:
            response = requests.get(
                url,
                auth=(self._registry_user, self._registry_password),
                timeout=30,
                verify=False,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ValueError(f"Failed to get tags for image '{imagepath}': {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Registry returned a non-JSON response for tags request.") from exc

        if not isinstance(payload, dict):
            raise ValueError("Registry tags response is not a JSON object.")

        tags = payload.get("tags") or []
        if not isinstance(tags, list):
            raise ValueError("Registry tags response has invalid 'tags' format.")

        normalized_tags = [tag for tag in tags if isinstance(tag, str)]
        return normalized_tags


    def list_images(self) -> List[Dict[str, List[str]]]:
        """
        List all repositories from the Docker Registry catalog endpoint.

        Handles pagination by following RFC5988 Link headers until no "next"
        page is present.
        """
        img_list: List[str] = []
        seen_repositories = set()

        page = 1000
        next_url = f"{self._registry_url}{self._CATALOG}".replace("$page", str(page))

        while next_url:
            try:
                response = requests.get(
                    next_url,
                    auth=(self._registry_user, self._registry_password),
                    timeout=30,
                    verify=False,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                raise ValueError(f"Failed to list images from registry: {exc}") from exc

            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError("Registry returned a non-JSON response for catalog request.") from exc

            if not isinstance(payload, dict):
                raise ValueError("Registry catalog response is not a JSON object.")

            page_repos = payload.get("repositories", [])
            if not isinstance(page_repos, list):
                raise ValueError("Registry catalog response has invalid 'repositories' format.")

            for repo in page_repos:
                if isinstance(repo, str) and repo not in seen_repositories:
                    seen_repositories.add(repo)
                    img_list.append(repo)

            # requests parses RFC5988 Link header into response.links
            next_link = response.links.get("next", {}).get("url")
            if next_link and next_link.startswith("/"):
                next_link = f"{self._registry_url}{next_link}"
            next_url = next_link

        repositories = []
        for i in img_list:
            try:
                tags = self._get_tags(i)
            except Exception as e:
                logger.error("Failed to get tags for image '%s': %s", i, e)
                tags = []
            latest_tag = max(tags) if tags else "none"
            logger.debug(
                "Retrieved image '%s' with %d tag(s), latest tag: %s",
                i,
                len(tags),
                latest_tag,
            )
            repositories.append({"image": i, "tags": tags})

        return repositories

    def list_images_mock(self) -> List[Dict[str, List[str]]]:
        repo = json.load(open("test.json", "r"))
        import time
        time.sleep(10)
        return repo


if __name__ == '__main__':
    reg = EmbeddedRegistryUtil("https://54.255.111.147:5000", "registry-user", "cloudera")
    result = reg.list_images_mock()
    #result = reg._get_tags("cloudera_thirdparty/longhornio/longhorn-instance-manager")
    print(result)