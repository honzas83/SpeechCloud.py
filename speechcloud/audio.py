import asyncio
import logging
import sys
import abc
import numpy as np
from fractions import Fraction

logger = logging.getLogger(__name__)

# Handle optional dependencies
try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False

try:
    from aiortc import MediaStreamTrack
    from av import AudioFrame
    HAS_AIORTC = True
except ImportError:
    HAS_AIORTC = False
    # Dummy class to allow definition even if aiortc is missing
    class MediaStreamTrack: 
        kind = "audio"
        async def recv(self): pass

class AudioSource(MediaStreamTrack):
    """
    Abstract base class for audio sources.
    Inherits from MediaStreamTrack to be compatible with aiortc.
    """
    kind = "audio"

    @abc.abstractmethod
    def stop(self):
        """Stop the audio source and release resources."""
        pass

class AudioSink(abc.ABC):
    """
    Abstract base class for audio sinks (e.g. Speakers, FileWriters).
    """
    
    @abc.abstractmethod
    def addTrack(self, track):
        """
        Add a MediaStreamTrack to be consumed by this sink.
        
        Args:
            track: An aiortc.MediaStreamTrack instance (or compatible).
        """
        pass
        
    @abc.abstractmethod
    def stop(self):
        """Stop the audio sink and release resources."""
        pass

class MicrophoneSource(AudioSource):
    """
    A MediaStreamTrack that reads from the system microphone using sounddevice.
    """
    
    def __init__(self, rate=48000, channels=1, ptime=0.02):
        super().__init__()
        if not HAS_SOUNDDEVICE:
            raise ImportError("sounddevice is required for MicrophoneSource")
        
        self.rate = rate
        self.channels = channels
        self.samples_per_frame = int(rate * ptime)
        
        self.q = asyncio.Queue()
        self.loop = asyncio.get_event_loop()
        self.stream = sd.InputStream(
            channels=self.channels,
            samplerate=self.rate,
            dtype="int16",
            blocksize=self.samples_per_frame,
            callback=self._callback
        )
        self.stream.start()
        self._timestamp = 0
        logger.info(f"MicrophoneSource initialized: {rate}Hz, {channels}ch")

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
        
        # Use strictly monotonic PTS
        pts = self._timestamp
        
        # Create AudioFrame from numpy array
        # layout='mono' expects (1, samples)
        # We assume mono for now based on default init
        layout = 'mono' if self.channels == 1 else 'stereo'
        
        frame = AudioFrame.from_ndarray(data.T, format='s16', layout=layout)
        frame.sample_rate = self.rate
        frame.pts = pts
        frame.time_base = Fraction(1, self.rate)
        
        self._timestamp += self.samples_per_frame
        return frame

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            logger.info("MicrophoneSource stopped")

class SpeakerSink(AudioSink):
    """
    Receives an audio track and plays it via sounddevice.
    """
    def __init__(self):
        if not HAS_SOUNDDEVICE:
            raise ImportError("sounddevice is required for SpeakerSink")
            
        self.stream = None
        self.track = None
        self.task = None
        self.current_sample_rate = None
        self.current_channels = None
        self._stopped = False

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
        try:
            while not self._stopped:
                try:
                    frame = await asyncio.wait_for(self.track.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("Speaker: No audio received for 5 seconds")
                    continue
                except Exception as e:
                    # Likely track ended
                    logger.info(f"Speaker track ended or error: {e}")
                    break
                
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

                if self.stream:
                    await asyncio.to_thread(self.stream.write, data)
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Speaker error: {e}")
        finally:
            if self.stream:
                self.stream.stop()
                self.stream.close()

    def stop(self):
        self._stopped = True
        if self.task:
            self.task.cancel()
        if self.stream:
            self.stream.stop()
            self.stream.close()
        logger.info("SpeakerSink stopped")
