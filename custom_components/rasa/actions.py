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
from rasa_sdk.events import AllSlotsReset, BotUttered, EventType, SlotSet
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.forms import FormValidationAction

from .hass_if import HassIface

logger = logging.getLogger(__name__)

_HASS_IF: HassIface = None


class UnknownName(BaseException):
    """An exception class for indicating that we don't recognize the string."""


def register_hass(hass_if: HassIface):
    """Register HASS interface with action server."""
    # pylint: disable=global-statement
    global _HASS_IF
    _HASS_IF = hass_if


class DeviceLocationForm(FormValidationAction):
    """Docstring."""

    def name(self) -> str:
        """Name."""
        return "_helper_device_location_form"

    async def validate_location(self, candidate: str) -> dict[str, Any]:
        """Validate the requested location.

        Candidate location should already be lowercased.
        """
        logger.debug("Validating location '%s'", candidate)

        if candidate in ("any", "all", "each"):
            # These values indicate we should be dealing with all entities matching
            # any remaining conditions and that we probably expect multiple entities
            # to match.
            # Set an empty string rather than None to indicate that the slot was
            # actually set.
            return {"multiple": True, "location": ""}

        loc = _HASS_IF.find_location_by_name(candidate)
        if loc is not None:
            return {"location": loc["id"]}

        raise UnknownName(f"Sorry, I don't know the location {candidate}")

    async def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: dict,
    ) -> list[EventType]:
        """Validate form.

        For DeviceLocation we only have location, device, and parameters.
        We need to validate all of these at the same time, though the location can be validated
        first.
        """

        current_slots: dict[str, Any] = {}
        for k, v in tracker.slots.items():
            if isinstance(v, str):
                current_slots[k] = v.lower()
            else:
                current_slots[k] = v

        slots_to_set: dict[str, Any] = {}
        # Validate location first.
        if tracker.slots["location"]:
            try:
                updates = self.validate_location(tracker.slots["location"])
                # Apply changes to the current working set of slots and keep track of
                # which slots need to be set on the server.
                slots_to_set.update(updates)
            except UnknownName as ex:
                return [BotUttered(str(ex))]

        if current_slots["device"]:
            device: str = current_slots["device"]
            if device.endswith("s"):
                device = device.rstrip("s")
                slots_to_set["multiple"] = True
                slots_to_set["device"] = device

        # Apply any slot changes we've accumulated so far
        current_slots.update(slots_to_set)

        logger.debug("Validating %s: %s", self.__class__.__name__, current_slots)

        _, location_ids, entity_ids, parameters = _HASS_IF.match_entities(current_slots)

        logger.debug(
            "Found %d locations, %d entities, %d parameters",
            len(location_ids),
            len(entity_ids),
            len(parameters),
        )

        if len(entity_ids) == 0:
            filters = []
            if current_slots["location"]:
                filters.append("in " + current_slots["location"])

            if current_slots["device"] is not None:
                filters.append("called " + current_slots["device"])

            if current_slots["parameter"] is not None:
                filters.append("with a " + current_slots["parameter"])

            filter_msg = " ".join(filters)
            logger.warning("no devices found %s", filter_msg)
            response = BotUttered(f"Sorry, I don't know of any devices {filter_msg}.")

            # TODO: what do we do if nothing is found? Can we discard just one constraint?
            return [response, AllSlotsReset()]

        if len(entity_ids) > 1 and not current_slots.get("multiple", False):
            # Found more than one matching entity ID but only expected one.
            # TODO: confirmation dialog path
            # TODO: actually we probably only want to confirm this once we know for sure
            # we've filled all slots, which means we need a "plural" slot.
            rsp: list[EventType] = [
                BotUttered(
                    f"Found {len(entity_ids)} devices in {len(location_ids)} locations, but it sounds like you only wanted one. Do you want to adjust them all?"
                )
            ]
            rsp.extend(SlotSet(key=k, value=v) for k, v in slots_to_set.items())
            return rsp

        # Found at least one matching entity
        if len(location_ids) == 1:
            loc_id = location_ids.pop()
            logger.debug("Found single location %s", loc_id)
            slots_to_set["location"] = loc_id
        if len(entity_ids) == 1:
            ent_id = entity_ids.pop()
            logger.debug("Found single device %s", ent_id)
            slots_to_set["device"] = ent_id
        if len(parameters) == 1:
            # There's a relationship between parameters and actions that doesn't seem well
            # defined just yet. "turn on" may affect brightness, for instance, but we don't
            # necessarily need an "amount" for such an action. Similar for mute, etc.
            #
            # See e.g. the action schema in homeassistant/components/light/device_action.py
            # This may become more clear when we figure out how to actually *call* the
            # actions.
            parameter = parameters.pop()
            logger.debug("Found single parameter %s", parameter)
            slots_to_set["parameter"] = parameter

        logger.debug("Finishing with slots: %s", slots_to_set)

        return [SlotSet(key=k, value=v) for k, v in slots_to_set.items()]


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

    async def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: dict,
    ) -> list[EventType]:
        """Validate form.

        For DeviceAmountForm we include the action along with the location, device, and parameters.
        We need to validate all of these at the same time, though the location can be validated
        first.
        """

        # TODO: there's a lot of repetition here. There might be a better way of structuring this.

        current_slots: dict[str, Any] = {}
        for k, v in tracker.slots.items():
            if isinstance(v, str):
                current_slots[k] = v.lower()
            else:
                current_slots[k] = v

        slots_to_set: dict[str, Any] = {}
        # Validate location first.
        if tracker.slots["location"]:
            try:
                updates = self.validate_location(tracker.slots["location"])
                # Apply changes to the current working set of slots and keep track of
                # which slots need to be set on the server.
                slots_to_set.update(updates)
            except UnknownName as ex:
                return [BotUttered(str(ex))]

        if current_slots["action"]:
            # Actions are snake case
            slots_to_set["action"] = current_slots["action"].replace(" ", "_")
        if current_slots["device"]:
            device: str = current_slots["device"]
            if device.endswith("s"):
                device = device.rstrip("s")
                slots_to_set["multiple"] = True
                slots_to_set["device"] = device

        # Apply any slot changes we've accumulated so far
        current_slots.update(slots_to_set)

        logger.debug("Validating %s: %s", self.__class__.__name__, current_slots)

        actions, location_ids, entity_ids, parameters = _HASS_IF.match_entities(
            current_slots
        )

        logger.debug(
            "Found %d actions, %d locations, %d entities, %d parameters",
            len(actions),
            len(location_ids),
            len(entity_ids),
            len(parameters),
        )

        if len(entity_ids) == 0:
            filters = []
            if current_slots["location"]:
                filters.append("in " + current_slots["location"])

            if current_slots["device"] is not None:
                filters.append("called " + current_slots["device"])

            if current_slots["parameter"] is not None:
                filters.append("with a " + current_slots["parameter"])

            if current_slots["action"] is not None:
                filters.append("that we can " + current_slots["action"])

            filter_msg = " ".join(filters)
            logger.warning("no devices found %s", filter_msg)
            response = BotUttered(f"Sorry, I don't know of any devices {filter_msg}.")

            # TODO: what do we do if nothing is found? Can we discard just one constraint?
            return [response, AllSlotsReset()]

        if len(entity_ids) > 1 and not current_slots.get("multiple", False):
            # Found more than one matching entity ID but only expected one.
            # TODO: confirmation dialog path
            # TODO: actually we probably only want to confirm this once we know for sure
            # we've filled all slots, which means we need a "plural" slot.
            rsp: list[EventType] = [
                BotUttered(
                    f"Found {len(entity_ids)} devices in {len(location_ids)} locations, but it sounds like you only wanted one. Do you want to adjust them all?"
                )
            ]
            rsp.extend(SlotSet(key=k, value=v) for k, v in slots_to_set.items())
            return rsp

        # Found at least one matching entity
        if len(actions) == 1:
            action = actions.pop()
            logger.debug("Found single action %s", action)
            slots_to_set["location"] = action
        if len(location_ids) == 1:
            loc_id = location_ids.pop()
            logger.debug("Found single location %s", loc_id)
            slots_to_set["location"] = loc_id
        if len(entity_ids) == 1:
            ent_id = entity_ids.pop()
            logger.debug("Found single device %s", ent_id)
            slots_to_set["device"] = ent_id
        if len(parameters) == 1:
            # There's a relationship between parameters and actions that doesn't seem well
            # defined just yet. "turn on" may affect brightness, for instance, but we don't
            # necessarily need an "amount" for such an action. Similar for mute, etc.
            #
            # See e.g. the action schema in homeassistant/components/light/device_action.py
            # This may become more clear when we figure out how to actually *call* the
            # actions.
            parameter = parameters.pop()
            logger.debug("Found single parameter %s", parameter)
            slots_to_set["parameter"] = parameter

        logger.debug("Finishing with slots: %s", slots_to_set)

        return [SlotSet(key=k, value=v) for k, v in slots_to_set.items()]

    async def validate_amount(
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
