import pytest
import asyncio
import logging
from unittest.mock import MagicMock, AsyncMock, patch
import aiohttp
from speechcloud import SpeechCloud, AiortcSIPHandler
from speechcloud.audio import AudioSource, AudioSink

# --- Mocks for Audio ---
class MockMicrophoneSource(AudioSource):
    def __init__(self):
        super().__init__()
    
    def stop(self):
        pass
        
    async def recv(self):
        return MagicMock()

class MockSpeakerSink(AudioSink):
    def __init__(self):
        pass

    def addTrack(self, track):
        pass
        
    def stop(self):
        pass

# --- Tests ---

@pytest.mark.asyncio
async def test_speechcloud_initialization():
    # 1. Setup SIP Handler Mock
    sip_handler = MagicMock(spec=AiortcSIPHandler)
    sip_handler.initialize = AsyncMock()
    sip_handler.disconnect = AsyncMock()
    sip_handler.set_codec_preferences = MagicMock()

    # 2. Setup SpeechCloud Configuration
    uri = "wss://mock.uri/ws"
    options = {
        "uri": uri,
        "sip_handler": sip_handler,
        "ssl_verify": False
    }

    sc = SpeechCloud(options)

    # 3. Setup Mock WebSocket
    class MockWebSocket:
        def __init__(self):
            self.close = AsyncMock()
            
        async def __aiter__(self):
            # Yield a 'start_session' message
            msg = MagicMock()
            msg.type = 1 # aiohttp.WSMsgType.TEXT
            msg.data = '{"type": "sc_start_session", "session_id": "test-session", "schema": {}}'
            yield msg
            
            # Keep the connection "open" for a bit
            await asyncio.sleep(0.5)

    ws_mock = MockWebSocket()

    # 4. Mock aiohttp ClientSession
    with patch("aiohttp.ClientSession") as MockSession:
        # Enable logging for debugging
        logging.basicConfig(level=logging.DEBUG)
        
        session_instance = MockSession.return_value
        
        # Mock GET request (for initial config)
        get_ctx = session_instance.get.return_value.__aenter__.return_value
        get_ctx.status = 200
        get_ctx.text = AsyncMock(return_value='{"client_wss": "wss://mock-ws"}')
        
        # Mock WebSocket connection
        session_instance.ws_connect = AsyncMock(return_value=ws_mock)
        
        # Mock session close method
        session_instance.close = AsyncMock()

        # 5. Setup Async Event Waiting
        future = asyncio.get_event_loop().create_future()
        
        @sc.on('ws_session')
        def on_session(**kwargs):
            if not future.done():
                future.set_result(True)

        @sc.on('ws_error')
        def on_error(text=None, **kwargs):
            print(f"WS Error: {text}")

        @sc.on('error_init')
        def on_init_error(text=None, **kwargs):
            if not future.done():
                future.set_exception(Exception(f"Init Error: {text}"))

        # 6. Run Initialization
        await sc.init()
        
        # 7. Wait for Session Start
        try:
            await asyncio.wait_for(future, timeout=2.0)
        except asyncio.TimeoutError:
            if not sc._ws:
                pytest.fail("WebSocket closed prematurely without session event")
            pytest.fail("Timeout waiting for ws_session event")
        except Exception as e:
            pytest.fail(f"Initialization failed: {e}")

        # 8. Assertions
        assert sc.speechcloud_session_started is True
        sip_handler.initialize.assert_called_once()
        
        # 9. Cleanup
        await sc.terminate()
        sip_handler.disconnect.assert_called_once()
        await ws_mock.close()

@pytest.mark.asyncio
async def test_audio_setup_mocked():
    # Verify we can instantiate our mocks and pass them around
    mic = MockMicrophoneSource()
    speaker = MockSpeakerSink()
    
    # Mock dependencies for AiortcSIPHandler since we can't easily run real aiortc in unit tests without it
    with patch("speechcloud.sip_aiortc.HAS_AIORTC", True):
        # We need to mock aiohttp inside AiortcSIPHandler too if we init it
        # But here we just test that we can pass tracks to the handler's constructor
        handler = AiortcSIPHandler(input_track=mic, output_recorder=speaker, ssl_verify=False)
        assert handler.input_track == mic
        assert handler.output_recorder == speaker
        
        handler.set_codec_preferences("OPUS")
        assert handler.codec_preference == "OPUS"
