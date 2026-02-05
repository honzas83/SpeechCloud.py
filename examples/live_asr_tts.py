import asyncio
import logging
import argparse
from speechcloud import SpeechCloudClient
from speechcloud.dialog import Dialog

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("example")
# Disable debug logs for specific libraries
logging.getLogger("aiortc").setLevel(logging.WARNING)

class EchoDialog(Dialog):
    async def main(self):
        print("[Dialog] Started")
        await self.synthesize_and_wait(text="Ahoj, jsem připraven.", voice="Radka210")
        
        while True:
            try:
                # Wait for ASR result
                print("[Dialog] Listening...")
                result = await self.recognize_and_wait_for_asr_result(timeout=10.0)
                
                if result:
                    text = result.get('result')
                    print(f"[Dialog] Recognized: {text}")
                    if text:
                        await self.synthesize_and_wait(text=text, voice="Radka210")
                else:
                    print("[Dialog] No speech detected.")
                    
            except Exception as e:
                logger.error(f"Dialog error: {e}")
                await asyncio.sleep(1)

async def main():
    parser = argparse.ArgumentParser(description="SpeechCloud Live ASR/TTS Example (Client Mode)")
    parser.add_argument("--uri", default="https://speechcloud-dev.kky.zcu.cz:9443/v1/speechcloud/honzas/zipformer", help="SpeechCloud URI")
    args = parser.parse_args()

    print(f"Connecting to: {args.uri}")

    client = SpeechCloudClient(args.uri)
    
    # Run the client with the EchoDialog
    # This call blocks until the dialog finishes (or Ctrl+C)
    await client.run_async(EchoDialog)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopping...")