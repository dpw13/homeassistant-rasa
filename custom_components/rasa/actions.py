"""Contains your custom actions which can be used to run custom Python code."""

# pylint: disable=fixme

# See this guide on how to implement these actions:
# https://rasa.com/docs/rasa/custom-actions


# This is a simple example for a custom action which utters "Hello World!"

# from typing import Any, Text, dict, list
#
# from rasa_sdk import Action, Tracker
# from rasa_sdk.executor import CollectingDispatcher
#
#
# class ActionHelloWorld(Action):
#
#     def name(self) -> Text:
#         return "action_hello_world"
#
#     def run(self, dispatcher: CollectingDispatcher,
#             tracker: Tracker,
#             domain: dict[Text, Any]) -> list[dict[Text, Any]]:
#
#         dispatcher.utter_message(text="Hello World!")
#
#         return []

import logging
from typing import Any

from rasa_sdk import Action, Tracker
from rasa_sdk.events import AllSlotsReset, BotUttered, EventType
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.forms import FormValidationAction

from .hass_if import HassIface

logger = logging.getLogger(__name__)

_HASS_IF: HassIface = None


def register_hass(hass_if: HassIface):
    """Register HASS interface with action server."""
    # pylint: disable=global-statement
    global _HASS_IF
    _HASS_IF = hass_if


class DeviceLocationForm(FormValidationAction):
    """Doctstring."""

    def name(self) -> str:
        """Name."""
        return "_helper_device_location_form"

    async def validate_location(
        self,
        slot_value: str,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: dict,
    ) -> dict[str, Any]:
        """Validate the requested location."""
        slot_value = slot_value.lower()
        logger.debug("Validating location '%s'", slot_value)

        if slot_value in ("any", "all", "each"):
            # These values indicate we should be dealing with all entities matching
            # any remaining conditions and that we probably expect multiple entities
            # to match.
            return {"multiple": True, "location": None}

        if _HASS_IF.is_known_location(slot_value):
            return {"location": slot_value}

        dispatcher.utter_message(f"Sorry, I don't know the location {slot_value}")
        return {"location": None}

    def validate_device(
        self,
        slot_value: str,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: dict,
    ) -> dict[str, Any]:
        """Validate the device slot.

        As with the other slot validation functions, check to see how many devices match
        the current slot value. Make a (feeble) attempt at determining whether the user's
        intent is to match multiple devices by checking whether the slot name is plural.
        """
        # TODO: better lemmatization
        # TODO: determine whether we allow multiple matches by whether the device is plural
        # TODO: support multiple matching devices
        slot_value = slot_value.lower()
        plural = slot_value.endswith("s")
        slot_value = slot_value.rstrip("s")

        logger.debug("Validating device '%s'", slot_value)
        new_slots = dict(tracker.slots)
        new_slots.update({"device": slot_value})

        actions, location_ids, entity_ids, parameters = _HASS_IF.match_entities(
            new_slots
        )

        if not entity_ids:
            filters = []
            if tracker.slots["location"] is not None:
                filters.append("in " + tracker.slots["location"])

            if tracker.slots["parameter"] is not None:
                filters.append("with a " + tracker.slots["parameter"])

            if tracker.slots["action"] is not None:
                filters.append("that we can " + tracker.slots["action"])

            filter_msg = " ".join(filters)
            logger.warning("no device '%s' found %s", slot_value, filter_msg)
            dispatcher.utter_message(
                f"Sorry, I don't know of any devices called {slot_value} {filter_msg}."
            )
            return {"device": None}

        if len(entity_ids) > 1 and not plural:
            # Found more than one matching entity ID but only expected one.
            # TODO: confirmation dialog path
            # TODO: actually we probably only want to confirm this once we know for sure
            # we've filled all slots, which means we need a "plural" slot.
            dispatcher.utter_message(
                f"Found {len(entity_ids)} {slot_value}s in {len(location_ids)} locations, but it sounds like you only wanted one. Do you want to adjust them all?"
            )

        # Found at least one matching entity
        ret = {"device": slot_value, "multiple": plural}
        if len(location_ids) == 1:
            loc = _HASS_IF.get_location_by_id(location_ids[0])
            logger.debug("Assuming location %s from device", loc)
            ret["location"] = loc
        if len(parameters) == 1:
            logger.debug("Assuming parameter %s from device", parameters[0])
            ret["parameter"] = parameters[0]
        if len(actions) == 1:
            # This seems pretty unlikely. Any device will at least have both turn_on and
            # turn_off. Still, include for completion.
            logger.debug("Assuming action %s from device", actions[0])
            ret["action"] = actions[0]

        return ret

    def validate_parameter(
        self,
        slot_value: str,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: dict,
    ) -> dict[str, Any]:
        """Validate parameter to try to find a set of devices given the other constraints."""
        slot_value = slot_value.lower()

        logger.debug("Validating parameter '%s'", slot_value)
        new_slots = dict(tracker.slots)
        new_slots.update({"parameter": slot_value})

        actions, location_ids, entity_ids, parameters = _HASS_IF.match_entities(
            new_slots
        )

        if not parameters:
            filters = []
            if tracker.slots["location"] is not None:
                filters.append("in " + tracker.slots["location"])

            if tracker.slots["device"] is not None:
                filters.append("called " + tracker.slots["device"])

            if tracker.slots["action"] is not None:
                filters.append("that we can " + tracker.slots["action"])

            filter_msg = " ".join(filters)
            logger.warning("no devices with a '%s' found %s", slot_value, filter_msg)
            dispatcher.utter_message(
                f"Sorry, I don't know of any devices with a {slot_value} {filter_msg}."
            )
            return {"device": None}

        # TODO: figure out what to do when we find multiple devices when searching by parameter.

        # Found at least one matching entity
        ret = {"parameter": slot_value}
        if len(location_ids) == 1:
            loc = _HASS_IF.get_location_by_id(location_ids[0])
            logger.debug("Assuming location %s from parameter", loc)
            ret["location"] = loc
        if len(entity_ids) == 1:
            ent = _HASS_IF.get_entity_by_id(entity_ids[0])
            logger.debug("Assuming device %s from parameter", ent)
            ret["device"] = ent
        if len(actions) == 1:
            # See note above, this seems pretty unlikely.
            logger.debug("Assuming action %s from device", actions[0])
            ret["action"] = actions[0]

        return ret


class DeviceAmountForm(DeviceLocationForm):
    """Common functions for validating and parsing amounts."""

    def name(self) -> str:
        """Docstring."""
        return "_helper_device_amount_form"

    # Format: dict{txt: (Absolute, Amount)}
    ACTION_DICT = {
        "turn up": ("set_relative", 0.20),
        "turn down": ("set_relative", 0.20),
        "turn on": ("set_absolute", 1.0),
        "turn off": ("set_absolute", 0.0),
        "open": ("set_absolute", 1.0),
        "close": ("set_absolute", 0.0),
        "mute": ("set_absolute", 0.0),
        "unmute": (
            "set_absolute",
            0.5,
        ),  # TODO: restore previous volume? Maybe mute is special?
        # TODO: there are likely other device-specific actions like play, stop, etc
        # TODO: check whether an action applies to a specific device?
        # TODO: relative amount depends on what you're adjusting. A fan might be turned up by 25%,
        #   lights by 15%, and temperature by 2 degrees.
    }

    # Docs: Concepts -> Actions -> Forms
    def validate_action(
        self,
        slot_value: str,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: dict,
    ) -> dict[str, Any]:
        """Validate action."""
        slot_value = slot_value.lower().replace(" ", "_")
        new_slots = dict(tracker.slots)
        new_slots.update({"action": slot_value})

        actions, location_ids, entity_ids, parameters = _HASS_IF.match_entities(
            new_slots
        )

        if not actions:
            filters = []
            if tracker.slots["location"] is not None:
                filters.append("in " + tracker.slots["location"])

            if tracker.slots["device"] is not None:
                filters.append("called " + tracker.slots["device"])

            if tracker.slots["parameter"] is not None:
                filters.append("with a " + tracker.slots["parameter"])

            filter_msg = " ".join(filters)
            logger.warning(
                "no devices that we can '%s' found %s", slot_value, filter_msg
            )
            dispatcher.utter_message(
                f"Sorry, I don't know of any devices we can {slot_value} {filter_msg}."
            )
            return {"action": None}

        # TODO: figure out what to do when we find multiple devices when searching by action.

        # Found at least one matching entity
        ret = {"action": slot_value}
        if len(location_ids) == 1:
            loc = _HASS_IF.get_location_by_id(location_ids[0])
            logger.debug("Assuming location %s from action", loc)
            ret["location"] = loc
        if len(entity_ids) == 1:
            ent = _HASS_IF.get_entity_by_id(entity_ids[0])
            logger.debug("Assuming device %s from action", ent)
            ret["device"] = ent
        if len(parameters) == 1:
            # There's a relationship between parameters and actions that doesn't seem well
            # defined just yet. "turn on" may affect brightness, for instance, but we don't
            # necessarily need an "amount" for such an action. Similar for mute, etc.
            #
            # See e.g. the action schema in homeassistant/components/light/device_action.py
            # This may become more clear when we figure out how to actually *call* the
            # actions.
            logger.debug("Assuming parameter %s from action", parameters[0])
            ret["parameter"] = parameters[0]

        return ret

    def validate_amount(
        self,
        slot_value: str,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: dict,
    ) -> dict[str, Any]:
        """Validate and parse the amount."""
        ret: dict[str, Any] = {"amount": slot_value}
        mult = 1.0

        if isinstance(slot_value, str):
            words = slot_value.split(" ")
            for word in words:
                if word == "percent":
                    mult = 0.01
                else:
                    try:
                        ret["amount"] = float(word)
                        if tracker.slots["action"] is None:
                            # Implicitly set absolute action when parsing amount
                            ret["action"] = "absolute"
                    except ValueError:
                        pass

        if isinstance(ret["amount"], float):
            # Apply percentage or units
            ret["amount"] *= mult

        return ret


class ValidateAdjustForm(DeviceAmountForm):
    """Docstring."""

    def name(self) -> str:
        """Docstring."""
        return "validate_adjust_form"


class SubmitAdjust(Action):
    """Action for submitting a change to a device."""

    def name(self) -> str:
        """Docstring."""
        return "action_submit_adjust"

    # TODO: indicate how many devices were altered when form is submitted
    async def run(
        self,
        dispatcher,
        tracker: Tracker,
        domain: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Apply the requested adjustment and report back."""
        args = {
            k: tracker.slots[k]
            for k in ("action", "location", "device", "parameter", "amount")
        }
        logger.info("Executing: %s", args)

        # TODO: validate action as well

        try:
            # TODO: May be better to set a slot and utter something in domain.yml
            # TODO: support multiple devices being set at once
            dispatcher.utter_message(
                f"Set {tracker.slots['device']} {tracker.slots['parameter']}"
            )
        except KeyError:
            logger.exception("Error making adjustment")
            dispatcher.utter_message(
                f"Sorry, there was an error setting the {tracker.slots['device']} {tracker.slots['parameter']}."
            )

        return [BotUttered("Unimplemented, come back later."), AllSlotsReset()]


########################
# Queries
########################


class ValidateQueryForm(DeviceLocationForm):
    """Docstring."""

    def name(self) -> str:
        """Docstring."""
        return "validate_query_parameter_form"


class SubmitQuery(Action):
    """Action for submitting a change to a device."""

    def name(self) -> str:
        """Docstring."""
        return "action_submit_query_parameter"

    async def run(
        self,
        dispatcher,
        tracker: Tracker,
        domain: dict[str, Any],
    ) -> list[EventType]:
        """Docstring."""
        args = {k: tracker.slots[k] for k in ("location", "device", "parameter")}
        logger.info("Executing: %s", args)

        return [BotUttered("Unimplemented, come back later."), AllSlotsReset()]


class SubmitFilter(Action):
    """Docstring."""

    def name(self) -> str:
        """Docstring."""
        return "action_submit_filter"

    async def run(
        self,
        dispatcher,
        tracker: Tracker,
        domain: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Docstring."""
        return [BotUttered("Unimplemented, come back later."), AllSlotsReset()]
