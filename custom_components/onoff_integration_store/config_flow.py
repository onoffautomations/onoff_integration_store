from __future__ import annotations

import logging
import os
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util.yaml import load_yaml

from .const import DOMAIN, MODE_ASSET, MODE_ZIPBALL, TYPE_INTEGRATION, TYPE_LOVELACE, TYPE_BLUEPRINTS, CONF_SIDE_PANEL
from .gitea import GiteaClient
from ._utils import get_primary_endpoint

_LOGGER = logging.getLogger(__name__)


def load_store_list(hass: HomeAssistant) -> list[dict]:
    """Load store list from YAML file."""
    try:
        # Get the integration directory
        integration_dir = os.path.dirname(__file__)
        store_list_path = os.path.join(integration_dir, "store_list.yaml")

        if not os.path.exists(store_list_path):
            _LOGGER.warning("Store list file not found: %s", store_list_path)
            return []

        # Use Home Assistant's YAML loader
        data = load_yaml(store_list_path)

        packages = data.get('packages', []) if data else []

        # Filter out empty or None packages
        packages = [p for p in packages if p and isinstance(p, dict)]

        _LOGGER.info("Loaded %d packages from store list", len(packages))
        return packages

    except Exception as e:
        _LOGGER.error("Failed to load store list: %s", e, exc_info=True)
        return []


class OnOffGiteaStoreConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        """Initialize config flow."""
        self.config_data = {}
        self.entry_id = None

    async def async_step_user(self, user_input=None):
        """Handle initial configuration."""
        _LOGGER.debug("async_step_user called with input: %s", user_input)
        errors = {}

        # Only allow one instance
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            # Use hardcoded obfuscated endpoint
            base_url = get_primary_endpoint()
            token = user_input.get("token", "").strip() or None
            owner = user_input.get("owner", "").strip() or None

            # Test connection (skip auth test if no token - for public repos)
            try:
                _LOGGER.debug("Testing Gitea client... %s", base_url)
                client = GiteaClient(self.hass, base_url=base_url, token=token)

                if token:
                    # Test with auth
                    ok = await client.test_auth()
                    if not ok:
                        errors["token"] = "invalid_auth"
                else:
                    # Just test base URL is reachable (public access)
                    ok = True  # Assume OK for public repos
                    _LOGGER.info("No token provided - will access public repos only")

            except Exception as e:
                _LOGGER.error("Cannot connect to endpoint %s: %s", base_url, e, exc_info=True)
                errors["base"] = "cannot_connect"
                ok = False

            if not errors:
                # Store configuration
                self.config_data = {
                    "base_url": base_url,
                    "token": token or "",
                    "owner": owner or "",
                    CONF_SIDE_PANEL: user_input.get(CONF_SIDE_PANEL, True),
                }

                # Move to store selection BEFORE creating entry
                _LOGGER.debug("Moving to store selection...")
                return await self.async_step_store_selection()

        schema = vol.Schema(
            {
                vol.Optional("token", default=""): str,
                vol.Optional("owner", default=""): str,
                vol.Optional(CONF_SIDE_PANEL, default=True): bool,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        """Handle reconfiguration of the integration."""
        _LOGGER.debug("async_step_reconfigure called")

        # Get the config entry being reconfigured
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if not entry:
            return self.async_abort(reason="cannot_reconfigure")

        if user_input is not None:
            token = user_input.get("token", "").strip() or None
            owner = user_input.get("owner", "").strip() or None
            base_url = entry.data.get("base_url", get_primary_endpoint())

            # Test connection if token provided
            if token:
                try:
                    client = GiteaClient(self.hass, base_url=base_url, token=token)
                    if not await client.test_auth():
                        return self.async_show_form(
                            step_id="reconfigure",
                            data_schema=self._get_reconfigure_schema(entry),
                            errors={"token": "invalid_auth"},
                        )
                except Exception as e:
                    _LOGGER.error("Auth test failed: %s", e)
                    return self.async_show_form(
                        step_id="reconfigure",
                        data_schema=self._get_reconfigure_schema(entry),
                        errors={"base": "cannot_connect"},
                    )

            # Update the entry
            new_data = {**entry.data}
            new_data["token"] = token or ""
            new_data["owner"] = owner or ""
            new_data[CONF_SIDE_PANEL] = user_input.get(CONF_SIDE_PANEL, True)

            self.hass.config_entries.async_update_entry(entry, data=new_data)

            # Update runtime data if available
            if DOMAIN in self.hass.data and entry.entry_id in self.hass.data[DOMAIN]:
                runtime = self.hass.data[DOMAIN][entry.entry_id]
                if "client" in runtime:
                    runtime["client"].token = token
                    runtime["client"]._token_valid = True if token else False
                if "default_owner" in runtime:
                    runtime["default_owner"] = owner
                runtime["headers"] = {"Accept": "application/json"}
                if token:
                    runtime["headers"]["Authorization"] = f"token {token}"

            return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._get_reconfigure_schema(entry),
        )

    def _get_reconfigure_schema(self, entry):
        """Get schema for reconfigure form."""
        return vol.Schema(
            {
                vol.Optional("token", default=entry.data.get("token", "")): str,
                vol.Optional("owner", default=entry.data.get("owner", "")): str,
                vol.Optional(CONF_SIDE_PANEL, default=entry.data.get(CONF_SIDE_PANEL, True)): bool,
            }
        )

    async def async_step_store_selection(self, user_input=None):
        """Show store list for package selection."""
        errors = {}

        if user_input is not None:
            # Create the entry with pending installations metadata
            selected = user_input.get("packages", [])
            self.config_data["pending_installs"] = selected
            return self.async_create_entry(
                title="OnOff Store",
                data=self.config_data,
            )

        return await self._show_store_form(errors)

    async def _show_store_form(self, errors=None):
        """Show the store selection form."""
        if errors is None:
            errors = {}

        # Load store list
        try:
            packages = await self.hass.async_add_executor_job(load_store_list, self.hass)
        except Exception as e:
            _LOGGER.error("Failed to load store list: %s", e, exc_info=True)
            packages = []

        if not packages:
            _LOGGER.info("Store list is empty, finishing setup")
            return self.async_abort(reason="no_packages")

        # Create options for multi-select
        package_options = {}
        for pkg in packages:
            try:
                name = pkg.get("name", "Unknown")
                pkg_type = pkg.get("type", "unknown")
                desc = pkg.get("description", "")
                label = f"{name} ({pkg_type})"
                if desc:
                    label = f"{label} - {desc}"
                key = f"{pkg.get('owner', '')}_{pkg.get('repo', '')}"
                package_options[key] = label
            except Exception as e:
                _LOGGER.warning("Skipping invalid package: %s", e)
                continue

        schema = vol.Schema(
            {
                vol.Optional("packages", default=[]): cv.multi_select(package_options),
            }
        )

        return self.async_show_form(
            step_id="store_selection",
            data_schema=schema,
            errors=errors,
        )

    async def _install_packages_via_services(self, selected_keys: list[str]) -> bool:
        """Install selected packages via services. Returns True if any integration was installed."""
        packages = await self.hass.async_add_executor_job(load_store_list, self.hass)
        installed_integration = False

        for key in selected_keys:
            # Find package by key
            pkg = None
            for p in packages:
                pkg_key = f"{p.get('owner', '')}_{p.get('repo', '')}"
                if pkg_key == key:
                    pkg = p
                    break

            if not pkg:
                _LOGGER.error("Package not found for key: %s", key)
                continue

            repo = pkg.get("repo")
            owner = pkg.get("owner", self.config_data.get("owner", ""))
            pkg_type = pkg.get("type", "integration")
            mode = pkg.get("mode")
            asset_name = pkg.get("asset_name")

            if not repo or not owner:
                _LOGGER.error("Invalid package data: %s", pkg)
                raise HomeAssistantError(f"Invalid package: {pkg.get('name', 'unknown')}")

            # Track if integration was installed
            if pkg_type == "integration":
                installed_integration = True

            _LOGGER.info("Installing package: %s/%s (type: %s)", owner, repo, pkg_type)

            # Build service data for unified Generic Install
            service_data = {
                "owner": owner,
                "repo": repo,
                "type": pkg_type,
            }
            if mode:
                service_data["mode"] = mode
            if asset_name:
                service_data["asset_name"] = asset_name

            # Call installation service
            try:
                await self.hass.services.async_call(
                    DOMAIN,
                    "install",
                    service_data,
                    blocking=True
                )
                _LOGGER.info("✓ Installed and tracked: %s/%s", owner, repo)
            except Exception as e:
                error_msg = str(e)
                _LOGGER.error("Failed to install %s/%s: %s", owner, repo, error_msg, exc_info=True)
                raise HomeAssistantError(f"Failed to install {owner}/{repo}: {error_msg}") from e

        return installed_integration

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for reconfiguration and installation."""

    # No __init__ needed - self.config_entry is set automatically by Home Assistant

    async def async_step_init(self, user_input=None):
        """Manage integration settings."""
        errors = {}
        entry_data = self.config_entry.data
        
        if user_input is not None:
            # Update basic settings
            token = user_input.get("token", "").strip() or ""
            owner = user_input.get("owner", "").strip() or ""
            side_panel = user_input.get(CONF_SIDE_PANEL, True)
            
            new_data = dict(entry_data)
            new_data["token"] = token
            new_data["owner"] = owner
            new_data[CONF_SIDE_PANEL] = side_panel
            
            # Update the entry
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Optional("token", default=entry_data.get("token", "")): str,
                vol.Optional("owner", default=entry_data.get("owner", "")): str,
                vol.Optional(CONF_SIDE_PANEL, default=entry_data.get(CONF_SIDE_PANEL, True)): bool,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
