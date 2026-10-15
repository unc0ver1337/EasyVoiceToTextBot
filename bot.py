"""Telegram media -> local Whisper large-v3-turbo, with a bounded worker queue."""
import asyncio
from collections import deque
from contextlib import suppress
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
from pathlib import Path
import tempfile
import time
import secrets
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import TelegramError, NetworkError, RetryAfter
from engine import Engine, ROOT

CONFIG=json.loads((ROOT/'config.json').read_text(encoding='utf-8-sig'))
(ROOT/'logs').mkdir(exist_ok=True)
(ROOT/'temp').mkdir(exist_ok=True)
TOKEN=CONFIG['bot_token']
class Redact(logging.Filter):
    def filter(self,record):
        record.msg=record.getMessage().replace(TOKEN,'[REDACTED]')
        record.args=()
        return True
handler=RotatingFileHandler(ROOT/'logs/bot.log',maxBytes=2_000_000,backupCount=3,encoding='utf-8')
console=logging.StreamHandler()
for h in (handler,console):
    h.addFilter(Redact())
    h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
logging.basicConfig(level=logging.INFO,handlers=[handler,console])
logging.getLogger('httpx').setLevel(logging.WARNING)
LOG=logging.getLogger('bot')
engine=Engine(CONFIG)
queue=asyncio.Queue(maxsize=CONFIG['queue_size'])
pending=set()
history={}
notices={}
active_user=None
tasks=[]
waiting={}
greeted=set()
job_tokens={}
LANGUAGES={'kk':'Қазақша','ru':'Русский','en':'English','auto':'Авто'}
PRIVACY_URL=CONFIG.get('privacy_url','')
active_job=None
shutting_down=False
KEYBOARD=ReplyKeyboardMarkup([['Помощь','Состояние'],['Конфиденциальность']],resize_keyboard=True)
HELP=("Привет! Отправь голосовое, кружочек, аудио или видео.\n\n"
      "Затем выбери язык кнопкой под записью: Қазақша, Русский, English или Авто. После выбора я пришлю текст.\n\n"
      "До 20 МБ и 30 минут. Отменить запрос можно кнопкой под ним.")

def cancel_markup(uid):
    token=job_tokens.get(uid)
    return InlineKeyboardMarkup([[InlineKeyboardButton('Отменить',callback_data=f'cancel:{token}')]]) if token else None

def language_markup(token):
    return InlineKeyboardMarkup([[InlineKeyboardButton(name,callback_data=f'lang:{token}:{code}') for code,name in list(LANGUAGES.items())[:2]],
                                [InlineKeyboardButton(name,callback_data=f'lang:{token}:{code}') for code,name in list(LANGUAGES.items())[2:]],
                                [InlineKeyboardButton('Отменить',callback_data=f'cancel:{token}')]])

def allowed(user):
    return user and not user.is_bot and (not CONFIG['allowed_user_ids'] or user.id in CONFIG['allowed_user_ids'])

async def notice(message,text,**kwargs):
    uid=message.from_user.id
    now=time.monotonic()
    if now-notices.get(uid,0)<5: return
    notices[uid]=now
    await message.reply_text(text,**kwargs)

async def home(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user): return
    m=update.effective_message
    text=(m.text or '').strip()
    if text=='Состояние':
        if update.effective_user.id in waiting:
            message='Выбери язык под своей записью — распознавание ещё не началось.'
        elif update.effective_user.id in pending:
            message='Твоя запись обрабатывается или ждёт очереди. Отмена — кнопкой под запросом.'
        else:
            message=f'Готов к работе. В очереди: {queue.qsize()}. Пришли запись.'
        await notice(m,message,reply_markup=KEYBOARD)
    elif text=='Конфиденциальность':
        content=('Аудио обрабатывается на компьютере владельца бота. Telegram передаёт запись и результат. '
                 'Временные файлы удаляются после обработки; после аварийного выключения могут остаться. '
                 'Технический журнал содержит длительность, язык и время обработки. Бот не архивирует расшифровки. '
                 'Не присылай запись, если не согласен с такой обработкой.')
        if PRIVACY_URL: content+='\n\nПодробности: '+PRIVACY_URL
        await notice(m,content,reply_markup=KEYBOARD)
    else:
        await notice(m,HELP,reply_markup=KEYBOARD)

async def choose(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not q or not allowed(q.from_user): return
    uid=q.from_user.id
    parts=(q.data or '').split(':')
    if len(parts)<2 or job_tokens.get(uid)!=parts[1]:
        await q.answer('Эта кнопка устарела или относится к чужому запросу.',show_alert=True)
        return
    if parts[0]=='cancel':
        await q.answer('Отменяю…')
        if uid in waiting:
            m,media,status,token,created=waiting.pop(uid)
            pending.discard(uid);job_tokens.pop(uid,None)
            await status.edit_text('Запрос отменён.',reply_markup=None)
        elif active_user==uid and active_job and not active_job.done():
            active_job.cancel()
        else:
            kept=[];removed=[]
            while not queue.empty():
                job=queue.get_nowait();queue.task_done()
                if job[0].from_user.id==uid: removed.append(job)
                else: kept.append(job)
            for job in kept: queue.put_nowait(job)
            if removed:
                pending.discard(uid);job_tokens.pop(uid,None)
                for job in removed: await job[2].edit_text('Запрос отменён.',reply_markup=None)
        return
    if len(parts)!=3 or parts[0]!='lang' or parts[2] not in LANGUAGES or uid not in waiting:
        await q.answer('Язык уже выбран.',show_alert=True)
        return
    if queue.full():
        await q.answer('Очередь заполнена. Попробуй выбрать язык чуть позже.',show_alert=True)
        return
    await q.answer()
    m,media,status,token,created=waiting.pop(uid)
    language=parts[2]
    try:
        await status.edit_text(f'Язык: {LANGUAGES[language]}. Принято ✓ Перед тобой: {queue.qsize()+int(active_user is not None)}.',reply_markup=cancel_markup(uid))
    except TelegramError:
        waiting[uid]=(m,media,status,token,created)
        raise
    queue.put_nowait((m,media,status,(language,'quality')))

async def receive(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user): return
    m=update.effective_message
    media=m.voice or m.video_note or m.audio or m.video or m.document
    if not media:
        await home(update,context)
        return
    uid=update.effective_user.id
    if uid in pending:
        await notice(m,'У тебя уже есть запрос. Выбери язык под ним или нажми «Отменить».')
        return
    if (media.file_size or 0)>CONFIG['max_file_mb']*1024*1024:
        await notice(m,f"Файл больше {CONFIG['max_file_mb']} МБ — лимит скачивания Telegram Bot API. Раздели или сожми запись.")
        return
    duration=getattr(media,'duration',0) or 0
    if hasattr(duration,'total_seconds'): duration=duration.total_seconds()
    if duration>CONFIG['max_duration_seconds']:
        await notice(m,'Запись длиннее 30 минут. Раздели на части.')
        return
    if m.document:
        ext=Path(m.document.file_name or '').suffix.lower()
        mime=m.document.mime_type or ''
        if not (mime.startswith(('audio/','video/')) or ext in {'.wav','.mp3','.m4a','.ogg','.opus','.flac','.aac','.wma','.mp4','.mov','.mkv','.webm','.avi','.mpeg','.amr','.aiff','.3gp','.oga'}):
            await notice(m,'Нужен аудио- или видеофайл. Документы, картинки и архивы не поддерживаются.')
            return
    now=time.monotonic()
    times=history.setdefault(uid,deque())
    while times and now-times[0]>=3600: times.popleft()
    if times and now-times[-1]<CONFIG['cooldown_seconds']:
        await notice(m,'Слишком часто. Подожди 10 секунд между запросами.')
        return
    if len(times)>=CONFIG['requests_per_hour']:
        await notice(m,'Лимит: 20 записей в час. Попробуй позже.')
        return
    if len(waiting)>=32:
        await notice(m,'Очередь заполнена. Попробуй немного позже.')
        return
    pending.add(uid)
    times.append(now)
    try:
        if uid not in greeted:
            await m.reply_text('Запись получена. Выбери язык ниже.',reply_markup=KEYBOARD)
            greeted.add(uid)
        token=secrets.token_hex(5)
        job_tokens[uid]=token
        status=await m.reply_text('На каком языке запись? Выбери язык, чтобы начать распознавание.',reply_markup=language_markup(token))
        waiting[uid]=(m,media,status,token,time.monotonic())
    except BaseException:
        pending.discard(uid)
        job_tokens.pop(uid,None)
        times.pop()
        raise

async def edit(status,text,reply_markup=None):
    with suppress(TelegramError): await status.edit_text(text,reply_markup=reply_markup)

async def download(media,source):
    for attempt in range(3):
        try:
            f=await media.get_file()
            if (f.file_size or 0)>CONFIG['max_file_mb']*1024*1024:
                raise ValueError('Файл больше 20 МБ. Раздели или сожми запись.')
            await f.download_to_drive(source,read_timeout=120,write_timeout=120,connect_timeout=30)
            return
        except (NetworkError,RetryAfter) as exc:
            if attempt==2: raise
            delay=getattr(exc,'retry_after',2**attempt)
            if hasattr(delay,'total_seconds'): delay=delay.total_seconds()
            await asyncio.sleep(min(float(delay),60))

async def progress(status,start,uid):
    while True:
        await asyncio.sleep(20)
        elapsed=int(time.monotonic()-start)
        await edit(status,f'Распознаю речь… {elapsed//60}:{elapsed%60:02d}',reply_markup=cancel_markup(uid))

async def process_job(m,media,status,prefs):
    start=time.monotonic()
    ticker=None
    try:
        with tempfile.TemporaryDirectory(dir=ROOT/'temp') as td:
            source=Path(td)/'source.media'
            wav=Path(td)/'audio.wav'
            await edit(status,'Скачиваю и извлекаю звук…',reply_markup=cancel_markup(m.from_user.id))
            await download(media,source)
            if source.stat().st_size>CONFIG['max_file_mb']*1024*1024:
                raise ValueError('Файл больше 20 МБ.')
            duration=await engine.convert(source,wav)
            language,mode=prefs
            await edit(status,f'Распознаю речь • {LANGUAGES[language]}',reply_markup=cancel_markup(m.from_user.id))
            ticker=asyncio.create_task(progress(status,start,m.from_user.id))
            text=await engine.transcribe(wav,language=language,mode=mode)
            ticker.cancel()
            await asyncio.gather(ticker,return_exceptions=True)
            if not text:
                await edit(status,'Речь не обнаружена.')
                return
            elapsed=time.monotonic()-start
            if len(text.encode('utf-16-le'))//2<=3900:
                # Replace the progress bubble with the transcript; original media stays linked.
                await status.edit_text(text,reply_markup=None)
            else:
                await status.edit_text(text[:900]+'…\n\nПолная расшифровка в файле ниже.',reply_markup=None)
                data=io.BytesIO(text.encode('utf-8')); data.name='transcript.txt'
                await m.reply_document(data,filename='transcript.txt',caption=f'Готово • {duration:.0f} с аудио • {elapsed:.0f} с обработки')
            LOG.info('Request completed: audio=%.1fs wall=%.1fs mode=%s language=%s',duration,elapsed,mode,language)
    except ValueError as exc:
        await edit(status,str(exc))
    except asyncio.CancelledError:
        await edit(status,'Бот остановлен. Пришли запись повторно.' if shutting_down else 'Запрос отменён.')
        raise
    except Exception as exc:
        LOG.error('Request failed: %s',type(exc).__name__)
        await edit(status,'Не удалось обработать запись. Попробуй снова через минуту.')
    finally:
        if ticker:
            ticker.cancel()
            await asyncio.gather(ticker,return_exceptions=True)

async def worker():
    global active_user,active_job
    while True:
        m,media,status,prefs=await queue.get()
        active_user=m.from_user.id
        active_job=asyncio.create_task(process_job(m,media,status,prefs))
        try:
            await active_job
        except asyncio.CancelledError:
            if shutting_down: raise
            await edit(status,'Запрос отменён.')
        finally:
            pending.discard(active_user)
            job_tokens.pop(active_user,None)
            active_user=None
            active_job=None
            queue.task_done()

async def maintenance(app):
    while True:
        await asyncio.sleep(5)
        if (ROOT/'stop.flag').exists():
            app.stop_running()
            return
        if not queue.qsize() and active_user is None:
            await engine.unload_if_idle()
        now=time.monotonic()
        for uid,job in list(waiting.items()):
            if now-job[4]>=600:
                waiting.pop(uid,None);pending.discard(uid);job_tokens.pop(uid,None)
                await edit(job[2],'Время выбора языка истекло. Пришли запись заново.')
        for uid in list(history):
            while history[uid] and now-history[uid][0]>=3600: history[uid].popleft()
            if not history[uid]: del history[uid]
        for uid in list(notices):
            if now-notices[uid]>3600: del notices[uid]

async def startup(app):
    (ROOT/'bot.pid').write_text(str(__import__('os').getpid()))
    await app.bot.delete_my_commands()
    tasks.extend([asyncio.create_task(worker()),asyncio.create_task(maintenance(app))])
    LOG.info('Bot @%s ready; model loads on demand',app.bot.username)

async def shutdown(app):
    global shutting_down
    shutting_down=True
    for task in tasks: task.cancel()
    await asyncio.gather(*tasks,return_exceptions=True)
    while not queue.empty():
        m,media,status,prefs=queue.get_nowait()
        await edit(status,'Бот остановлен. Отправь запись повторно после запуска.')
        queue.task_done()
    for uid,job in list(waiting.items()):
        await edit(job[2],'Бот перезапускается. Пришли запись заново.')
    waiting.clear()
    await engine.stop()
    (ROOT/'bot.pid').unlink(missing_ok=True)

async def error_handler(update,context):
    LOG.error('Telegram error: %s',type(context.error).__name__)

if __name__=='__main__':
    # OS releases the lock on exit, including crashes. Repeated start.bat is safe.
    if not (ROOT/'bot.lock').exists(): (ROOT/'bot.lock').write_bytes(b'0')
    lockfile=(ROOT/'bot.lock').open('r+b')
    lockfile.seek(0)
    try: msvcrt.locking(lockfile.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        print('Bot is already running.')
        raise SystemExit(0)
    app=(Application.builder().token(TOKEN).concurrent_updates(False).connect_timeout(30).read_timeout(60).write_timeout(60).post_init(startup).post_stop(shutdown).build())
    app.add_handler(CommandHandler(['start','help'],home))
    app.add_handler(CallbackQueryHandler(choose,pattern=r'^(lang|cancel):'))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,receive))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=['message','callback_query'],drop_pending_updates=False,bootstrap_retries=-1)
