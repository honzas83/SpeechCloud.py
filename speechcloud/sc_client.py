import json
import logging
import asyncio
from typing import Optional, Type, Dict, Any

from .client import SpeechCloud
from .sip_aiortc import AiortcSIPHandler
from .audio import MicrophoneSource, SpeakerSink
from .dialog import Dialog

logger = logging.getLogger(__name__)

class SpeechCloudClient:
    """
    A high-level client for SpeechCloud that integrates:
    - Audio I/O (Microphone/Speaker)
    - SIP Signaling (via AiortcSIPHandler)
    - Dialog Management (via SpeechCloud client & Dialog class)
    """

    def __init__(self, uri: str, ssl_verify: bool = True):
        self.uri = uri
        self.ssl_verify = ssl_verify
        self.sc_client: Optional[SpeechCloud] = None
        self.sip_handler: Optional[AiortcSIPHandler] = None
        self.mic_source: Optional[MicrophoneSource] = None
        self.speaker_sink: Optional[SpeakerSink] = None

    async def run_async(self, dialog_class: Type[Dialog], options: Dict[str, Any] = None):
        """
        Runs the SpeechCloud client with the specified dialog.
        
        Args:
            dialog_class: The Dialog class to instantiate and run.
            options: Additional options for the SpeechCloud client.
        """
        if options is None:
            options = {}

        # 1. Initialize Audio
        try:
            self.mic_source = MicrophoneSource()
            logger.info("Microphone initialized.")
            
            self.speaker_sink = SpeakerSink()
            logger.info("Speaker initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize audio devices: {e}")
            await self._cleanup()
            raise

        # 2. Initialize SIP Handler
        try:
            self.sip_handler = AiortcSIPHandler(
                input_track=self.mic_source,
                output_recorder=self.speaker_sink,
                ssl_verify=self.ssl_verify
            )
            self.sip_handler.set_codec_preferences("OPUS")
        except ImportError as e:
            logger.error(f"Failed to initialize SIP handler (aiortc missing?): {e}")
            await self._cleanup()
            raise

        # 3. Configure SpeechCloud Options
        sc_options = {
            "uri": self.uri,
            "sip_handler": self.sip_handler,
            "ssl_verify": self.ssl_verify,
            "dialog_class": dialog_class,
            **options # Allow overriding/extending options
        }

        # 4. Initialize and Run SpeechCloud Client
        self.sc_client = SpeechCloud(sc_options)

        # Handle specific events if needed, for example logging
        @self.sc_client.on('ws_error')
        async def on_ws_error(status=None, text=None, **kwargs):
            logger.error(f"SpeechCloud WebSocket Error: status={status}, text={text}")

        # Input task for interaction
        input_task = asyncio.create_task(self._input_loop())

        try:
            await self.sc_client.init()
            
            # Wait until the session ends or connection closes
            # The Dialog's lifecycle is managed within SpeechCloud client
            
            # Create a future to wait on for completion
            completion_future = asyncio.get_event_loop().create_future()
            
            @self.sc_client.on('ws_closed')
            async def on_ws_closed(**kwargs):
                if not completion_future.done():
                    completion_future.set_result(True)

            await completion_future

        except asyncio.CancelledError:
            logger.info("SpeechCloudClient run_async cancelled")
        except Exception as e:
            logger.exception(f"Error during SpeechCloud execution: {e}")
        finally:
            input_task.cancel()
            await self._cleanup()

    async def _input_loop(self):
        """Loop to read user input and trigger Dialog events."""
        print("\n--- Interaction Console ---")
        print("Press Enter to trigger 'dm_send_message' or type JSON data.")
        print("---------------------------\n")
        
        while True:
            try:
                # Use to_thread to avoid blocking the event loop
                user_input = await asyncio.to_thread(input, "SpeechCloud > ")
                user_input = user_input.strip()
                
                if not user_input:
                    data = {}
                else:
                    try:
                        data = json.loads(user_input)
                    except json.JSONDecodeError:
                        # If not valid JSON, maybe it's just a string intended as data
                        data = {"text": user_input}
                
                if self.sc_client:
                    self.sc_client.emit('dm_send_message', data=data)
                    
            except asyncio.CancelledError:
                break
            except EOFError:
                break
            except Exception as e:
                logger.error(f"Input loop error: {e}")
                await asyncio.sleep(1)

    async def _cleanup(self):
        """Terminates all connections and stops audio."""
        logger.info("Cleaning up SpeechCloudClient resources...")
        
        if self.sc_client:
            await self.sc_client.terminate()
            self.sc_client = None

        if self.mic_source:
            self.mic_source.stop()
            self.mic_source = None

        if self.speaker_sink:
            self.speaker_sink.stop()
            self.speaker_sink = None
            
        # SIP handler is cleaned up by sc_client.terminate(), but we clear ref
        self.sip_handler = None
        logger.info("Cleanup complete.")

    def run(self, dialog_class: Type[Dialog], options: Dict[str, Any] = None):
        """
        Synchronous wrapper to run the client (blocking).
        """
        try:
            asyncio.run(self.run_async(dialog_class, options))
        except KeyboardInterrupt:
            pass
