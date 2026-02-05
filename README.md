# SpeechCloud Python Client

A Python analog of the `@SpeechCloud.js` library for interacting with the SpeechCloud API.

## Installation

```bash
pip install .
```

## Usage

This library uses `asyncio`.

```python
import asyncio
from speechcloud import SpeechCloud

async def main():
    options = {
        "uri": "https://speechcloud.speechtech.cz:9443/v1/speechcloud/YOUR_APP_ID",
        # "sip_handler": MySIPHandler() # Optional: Implement SIPInterface for audio
    }
    
    sc = SpeechCloud(options)
    
    @sc.on('ws_connected')
    async def on_connected():
        print("Connected to SpeechCloud!")

    @sc.on('ws_session')
    async def on_session(data):
        print(f"Session started: {data['id']}")
        # Once session starts, dynamic methods are available
        # await sc.asr_recognize() 

    @sc.on('asr_result')
    async def on_result(data):
        print(f"Result: {data}")

    await sc.init()
    
    # Keep running
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
```

## SIP / Audio Handling

Unlike the JavaScript library which uses WebRTC in the browser, Python does not have a built-in standard for this.

To handle audio (ASR/TTS), you must implement the `SIPInterface` abstract base class (using libraries like `pjsua` or `aiortc`) and pass an instance in the `options['sip_handler']`.

If no SIP handler is provided, only the WebSocket control channel (for messages/events) will be active.
