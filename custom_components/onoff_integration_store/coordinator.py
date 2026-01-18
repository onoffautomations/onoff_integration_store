"""Coordinator for OnOff Integration Store package tracking."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    STORAGE_KEY_PACKAGES,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class OnOffGiteaStoreCoordinator(DataUpdateCoordinator):
    """Coordinator to manage package tracking and updates."""

    def __init__(self, hass: HomeAssistant, entry_id: str, client) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
        )
        self.hass = hass
        self.entry_id = entry_id
        self.client = client
        self.packages: dict[str, dict[str, Any]] = {}
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_PACKAGES)
        self._add_entities_callback = None  # Will be set by sensor platform
        self._add_button_entities_callback = None  # Will be set by button platform
        # Don't override _listeners - parent class handles it

    async def async_load_packages(self) -> None:
        """Load tracked packages from storage."""
        _LOGGER.info("Loading tracked packages...")
        data = await self._store.async_load()

        if data:
            self.packages = data.get("packages", {})
            _LOGGER.info("Loaded %d tracked packages", len(self.packages))
        else:
            self.packages = {}
            _LOGGER.info("No tracked packages found")

    async def async_save_packages(self) -> None:
        """Save tracked packages to storage."""
        _LOGGER.info("Saving %d tracked packages...", len(self.packages))
        await self._store.async_save({"packages": self.packages})
        _LOGGER.info("✓ Packages saved")

    async def async_add_or_update_package(
        self,
        repo_name: str,
        owner: str,
        package_type: str,
        installed_version: str,
        mode: str = None,
        asset_name: str = None,
    ) -> str:
        """Add or update a tracked package."""
        package_id = f"{owner}_{repo_name}".lower().replace("-", "_")

        is_new_package = package_id not in self.packages

        _LOGGER.info("Adding/updating package: %s (new: %s)", package_id, is_new_package)

        # Get existing data to preserve some fields
        existing_data = self.packages.get(package_id, {})

        package_data = {
            "repo_name": repo_name,
            "owner": owner,
            "package_type": package_type,
            "installed_version": installed_version,
            "latest_version": installed_version,  # When installing, latest = installed
            "update_available": False,  # Just installed, so no update available
            "install_date": existing_data.get("install_date", datetime.now().isoformat()),
            "last_update": datetime.now().isoformat(),
            "last_check": existing_data.get("last_check"),  # Preserve last check time
            "mode": mode,
            "asset_name": asset_name,
        }

        _LOGGER.info("Package data for %s: installed=%s, latest=%s, update_available=%s",
                    package_id, installed_version, installed_version, False)

        self.packages[package_id] = package_data
        await self.async_save_packages()

        _LOGGER.info("✓ Package %s tracked", package_id)

        # If this is a new package and we have the callback, create sensors immediately
        if is_new_package and self._add_entities_callback:
            _LOGGER.info("Creating sensors for new package: %s", package_id)
            await self._create_sensors_for_package(package_id, package_data)
        else:
            # If updating existing package, notify sensors to refresh
            _LOGGER.info("Notifying sensors to update for: %s", package_id)
            self.async_update_listeners()

            # Update device registry with new version
            from homeassistant.helpers import device_registry as dr
            device_registry = dr.async_get(self.hass)
            device = device_registry.async_get_device(identifiers={(DOMAIN, package_id)})
            if device:
                device_registry.async_update_device(
                    device.id,
                    sw_version=installed_version
                )
                _LOGGER.info("✓ Updated device registry sw_version to %s", installed_version)

        return package_id

    async def _create_sensors_for_package(self, package_id: str, package_data: dict) -> None:
        """Create sensors and button for a package dynamically."""
        # Create sensors
        if self._add_entities_callback:
            try:
                # Import here to avoid circular import
                from .sensor import (
                    PackageVersionSensor,
                    PackageUpdateSensor,
                    PackageTypeSensor,
                    WaitingRestartSensor,
                )

                new_sensors = [
                    PackageVersionSensor(self, package_id, package_data),
                    PackageUpdateSensor(self, package_id, package_data),
                    PackageTypeSensor(self, package_id, package_data),
                ]

                # Add restart sensor only for integrations
                if package_data.get('package_type') == 'integration':
                    new_sensors.append(WaitingRestartSensor(self, package_id, package_data, self.hass))

                self._add_entities_callback(new_sensors)
                _LOGGER.info("✓ Created %d sensors for %s", len(new_sensors), package_data["repo_name"])

            except Exception as e:
                _LOGGER.error("Failed to create sensors for %s: %s", package_id, e, exc_info=True)
        else:
            _LOGGER.warning("Cannot create sensors - no callback registered")

        # Create button
        if self._add_button_entities_callback:
            try:
                # Import here to avoid circular import
                from .button import PackageUpdateButton

                # Get entry_id from hass data
                entry_id = self.entry_id
                entry = None
                for config_entry_id, data in self.hass.data.get(DOMAIN, {}).items():
                    if data.get("coordinator") == self:
                        entry = self.hass.config_entries.async_get_entry(config_entry_id)
                        break

                if entry:
                    new_button = [PackageUpdateButton(self, package_id, package_data, entry)]
                    self._add_button_entities_callback(new_button)
                    _LOGGER.info("✓ Created update button for %s", package_data["repo_name"])
                else:
                    _LOGGER.warning("Could not find config entry for button creation")

            except Exception as e:
                _LOGGER.error("Failed to create button for %s: %s", package_id, e, exc_info=True)
        else:
            _LOGGER.debug("Button callback not registered yet")

    async def async_check_updates(self, now=None) -> None:
        """Check for updates for all tracked packages."""
        if not self.packages:
            _LOGGER.info("No packages tracked yet, skipping update check")
            return

        _LOGGER.info("Checking for updates for %d packages...", len(self.packages))

        for package_id, package_data in self.packages.items():
            try:
                owner = package_data["owner"]
                repo = package_data["repo_name"]
                installed_version = package_data["installed_version"]

                _LOGGER.debug("Checking %s/%s (installed: %s)", owner, repo, installed_version)

                # Get latest release
                latest_release = await self.client.get_latest_release(owner, repo)

                if latest_release:
                    latest_version = latest_release.get("tag_name", "unknown")
                    _LOGGER.debug("Latest version: %s", latest_version)

                    # Check if update available
                    update_available = latest_version != installed_version

                    # Update package data
                    package_data["latest_version"] = latest_version
                    package_data["update_available"] = update_available
                    package_data["last_check"] = datetime.now().isoformat()

                    if update_available:
                        _LOGGER.info("✓ Update available for %s: %s → %s",
                                   repo, installed_version, latest_version)
                    else:
                        _LOGGER.debug("No update available for %s", repo)
                else:
                    # No release found
                    _LOGGER.debug("No releases found for %s/%s", owner, repo)
                    package_data["last_check"] = datetime.now().isoformat()

            except Exception as e:
                error_str = str(e)
                # Check if it's a 404 (repo doesn't exist or no access)
                if "404" in error_str or "not found" in error_str.lower():
                    _LOGGER.warning("Package %s/%s not found (404). Check if owner/repo is correct or if you have access.", owner, repo)
                else:
                    _LOGGER.error("Error checking updates for %s: %s", package_id, e)
                # Mark as checked even on error to avoid repeated errors
                package_data["last_check"] = datetime.now().isoformat()

        # Save updated data
        await self.async_save_packages()

        # Notify sensors to update
        self.async_update_listeners()

        _LOGGER.info("✓ Update check complete")

    async def async_get_package_info(self, package_id: str) -> dict[str, Any] | None:
        """Get package information by ID."""
        return self.packages.get(package_id)

    def get_package_by_repo(self, owner: str, repo_name: str) -> dict[str, Any] | None:
        """Get package information by owner and repo name."""
        package_id = f"{owner}_{repo_name}".lower().replace("-", "_")
        return self.packages.get(package_id)
