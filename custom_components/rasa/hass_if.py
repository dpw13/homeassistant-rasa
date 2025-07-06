"""Hass interface for the Rasa action server."""

# pylint: disable=fixme

from __future__ import annotations

from collections.abc import Iterable
import logging
from operator import attrgetter
from typing import Any

import voluptuous as vol

from homeassistant import core
from homeassistant.components.climate import (
    SERVICE_SET_HUMIDITY,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.device_automation import (
    DeviceAutomationType,
    async_get_device_automations,
)
from homeassistant.components.homeassistant import async_should_expose
from homeassistant.components.media_player import SERVICE_VOLUME_SET
from homeassistant.const import (
    CONF_ENTITY_ID,
    CONF_TYPE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)

_LOGGER = logging.getLogger(__name__)


INTERESTING_ATTRIBUTES = {
    "temperature",
    "current_temperature",
    "temperature_unit",
    "brightness",
    "humidity",
    "unit_of_measurement",
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

    _LOGGER.debug("Checking all entities for exposure to %s", assistant)

    # Check services for all domains.
    # DO NOT MODIFY THIS DICT! We are using it in-place for efficiency.
    svcs = hass.services.async_services_internal()
    # TODO: we are throwing away schema information here
    actions = {d: tuple(s.keys()) for d, s in svcs.items()}

    for state in sorted(hass.states.async_all(), key=attrgetter("name")):
        if not async_should_expose(hass, assistant, state.entity_id):
            continue

        entity_entry = entity_registry.async_get(state.entity_id)
        names = [state.name.lower()]
        area_ids = []

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

                # TODO:
                # async_get_device_automations returns something that isn't the services
                # associated with the entity and isn't what's listed for "automations" in
                # the web UI. It's unclear exactly what the distinction is between
                # async_get_device_automations and services. Instead, we query all services
                # registered to each domain and reference those instead.

            info: dict[str, Any] = {
                "names": names,
                "domain": state.domain,
                "platform": entity_entry.platform,
                "area_ids": area_ids,
                # Some entities have no actions, like read-only sensors
                "action": actions.get(state.domain, []),
            }

            info["attributes"] = [
                attr_name
                for attr_name in state.attributes
                if attr_name in INTERESTING_ATTRIBUTES
            ]

            # HACK:
            # Just pretend media players have a volume even though all the adjustments
            # are done through special service calls.
            if state.domain in ("media_player", "remote"):
                info["attributes"].append("volume")

            # _LOGGER.debug("Entity %s: %s", state.entity_id, info)
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

        actions = set()
        for ent in self._entity_by_id.values():
            actions.update(ent["action"])

        _LOGGER.debug("Areas: %s", self._area_by_name.keys())
        _LOGGER.debug("Floors: %s", self._floor_by_name.keys())
        _LOGGER.debug("Entities: %s", self._entity_by_name.keys())
        _LOGGER.debug("Actions: %s", actions)

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

    @staticmethod
    def _match_actions(entity: dict[str, Any], actions: list[str]) -> set[str]:
        """Determine whether this action can be performed on this entity.

        Returns the canonical action name if a match is found.
        """
        valid_actions = set()
        for action in actions:
            domain = entity["domain"]
            # A generic action like "stop" can be implemented as "stop_cover" for
            # the cover class or "media_stop" for the media player class. This heuristic
            # tries both to try to find a match.
            action_candidates = [action, f"{action}_{domain}", f"{domain}_{action}"]
            for c in action_candidates:
                if c in entity["action"]:
                    valid_actions.add(c)

        return valid_actions

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
            # Don't check actions here since we don't have a way of setting
            # the coerced action.
            if not entity["action"]:
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

        _LOGGER.debug("Matching entities: %s", slots)

        if isinstance(slots["parameter"], str):
            params = [slots["parameter"]]
        elif slots["parameter"] is None:
            params = []
        else:
            params = slots["parameter"]

        if isinstance(slots["action"], str):
            actions = [slots["action"]]
        elif slots["action"] is None:
            actions = []
        else:
            actions = slots["action"]

        # Remove set_relative and set_absolute; these are handled differently and
        # not actions as HA sees them.
        # ... except when they are like for volume_set or volume_up/down
        valid_actions = [
            a for a in actions if a not in ("set_relative", "set_absolute")
        ]
        is_adjust = "set_relative" in actions or "set_absolute" in actions
        actions = valid_actions

        _LOGGER.debug("Actions: %s", actions)

        # TODO: could make this dynamically call hass to query entities
        matching_entities = set()
        matching_areas = set()
        matching_attributes = set()
        matching_actions = set()
        for area_id in area_ids:
            for entity_id in self._get_entities_by_area(area_id):
                if self._entity_is_candidate(
                    entity_id, slots["device"], params, actions
                ):
                    entity = self._entity_by_id[entity_id]

                    # Actions work very similarly to parameters but the naming is much
                    # less regular. Check actions first because we may still decide to ignore
                    # this entity if no actions match.
                    if actions:
                        # Only add matching actions
                        ent_actions = self._match_actions(entity, actions)
                        # if no actions match, don't add entity unless the user wants to set
                        # an attribute.
                        if not ent_actions and not is_adjust:
                            _LOGGER.debug(
                                "Skipping %s because no actions match %s",
                                entity_id,
                                actions,
                            )
                            continue
                        matching_actions.update(ent_actions)
                    else:
                        # Accumulate all actions for matching entities
                        matching_actions.update(entity["action"])

                    if params:
                        # Only add matching parameters if parameters were specified.
                        matching_attributes.update(
                            a for a in entity["attributes"] if a in params
                        )
                    else:
                        # If no parameters were specified, collect all attributes
                        # of matching entities.
                        matching_attributes.update(entity["attributes"])

                    matching_areas.add(area_id)
                    matching_entities.add(entity_id)

        return matching_actions, matching_areas, matching_entities, matching_attributes

    async def _apply_abs_adjustment(
        self,
        parameter: str,
        amount: Any,
        state: core.State,
    ):
        """Make the requested adjustment to the specified device. State must be pre-filled.

        Note that this method can tolerate the device not having the attribute present. If
        the attribute is missing, we allow changing the state to on or off based on the intended
        value.
        """

        # By default assume we don't need to change the state.
        new_state = state.state

        service_data = {CONF_ENTITY_ID: state.entity_id}
        if parameter in state.attributes:
            # Note that we check for "approximately off", or less than 1% on.
            threshold = 1
            service_data[parameter] = amount
        else:
            # Set a threshold of 20%
            # TODO: I really hate the inconsistent use of percent units
            threshold = 20

        if state.state == "off" and amount >= threshold:
            new_state = "on"
        if state.state == "on" and abs(amount) < threshold:
            new_state = "off"

        # Note that _hass.states.async_set will update the internal HA state but
        # not actually change the device. Instead, we need to call the appropriate
        # service based on the parameter being changed.

        # Ugh. Heuristic. May need adjustment. What a mess. See e.g.
        # homeassistant/components/alexa/handlers.py:async_api_set_range() for what
        # other assistants have done. Maybe re-use some of that.
        PARAM_TO_SVC = {
            "temperature": SERVICE_SET_TEMPERATURE,
            "humidity": SERVICE_SET_HUMIDITY,
            "volume": SERVICE_VOLUME_SET,
            # "mode" can refer to a variety of things depending on domain. Don't try to
            # set that (yet)
        }

        if parameter in PARAM_TO_SVC:
            svc = PARAM_TO_SVC[parameter]
        elif new_state == "on":
            svc = SERVICE_TURN_ON
        elif new_state == "off":
            svc = SERVICE_TURN_OFF

        await self._hass.services.async_call(
            state.domain,
            service=svc,
            context=state.context,
            service_data=service_data,
            blocking=False,
        )

    async def apply_abs_adjustment(
        self, device_ids: list[str], parameter: str | None, amount: Any
    ) -> int:
        """Make the requested adjustment to the specified devices.

        Returns the list of successfully adjust device IDs.
        """

        success_ids = []
        for did in device_ids:
            state = self._hass.states.get(did)
            if not state:
                raise ValueError(f"Entity '{did}' does not exist")

            # We allow state changes when the value attribute is missing
            current_value = state.attributes.get(parameter, 0.0)

            _LOGGER.debug(
                "Changing %s %s from %s to %s",
                did,
                parameter,
                current_value,
                amount,
            )
            await self._apply_abs_adjustment(parameter, amount, state)
            success_ids.append(did)

        return success_ids

    async def apply_rel_adjustment(
        self, device_ids: list[str], parameter: str | None, amount: float
    ) -> list[str]:
        """Make the requested adjustment to the specified devices.

        Returns the list of successfully adjust device IDs.
        """

        success_ids = []
        for did in device_ids:
            state = self._hass.states.get(did)
            if not state:
                raise ValueError(f"Entity '{did}' does not exist")

            if parameter not in state.attributes:
                _LOGGER.info(
                    "Entity '%s' has no attribute '%s', will try to use state",
                    did,
                    parameter,
                )

            current_value = state.attributes.get(parameter, None)
            if current_value is None:
                current_value = 0.0

            if not isinstance(current_value, (float, int)):
                # We can't perform a relative adjustment if the original value
                # isn't numeric
                _LOGGER.warning(
                    "Entity '%s' attribute '%s' is not numeric",
                    did,
                    parameter,
                )
                continue

            new_amount = current_value + amount
            _LOGGER.debug(
                "Changing %s %s from %s to %s",
                did,
                parameter,
                state.attributes.get(parameter, None),
                new_amount,
            )
            await self._apply_abs_adjustment(parameter, new_amount, state)
            success_ids.append(did)

        return success_ids

    async def apply_action(self, action: str, device_ids: list[str]) -> int:
        """Make the requested adjustment to the specified devices."""

        for did in device_ids:
            state = self._hass.states.get(did)

            if state is None:
                raise ValueError(f"No such device '{did}'")

            _LOGGER.debug("Calling %s.%s on %s", state.domain, action, did)
            # TODO: you can actually 'turn_on' all entities in an area or on
            # a floor. It may make more sense to do things that way eventually
            # if performance is poor.
            # TODO: some service schemas require additional information.
            service_data = {CONF_ENTITY_ID: did}
            try:
                # Apparently the "device automations" that define things like "open"
                # and "close" for the `cover` domain don't actually map to services,
                # so we get an error when attempting to open or close a cover. This
                # may be true of other automations where the automation action does
                # not match the service name.

                # TODO: I have no idea how to actually trigger an automation action.
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

        return device_ids
