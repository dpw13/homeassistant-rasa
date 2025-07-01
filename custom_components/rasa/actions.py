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
from rasa_sdk.events import ActiveLoop, AllSlotsReset, BotUttered, EventType, SlotSet
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.forms import REQUESTED_SLOT, FormValidationAction

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


def _update_if_amount(
    slots: dict[str, Any], elements: set[str], name: str, multiple: bool
):
    """Update the slots to set depending on whether we allow multiple elements."""
    if not multiple and len(elements) == 1:
        action = elements.pop()
        logger.debug("Found single %s %s", name, action)
        slots[name] = action
    elif multiple:
        slots[name] = list(elements)


class DeviceLocationForm(FormValidationAction):
    """Docstring."""

    def name(self) -> str:
        """Name."""
        return "_helper_device_location_form"

    def validate_location(self, candidate: str) -> dict[str, Any]:
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

        loc_ids = _HASS_IF.find_location_by_name(candidate)
        if loc_ids:
            return {"location": loc_ids}

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
        if current_slots["location"]:
            try:
                updates = self.validate_location(current_slots["location"])
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
                if isinstance(current_slots["location"], str):
                    locs = [current_slots["location"]]
                else:
                    locs = current_slots["location"]
                filters.append("in " + ", ".join(locs))

            if current_slots["device"] is not None:
                filters.append("called " + current_slots["device"])

            if current_slots["parameter"] is not None:
                filters.append("with a " + current_slots["parameter"])

            filter_msg = " ".join(filters)
            logger.warning("no devices found %s", filter_msg)
            response = BotUttered(f"Sorry, I don't know of any devices {filter_msg}.")

            # TODO: what do we do if nothing is found? Can we discard just one constraint?
            return [response, AllSlotsReset()]

        multiple = current_slots.get("multiple", False)

        if len(entity_ids) > 1 and not multiple:
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

        # Update selected entity IDs if the user expects multiple devices.
        _update_if_amount(slots_to_set, entity_ids, "device", multiple)

        # Locations and parameters we only set if we only find one.
        _update_if_amount(slots_to_set, location_ids, "location", False)
        _update_if_amount(slots_to_set, parameters, "parameter", False)

        logger.info("Finishing with slots: %s", slots_to_set)

        ret = [SlotSet(key=k, value=v) for k, v in slots_to_set.items()]
        # We can terminate the form with SlotSet(REQUESTED_SLOT, None) or
        # with ActiveLoop(None). If we don't terminate and any slots in
        # domain.yml are unset, the server will request more slots to be
        # filled.
        # See rasa/core/actions/forms.py: FormAction::is_done()
        next_slot = self._next_slot(current_slots)
        if next_slot is not None:
            ret.append(SlotSet(key=REQUESTED_SLOT, value=next_slot))
        else:
            # We have all the data we need; terminate the form
            ret.append(ActiveLoop(None))

        return ret

    def _next_slot(self, current_slots: dict[str, Any]) -> str | None:
        """Determine which slots still need to be filled.

        Scenarios:
        turn on all the lights:
            turn on requires no amount or parameter
            "all" indicates multiple intent so no location required
        turn off the upstairs lights:
            turn off requires no amount or parameter
            "lights" indicates multiple intent so multiple matches are acceptable
        """
        if isinstance(current_slots["device"], (list, set)):
            device_count = len(current_slots["device"])
        elif isinstance(current_slots["device"], str):
            device_count = 1
        else:
            device_count = 0

        if not current_slots["action"]:
            # We always need an action.
            return "action"
        if device_count == 0:
            # We always need at least one device.
            return "device"
        if device_count > 1 and not current_slots["multiple"]:
            # We match more devices than expected. Try to narrow them down.
            return "location"

        return None


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
        if current_slots["location"]:
            try:
                updates = self.validate_location(current_slots["location"])
                # Apply changes to the current working set of slots and keep track of
                # which slots need to be set on the server.
                slots_to_set.update(updates)
            except UnknownName as ex:
                return [BotUttered(str(ex))]
        if isinstance(current_slots["amount"], str):
            # Attempt to extract amount if the slot is set
            updates = self.validate_amount(current_slots, current_slots["amount"])
            slots_to_set.update(updates)

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
                if isinstance(current_slots["location"], str):
                    locs = [current_slots["location"]]
                else:
                    locs = current_slots["location"]
                filters.append("in " + ", ".join(locs))

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

        multiple = current_slots.get("multiple", False)

        if len(entity_ids) > 1 and not multiple:
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

        # Update selected entity IDs if the user expects multiple devices.
        _update_if_amount(slots_to_set, entity_ids, "device", multiple)

        # Locations, actions, and parameters we only set if we only find one.
        _update_if_amount(slots_to_set, actions, "action", False)
        _update_if_amount(slots_to_set, location_ids, "location", False)
        _update_if_amount(slots_to_set, parameters, "parameter", False)

        logger.debug("Finishing with slots: %s", slots_to_set)

        ret = [SlotSet(key=k, value=v) for k, v in slots_to_set.items()]

        # Determine whether to terminate the form or ask for more data.
        next_slot = self._next_slot(current_slots)
        if next_slot is not None:
            ret.append(SlotSet(key=REQUESTED_SLOT, value=next_slot))
        else:
            # We have all the data we need; terminate the form
            ret.append(ActiveLoop(None))

        return ret

    def validate_amount(
        self, current_slots: dict[str, Any], candidate: str
    ) -> dict[str, Any]:
        """Validate and parse the amount."""
        ret: dict[str, Any] = {"amount": candidate}
        mult = 1.0

        if isinstance(candidate, str):
            words = candidate.split(" ")
            for word in words:
                if word == "percent":
                    mult = 0.01
                else:
                    try:
                        ret["amount"] = float(word)
                        if current_slots["action"] is None:
                            # Implicitly set absolute action when parsing amount
                            ret["action"] = "set_absolute"
                    except ValueError:
                        pass

        if isinstance(ret["amount"], float):
            # Apply percentage or units
            ret["amount"] *= mult

        return ret

    def _next_slot(self, current_slots: dict[str, Any]) -> str | None:
        """Determine which slots still need to be filled.

        Scenarios:
        set the lights:
            still need amount
            parameter should be filled by search
        """
        next_slot = super()._next_slot(current_slots)
        if next_slot is not None:
            return next_slot

        if current_slots["action"] in ("set_absolute", "set_relative"):
            # For setting values we need the amount and parameter.
            if not current_slots["amount"]:
                return "amount"
            if not current_slots["parameter"]:
                # We weren't able to determine a parameter to adjust from the
                # device. This may indicate a more nuanced problem, but for
                # now just ask for the parameter to adjust.
                return "parameter"

        return None


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

        msg = None

        action: str = tracker.slots["action"]
        devices = tracker.slots["device"]
        if action == "set_relative":
            param = tracker.slots["parameter"]
            amount = tracker.slots["amount"]
            if isinstance(amount, (int, float)):
                msg = f"Sorry, I didn't understand the relative amount {amount}"
            else:
                try:
                    cnt = await _HASS_IF.apply_rel_adjustment(
                        device_ids=devices,
                        parameter=param,
                        amount=amount,
                    )
                    msg = f"Changed {param} on {cnt} device{'s' if cnt > 0 else ''}"
                except ValueError as ex:
                    msg = str(ex)
        elif action == "set_absolute":
            param = tracker.slots["parameter"]
            amount = tracker.slots["amount"]

            try:
                cnt = await _HASS_IF.apply_abs_adjustment(
                    device_ids=devices,
                    parameter=param,
                    amount=amount,
                )
                msg = f"Set {param} on {cnt} device{'s' if cnt > 0 else ''}"
            except ValueError as ex:
                msg = str(ex)
        else:
            try:
                cnt = await _HASS_IF.apply_action(action=action, device_ids=devices)
                # TODO: better past tense
                action_names = action.split("_")
                action_names[0] += "ed"
                action_name = " ".join(action_names).capitalize()
                msg = f"{action_name} {cnt} device{'s' if cnt > 0 else ''}"
            except ValueError as ex:
                msg = str(ex)

        return [BotUttered(msg), AllSlotsReset()]


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
