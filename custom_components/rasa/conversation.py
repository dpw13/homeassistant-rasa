"""Config flow for the Rasa NLP integration."""

# pylint: disable=fixme

from __future__ import annotations

import logging

import rasa_client
from rasa_client.rest import ApiException

from homeassistant import core
from homeassistant.components.conversation import (
    AbstractConversationAgent,
    ChatLog,
    ConversationEntity,
    ConversationEntityFeature,
    ConversationInput,
    ConversationResult,
    async_set_agent,
)
from homeassistant.components.homeassistant import async_should_expose
from homeassistant.const import MATCH_ALL
from homeassistant.exceptions import IntegrationError
from homeassistant.helpers import device_registry as dr, intent, start as ha_start
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_added_domain

from . import RasaConfigEntry
from .action_server import RasaActionServer
from .const import DATA_RASA_ENTITY, DEFAULT_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)


# Copied from homeassistant/components/conversation/default_agent.py
async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: RasaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up entity registry listener for the rasa agent."""
    agent = RasaAgent(hass, config_entry)
    # How are the entity and the agent related?
    async_add_entities([agent])

    hass.data[DATA_RASA_ENTITY] = agent
    # Registers the agent with the manager
    async_set_agent(hass, config_entry, agent)

    # components.conversation.async_setup calls async_setup_default_agent
    # instead of calling async_setup() on the agent. TODO: see how other
    # conversation agents handle prepare().
    await agent.async_setup()

    # TODO: unclear if the below are necessary for non-default agent
    @core.callback
    def async_entity_state_listener(
        event: core.Event[core.EventStateChangedData],
    ) -> None:
        """Set expose flag on new entities."""
        async_should_expose(hass, DOMAIN, event.data["entity_id"])

    @core.callback
    def async_hass_started(hass: core.HomeAssistant) -> None:
        """Set expose flag on all entities."""
        for state in hass.states.async_all():
            async_should_expose(hass, DOMAIN, state.entity_id)
        async_track_state_added_domain(hass, MATCH_ALL, async_entity_state_listener)

    ha_start.async_at_started(hass, async_hass_started)


class RasaAgent(ConversationEntity, AbstractConversationAgent):
    """Entity that communicates with a Rasa server."""

    def __init__(self, hass: core.HomeAssistant, entry: RasaConfigEntry) -> None:
        """Initialize the agent."""
        self._hass = hass
        self._entry = entry
        self._api = entry.runtime_data
        self._server_info_api = rasa_client.ServerInformationApi(self._api)
        self._tracker_api = rasa_client.TrackerApi(self._api)
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Rasa",
            model="Custom",
            entry_type=dr.DeviceEntryType.SERVICE,
        )
        self._attr_supported_features = ConversationEntityFeature.CONTROL
        self._action_server = RasaActionServer(
            hass, entry.data.get("action_port", 5055)
        )

    @property
    def supported_languages(self) -> list[str]:
        """Return a list of supported languages."""
        return ["en"]

    async def async_setup(self) -> None:
        """Connect to server."""
        try:
            rsp_ver = await self._server_info_api.get_version(DEFAULT_TIMEOUT)
            _LOGGER.info("Connected to Rasa server version %s", rsp_ver.version)
            rsp_stat = await self._server_info_api.get_status(DEFAULT_TIMEOUT)
            _LOGGER.info(
                "Rasa server running model %s at %s",
                rsp_stat.model_id,
                rsp_stat.model_file,
            )
        except ApiException as ex:
            raise IntegrationError from ex

        await self._action_server.launch()

    # This is where the actual conversation entity functionality is
    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult:
        """Process a sentence."""

        # Send text to rasa
        # See rasa/core/training/interactive.py::
        #     send_message: send message to server (POST conversations/<id>/messages)
        #         request_prediction: request next predicted action from server (POST predict)
        #         send_action: execute action on server (POST execute)
        #         ^-- repeat until action is listen
        #     ^-- repeat until conversation done
        conv_id = user_input.conversation_id or "homeassistant"

        # TODO: it looks like `triggerConversationIntent` might be the simpler way
        # to go about this as it predicts and executes actions immediately.
        # Alternatively it looks like `addConversationTrackerEvents` will automatically
        # create a new session if needed.
        if len(chat_log.content) == 2:  # TODO: HACK
            msg_req = rasa_client.AddConversationTrackerEventsRequest(
                rasa_client.Event(
                    rasa_client.SessionStartedEvent.from_dict(
                        {"event": "session_started"}
                    )
                )
            )
            await self._tracker_api.add_conversation_tracker_events(
                conversation_id=conv_id,
                add_conversation_tracker_events_request=msg_req,
            )
        else:
            _LOGGER.info("Chat log so far: %s", chat_log)
        tracker = await self._tracker_api.add_conversation_message(
            conversation_id=conv_id,
            message=rasa_client.Message(
                text=user_input.text,
                sender="user",
                # TODO: update server to support/record sender ID
                # sender=user_input.context.user_id
            ),
        )
        if tracker.latest_message and tracker.latest_message.intent:
            rasa_intent = tracker.latest_message.intent
            _LOGGER.info("<- %f intent: %s", rasa_intent.confidence, rasa_intent.name)

        prediction: rasa_client.PredictResultScoresInner | None = None
        messages: list[str] = []
        while prediction is None or prediction.action != "action_listen":
            # Predict
            predict_result = await self._tracker_api.predict_conversation_action(
                conversation_id=conv_id
            )
            if predict_result.scores:
                for score in predict_result.scores[:5]:
                    _LOGGER.info("<- %f: %s", score.score, score.action)
            else:
                raise IntegrationError("Received empty prediction result from server")

            # Scores are sorted descending before being returned.
            prediction = predict_result.scores[0]
            if not prediction.action:
                raise IntegrationError("Action prediction name is empty")
            _LOGGER.info("-> executing %s", prediction.action)

            # Execute
            action_req = rasa_client.ActionRequest(
                name=prediction.action,
                policy=predict_result.policy,
                confidence=prediction.score,
            )
            exec_result = await self._tracker_api.execute_conversation_action(
                conversation_id=conv_id, action_request=action_req
            )
            if exec_result.messages:
                messages.extend([m.text for m in exec_result.messages if m.text])

        _LOGGER.info("<- %d messages", len(messages))
        if messages:
            rsp_text = "\n".join(messages)
        else:
            rsp_text = "... I have nothing to say."

        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(rsp_text)
        return ConversationResult(
            conversation_id=conv_id,
            response=response,
            continue_conversation=False,
        )
