"""Config flow for the Rasa NLP integration."""

# pylint: disable=fixme

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
import logging
from operator import attrgetter
from typing import Any

from rasa_sdk.endpoint import create_app, load_tracer_provider
from rasa_sdk.executor import ActionExecutor
from rasa_sdk.plugin import plugin_manager
from sanic.worker.loader import AppLoader

from homeassistant import core
from homeassistant.components.homeassistant import async_should_expose
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)

from . import actions
from .const import SERVER_KEEPALIVE

_LOGGER = logging.getLogger(__name__)


INTERESTING_ATTRIBUTES = {
    "temperature",
    "current_temperature",
    "temperature_unit",
    "brightness",
    "humidity",
    "unit_of_measurement",
    "device_class",
    "current_position",
    "percentage",
    "volume_level",
    "media_title",
    "media_artist",
    "media_album_name",
}


async def _get_areas(
    hass: core.HomeAssistant, area_ids: Iterable[Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retrieve all areas and floors given the area IDs.

    Floors contain a list of area IDs they include.
    """
    area_registry = ar.async_get(hass)
    floor_reg = fr.async_get(hass)

    areas: dict[str, Any] = {}
    floors: dict[str, Any] = {}

    for area_id in area_ids:
        area = area_registry.async_get_area(area_id)
        if area is None:
            continue

        area_names = [area.name]
        area_names.extend(area.aliases)
        areas[area_id] = {
            "names": area_names,
            "floor_id": area.floor_id,
        }

        if area.floor_id:
            if area.floor_id not in floors:
                floor = floor_reg.async_get_floor(area.floor_id)
                if floor is None:
                    continue

                floor_names = [floor.name]
                floor_names.extend(floor.aliases)

                floors[area.floor_id] = {"names": floor_names, "area_ids": [area_id]}
            else:
                floors[area.floor_id]["area_ids"].append(area_id)

    return areas, floors


# Refactored from helpers/llm.py
async def _get_exposed_entities(
    hass: core.HomeAssistant,
    assistant: str,
) -> tuple[
    dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]
]:
    """Get exposed entities.

    Returns entities, areas, and floor. Each entry is a dict keyed by ID. Areas
    include a list of entity IDs belonging to the area.
    """
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    entities = {}
    entities_by_area: dict[str, Any] = {}

    for state in sorted(hass.states.async_all(), key=attrgetter("name")):
        if not async_should_expose(hass, assistant, state.entity_id):
            continue

        entity_entry = entity_registry.async_get(state.entity_id)
        names = [state.name]
        area_ids = []

        if entity_entry is not None:
            names.extend(entity_entry.aliases)
            if entity_entry.area_id:
                # Entity is in area
                area_ids.append(entity_entry.area_id)
                if entity_entry.area_id not in entities_by_area:
                    entities_by_area[entity_entry.area_id] = [state.entity_id]
                else:
                    entities_by_area[entity_entry.area_id].append(state.entity_id)
            if entity_entry.device_id and (
                device := device_registry.async_get(entity_entry.device_id)
            ):
                # Check device area
                if device.area_id:
                    area_ids.append(device.area_id)
                    if device.area_id not in entities_by_area:
                        entities_by_area[device.area_id] = [state.entity_id]
                    else:
                        entities_by_area[device.area_id].append(state.entity_id)

        info: dict[str, Any] = {
            "names": names,
            "domain": state.domain,
            "area_ids": area_ids,
        }

        info["attributes"] = [
            attr_name
            for attr_name in state.attributes
            if attr_name in INTERESTING_ATTRIBUTES
        ]

        entities[state.entity_id] = info

    areas, floors = await _get_areas(hass, entities_by_area.keys())
    for area_id, entities in entities_by_area.items():
        areas[area_id]["entity_ids"] = entities

    return entities, areas, floors


def _reverse_map(map: dict[str, Any]) -> dict[str, Any]:
    """Reverse a dictionary of name lists."""
    result: dict[str, Any] = {}

    for k, v in map.items():
        val = dict(v)
        names = val.pop("names", [])
        val["id"] = k
        for name in names:
            # Note that we are using the identical `val` here so there are multiple
            # references to the same value dictionary.
            if name in result:
                raise ValueError(
                    f"Key collision: The name {name} already refers to an object"
                )
            result[name] = val

    return result


def _entity_is_candidate(
    entity: dict[str, Any], entity_names: list[str], attributes: list[str]
) -> bool:
    """Determine whether the entity matches the list of specified names or attributes.

    Only check entity names and attributes if the corresponding parameter is populated.
    """

    if entity_names and entity["name"] not in entity_names:
        # entity_names is populated but this entity's name does not match
        return False

    if attributes:
        if entity["attributes"]:
            if all(attr not in attributes for attr in entity["attributes"]):
                # attributes are populated and this entity has attributes, but none of
                # them match the desired list.
                return False
        else:
            # attributes specified but this entity has no attributes. Consider this not a match.
            return False

    # TODO: check entity type (light, fan, etc) as well as entity name

    # We weren't able to filter this entity away
    return True


class SdkArgs:
    """Helper class for passing arguments to SDK action server."""

    def __init__(self, **kwargs) -> None:
        """Initialize the object with any desired attributes."""
        self._data = kwargs

    def __getattr__(self, name: str):
        """Retrieve any set attributes or None without raising."""
        if name in self._data:
            return self._data[name]
        return None


class RasaActionServer:
    """Action server that receives queries from the Rasa server."""

    def __init__(self, hass: core.HomeAssistant, port: int) -> None:
        """Initialize the action server."""
        self._hass = hass
        self._port = port
        # These are dictionaries mapping the object ID to the relevant object info.
        self._entities: dict[str, Any] = {}
        self._areas: dict[str, Any] = {}
        self._floors: dict[str, Any] = {}
        # These are the same as above, but the key is by object name. Aliases are
        # also present as keys.
        self._entity_map: dict[str, Any] = {}
        self._area_map: dict[str, Any] = {}
        self._floor_map: dict[str, Any] = {}

        # Server settings
        self._host = "0.0.0.0"
        self._protocol = "http"

    async def launch(self) -> None:
        """Launch server."""
        self._entities, self._areas, self._floors = await _get_exposed_entities(
            self._hass, assistant="rasa"
        )
        # Remap by names
        self._entity_map = _reverse_map(self._entities)
        self._area_map = _reverse_map(self._areas)
        self._floor_map = _reverse_map(self._floors)

        # TODO: unpack `run` and un-modify sdk package
        loader = AppLoader(factory=self._create_server)
        self._hass.async_create_background_task(self._run_server(loader), "rasa-action")

    def _create_server(self):
        # TODO: callback from action server to here
        action_executor = ActionExecutor()
        action_executor.register_package(actions)

        _LOGGER.info("Starting action endpoint server")
        app = create_app(action_executor, auto_reload=False)

        app.config.KEEP_ALIVE_TIMEOUT = SERVER_KEEPALIVE
        app.config.MOTD = False
        # We don't use sanic extensions. This allows us to bypass the attempt
        # to load them, which causes a blocking import call inside the event
        # loop and a warning by HA.
        app.config.AUTO_EXTEND = False

        app.register_listener(
            partial(load_tracer_provider, "endpoints.yml"),
            "before_server_start",
        )

        # Attach additional sanic extensions: listeners, middleware and routing
        _LOGGER.info("Starting plugins")
        plugin_manager().hook.attach_sanic_app_extensions(app=app)

        return app

    async def _run_server(self, loader: AppLoader):
        """Instantiate the app via the loader then create and run the server."""
        app = loader.load()
        server = await app.create_server(
            host=self._host,
            port=self._port,
        )
        if not server:
            raise RuntimeError("Action server not created")

        await server.startup()

        _LOGGER.info(
            "Action endpoint is up and running on %s://%s:%d",
            self._protocol,
            self._host,
            self._port,
        )

        await server.serve_forever()

    def _get_area_ids(self, location: str) -> list[str]:
        """Check floors and areas to find all area IDs compatible with this location name."""
        if location in self._floor_map:
            # If the location is a floor, include all areas on that floor
            return self._floor_map[location]["area_ids"]

        if location in self._area_map:
            # Not a floor but is an area name
            return [self._area_map[location]["id"]]

        # No locations found
        raise ValueError(f"Location {location} not found")

    def _get_matching_entities(
        self, locations: list[str], entities: list[str], attributes: list[str]
    ) -> set[str]:
        """Get a list of entity IDs matching the specified parameters.

        If any of location, entity, or attribute are an empty list, do not
        filter on those names. Each of the arguments is a list of descriptive
        names, not the entity or location IDs.
        """

        if locations:
            area_ids: set[str] = set()
            for loc in locations:
                # Collect all applicable location IDs
                area_ids.update(self._get_area_ids(loc))
        else:
            # If no locations specified, use all locations.
            area_ids = set(self._areas.keys())

        # TODO: could make this dynamically call hass to query entities
        entity_ids = set()
        for area_id in area_ids:
            for entity in self._areas[area_id]["entity_ids"]:
                if _entity_is_candidate(entity, entities, attributes):
                    entity_ids.add(entity)

        return entity_ids

    def _find_entity(self, location: str | None, thing: str):
        """Find the best matching entities given the user-specified location and entity or attribute name."""
        # TODO: it's not super clear what the best approach is here. The
        # user can ask to "increase the temperature" (attribute) or
        # "turn off the fan" (entity).
        if location:
            loc_list = [location]
        else:
            loc_list = []

        # Try entity name/type first
        candidate_ids = self._get_matching_entities(
            locations=loc_list, entities=[thing], attributes=[]
        )

        if not candidate_ids:
            # Try searching by attribute
            candidate_ids = self._get_matching_entities(
                locations=loc_list, entities=[], attributes=[thing]
            )

        return candidate_ids
