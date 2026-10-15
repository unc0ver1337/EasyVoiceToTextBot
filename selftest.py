import asyncio,json,time
from pathlib import Path
from engine import Engine,ROOT
async def main():
 (ROOT/'logs').mkdir(exist_ok=True)
 config=json.loads((ROOT/'config.json').read_text())
 e=Engine(config)
 try:
  start=time.monotonic()
  wav=ROOT/'runtime/samples/jfk.wav'
  print('Starting real RX580 inference',flush=True)
  text=await e.transcribe(wav)
  print('TEXT:',text,flush=True)
  print('SECONDS:',round(time.monotonic()-start,2),flush=True)
  assert 'country' in text.lower(),text
  pid=e.process.pid
  assert not await e.unload_if_idle()
  e.last_used=time.monotonic()-601
  assert await e.unload_if_idle()
  assert e.process is None
  print('IDLE UNLOAD PASSED; reloading now',flush=True)
  text2=await e.transcribe(wav)
  assert 'country' in text2.lower()
  print('RELOAD PASSED',flush=True)
 finally: await e.stop()
asyncio.run(main())
