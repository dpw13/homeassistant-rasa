"""Action server wrapper for the Rasa NLP integration."""

# pylint: disable=fixme

from __future__ import annotations

from functools import partial
import logging

from rasa_sdk.endpoint import create_app, load_tracer_provider
from rasa_sdk.executor import ActionExecutor
from rasa_sdk.plugin import plugin_manager
from sanic.worker.loader import AppLoader

from homeassistant import core

from . import actions
from .const import SERVER_KEEPALIVE
from .hass_if import HassIface

_LOGGER = logging.getLogger(__name__)


class RasaActionServer:
    """Action server that receives queries from the Rasa server."""

    def __init__(self, hass: core.HomeAssistant, port: int) -> None:
        """Initialize the action server."""
        self._hass = hass
        self._port = port
        self._iface = None

        # Server settings
        self._host = "0.0.0.0"
        self._protocol = "http"

    async def launch(self) -> None:
        """Launch server."""
        self._iface = HassIface(self._hass)
        actions.register_hass(self._iface)

        loader = AppLoader(factory=self._create_server)
        self._hass.async_create_background_task(self._run_server(loader), "rasa-action")

    async def update(self) -> None:
        """Update HA data."""
        await self._iface.load()

    def _create_server(self):
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
        await self._iface.load()

        await server.serve_forever()
