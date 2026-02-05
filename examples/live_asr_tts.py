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

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("example")
# Disable debug logs for specific libraries
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcrtpreceiver").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcsrtptransport").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcrtpsender").setLevel(logging.WARNING)

# Audio Configuration
KX = 48000 
CHANNELS = 1
PTH_TIME = 0.02 # 20ms ptime
SAMPLES_PER_FRAME = int(KX * PTH_TIME)

class MicrophoneStreamTrack(MediaStreamTrack):
    """
    A MediaStreamTrack that reads from the system microphone using sounddevice.
    """
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.q = asyncio.Queue()
        self.loop = asyncio.get_event_loop()
        self.stream = sd.InputStream(
            channels=CHANNELS,
            samplerate=KX,
            dtype="int16",
            blocksize=SAMPLES_PER_FRAME,
            callback=self._callback
        )
        self.stream.start()
        self._timestamp = 0

    def _callback(self, indata, frames, time, status):
        if status:
            print(status, file=sys.stderr)
        # We need to copy indata because it is reused by sounddevice
        self.loop.call_soon_threadsafe(self.q.put_nowait, indata.copy())

    async def recv(self):
        # Monitor queue size to detect if we are falling behind
        qsize = self.q.qsize()
        if qsize > 10:
            logger.warning(f"Mic: queue backup! size={qsize}")

        data = await self.q.get()
        
        # Monitor volume and frame stats
        volume = np.max(np.abs(data))
        frame_no = self._timestamp // SAMPLES_PER_FRAME
        
        if frame_no % 50 == 0:
             logger.info(f"OUTGOING Audio Frame #{frame_no}: vol={volume}, samples={len(data)}, qsize={qsize}")
        elif volume > 1000:
             logger.debug(f"Mic: loud sound! volume={volume}")
        
        # Use strictly monotonic PTS
        pts = self._timestamp
        
        # Create AudioFrame from numpy array
        # layout='mono' expects (1, samples)
        frame = AudioFrame.from_ndarray(data.T, format='s16', layout='mono')
        frame.sample_rate = KX
        frame.pts = pts
        frame.time_base = Fraction(1, KX)
        
        self._timestamp += SAMPLES_PER_FRAME
        return frame

    def stop(self):
        self.stream.stop()
        self.stream.close()

class Speaker:
    """
    Receives an audio track and plays it via sounddevice.
    """
    def __init__(self):
        self.stream = None
        self.track = None
        self.task = None
        self.current_sample_rate = None
        self.current_channels = None

    def addTrack(self, track):
        if self.track is not None:
            if self.track.id == track.id:
                logger.debug(f"Speaker: ignoring duplicate track {track.id}")
                return
            logger.warning(f"Speaker already has a track {self.track.id}, ignoring additional track {track.id}")
            return
        logger.info(f"Speaker: remote track {track.id} added")
        self.track = track
        self.task = asyncio.create_task(self._play())

    async def _play(self):
        logger.info(f"Speaker: playback task started for track {self.track.id}")
        total_samples = 0
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(self.track.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("Speaker: No audio received for 5 seconds")
                    continue
                
                # Dynamically open/reopen stream if sample rate or channels change
                num_channels = len(frame.layout.channels)
                data = frame.to_ndarray()
                
                if num_channels > 1 and data.shape[0] == 1:
                    # Interleaved stereo (packed format like s16)
                    data = data.reshape(-1, num_channels)
                elif data.shape[0] == num_channels:
                    # Planar format
                    data = data.T
                
                if not data.flags['C_CONTIGUOUS']:
                    data = np.ascontiguousarray(data)

                actual_channels = data.shape[1] if len(data.shape) > 1 else 1

                if self.stream is None or self.current_sample_rate != frame.sample_rate or self.current_channels != actual_channels:
                    if self.stream:
                        self.stream.stop()
                        self.stream.close()
                    
                    logger.info(f"Speaker: Opening output stream at {frame.sample_rate}Hz {actual_channels} channels")
                    try:
                        self.stream = sd.OutputStream(
                            device=None,
                            channels=actual_channels,
                            samplerate=frame.sample_rate,
                            dtype="int16"
                        )
                        self.stream.start()
                    except Exception as e:
                        logger.error(f"Failed to open speaker stream: {e}")
                        self.stream = None
                    self.current_sample_rate = frame.sample_rate
                    self.current_channels = actual_channels

                total_samples += len(data)
                # Log every frame to debug "no sound"
                if total_samples % 100 == 0:
                     logger.debug(f"Audio Frame: fmt={frame.format.name}, rate={frame.sample_rate}, channels={actual_channels}, shape={data.shape}, min={data.min()}, max={data.max()}")
                
                if self.stream:
                    await asyncio.to_thread(self.stream.write, data)
                
        except Exception as e:
            logger.error(f"Speaker error: {e}")
        finally:
            if self.stream:
                self.stream.stop()
                self.stream.close()

async def main():
    parser = argparse.ArgumentParser(description="SpeechCloud Live ASR/TTS Example")
    parser.add_argument("--uri", default="https://speechcloud-dev.kky.zcu.cz:9443/v1/speechcloud/honzas/zipformer", help="SpeechCloud URI")
    args = parser.parse_args()

    print("Available Audio Devices:")
    print(sd.query_devices())

    print(f"Connecting to: {args.uri}")

    # 1. Setup Audio Input
    try:
        mic_track = MicrophoneStreamTrack()
        print("Microphone initialized.")
    except Exception as e:
        print(f"Failed to initialize microphone: {e}")
        return

    # 2. Setup Audio Output
    speaker = Speaker()
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
