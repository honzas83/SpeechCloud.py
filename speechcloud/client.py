import json
import logging
import asyncio
import aiohttp
from pyee.asyncio import AsyncIOEventEmitter
from .sip_interface import SIPInterface

logger = logging.getLogger(__name__)

class SpeechCloud(AsyncIOEventEmitter):
    def __init__(self, options: dict):
        """
        Initialize SpeechCloud client.
        
        Args:
            options: Dictionary containing configuration options.
                     Required: 'uri' (SpeechCloud API URL).
                     Optional: 'local_dm' (URL), 'initialize_sip_later' (bool), 'sip_handler' (instance of SIPInterface),
                               'dialog_class' (class inheriting from Dialog).
        """
        super().__init__()
        self.options = options
        self.config = None
        self.is_recognizing = False
        self.initialize_sip_later = bool(options.get('initialize_sip_later', False))
        self.dialog_class = options.get('dialog_class')
        self.rtt_delay = 0.
        self._api_methods = {}
        self._api_events = {}
        self.dm = None
        self._task = None
        self.dm_schema = None
        
        self.sip_registered = False
        self.speechcloud_session_started = False
        self.speechcloud_audio_constraints = {}
        
        self.uri = options.get('uri')
        if not self.uri:
            raise ValueError('Options must contain SpeechCloud uri.')

        self.local_dm = options.get('local_dm')
        self.ssl_verify = options.get('ssl_verify', True)
        self._ws = None
        self._ws_local_dm = None
        self._session = None # aiohttp session
        self._sip_handler: SIPInterface = options.get('sip_handler') # Optional SIP implementation injection

        if self.dialog_class:
            self.dm = self.dialog_class(self)
            self.dm_schema = self.dm.get_schema()

    async def init(self):
        """Initializes the connection to SpeechCloud."""
        try:
            # Create a single session for requests and websockets
            connector = aiohttp.TCPConnector(ssl=None if self.ssl_verify else False)
            self._session = aiohttp.ClientSession(connector=connector)
            async with self._session.get(self.uri) as response:
                if 200 <= response.status < 400:
                    text_response = await response.text()
                    self.config = json.loads(text_response)
                    await self._init_connections()
                else:
                    self.emit('ws_error', status=response.status, text=response.reason)
        except Exception as e:
            logger.error(f"Initialization error: {e}")
            self.emit('ws_error', status=0, text=str(e))

    async def terminate(self):
        """Terminates all connections."""
        logger.info('SpeechCloud terminate called')
        
        if self._sip_handler:
            logger.info('SIP disconnect')
            await self._sip_handler.disconnect()
            
        if self._ws:
            logger.info('SpeechCloud WebSocket closing')
            await self._ws.close()
            self._ws = None
            logger.info('SpeechCloud WebSocket closed')

        if self._ws_local_dm:
            logger.info('SpeechCloud local DM WebSocket closing')
            await self._ws_local_dm.close()
            self._ws_local_dm = None
            logger.info('SpeechCloud local DM WebSocket closed')
            
        if self._session:
            await self._session.close()
            self._session = None

    async def _init_connections(self):
        if not self.config:
            logger.error("Configuration not loaded. Cannot init connections.")
            return

        ws_url = self.config.get('client_wss')
        if self.local_dm:
            ws_url += "?activate_client=1"
        
        logger.info(f"Connecting WebSocket {ws_url}")
        
        try:
            self._ws = await self._session.ws_connect(ws_url)
            self.emit('_ws_connected')
            self.emit('ws_connected')
            
            # Start listener task
            asyncio.create_task(self._listen_ws())
            
        except Exception as e:
            self.emit('error_init', status=getattr(e, 'status', 0), text=str(e))
            self.emit('ws_error_init', status=getattr(e, 'status', 0), text=str(e))
            return

        if self.local_dm:
            logger.info(f"Connecting local DM WebSocket {self.local_dm}")
            try:
                self._ws_local_dm = await self._session.ws_connect(self.local_dm)
                self.emit('ws_local_dm_connected')
                asyncio.create_task(self._listen_local_dm())
            except Exception as e:
                self.emit('ws_local_dm_error_init', status=getattr(e, 'status', 0), text=str(e))

    async def _listen_ws(self):
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._on_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self.emit('ws_error', text='WebSocket error')
        except Exception as e:
            logger.error(f"WebSocket listener error: {e}")
        finally:
            self.emit('_ws_closed')
            self.emit('ws_closed')
            self._ws = None

    async def _listen_local_dm(self):
        try:
            async for msg in self._ws_local_dm:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._on_dm_message(msg.data)
        except Exception as e:
            logger.error(f"Local DM WebSocket listener error: {e}")
        finally:
            self.emit('ws_local_dm_closed')
            self._ws_local_dm = None

    async def _on_message(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            logger.error("Received invalid JSON from WebSocket")
            return

        msg_type = data.get('type')

        if msg_type == "sc_activate":
            await self._init_API_schema()
        
        elif msg_type == 'asr_paused':
            self.is_recognizing = False
            self.emit(msg_type, **data)
        
        elif msg_type == 'asr_recognizing':
            self.is_recognizing = True
            self.emit(msg_type, **data)
            
        elif msg_type == 'asr_ready':
            self.emit(msg_type, **data)

        elif msg_type == 'sc_start_session':
            schema = data.get('schema', {})
            if self.dm:
                schema = self._prepare_dm_methods_events(schema)

            self._init_api_methods(schema.get('methods', {}))
            self._init_api_events(schema.get('events', {}))
            
            self.emit('_ws_session', type='started', id=data.get('session_id'))
            self.emit('ws_session', type='started', id=data.get('session_id'))
            
            logger.info(f"SpeechCloud session started, session_id: {data.get('session_id')}")
            
            self.speechcloud_session_started = True
            params = data.get('session_parameters', {})
            self.speechcloud_audio_constraints = params.get('worker', {}).get('webrtc', {})
            
            if not self.initialize_sip_later:
                await self.initialize_sip()
            
            if self.dm:
                self._task = asyncio.create_task(self.dm._main(data))
                # Add callback for cleanup if needed

            self.emit(msg_type, **data)
        
        else:
            if self._api_events and msg_type in self._api_events:
                self.emit(msg_type, **data)
        
        if self._ws_local_dm and not self._ws_local_dm.closed:
            await self._ws_local_dm.send_str(text_data)

    async def _on_dm_message(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        msg_type = data.get('type')
        
        if self._api_events and msg_type in self._api_events:
            self.emit(msg_type, **data)
            
        if self._ws and not self._ws.closed:
            await self._ws.send_str(text_data)

    def _create_method(self, method, schema):
        async def func(**kwargs):
            msg = kwargs
            msg['type'] = method
            if self._ws and not self._ws.closed:
                await self._ws.send_str(json.dumps(msg))
            if self._ws_local_dm and not self._ws_local_dm.closed:
                await self._ws_local_dm.send_str(json.dumps(msg))

        func.__name__ = str(method)
        func.__doc__ = schema.get('description')
        return func

    def _create_event(self, event, schema):
        def func(*args, **kwargs):
            future = asyncio.get_event_loop().create_future()
            def handler(**hkwargs):
                try:
                    if not future.done():
                        future.set_result(hkwargs)
                except asyncio.InvalidStateError:
                    pass

            self.once(event, handler)
            return future

        func.__name__ = str(event)
        func.__doc__ = schema.get('description')
        return func

    def _prepare_dm_methods_events(self, schema):
        if not self.dm_schema:
             return schema

        dm_events = self.dm_schema.get("events", {})
        dm_methods = self.dm_schema.get("methods", {})

        new_schema = schema.copy()
        new_methods = {}
        new_events = {}

        # First, iterate over what's in the application schema from SpeechCloud
        for method, method_schema in schema.get("methods", {}).items():
            if method not in dm_methods:
                new_methods[method] = method_schema
            else:
                new_events[method] = method_schema

        for event, event_schema in schema.get("events", {}).items():
            if event not in dm_events:
                new_events[event] = event_schema
            else:
                new_methods[event] = event_schema
        
        # Then, add DM-specific methods/events that might not be in the application schema yet
        for method, method_schema in dm_methods.items():
            if method not in new_methods and method not in new_events:
                # DM provides this method, so for the client code it's an EVENT to wait on
                new_events[method] = method_schema

        for event, event_schema in dm_events.items():
            if event not in new_methods and event not in new_events:
                # DM emits this event, so for the client code it's a METHOD to call
                new_methods[event] = event_schema

        new_schema["methods"] = new_methods
        new_schema["events"] = new_events
        return new_schema

    def _init_api_methods(self, methods):
        self._api_methods = methods
        for method, schema in methods.items():
            func = self._create_method(method, schema)
            setattr(self, method, func)

    def _init_api_events(self, events):
        self._api_events = events
        for event, schema in events.items():
            func = self._create_event(event, schema)
            setattr(self, event, func)

    async def _init_API_schema(self):
        if self.dm:
            dm_schema = self.dm.get_schema()
            await self._ws.send_str(json.dumps({"type": "sc_activated", "schema": dm_schema}))

    def __getattr__(self, name):
        """
        Fallback for dynamically called methods if they weren't explicitly initialized.
        """
        if name in self._api_methods:
             return getattr(self, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    async def initialize_sip(self):
        if self._sip_handler:
             await self._sip_handler.initialize(self.config, self.speechcloud_audio_constraints)
        else:
            logger.warning("SIP handler not configured. Skipping SIP initialization.")