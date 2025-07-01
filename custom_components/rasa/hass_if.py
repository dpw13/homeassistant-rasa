"""Hass interface for the Rasa action server."""

# pylint: disable=fixme

from __future__ import annotations

from collections.abc import Iterable
import logging
from operator import attrgetter
from typing import Any

from homeassistant import core
from homeassistant.components.device_automation import (
    DeviceAutomationType,
    async_get_device_automations,
)
from homeassistant.components.homeassistant import async_should_expose
from homeassistant.const import CONF_TYPE, CONF_ENTITY_ID
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
import voluptuous as vol

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

        area_names = [area.name.lower()]
        area_names.extend(a.lower() for a in area.aliases)
        areas[area_id] = {
            "names": area_names,
            "floor_id": area.floor_id,
        }

        if area.floor_id:
            if area.floor_id not in floors:
                floor = floor_reg.async_get_floor(area.floor_id)
                if floor is None:
                    continue

                floor_names = [floor.name.lower()]
                floor_names.extend(a.lower() for a in floor.aliases)

                floors[area.floor_id] = {"names": floor_names, "area_ids": [area_id]}
            else:
                floors[area.floor_id]["area_ids"].append(area_id)

    return areas, floors


# Refactored from helpers/llm.py
async def _get_exposed_entities(
    hass: core.HomeAssistant,
    assistant: str = "conversation",
) -> tuple[
    dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]
]:
    """Get exposed entities.

    Returns entities, areas, and floor. Each entry is a dict keyed by ID. Areas
    include a list of entity IDs belonging to the area.

    Important note about the "assistant" argument. Conversation plugins such as
    ollama use helpers/llm.py to access various HA entities. However, within
    llm.py, an "LLM context" object is instantiated where the "assistant" argument
    is set to `DOMAIN`. But `DOMAIN` in llm.py is "conversation"; llm.py is
    designed to be used by the native HA conversation assistant. The Rasa integration
    is built the same way. So despite `DOMAIN` here being `rasa`, we need to query
    entity exposure based on the assistant value of "conversation".
    """
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    entities = {}
    entities_by_area: dict[str, list[str]] = {}
    entities_by_floor: dict[str, list[str]] = {}

    _LOGGER.debug("Checking all entities for exposure to %s", assistant)

    for state in sorted(hass.states.async_all(), key=attrgetter("name")):
        _LOGGER.debug("Should expose? %s", state)
        if not async_should_expose(hass, assistant, state.entity_id):
            continue

        entity_entry = entity_registry.async_get(state.entity_id)
        names = [state.name.lower()]
        area_ids = []
        actions = {}

        if entity_entry is not None:
            names.extend(a.lower() for a in entity_entry.aliases)
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

                actions_map = await async_get_device_automations(
                    hass, DeviceAutomationType.ACTION, (entity_entry.device_id,)
                )
                # async_get_device_automations returns actions for all device IDs, but we
                # only care about one right now. Restructure into dict by keying on
                # the action name.
                # It doesn't appear that use of `CONF_TYPE` is *enforced* so this could break
                # for some devices.
                actions = {a[CONF_TYPE]: a for a in actions_map[entity_entry.device_id]}

            info: dict[str, Any] = {
                "names": names,
                "domain": state.domain,
                "platform": entity_entry.platform,
                "area_ids": area_ids,
                "actions": actions,
            }

            info["attributes"] = [
                attr_name
                for attr_name in state.attributes
                if attr_name in INTERESTING_ATTRIBUTES
            ]

            _LOGGER.debug("Entity %s: %s", state.entity_id, info)
            entities[state.entity_id] = info

    areas, floors = await _get_areas(hass, entities_by_area.keys())
    for area_id, ent in entities_by_area.items():
        areas[area_id]["entity_ids"] = ent
    # Calculate all entities on floor by accumulating all entites in all areas.
    for floor in floors.values():
        floor["entity_ids"] = []
        for area_id in floor["area_ids"]:
            floor["entity_ids"].extend(areas[area_id]["entity_ids"])

    return entities, areas, floors


def _reverse_map(map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reverse a dictionary of name lists."""
    result: dict[str, dict[str, Any]] = {}

    for k, v in map.items():
        # Create copy
        val = dict(v)
        names = val.pop("names", [])
        val["id"] = k
        for name in names:
            # Note that we are using the identical `val` here so there are multiple
            # references to the same value dictionary.
            if name in result:
                _LOGGER.warning(
                    "Key collision: The name %s already refers to an object. Control may be impaired",
                    name,
                )
            result[name] = val

    return result


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


class HassIface:
    """Action server that receives queries from the Rasa server."""

    def __init__(self, hass: core.HomeAssistant) -> None:
        """Initialize the action server."""
        self._hass = hass
        # These are dictionaries mapping the object ID to the relevant object info.
        self._entity_by_id: dict[str, dict[str, Any]] = {}
        self._area_by_id: dict[str, dict[str, Any]] = {}
        self._floor_by_id: dict[str, dict[str, Any]] = {}
        # These are the same as above, but the key is by object name. Aliases are
        # also present as keys.
        self._entity_by_name: dict[str, dict[str, Any]] = {}
        self._area_by_name: dict[str, dict[str, Any]] = {}
        self._floor_by_name: dict[str, dict[str, Any]] = {}

        # Server settings
        self._host = "0.0.0.0"
        self._protocol = "http"

    async def load(self) -> None:
        """Load entities."""
        (
            self._entity_by_id,
            self._area_by_id,
            self._floor_by_id,
        ) = await _get_exposed_entities(self._hass)
        # Remap by names
        self._entity_by_name = _reverse_map(self._entity_by_id)
        self._area_by_name = _reverse_map(self._area_by_id)
        self._floor_by_name = _reverse_map(self._floor_by_id)

        _LOGGER.info("Areas: %s", self._area_by_name.keys())
        _LOGGER.info("Floors: %s", self._floor_by_name.keys())
        _LOGGER.info("Entities: %s", self._entity_by_name.keys())

    def get_location_by_id(self, loc_id: str) -> str:
        """Get the location name for the location ID."""
        if loc_id in self._area_by_id:
            return self._area_by_id[loc_id]
        if loc_id in self._floor_by_id:
            return self._floor_by_id[loc_id]
        raise IndexError(f"Location ID {loc_id} matches no known location")

    def get_entity_by_id(self, ent_id: str) -> str:
        """Get the device name for the device ID."""
        return self._entity_by_id[ent_id]

    def _get_area_ids(self, location: str) -> list[str]:
        """Check floors and areas to find all area IDs compatible with this location name."""
        if location in self._floor_by_name:
            # If the location is a floor, include all areas on that floor
            return self._floor_by_name[location]["area_ids"]

        if location in self._area_by_name:
            # Not a floor but is an area name
            return [self._area_by_name[location]["id"]]

        # No locations found
        raise ValueError(f"Location {location} not found")

    def _entity_is_candidate(
        self,
        entity_id: str,
        entity_names: list[str],
        attributes: list[str],
        actions: list[str],
    ) -> bool:
        """Determine whether the entity matches the list of specified names or attributes.

        Only check entity names and attributes if the corresponding parameter is populated.
        """
        entity = self._entity_by_id[entity_id]

        if (
            entity_names
            and all(name not in entity_names for name in entity["names"])
            and entity["domain"] not in entity_names
        ):
            # entity_names is populated but this entity's name and domain both do not match
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

        if actions:
            if entity["actions"]:
                if all(act not in actions for act in entity["actions"]):
                    # No overlap between specified actions and device.
                    return False
            else:
                # specific action requested but device has no actions
                return False

        # We weren't able to filter this entity away
        return True

    def find_location_by_name(self, loc: str) -> list[str] | None:
        """Return the location IDs with the specified name."""
        # If a location name matches both floor and area, use both IDs.

        ret = []
        if loc in self._area_by_name:
            ret.append(self._area_by_name[loc]["id"])
        if loc in self._floor_by_name:
            ret.append(self._floor_by_name[loc]["id"])
        return ret

    def _get_entities_by_area(self, area_id: str) -> list[str]:
        """Get all entity IDs in floors or areas with the given ID."""
        res: list[str] = []
        if area_id in self._area_by_id:
            res.extend(self._area_by_id[area_id]["entity_ids"])
        if area_id in self._floor_by_id:
            res.extend(self._floor_by_id[area_id]["entity_ids"])

        return res

    def match_entities(
        self, slots: dict[str, Any]
    ) -> tuple[set[str], set[str], set[str], set[str]]:
        """Find all matching entities and their associated locations and parameters.

        If any of location, entity, or attribute are an empty list, do not
        filter on those names. Each of the arguments is a list of descriptive
        names, not the entity or location IDs.

        First, find the set of all entities matching the specified location,
        entity name, and attributes. In addition to the set of matching
        entities, collect the set of locations and parameters of those
        matching entities.

        Return value:
        (actions, location_ids, entity_ids, attributes)
        """

        # Location slot must be location ID at this stage.
        if slots["location"] is None or not slots["location"]:
            # If no locations specified, use all locations.
            area_ids = set(self._area_by_id.keys())
        elif isinstance(slots["location"], str):
            area_ids = {
                slots["location"],
            }
        else:
            area_ids = set(slots["location"])

        # TODO: could make this dynamically call hass to query entities
        matching_entities = set()
        matching_areas = set()
        matching_attributes = set()
        matching_actions = set()
        for area_id in area_ids:
            for entity_id in self._get_entities_by_area(area_id):
                if self._entity_is_candidate(
                    entity_id, slots["device"], slots["parameter"], slots["action"]
                ):
                    matching_areas.add(area_id)
                    matching_entities.add(entity_id)

                    entity = self._entity_by_id[entity_id]
                    if slots["parameter"]:
                        # Only add matching parameters if parameters were specified.
                        matching_attributes.update(
                            a for a in entity["attributes"] if a in slots["parameter"]
                        )
                    else:
                        # If no parameters were specified, collect all attributes
                        # of matching entities.
                        matching_attributes.update(entity["attributes"])

                    # Actions work very similarly to parameters
                    if slots["action"]:
                        # Only add matching actions
                        matching_actions.update(
                            a for a in entity["actions"] if a in slots["action"]
                        )
                    else:
                        # Accumulate all matching actions
                        matching_actions.update(entity["actions"])

        return matching_actions, matching_areas, matching_entities, matching_attributes

    def find_entity(self, location: str | None, thing: str):
        """Find the best matching entities given the user-specified location and entity or attribute name."""
        # TODO: it's not super clear what the best approach is here. The
        # user can ask to "increase the temperature" (attribute) or
        # "turn off the fan" (entity).
        if location:
            loc_list = (location,)
        else:
            loc_list = ()

        # Try entity name/type first
        candidate_ids = self.get_matching_entities(
            locations=loc_list, entities=[thing], attributes=[]
        )

        if not candidate_ids:
            # Try searching by attribute
            candidate_ids = self.get_matching_entities(
                locations=loc_list, entities=[], attributes=[thing]
            )

        return candidate_ids

    async def _apply_abs_adjustment(
        self,
        parameter: str,
        amount: Any,
        state: core.State,
    ):
        """Make the requested adjustment to the specified device. State must be pre-filled."""

        # By default assume we don't need to change the state.
        new_state = state.state
        if state.state == "off" and amount > 0:
            new_state = "on"
        if state.state == "on" and abs(amount) < 0.01:
            # Note that we check for "approximately off", or less than 1% on.
            new_state = "off"

        attributes = {parameter: amount}

        await self._hass.states.async_set(
            state.domain,
            new_state=new_state,
            attributes=attributes,
            context=state.context,
        )

    async def apply_abs_adjustment(
        self, action: str, device_ids: list[str], parameter: str | None, amount: Any
    ) -> int:
        """Make the requested adjustment to the specified devices."""

        for did in device_ids:
            state = self._hass.states.get(did)
            if not state:
                raise ValueError(f"Entity '{did}' does not exist")

            if parameter not in state.attributes:
                raise ValueError(
                    f"Entity '{did}' does not have attribute '{parameter}'"
                )

            _LOGGER.debug(
                "Changing %s %s from %s to %s",
                did,
                parameter,
                state.attributes[parameter],
                amount,
            )
            self._apply_abs_adjustment(parameter, amount, state)

        return len(device_ids)

    async def apply_rel_adjustment(
        self, action: str, device_ids: list[str], parameter: str | None, amount: float
    ) -> int:
        """Make the requested adjustment to the specified devices."""

        for did in device_ids:
            state = self._hass.states.get(did)
            if not state:
                raise ValueError(f"Entity '{did}' does not exist")

            if parameter not in state.attributes:
                raise ValueError(
                    f"Entity '{did}' does not have attribute '{parameter}'"
                )

            if isinstance(state.attributes[parameter], (float, int)):
                raise TypeError(
                    f"Entity '{did}' attribute '{parameter}' is not numeric"
                )

            new_amount = state.attributes[parameter] + amount
            _LOGGER.debug(
                "Changing %s %s from %s to %s",
                did,
                parameter,
                state.attributes[parameter],
                new_amount,
            )
            self._apply_abs_adjustment(parameter, new_amount, state)

        return len(device_ids)

    async def apply_action(self, action: str, device_ids: list[str]) -> int:
        """Make the requested adjustment to the specified devices."""

        for did in device_ids:
            state = self._hass.states.get(did)

            _LOGGER.debug("Calling %s.%s on %s", state.domain, action, did)
            # TODO: you can actually 'turn_on' all entities in an area or on
            # a floor. It may make more sense to do things that way eventually
            # if performance is poor.
            # TODO: some service schemas require additional information.
            service_data = {CONF_ENTITY_ID: did}
            try:
                await self._hass.services.async_call(
                    state.domain,
                    action,
                    context=state.context,
                    service_data=service_data,
                    blocking=False,
                )
            except ServiceNotFound as ex:
                raise ValueError(
                    f"No action {action} exists for {state.domain}"
                ) from ex
            except vol.Invalid as ex:
                # Service schema validation failure. We probably missed setting something.
                raise ValueError(f"Could not {action} {did}") from ex

        return len(device_ids)
