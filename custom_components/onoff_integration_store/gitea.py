from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)


class GiteaClient:
    def __init__(self, hass: HomeAssistant, base_url: str, token: str = None):
        self.hass = hass
        self.base_url = base_url.rstrip("/")
        self.token = token or None

    def _headers(self) -> dict:
        """Get headers - with or without auth token."""
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return headers

    async def test_auth(self) -> bool:
        """Test authentication - returns True if no token (public access)."""
        if not self.token:
            _LOGGER.info("No token - assuming public access")
            return True

        try:
            sess = async_get_clientsession(self.hass)
            url = f"{self.base_url}/api/v1/user"
            async with sess.get(url, headers=self._headers(), timeout=20) as resp:
                return resp.status == 200
        except Exception as e:
            _LOGGER.debug("Auth test failed: %s", e)
            return False

    async def get_repo(self, owner: str, repo: str) -> dict:
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/repos/{owner}/{repo}"
        async with sess.get(url, headers=self._headers(), timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Repo fetch failed: {resp.status} {await resp.text()}")
            return await resp.json()

    async def get_latest_release(self, owner: str, repo: str) -> dict:
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/repos/{owner}/{repo}/releases/latest"
        async with sess.get(url, headers=self._headers(), timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Latest release fetch failed: {resp.status} {await resp.text()}")
            return await resp.json()

    async def get_release_by_tag(self, owner: str, repo: str, tag: str) -> dict:
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/repos/{owner}/{repo}/releases/tags/{tag}"
        async with sess.get(url, headers=self._headers(), timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Release-by-tag fetch failed: {resp.status} {await resp.text()}")
            return await resp.json()

    def pick_asset(self, release: dict, asset_name: str | None = None) -> dict:
        assets = release.get("assets") or []
        if not assets:
            raise RuntimeError("Release has no assets. Attach a ZIP asset to the release, or use mode=zipball.")

        if asset_name:
            for a in assets:
                if a.get("name") == asset_name:
                    return a
            raise RuntimeError(f"Asset '{asset_name}' not found in release assets.")

        # Prefer a single .zip
        zips = [a for a in assets if (a.get("name") or "").lower().endswith(".zip")]
        if len(zips) == 1:
            return zips[0]

        if len(assets) == 1:
            return assets[0]

        raise RuntimeError("Multiple assets found. Specify asset_name.")

    def archive_zip_url(self, owner: str, repo: str, ref: str) -> str:
        # Gitea archive endpoint (zip of repo at ref)
        # Example: /api/v1/repos/:owner/:repo/archive/:ref.zip
        return f"{self.base_url}/api/v1/repos/{owner}/{repo}/archive/{ref}.zip"
