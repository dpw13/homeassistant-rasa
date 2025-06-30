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
from rasa_sdk.events import AllSlotsReset, EventType
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.forms import FormValidationAction

logger = logging.getLogger(__name__)


TEST_DEVICE_LIST: dict[str, dict[str, dict[str, Any]]] = {
    "basement": {
        "light": {
            "brightness": 4,
        },
        "fan": {
            "speed": 1,
        },
        "thermostat": {
            "temperature": 72,
        },
    },
    "upstairs": {
        "light": {
            "brightness": 2,
        },
    },
    "living room": {
        "fan": {
            "speed": 1,
        },
        "light": {
            "brightness": 4,
        },
        "thermostat": {
            "temperature": 72,
        },
    },
    "outside": {
        "gate": {
            "state": "closed",
        },
        "garage": {
            "state": "closed",
        },
    },
}


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
        if slot_value not in TEST_DEVICE_LIST:
            dispatcher.utter_message(f"Sorry, I don't know the location {slot_value}")
            return {"location": None}
        return {"location": slot_value}

    def validate_device(
        self,
        slot_value: str,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: dict,
    ) -> dict[str, Any]:
        """Validate device to try to find a set of devices given the other constraints."""
        # TODO: better lemmatization
        # TODO: determine whether we allow multiple matches by whether the device is plural
        # TODO: support multiple matching devices
        slot_value = slot_value.rstrip("s")
        location = tracker.slots["location"]
        for loc, devices in TEST_DEVICE_LIST.items():
            if location is None or loc == location:
                if slot_value in devices:
                    logger.debug("Found matching device %s %s", loc, slot_value)
                    ret = {"device": slot_value}
                    if tracker.slots["parameter"] is None:
                        parameters = list(devices[slot_value].keys())
                        logger.debug("Assuming parameter %s from device", parameters[0])
                        ret["parameter"] = parameters[0]
                    if tracker.slots["location"] is None:
                        logger.debug("Assuming location %s from device", loc)
                        ret["location"] = loc

                    return ret

        if location is None:
            logger.warning("no device '%s' found", slot_value)
            dispatcher.utter_message(
                f"Sorry, I don't know of any devices called {slot_value}"
            )
        else:
            logger.warning("no device '%s' found for %s", slot_value, location)
            dispatcher.utter_message(
                f"Sorry, I don't know of any devices called {slot_value} in {location}"
            )
        return {"device": None}

    def validate_parameter(
        self,
        slot_value: str,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: dict,
    ) -> dict[str, Any]:
        """Validate parameter to try to find a set of devices given the other constraints."""

        # TODO: support multiple matching devices
        location = tracker.slots["location"]
        for loc, devices in TEST_DEVICE_LIST.items():
            if location is None or loc == location:
                for device, params in devices.items():
                    if slot_value in params:
                        logger.debug(
                            "Found matching device %s %s with parameter %s",
                            loc,
                            device,
                            slot_value,
                        )
                        return {"device": device, "parameter": slot_value}

        # No matching device was found. At this point just determine how we want to respond to
        # the user.
        if location is None:
            logger.warning("no devices with '%s' found", slot_value)
            dispatcher.utter_message(
                f"Sorry, I don't know of any devices with a {slot_value}"
            )
        else:
            logger.warning("no devices with '%s' found for %s", slot_value, location)
            dispatcher.utter_message(
                f"Sorry, I don't know of any devices with a {slot_value} in {location}"
            )
        return {"parameter": None}


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
        logger.info("Found action '%s'", slot_value)
        if slot_value in self.ACTION_DICT:
            t = self.ACTION_DICT[slot_value]
            ret: dict[str, Any] = {"action": t[0]}
            if tracker.slots["amount"] is None:
                # Only set the amount if it's not already set
                # TODO: can we ensure amount is parsed first?
                ret["amount"] = t[1]
            return ret

        return {}

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

        try:
            loc = TEST_DEVICE_LIST[tracker.slots["location"]]
            dev = loc[tracker.slots["device"]]
            if tracker.slots["action"] == "set_absolute":
                dev[tracker.slots["parameter"]] = tracker.slots["amount"]
            elif tracker.slots["action"] == "set_relative":
                # TODO: clamp values
                dev[tracker.slots["parameter"]] += tracker.slots["amount"]
            else:
                raise ValueError(
                    f"Action {tracker.slots['action']} unimplemented for {tracker.slots['device']}"
                )
            # TODO: May be better to set a slot and utter something in domain.yml
            # TODO: support multiple devices being set at once
            dispatcher.utter_message(
                f"Set {tracker.slots['device']} {tracker.slots['parameter']} to {dev[tracker.slots['parameter']]}"
            )
        except KeyError:
            logger.exception("Error making adjustment")
            dispatcher.utter_message(
                f"Sorry, there was an error setting the {tracker.slots['device']} {tracker.slots['parameter']}."
            )

        return [AllSlotsReset()]


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

        try:
            loc = TEST_DEVICE_LIST[tracker.slots["location"]]
            dev = loc[tracker.slots["device"]]
            amount = dev[tracker.slots["parameter"]]
            # TODO: May be better to set a slot and utter something in domain.yml
            dispatcher.utter_message(
                f"The {tracker.slots['location']} {tracker.slots['device']} {tracker.slots['parameter']} is {amount}"
            )
        except KeyError:
            logger.exception("Error submitting query")
            dispatcher.utter_message(
                f"Sorry, there was an error getting the {tracker.slots['device']} {tracker.slots['parameter']}."
            )

        return [AllSlotsReset()]


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
        logger.warning("UNIMPLEMENTED")
        return [AllSlotsReset()]
