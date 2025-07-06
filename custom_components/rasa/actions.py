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


def _get_if_single(slots: dict[str, Any], elements: set[str], name: str):
    """Update the slots to set depending on whether we allow multiple elements."""
    if len(elements) == 1:
        element = elements.pop()
        logger.debug("Found single %s %s", name, element)
        slots[name] = element


def _english_list(objs: list[Any], join: str = "and") -> str:
    """Create an english-language sequence of things."""
    count = len(objs)
    if count == 0:
        # Shouldn't happen
        return ""
    if count == 1:
        # Handle sets as well as tuples and lists
        return "".join(objs)
    if count == 2:
        return f" {join} ".join(objs)

    # Oxford comma 4lyfe
    return ", ".join(objs[:-1]) + f", {join} " + str(objs[-1])


class DeviceLocationForm(FormValidationAction):
    """Form for identifying a specific device in a particular location."""

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

    @staticmethod
    def get_slots_from_tracker(tracker: Tracker) -> dict[str, Any]:
        """Extract current known slots from teh tracker.

        Lowercase all text.
        """
        current_slots: dict[str, Any] = {}
        for k, v in tracker.slots.items():
            if isinstance(v, str):
                current_slots[k] = v.lower()
            else:
                current_slots[k] = v

        return current_slots

    def _extract_location(self, candidate_loc: str) -> dict[str, Any]:
        """Interpret the location and return a set of slots to update.

        Throws UnknownName if no location was found.
        """

        if candidate_loc:
            return self.validate_location(candidate_loc)
        return {}

    def _extract_device(self, candidate_dev: str) -> dict[str, Any]:
        """Interpret a device name.

        Attempt to detect a plural noun or phrase and return the device as a list.
        """

        slots_to_set: dict[str, Any] = {}
        if candidate_dev:
            device: str = candidate_dev
            # `device` should be list of names
            slots_to_set["device"] = [device]
            if device.endswith("s"):
                singular = device.rstrip("s")
                slots_to_set["multiple"] = True
                # Look for the singular form of the name as well
                slots_to_set["device"].append(singular)

        return slots_to_set

    def _find_alt(
        self, current_slots: dict[str, Any], exclude: list[str]
    ) -> str | None:
        """Find device matches leaving out one or more constraints.

        Useful if we find no matches and want to present alternatives.
        """
        alt_slots = {k: v for k, v in current_slots.items() if k not in exclude}
        alt_slots.update({k: {} for k in exclude})
        actions, location_ids, entity_ids, parameters = _HASS_IF.match_entities(
            alt_slots
        )

        if len(entity_ids) == 0:
            return None

        descr = []
        # TODO: not sure how much context to give on what we searched for.
        # Let's stick with just device for now to keep things simple.
        if "device" not in exclude and current_slots["device"]:
            descr.append("called " + _english_list(current_slots["device"], "or"))

        # TODO: this mirrors the "filters" functionality reproduced in both
        # `run` methods below. Refactor?
        # We only print this info if excluding the constraint produced more
        # results. That's why we check for both exclusion and the existence of
        # a previously set value.
        if "location" in exclude and current_slots["location"]:
            descr.append("in " + _english_list(location_ids))
        if "action" in exclude and current_slots["action"]:
            if actions:
                descr.append("that can " + _english_list(actions, "or"))
            else:
                descr.append("with no actions")
        if "parameter" in exclude and current_slots["parameter"]:
            if parameters:
                descr.append("with a " + _english_list(parameters))
            else:
                descr.append("with no parameters")
        return f"However I did find {len(entity_ids)} devices {' '.join(descr)}"

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
        current_slots = self.get_slots_from_tracker(tracker)

        slots_to_set: dict[str, Any] = {}

        # Validate location first.
        try:
            slots_to_set.update(self._extract_location(current_slots["location"]))
        except UnknownName as ex:
            return [BotUttered(str(ex))]

        slots_to_set.update(self._extract_device(current_slots["device"]))

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
                filters.append("in " + _english_list(locs))

            if current_slots["device"] is not None:
                filters.append("called " + _english_list(current_slots["device"]))

            if current_slots["parameter"] is not None:
                filters.append("with a " + current_slots["parameter"])

            filter_msg = " ".join(filters)
            logger.warning("no devices found %s", filter_msg)
            msg = f"Sorry, I don't know of any devices {filter_msg}."

            # TODO: we might want this extra search optional, it could get tedious.
            # For now only find alternates in situations that can get easily confused.
            alt_help = self._find_alt(current_slots, ["parameter", "action"])
            if alt_help is not None:
                msg += " " + alt_help

            response = BotUttered(msg)

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

        # Update selected entity IDs if the user expects multiple devices. The device
        # IDs should also always be a list, even if only one device is returned.
        slots_to_set["device"] = list(entity_ids)

        # Locations and parameters we only set if we only find one.
        _get_if_single(slots_to_set, location_ids, "location")
        _get_if_single(slots_to_set, parameters, "parameter")

        logger.info("Finishing with slots: %s", slots_to_set)

        ret = [SlotSet(key=k, value=v) for k, v in slots_to_set.items()]
        # We can terminate the form with SlotSet(REQUESTED_SLOT, None) or
        # with ActiveLoop(None). If we don't terminate and any slots in
        # domain.yml are unset, the server will request more slots to be
        # filled.
        # See rasa/core/actions/forms.py: FormAction::is_done()
        # First update our current state
        current_slots.update(slots_to_set)
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
    """Form for performing a specific action on a particular device in a location.

    An amount may optionally be specified for setting an attribute on a device like
    brightness or volume.
    """

    def name(self) -> str:
        """Docstring."""
        return "_helper_device_amount_form"

    # Format: dict{txt: (Absolute, Amount)}
    ACTION_DICT = {
        "turn_up": ("set_relative", 25),
        "turn_down": ("set_relative", 25),
        "increase": ("set_relative", 25),
        "decrease": ("set_relative", 25),
        "set": ("set_absolute", 0.0),
        # TODO: relative amount depends on what you're adjusting. A fan might be turned up by 25%,
        #   lights by 15%, and temperature by 2 degrees.
    }

    def _extract_action(self, candidate_act: str, amount: str) -> dict[str, Any]:
        """Attempt to interpret an action phrase.

        Actions are snake case and type str.
        """
        slots_to_set: dict[str, Any] = {}

        if candidate_act:
            # Actions are snake case
            action = candidate_act.replace(" ", "_")
            if action in self.ACTION_DICT:
                action, amount = self.ACTION_DICT[action]
                if amount is None:
                    slots_to_set["amount"] = amount
            slots_to_set["action"] = action

        return slots_to_set

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

        current_slots = self.get_slots_from_tracker(tracker)

        slots_to_set: dict[str, Any] = {}

        # Validate location first.
        try:
            slots_to_set.update(self._extract_location(current_slots["location"]))
        except UnknownName as ex:
            return [BotUttered(str(ex))]

        slots_to_set.update(
            self._extract_action(current_slots["action"], current_slots["amount"])
        )
        slots_to_set.update(
            self._extract_amount(current_slots["amount"], current_slots["action"])
        )
        slots_to_set.update(self._extract_device(current_slots["device"]))

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
                filters.append("in " + _english_list(locs))

            if current_slots["device"] is not None:
                filters.append("called " + _english_list(current_slots["device"]))

            if current_slots["parameter"] is not None:
                filters.append("with a " + current_slots["parameter"])

            if current_slots["action"] is not None:
                filters.append(
                    "that we can " + current_slots["action"].replace("_", " ")
                )

            filter_msg = " ".join(filters)
            logger.warning("no devices found %s", filter_msg)
            msg = f"Sorry, I don't know of any devices {filter_msg}."

            # TODO: we might want this extra search optional, it could get tedious.
            # For now only find alternates in situations that can get easily confused.
            alt_help = self._find_alt(current_slots, ["parameter", "action"])
            if alt_help is not None:
                msg += " " + alt_help

            response = BotUttered(msg)

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
        slots_to_set["device"] = list(entity_ids)

        # Locations, actions, and parameters we only set if we only find one.
        _get_if_single(slots_to_set, actions, "action")
        _get_if_single(slots_to_set, location_ids, "location")
        _get_if_single(slots_to_set, parameters, "parameter")

        logger.debug("Finishing with slots: %s", slots_to_set)

        ret = [SlotSet(key=k, value=v) for k, v in slots_to_set.items()]

        # Determine whether to terminate the form or ask for more data.
        # First update our current state
        current_slots.update(slots_to_set)
        next_slot = self._next_slot(current_slots)
        if next_slot is not None:
            ret.append(SlotSet(key=REQUESTED_SLOT, value=next_slot))
        else:
            # We have all the data we need; terminate the form
            ret.append(ActiveLoop(None))

        return ret

    def _extract_amount(self, candidate_amount: str, action: str) -> dict[str, Any]:
        """Validate and parse the amount.

        Implies 'set_absolute' if an amount was parsed but only if the
        action is not already set.
        """
        ret: dict[str, Any] = {"amount": candidate_amount}
        mult = 1.0

        if isinstance(candidate_amount, str):
            words = candidate_amount.split(" ")
            for word in words:
                if word == "percent":
                    mult = 0.01
                else:
                    try:
                        ret["amount"] = float(word)
                        if action is None:
                            # Implicitly set absolute action when parsing amount
                            ret["action"] = "set_absolute"
                    except ValueError:
                        pass

        if isinstance(ret["amount"], float):
            # Apply percentage or units
            ret["amount"] *= mult

        logger.debug("validate_amount %s -> %s", candidate_amount, ret)

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
            # Note we can't test for truthiness because 0.0 is a valid value
            if current_slots["amount"] is None:
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
            if not isinstance(amount, (int, float)):
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
