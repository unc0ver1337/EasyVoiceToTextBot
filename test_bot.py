import asyncio,time,tempfile,wave
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import unittest
import bot
class ButtonTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  bot.pending.clear();bot.waiting.clear();bot.job_tokens.clear();bot.history.clear();bot.notices.clear();bot.greeted.clear()
  bot.active_user=None;bot.active_job=None
  while not bot.queue.empty():bot.queue.get_nowait();bot.queue.task_done()
 def message(self,uid=1,**kwargs):
  status=NS(edit_text=AsyncMock())
  fields=dict(text=None,voice=NS(file_size=1024,duration=10),audio=None,video=None,video_note=None,document=None,from_user=NS(id=uid,is_bot=False),reply_text=AsyncMock(return_value=status))
  fields.update(kwargs)
  m=NS(**fields)
  return NS(effective_message=m,effective_user=m.from_user),status
 def query(self,uid,token,lang='kk',action='lang'):
  return NS(callback_query=NS(from_user=NS(id=uid,is_bot=False),data=f'{action}:{token}:{lang}' if action=='lang' else f'cancel:{token}',answer=AsyncMock()))
 async def test_media_waits_for_language(self):
  u,status=self.message();await bot.receive(u,None)
  self.assertIn(1,bot.waiting);self.assertIn(1,bot.pending);self.assertTrue(bot.queue.empty())
  markup=u.effective_message.reply_text.call_args.kwargs['reply_markup']
  labels=[b.text for row in markup.inline_keyboard for b in row]
  self.assertEqual(labels,['Қазақша','Русский','English','Авто','Отменить'])
 async def test_all_four_languages(self):
  for i,lang in enumerate(['kk','ru','en','auto'],1):
   u,status=self.message(i);await bot.receive(u,None)
   await bot.choose(self.query(i,bot.job_tokens[i],lang),None)
   job=bot.queue.get_nowait();bot.queue.task_done()
   self.assertEqual(job[3],(lang,'quality'));self.assertNotIn(i,bot.waiting)
 async def test_other_user_cannot_start(self):
  u,s=self.message();await bot.receive(u,None)
  await bot.choose(self.query(2,bot.job_tokens[1]),None)
  self.assertTrue(bot.queue.empty());self.assertIn(1,bot.waiting)
 async def test_double_click_no_duplicate(self):
  u,s=self.message();await bot.receive(u,None)
  q=self.query(1,bot.job_tokens[1]);await bot.choose(q,None);await bot.choose(q,None)
  self.assertEqual(bot.queue.qsize(),1)
 async def test_cancel_waiting(self):
  u,s=self.message();await bot.receive(u,None)
  await bot.choose(self.query(1,bot.job_tokens[1],action='cancel'),None)
  self.assertFalse(bot.waiting);self.assertFalse(bot.pending);self.assertFalse(bot.job_tokens)
 async def test_cancel_queued_preserves_other(self):
  for uid in [1,2]:
   u,s=self.message(uid);await bot.receive(u,None)
   await bot.choose(self.query(uid,bot.job_tokens[uid]),None)
  await bot.choose(self.query(1,bot.job_tokens[1],action='cancel'),None)
  self.assertEqual(bot.queue.qsize(),1);self.assertEqual(bot.queue.get_nowait()[0].from_user.id,2);bot.queue.task_done()
 async def test_full_queue_keeps_language_choice(self):
  for i in range(8):bot.queue.put_nowait((NS(from_user=NS(id=100+i)),None,None,None))
  u,s=self.message();await bot.receive(u,None)
  await bot.choose(self.query(1,bot.job_tokens[1]),None)
  self.assertIn(1,bot.waiting);self.assertEqual(bot.queue.qsize(),8)
 async def test_invalid_language(self):
  u,s=self.message();await bot.receive(u,None)
  await bot.choose(self.query(1,bot.job_tokens[1],'xx'),None)
  self.assertTrue(bot.queue.empty())
 async def test_duplicate_upload(self):
  u,s=self.message();await bot.receive(u,None);await bot.receive(u,None)
  self.assertEqual(len(bot.waiting),1)
 async def test_large_file(self):
  u,s=self.message(voice=NS(file_size=21*1024*1024,duration=10));await bot.receive(u,None)
  self.assertFalse(bot.waiting)
 async def test_hourly_limit(self):
  from collections import deque
  bot.history[1]=deque([time.monotonic()-100]*20)
  u,s=self.message();await bot.receive(u,None);self.assertFalse(bot.waiting)
 async def test_pending_capacity(self):
  for uid in range(1,34):
   u,s=self.message(uid);await bot.receive(u,None)
  self.assertEqual(len(bot.waiting),32);self.assertNotIn(33,bot.pending)
 async def test_silence_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   src=Path(td)/'silent.wav'
   with wave.open(str(src),'wb') as f:
    f.setparams((1,2,16000,0,'NONE',''));f.writeframes(bytes(32000))
   with self.assertRaisesRegex(ValueError,'тишина'):await bot.engine.convert(src,Path(td)/'out.wav')
 async def test_choice_edit_failure_keeps_request(self):
  from telegram.error import NetworkError
  u,s=self.message();await bot.receive(u,None)
  s.edit_text.side_effect=NetworkError('temporary')
  with self.assertRaises(NetworkError):await bot.choose(self.query(1,bot.job_tokens[1]),None)
  self.assertIn(1,bot.waiting);self.assertTrue(bot.queue.empty())
if __name__=='__main__':unittest.main()
