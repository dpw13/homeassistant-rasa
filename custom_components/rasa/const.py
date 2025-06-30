"""Constants for the Rasa NLP integration."""

from typing import TYPE_CHECKING

from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .conversation import RasaAgent

DOMAIN = "rasa"
DEFAULT_TIMEOUT = 5.0  # seconds
SERVER_KEEPALIVE = 20.0

DATA_COMPONENT: HassKey[EntityComponent["RasaAgent"]] = HassKey(DOMAIN)
DATA_RASA_ENTITY: HassKey["RasaAgent"] = HassKey(f"{DOMAIN}_rasa_entity")

CONF_SERVER_URL = "server_url"
CONF_ACTION_PORT = "action_port"
