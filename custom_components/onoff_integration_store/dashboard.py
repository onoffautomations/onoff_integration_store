"""Dashboard for OnOff Integration Store - V4 Robust."""
from __future__ import annotations

import logging
import os
import time
from aiohttp import web

from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components import frontend

from .const import DOMAIN, CONF_SIDE_PANEL, SERVICE_INSTALL
from .config_flow import load_store_list
from .installer import uninstall_package

_LOGGER = logging.getLogger(__name__)

URL_BASE = "/onoff_store_static"


async def async_setup_dashboard(hass: HomeAssistant, entry) -> None:
    """Set up the store dashboard."""
    static_dir = os.path.join(os.path.dirname(__file__), "dashboard_static")
    if not os.path.exists(static_dir):
        os.makedirs(static_dir, exist_ok=True)

    await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, static_dir, False)])

    if entry.data.get(CONF_SIDE_PANEL, True):
        # Sidebar entry for Admin users - Cache buster added
        # Check if panel already registered to avoid error on reload
        if "onoff_store" not in hass.data.get("frontend_panels", {}):
            try:
                frontend.async_register_built_in_panel(
                    hass,
                    component_name="iframe",
                    sidebar_title="OnOff Store",
                    sidebar_icon="mdi:storefront",
                    frontend_url_path="onoff_store",
                    config={"url": f"{URL_BASE}/index.html?v={int(time.time())}"},
                    require_admin=True,
                )
            except ValueError as e:
                # Panel already exists, this is fine
                _LOGGER.debug("Panel already registered: %s", e)

    # Register API views
    eid = entry.entry_id
    hass.http.register_view(OnOffStoreReposView(eid))
    hass.http.register_view(OnOffStoreInstallView(eid))
    hass.http.register_view(OnOffStoreReadmeView(eid))
    hass.http.register_view(OnOffStoreRefreshView(eid))
    hass.http.register_view(OnOffStoreAddCustomView(eid))
    hass.http.register_view(OnOffStoreHideView(eid))
    hass.http.register_view(OnOffStoreUnhideView(eid))
    hass.http.register_view(OnOffStoreUninstallView(eid))


class OnOffStoreReposView(HomeAssistantView):
    """API to list Gitea repositories."""
    url = "/api/onoff_store/repos"
    name = "api:onoff_store:repos"
    requires_auth = False 

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app.get("hass")
        try:
            # Dynamically find the entry if possible
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            
            if eid not in hass.data[DOMAIN]:
                # Try to find any existing entry (handles reload/reinstall cases)
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            comp = hass.data[DOMAIN][eid]
            client = comp["client"]
            coordinator = comp["coordinator"]

            # Check if we have a valid authenticated session
            is_authenticated = False
            if client.token:
                is_authenticated = await client.test_auth()
                if not is_authenticated:
                    _LOGGER.warning("Gitea token provided but authentication failed (expired or revoked). Using public access.")

            # A. Load from store_list.yaml first (Ensures these are ALWAYS visible)
            yaml_items = await hass.async_add_executor_job(load_store_list, hass)
            
            # 1. Fetch custom repos FIRST to ensure they bypass filters
            custom_repos_to_fetch = coordinator.custom_repos
            
            resp_data = []
            
            for y in yaml_items:
                try:
                    # Fetch full info from Gitea for the YAML item
                    r = await client.get_repo(y["owner"], y["repo"])
                    if r: self._fill(resp_data, r, coordinator, yaml_items=yaml_items, bypass_filter=True, is_authenticated=is_authenticated)
                except Exception:
                    pass

            # B. Fetch from Organizations
            # Default public orgs (always fetched)
            default_orgs = ["Zing", "OnOffPublic"]

            # If authenticated, also fetch from all orgs they have access to
            orgs_to_fetch = set(default_orgs)
            if is_authenticated:
                try:
                    user_orgs = await client.get_user_orgs()
                    for org in user_orgs:
                        org_name = org.get("username") or org.get("name")
                        if org_name:
                            orgs_to_fetch.add(org_name)
                    _LOGGER.debug("Fetching repos from %d organizations", len(orgs_to_fetch))
                except Exception as e:
                    _LOGGER.debug("Failed to fetch user orgs: %s", e)

            for o in orgs_to_fetch:
                try:
                    repos = await client.get_org_repos(o)
                    if isinstance(repos, list):
                        for r in repos:
                            self._fill(resp_data, r, coordinator, yaml_items=yaml_items)
                except Exception as e:
                    _LOGGER.debug("Store: Org %s error: %s", o, e)

            if is_authenticated:
                try:
                    # Fetch user's own repositories
                    sess = async_get_clientsession(hass)
                    async with sess.get(f"{client.base_url}/api/v1/user/repos", headers=client._headers()) as r:
                        if r.status == 200:
                            u_repos = await r.json()
                            if isinstance(u_repos, list):
                                for r in u_repos:
                                    self._fill(resp_data, r, coordinator, yaml_items=yaml_items, bypass_filter=True, is_authenticated=True)
                except Exception:
                    pass

            # 3. Explicitly fetch custom repos if they weren't in organizations
            for cr in custom_repos_to_fetch:
                if not any(x["owner"].lower() == cr["owner"].lower() and x["repo_name"].lower() == cr["repo"].lower() for x in resp_data):
                    try:
                        r = await client.get_repo(cr["owner"], cr["repo"])
                        if r: self._fill(resp_data, r, coordinator, yaml_items=yaml_items, bypass_filter=True, is_authenticated=is_authenticated)
                    except Exception:
                        pass

            return web.json_response(resp_data)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    def _fill(self, data_list, r, coord, yaml_items=None, bypass_filter=False, is_authenticated=False):
        name = r.get("full_name")
        if not name or any(x["name"] == name for x in data_list):
            return
        
        owner = r.get("owner", {}).get("login", "Unknown")
        rn = r.get("name", "Unknown")

        # Skip if MANUALLY hidden (unless we are showing hidden ones - logic will be in UI)
        is_hidden = coord.is_hidden_repo(owner, rn)

        # Hide 'x-' repos UNLESS they are in our custom_repos database OR bypassed
        # Allow x- repos if we have a valid authenticated session
        if not bypass_filter and not is_authenticated and rn.lower().startswith("x-") and not coord.is_custom_repo(owner, rn):
            return

        p = coord.get_package_by_repo(owner, rn)
        
        # Determine default mode/asset from YAML if present
        y_mode = None
        y_asset = None
        if yaml_items:
            y_pkg = next((y for y in yaml_items if y.get("owner") == owner and y.get("repo") == rn), None)
            if y_pkg:
                y_mode = y_pkg.get("mode")
                y_asset = y_pkg.get("asset_name")

        # Better type detection
        pkg_type = "integration"
        desc = (r.get("description") or "").lower()
        if p:
            pkg_type = p.get("package_type", "integration")
        elif "card" in rn.lower() or "lovelace" in rn.lower() or "card" in desc or "theme" in desc:
            pkg_type = "lovelace"
        elif "blueprint" in rn.lower() or "blueprint" in desc:
            pkg_type = "blueprints"

        data_list.append({
            "name": name,
            "repo_name": rn,
            "owner": owner,
            "type": pkg_type,
            "description": r.get("description") or "",
            "updated_at": r.get("updated_at", ""),
            "mode": p.get("mode") if p else y_mode,
            "asset_name": p.get("asset_name") if p else y_asset,
            "is_installed": p is not None,
            "update_available": p.get("update_available", False) if p else False,
            "latest_version": p.get("latest_version") if p else None,
            "release_notes": p.get("release_notes") if p else None,
            "is_hidden": is_hidden,
        })


class OnOffStoreInstallView(HomeAssistantView):
    """API to install integration."""
    url = "/api/onoff_store/install"
    name = "api:onoff_store:install"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids: return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            body = await request.json()
            o, r, t = body.get("owner"), body.get("repo"), body.get("type", "integration")
            mode = body.get("mode")
            asset_name = body.get("asset_name")
            
            if not o or not r:
                return web.json_response({"error": "Missing params"}, status=400)
            
            # Using the unified 'install' service
            svc_data = {
                "owner": o, 
                "repo": r,
                "type": t
            }
            if mode: svc_data["mode"] = mode
            if asset_name: svc_data["asset_name"] = asset_name

            await hass.services.async_call(DOMAIN, SERVICE_INSTALL, svc_data)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreReadmeView(HomeAssistantView):
    """API to fetch README."""
    url = "/api/onoff_store/readme/{owner}/{repo}"
    name = "api:onoff_store:readme"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.Response(text="Not ready", status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.Response(text="Not ready", status=503)

            client = hass.data[DOMAIN][eid].get("client")
            txt = await client.get_readme(owner, repo)
            return web.Response(text=txt or "No README found for this repository.", content_type="text/markdown")
        except Exception:
            return web.Response(text="Error", status=500)


class OnOffStoreRefreshView(HomeAssistantView):
    """API to trigger update check."""
    url = "/api/onoff_store/refresh"
    name = "api:onoff_store:refresh"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids: return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                await coordinator.async_check_updates()
                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreAddCustomView(HomeAssistantView):
    """API to add a custom repository."""
    url = "/api/onoff_store/custom/add"
    name = "api:onoff_store:custom:add"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await request.json()
            o, r = body.get("owner"), body.get("repo")
            if not o or not r:
                return web.json_response({"error": "Missing params"}, status=400)
            
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.json_response({"error": "Not ready"}, status=503)

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                await coordinator.async_add_custom_repo(o, r)
                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreHideView(HomeAssistantView):
    """API to hide a repository."""
    url = "/api/onoff_store/hide"
    name = "api:onoff_store:hide"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await request.json()
            o, r = body.get("owner"), body.get("repo")
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.json_response({"error": "Not ready"}, status=503)

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                await coordinator.async_hide_repo(o, r)
                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreUnhideView(HomeAssistantView):
    """API to unhide a repository."""
    url = "/api/onoff_store/unhide"
    name = "api:onoff_store:unhide"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await request.json()
            o, r = body.get("owner"), body.get("repo")
            
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.json_response({"error": "Not ready"}, status=503)

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                await coordinator.async_unhide_repo(o, r)
                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreUninstallView(HomeAssistantView):
    """API to uninstall a repository."""
    url = "/api/onoff_store/uninstall"
    name = "api:onoff_store:uninstall"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await request.json()
            o, r, t = body.get("owner"), body.get("repo"), body.get("type", "integration")
            if not o or not r:
                return web.json_response({"error": "Missing params"}, status=400)
            
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.json_response({"error": "Not ready"}, status=503)

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                # 1. Delete folder
                await hass.async_add_executor_job(uninstall_package, hass, t, r)
                # 2. Remove tracking
                await coordinator.async_remove_package(o, r)
                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
