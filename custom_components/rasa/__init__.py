"""The Rasa NLP integration."""

# pylint: disable=fixme

from __future__ import annotations

import logging

from aiohttp.client_exceptions import ClientError
import rasa_client
from rasa_client.rest import ApiException

from homeassistant import core
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryError

from .const import CONF_SERVER_URL, DEFAULT_TIMEOUT, DOMAIN

# List the platforms that you want to support.
_PLATFORMS: list[Platform] = [Platform.CONVERSATION]
_LOGGER = logging.getLogger(__name__)

type RasaConfigEntry = ConfigEntry[rasa_client.ApiClient]


# Copied from homeassistant/components/ollama/__init__.py
async def async_setup_entry(hass: core.HomeAssistant, entry: RasaConfigEntry) -> bool:
    """Set up Rasa server connection from a config entry."""
    server_config = rasa_client.Configuration(
        host=entry.data.get(CONF_SERVER_URL),
    )
    # ApiClient creates the RESTClientObject which creates the urllib3.PoolManager, so
    # this is the primary instantiation that we need. Creating individual API accessors
    # under `client` is trivial and does not produce additional connections to the server.

    # ApiClient init will call both load_default_certs and set_default_verify_paths, both
    # of which need to be run by the executor.
    client = await hass.async_add_executor_job(rasa_client.ApiClient, server_config)
    entry.runtime_data = client

    info = rasa_client.ServerInformationApi(client)
    try:
        # The first time this is called produces the following warning:
        # Detected blocking call to open with args ('/home/vscode/.netrc',) inside the event loop
        # by integration 'rasa' at homeassistant/components/rasa/__init__.py, line 43: rsp = await
        # info.get_version(DEFAULT_TIMEOUT) (offender: /usr/local/lib/python3.13/netrc.py, line
        # 74: with open(file, encoding="utf-8") as fp:)
        # Unfortunately I don't see an easy way to avoid this as the aiohttp code is attempting to
        # check the .netrc file for proxy configuration. The load is specifically done by adding
        # the check to a separate thread in aiohttp/client.py.
        # I don't understand python async and threading interactions well enough to start to debug
        # this so, tolerate the warning for now.
        rsp = await info.get_version(DEFAULT_TIMEOUT)
        _LOGGER.info("Connected to server version %s", rsp.version)
    except (ApiException, ClientError) as err:
        raise ConfigEntryError(err) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: core.HomeAssistant, entry: RasaConfigEntry) -> bool:
    """Unload Rasa."""
    if not await hass.config_entries.async_unload_platforms(entry, _PLATFORMS):
        return False
    hass.data[DOMAIN].pop(entry.entry_id)
    return True
