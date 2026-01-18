from __future__ import annotations

import logging
import os
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util.yaml import load_yaml

from .const import DOMAIN, MODE_ASSET, MODE_ZIPBALL, TYPE_INTEGRATION, TYPE_LOVELACE, TYPE_BLUEPRINTS
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
        errors = {}

        if user_input is not None:
            # Use hardcoded obfuscated endpoint
            base_url = get_primary_endpoint()
            token = user_input.get("token", "").strip() or None
            owner = user_input.get("owner", "").strip() or None

            # Test connection (skip auth test if no token - for public repos)
            try:
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
                _LOGGER.error("Cannot connect to endpoint: %s", e, exc_info=True)
                errors["base"] = "cannot_connect"
                ok = False

            if not errors:
                # Store configuration
                self.config_data = {
                    "base_url": base_url,
                    "token": token or "",
                    "owner": owner or "",
                }

                # Move to store selection BEFORE creating entry
                return await self.async_step_store_selection()

        schema = vol.Schema(
            {
                vol.Optional("token", default=""): str,
                vol.Optional("owner", default=""): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "note": "Configure token and owner, then select packages to install from the store."
            }
        )

    async def async_step_store_selection(self, user_input=None):
        """Show store list for package selection."""
        errors = {}

        if user_input is not None:
            # Get selected packages
            selected = user_input.get("packages", [])

            # Store pending installations in entry data
            # The actual installation will happen in async_setup_entry
            if selected:
                _LOGGER.info("Storing %d packages for installation during setup", len(selected))
                self.config_data["pending_installs"] = selected
            else:
                self.config_data["pending_installs"] = []

            # Create the entry with pending installations metadata
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
            # No packages in store, just finish setup
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
                # Use repo_owner as unique key
                key = f"{pkg.get('owner', '')}_{pkg.get('repo', '')}"
                package_options[key] = label
            except Exception as e:
                _LOGGER.warning("Skipping invalid package: %s", e)
                continue

        if not package_options:
            # No valid packages
            _LOGGER.warning("No valid packages found in store list")
            return self.async_abort(reason="no_valid_packages")

        schema = vol.Schema(
            {
                vol.Optional("packages", default=[]): cv.multi_select(package_options),
            }
        )

        return self.async_show_form(
            step_id="store_selection",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "info": f"Select packages to install from store ({len(package_options)} available). You can skip this step."
            }
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

            # Determine service name
            if pkg_type == "integration":
                service_name = "install_integration"
            elif pkg_type == "lovelace":
                service_name = "install_lovelace"
            elif pkg_type == "blueprints":
                service_name = "install_blueprints"
            else:
                _LOGGER.error("Unknown package type: %s", pkg_type)
                raise HomeAssistantError(f"Unknown package type: {pkg_type}")

            # Build service data
            service_data = {
                "owner": owner,
                "repo": repo,
            }
            if mode:
                service_data["mode"] = mode
            if asset_name:
                service_data["asset_name"] = asset_name

            # Call installation service
            try:
                await self.hass.services.async_call(
                    DOMAIN,
                    service_name,
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
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for reconfiguration and installation."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Show store list directly for package installation."""
        errors = {}
        installed_integration = False

        if user_input is not None:
            # Get selected packages
            selected = user_input.get("packages", [])

            # Install selected packages if any selected
            if selected:
                try:
                    _LOGGER.info("Installing %d packages from reconfigure", len(selected))
                    installed_integration = await self._install_packages(selected)

                    # Show restart notification if integration was installed
                    if installed_integration:
                        await self.hass.services.async_call(
                            "persistent_notification",
                            "create",
                            {
                                "title": "OnOff Store - Restart Required",
                                "message": "**Please restart Home Assistant**\n\nOne or more integrations were installed and require a restart to load.",
                                "notification_id": "onoff_store_restart_required"
                            },
                            blocking=False
                        )

                    return self.async_create_entry(title="", data={})

                except Exception as e:
                    error_msg = str(e)
                    _LOGGER.error("Failed to install packages: %s", error_msg, exc_info=True)
                    errors["base"] = "install_failed"
                    # Continue to show form with error
            else:
                # No packages selected, just close
                return self.async_create_entry(title="", data={})

        # Load store list
        try:
            packages = await self.hass.async_add_executor_job(load_store_list, self.hass)
        except Exception as e:
            _LOGGER.error("Failed to load packages: %s", e, exc_info=True)
            return self.async_abort(reason="unknown")

        if not packages:
            return self.async_abort(reason="no_packages_in_store")

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
                # Use repo_owner as unique key
                key = f"{pkg.get('owner', '')}_{pkg.get('repo', '')}"
                package_options[key] = label
            except Exception as e:
                _LOGGER.warning("Skipping invalid package: %s", e)
                continue

        if not package_options:
            return self.async_abort(reason="no_packages_in_store")

        schema = vol.Schema(
            {
                vol.Optional("packages", default=[]): cv.multi_select(package_options),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "info": f"Select packages to install from store ({len(package_options)} available)"
            }
        )

    async def _install_packages(self, selected_keys: list[str]) -> bool:
        """Install selected packages using services. Returns True if any integration was installed."""
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
            owner = pkg.get("owner", self.config_entry.data.get("owner", ""))
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

            # Determine service name
            if pkg_type == "integration":
                service_name = "install_integration"
            elif pkg_type == "lovelace":
                service_name = "install_lovelace"
            elif pkg_type == "blueprints":
                service_name = "install_blueprints"
            else:
                _LOGGER.error("Unknown package type: %s", pkg_type)
                raise HomeAssistantError(f"Unknown package type: {pkg_type}")

            # Build service data
            service_data = {
                "owner": owner,
                "repo": repo,
            }
            if mode:
                service_data["mode"] = mode
            if asset_name:
                service_data["asset_name"] = asset_name

            # Call installation service
            try:
                await self.hass.services.async_call(
                    DOMAIN,
                    service_name,
                    service_data,
                    blocking=True
                )
                _LOGGER.info("✓ Installed: %s/%s", owner, repo)
            except Exception as e:
                error_msg = str(e)
                _LOGGER.error("Failed to install %s/%s: %s", owner, repo, error_msg, exc_info=True)
                raise HomeAssistantError(f"Failed to install {owner}/{repo}: {error_msg}") from e

        return installed_integration
