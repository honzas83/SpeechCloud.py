import asyncio
import logging
import uuid
import hashlib
import random
import string
import aiohttp
from .sip_interface import SIPInterface

# Handle optional aiortc import
try:
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.rtcrtpparameters import RTCRtpCodecCapability
    HAS_AIORTC = True
except ImportError:
    HAS_AIORTC = False

logger = logging.getLogger(__name__)

def generate_nonce(length=16):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def compute_digest_response(username, realm, password, method, uri, nonce, algorithm="MD5"):
    # HA1 = MD5(username:realm:password)
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode('utf-8')).hexdigest()
    # HA2 = MD5(method:digestURI)
    ha2 = hashlib.md5(f"{method}:{uri}".encode('utf-8')).hexdigest()
    # Response = MD5(HA1:nonce:HA2)
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode('utf-8')).hexdigest()
    return response

class SIPMessage:
    def __init__(self, raw_data):
        self.raw_data = raw_data
        self.headers = {}
        self.body = ""
        self.method = ""
        self.uri = ""
        self.status_code = 0
        self.reason_phrase = ""
        self.is_response = False
        self._parse()

    def _parse(self):
        parts = self.raw_data.split('\r\n\r\n', 1)
        header_part = parts[0]
        self.body = parts[1] if len(parts) > 1 else ""
        
        lines = header_part.split('\r\n')
        start_line = lines[0]
        
        if start_line.startswith("SIP/2.0"):
            # Response
            self.is_response = True
            try:
                _, code, reason = start_line.split(" ", 2)
                self.status_code = int(code)
                self.reason_phrase = reason
            except ValueError:
                pass
        else:
            # Request
            self.is_response = False
            try:
                self.method, self.uri, _ = start_line.split(" ")
            except ValueError:
                pass
            
        for line in lines[1:]:
            if ": " in line:
                key, value = line.split(": ", 1)
                self.headers[key.lower()] = value

    def get_header(self, key):
        return self.headers.get(key.lower())

class AiortcSIPHandler(SIPInterface):
    def __init__(self, input_track=None, output_recorder=None, ssl_verify=True):
        if not HAS_AIORTC:
            raise ImportError("aiortc is required for AiortcSIPHandler")
            
        self.input_track = input_track
        self.output_recorder = output_recorder
        self.ssl_verify = ssl_verify
        
        self._ws = None
        self._session = None
        self._pc = None
        
        self.config = None
        self.sip_username = None
        self.sip_password = None
        self.sip_wss = None
        self.sip_domain = None
        self.sip_uri = None
        
        self.call_id = str(uuid.uuid4())
        self.cseq = 1
        self.local_tag = generate_nonce(10)
        self.remote_tag = None
        self.branch = "z9hG4bK" + generate_nonce(10)
        
        self.registered = False
        self.codec_preference = None

    async def initialize(self, config: dict, audio_constraints: dict):
        self.config = config
        self.sip_username = config.get('sip_username')
        self.sip_password = config.get('sip_password')
        self.sip_wss = config.get('sip_wss')
        
        # Determine domain
        self.sip_domain = "speechcloud.speechtech.cz" # default
        if 'sip_domain' in config:
            self.sip_domain = config['sip_domain']
        
        # Handle user@domain format
        if '@' in self.sip_username:
            user_part, domain_part = self.sip_username.rsplit('@', 1)
            self.sip_username = user_part
            if 'sip_domain' not in config:
                self.sip_domain = domain_part

        self.sip_uri = f"sip:{self.sip_username}@{self.sip_domain}"
        
        connector = aiohttp.TCPConnector(ssl=None if self.ssl_verify else False)
        self._session = aiohttp.ClientSession(connector=connector)
        
        logger.info(f"Connecting to SIP WSS: {self.sip_wss}")
        self._ws = await self._session.ws_connect(self.sip_wss, protocols=['sip'])
        
        asyncio.create_task(self._listen_sip())
        await self._register()

    async def disconnect(self):
        if self._pc:
            await self._pc.close()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    def set_codec_preferences(self, codec_name):
        self.codec_preference = codec_name

    async def make_call(self, target: str):
        if "sip:" not in target:
            target = f"sip:{target}@{self.sip_domain}"

        logger.info(f"Making SIP call to {target}")
        
        self._pc = RTCPeerConnection()
        self._pc.on("iceconnectionstatechange", self._on_ice_connection_state_change)
        self._pc.on("connectionstatechange", self._on_connection_state_change)
        self._pc.on("track", self._on_track)

        # Add media
        if self.input_track:
            sender = self._pc.addTrack(self.input_track)
            transceiver = next(t for t in self._pc.getTransceivers() if t.sender == sender)
        else:
            transceiver = self._pc.addTransceiver("audio", direction="recvonly")

        # Set Codec Preferences
        if self.codec_preference:
            codecs = []
            if self.codec_preference.upper() == "PCMU":
                codecs = [RTCRtpCodecCapability(mimeType="audio/PCMU", clockRate=8000, channels=1)]
            elif self.codec_preference.upper() == "PCMA":
                codecs = [RTCRtpCodecCapability(mimeType="audio/PCMA", clockRate=8000, channels=1)]
            elif self.codec_preference.upper() == "OPUS":
                codecs = [RTCRtpCodecCapability(mimeType="audio/opus", clockRate=48000, channels=2)]
            
            if codecs:
                transceiver.setCodecPreferences(codecs)

        # Create Offer
        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        
        sdp = self._pc.localDescription.sdp
        sdp = self._filter_local_sdp(sdp)
        
        # Send INVITE
        await self._send_invite(target, sdp)

    def _filter_local_sdp(self, sdp):
        lines = sdp.split('\r\n')
        new_lines = []
        for line in lines:
            if line.startswith("a=extmap:"):
                continue
            if line.startswith("a=group:BUNDLE"):
                continue
            new_lines.append(line)
        return '\r\n'.join(new_lines)

    def _on_track(self, track):
        logger.info(f"Track received: {track.kind} {track.id}")
        if track.kind == "audio" and self.output_recorder:
            self.output_recorder.addTrack(track)

    def _on_ice_connection_state_change(self):
        logger.info(f"ICE State: {self._pc.iceConnectionState}")

    def _on_connection_state_change(self):
        logger.info(f"Connection State: {self._pc.connectionState}")

    async def _listen_sip(self):
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_sip_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except Exception as e:
            logger.error(f"SIP Listen Error: {e}")

    async def _handle_sip_message(self, raw_data):
        msg = SIPMessage(raw_data)
        cseq_header = msg.get_header("CSeq")
        cseq_method = cseq_header.split(" ")[1] if cseq_header else ""
        
        logger.info(f"SIP RX: {msg.status_code} {cseq_method}")

        if msg.is_response:
            if cseq_method == "REGISTER":
                if msg.status_code == 401:
                    await self._handle_auth(msg, "REGISTER")
                elif msg.status_code == 200:
                    logger.info("SIP Registered")
                    self.registered = True
                    # Auto-call if registered
                    await self.make_call("sip:speechcloud")

            elif cseq_method == "INVITE":
                if msg.status_code in (401, 407):
                    await self._handle_auth(msg, "INVITE")
                elif msg.status_code == 200:
                    await self._handle_invite_200(msg)

    async def _handle_invite_200(self, msg):
        sdp_answer = msg.body
        # Fix missing mid/msid/ssrc attributes if needed (FreeSWITCH compat)
        sdp_answer = self._fix_remote_sdp(sdp_answer)
        
        remote_desc = RTCSessionDescription(sdp=sdp_answer, type='answer')
        await self._pc.setRemoteDescription(remote_desc)
        
        # Send ACK
        await self._send_ack(msg)

    def _fix_remote_sdp(self, sdp):
        lines = sdp.replace("\r\n", "\n").split("\n")
        new_lines = []
        in_audio = False
        has_mid = False
        has_msid = False
        
        for line in lines:
            if line.startswith("m=audio"):
                in_audio = True
            if in_audio:
                if line.startswith("a=mid:"):
                    has_mid = True
                if line.startswith("a=msid:"):
                    has_msid = True
            new_lines.append(line)

        # Re-scan to inject if missing
        if in_audio and (not has_mid or not has_msid):
            final_lines = []
            in_audio = False
            injected = False
            for line in new_lines:
                final_lines.append(line)
                if line.startswith("m=audio"):
                    in_audio = True
                
                if in_audio and not injected:
                    if not has_mid:
                        final_lines.append("a=mid:0")
                    if not has_msid:
                        final_lines.append("a=msid:default default")
                    # Ensure direction
                    if not any(line.startswith("a=send") or line.startswith("a=recv") for line in new_lines if "m=audio" in line):
                        final_lines.append("a=sendrecv")
                    injected = True
            return "\r\n".join(final_lines)

        return "\r\n".join(new_lines)

    async def _handle_auth(self, msg, method):
        auth_header = msg.get_header("WWW-Authenticate") or msg.get_header("Proxy-Authenticate")
        if not auth_header:
            return
        
        is_proxy = msg.status_code == 407
        params = {}
        content = auth_header[7:] if auth_header.startswith("Digest ") else auth_header
        for part in content.split(","):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                params[k] = v.strip('"')

        uri = self.sip_uri if method == "REGISTER" else "sip:speechcloud"
        response = compute_digest_response(
            self.sip_username, params.get("realm"), self.sip_password, method, uri, params.get("nonce"), params.get("algorithm", "MD5")
        )
        
        auth_val = (f'Digest username="{self.sip_username}", realm="{params.get("realm")}", '
                    f'nonce="{params.get("nonce")}", uri="{uri}", response="{response}", algorithm={params.get("algorithm", "MD5")}')
        
        self.cseq += 1
        self.branch = "z9hG4bK" + generate_nonce(10)
        
        header_name = "Proxy-Authorization" if is_proxy else "Authorization"
        
        if method == "REGISTER":
            await self._register(auth_header=(header_name, auth_val))
        elif method == "INVITE":
            await self._send_invite(uri, self._pc.localDescription.sdp, auth_header=(header_name, auth_val))

    async def _send(self, msg):
        logger.debug(f"SIP TX:\n{msg}")
        await self._ws.send_str(msg)

    async def _register(self, auth_header=None):
        msg = (
            f"REGISTER {self.sip_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/WSS {self.sip_domain};rport;branch={self.branch}\r\n"
            f"Max-Forwards: 70\r\n"
            f"From: <{self.sip_uri}>;tag={self.local_tag}\r\n"
            f"To: <{self.sip_uri}>\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} REGISTER\r\n"
            f"Contact: <{self.sip_uri};transport=ws>\r\n"
            f"Expires: 3600\r\n"
            f"Content-Length: 0\r\n"
        )
        if auth_header:
            msg += f"{auth_header[0]}: {auth_header[1]}\r\n"
        msg += "\r\n"
        await self._send(msg)

    async def _send_invite(self, target, sdp, auth_header=None):
        msg = (
            f"INVITE {target} SIP/2.0\r\n"
            f"Via: SIP/2.0/WSS {self.sip_domain};rport;branch={self.branch}\r\n"
            f"Max-Forwards: 70\r\n"
            f"From: <{self.sip_uri}>;tag={self.local_tag}\r\n"
            f"To: <{target}>\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} INVITE\r\n"
            f"Contact: <{self.sip_uri};transport=ws>\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n"
        )
        if auth_header:
            msg += f"{auth_header[0]}: {auth_header[1]}\r\n"
        msg += f"\r\n{sdp}"
        await self._send(msg)

    async def _send_ack(self, response_msg):
        contact = response_msg.get_header("Contact")
        target = contact.split(";")[0].strip('<>') if contact else "sip:speechcloud"
        
        to_header = response_msg.get_header("To")
        ack_branch = "z9hG4bK" + generate_nonce(10)
        
        msg = (
            f"ACK {target} SIP/2.0\r\n"
            f"Via: SIP/2.0/WSS {self.sip_domain};rport;branch={ack_branch}\r\n"
            f"Max-Forwards: 70\r\n"
            f"From: <{self.sip_uri}>;tag={self.local_tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} ACK\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        await self._send(msg)