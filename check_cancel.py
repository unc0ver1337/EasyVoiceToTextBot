import asyncio,json,time
from pathlib import Path
from engine import Engine,ROOT
async def main():
 e=Engine(json.loads((ROOT/'config.json').read_text()))
 try:
  await e.ensure_started()
  task=asyncio.create_task(e.transcribe(ROOT/'runtime/samples/jfk.wav',language='en',mode='quality'))
  await asyncio.sleep(.3)
  task.cancel()
  await asyncio.gather(task,return_exceptions=True)
  assert e.process is None
  print('ACTIVE INFERENCE CANCEL + VRAM RELEASE PASSED',flush=True)
  text=await e.transcribe(ROOT/'runtime/samples/jfk.wav',language='en',mode='fast')
  assert 'country' in text.lower() and '\n' not in text
  print('EXPLICIT LANGUAGE + FAST + RELOAD PASSED',flush=True)
 finally: await e.stop()
asyncio.run(main())
