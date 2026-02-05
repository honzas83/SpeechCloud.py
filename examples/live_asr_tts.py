import asyncio
import logging
import argparse
import sys
import time
from fractions import Fraction
import numpy as np
import sounddevice as sd
from av import AudioFrame
from aiortc import MediaStreamTrack
from speechcloud import SpeechCloud, AiortcSIPHandler
from speechcloud.audio import MicrophoneSource, SpeakerSink

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("example")
# Disable debug logs for specific libraries
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcrtpreceiver").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcsrtptransport").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcrtpsender").setLevel(logging.WARNING)

async def main():
    parser = argparse.ArgumentParser(description="SpeechCloud Live ASR/TTS Example")
    parser.add_argument("--uri", default="https://speechcloud-dev.kky.zcu.cz:9443/v1/speechcloud/honzas/zipformer", help="SpeechCloud URI")
    args = parser.parse_args()

    print("Available Audio Devices:")
    print(sd.query_devices())

    print(f"Connecting to: {args.uri}")

    # 1. Setup Audio Input
    try:
        mic_track = MicrophoneSource()
        print("Microphone initialized.")
    except Exception as e:
        print(f"Failed to initialize microphone: {e}")
        return

    # 2. Setup Audio Output
    speaker = SpeakerSink()
    print("Speaker initialized.")

    # 3. Initialize SIP Handler
    # We pass the mic track to send audio, and the speaker to receive audio
    sip_handler = AiortcSIPHandler(input_track=mic_track, output_recorder=speaker, ssl_verify=True)
    sip_handler.set_codec_preferences("OPUS")

    # 4. Initialize SpeechCloud Client
    options = {
        "uri": args.uri,
        "sip_handler": sip_handler,
        "ssl_verify": True
    }
    
    sc = SpeechCloud(options)

    @sc.on('asr_ready')
    async def on_asr_ready(**kwargs):
        print("[ASR] Ready")
        await sc.tts_synthesize(text="Ahoj, jak se máš?", voice="Radka210")
        await asyncio.sleep(5)
        await sc.asr_recognize()
        await asyncio.sleep(5)
        await sc.asr_pause()
        await asyncio.sleep(5)

    # 5. Define Event Handlers
    @sc.on('ws_connected')
    async def on_ws_connected(**kwargs):
        print("[WS] Connected")

    @sc.on('ws_session')
    async def on_ws_session(id=None, **kwargs):
        print(f"[WS] Session Started: {id}")

    @sc.on('asr_recognizing')
    async def on_recognizing(**kwargs):
        print("[ASR] Listening...")

    @sc.on('asr_result')
    async def on_result(result=None, **kwargs):
        if result:
            print(f"[ASR] Result: {result}")
            # Echo back with TTS
            print(f"[TTS] Synthesizing: {result}")
            await sc.tts_synthesize(text=result, voice='Radka210')

    @sc.on('sip_registered')
    async def on_sip_registered(**kwargs):
        print("[SIP] Registered")

    @sc.on('sip_connected')
    async def on_sip_connected(**kwargs):
        print("[SIP] Connected")
        
    @sc.on('ws_error')
    async def on_error(status=None, text=None, **kwargs):
        print(f"[ERROR] status={status}, text={text}")

    # 6. Start
    try:
        await sc.init()
        # Keep running
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        mic_track.stop()
        await sc.terminate()

if __name__ == "__main__":
    asyncio.run(main())
