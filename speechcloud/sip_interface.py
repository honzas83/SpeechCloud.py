from abc import ABC, abstractmethod

class SIPInterface(ABC):
    """
    Abstract base class for SIP handling in SpeechCloud.
    Implement this class using a library like pjsua2, aiortc, or similar
    to handle audio media streaming.
    """

    @abstractmethod
    async def initialize(self, config: dict, audio_constraints: dict):
        """
        Initialize the SIP user agent and register with the server.
        
        Args:
            config: Configuration dictionary received from SpeechCloud API (contains sip_wss, sip_username, etc.)
            audio_constraints: WebRTC/Audio constraints.
        """
        pass

    @abstractmethod
    async def disconnect(self):
        """
        Disconnect the SIP user agent and cleanup resources.
        """
        pass

    @abstractmethod
    async def make_call(self, target: str):
        """
        Initiate a SIP call.
        
        Args:
            target: SIP URI to call (e.g., 'sip:speechcloud')
        """
        pass
