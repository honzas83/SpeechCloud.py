from speechcloud.dialog import SpeechCloudWS, Dialog, ABNF_INLINE
import random
import asyncio
import logging
from pprint import pprint, pformat
from collections import Counter
from speechcloud import SpeechCloudClient

class ServirkaKarel(Dialog):
    def spoj_seznam(self, polozky, spojka):
        if len(polozky) == 0:
            return ""
        elif len(polozky) == 1:
            return polozky[0]
        else:
            return ", ".join(polozky[:-1])+" "+spojka+" "+polozky[-1]

    async def main(self):
        HLAS = "Katerina210"

        CENIK = {
            "pivo": 49,
            "whisky": 89,
            "rum": 65,
            "víno": 60,
        }

        UCET = Counter()

        await self.synthesize_and_wait(text="Dobrý den, jsem vaše virtuální servírka Karel. Až si budete něco přát, stiskněte tlačítko", voice=HLAS)
        while True:
            #self.sc.led_breath_slow()
            await self.sc.dm_send_message()
            #self.sc.led_off()
            result = await self.synthesize_and_wait_for_asr_result(text="Přejete si prosím?", voice=HLAS, timeout=10)

            while result is None:
                logging.info("Žádný výsledek nerozpoznán")
                result = await self.synthesize_and_wait_for_asr_result(text="Říkal jste něco?", voice=HLAS, timeout=10)

            rozpoznano = result["result"]
            logging.info(f"Rozpoznáno: {rozpoznano}")

            if "na_shledanou" in rozpoznano:
                if UCET:
                    await self.synthesize_and_wait(text="Moment, moment, ještě jste nezaplatil!", voice=HLAS)
                else:
                    await self.synthesize_and_wait(text="Děkujeme, nashledanou!", voice=HLAS)
                    break
            elif "plat" in rozpoznano:
                if UCET:
                    shrnuti = []
                    cena = 0
                    for polozka, pocet in UCET.items():
                        if pocet == 1:
                            shrnuti.append(polozka)
                        else:
                            shrnuti.append(f"{pocet} krát {polozka}")
                        cena += pocet * CENIK[polozka]

                    shrnuti = "Měl jste: " + self.spoj_seznam(shrnuti, "a") + f". To dělá {cena} korun."
                    await self.synthesize_and_wait(text=shrnuti, voice=HLAS)
                    UCET.clear()
                else:
                    nabidka = list(CENIK.keys())
                    nabdika = self.spoj_seznam(nabidka, "nebo")
                    await self.synthesize_and_wait(text=f"Ještě jste si nic neobjednal. Máme třeba {nabidka}.", voice=HLAS)
            else:
                pocty = Counter()
                for slovo in rozpoznano.split():
                    if slovo in CENIK:
                        UCET[slovo] += 1
                        pocty[slovo] += 1

                if not pocty:
                    await self.synthesize_and_wait(text="Úplně jsem vám nerozuměla, asi už byste měl jít domů.", voice=HLAS)
                else:
                    shrnuti = []
                    for polozka, pocet in pocty.items():
                        if pocet == 1:
                            shrnuti.append(polozka)
                        else:
                            shrnuti.append(f"{pocet} krát {polozka}")

                    shrnuti = "Objednáme "+ self.spoj_seznam(shrnuti, "a") + f"."
                    await self.synthesize_and_wait(text=shrnuti, voice=HLAS)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    uri = "https://speechcloud.kky.zcu.cz:9443/v1/speechcloud/F54AC144-8AE0-4315-98B0-B47C4CA1B580"
    sc = SpeechCloudClient(uri)
    sc.run(ServirkaKarel)
