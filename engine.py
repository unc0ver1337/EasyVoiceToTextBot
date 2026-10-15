"""Local Vulkan Whisper engine. No model process exists while idle."""
import asyncio
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import subprocess
import time
import wave
import re
from array import array
import httpx
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('engine')

class Engine:
    def __init__(self, config):
        self.config = config
        self.process = None
        self.logfile = None
        self.job = None
        self.last_used = time.monotonic()
        self.lock = asyncio.Lock()
        self.busy = False
        self.url = f"http://127.0.0.1:{config['port']}"

    def attach_job(self):
        # Ensure a crashed/killed Python process cannot leave a model in VRAM.
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',wintypes.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]
        class EXT(ctypes.Structure):
            _fields_ = [('BasicLimitInformation',BASIC),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
        k = ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p,wintypes.LPCWSTR]
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k.CreateJobObjectW(None,None)
        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not handle or not k.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not k.AssignProcessToJobObject(handle,wintypes.HANDLE(int(self.process._handle))):
            if handle: k.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(),'Cannot attach Whisper to lifetime job')
        self.job = handle

    async def ensure_started(self):
        if self.process and self.process.poll() is None:
            return
        await self.stop()
        exe = next((ROOT/'runtime').rglob('whisper-server.exe'))
        model = ROOT/'models/ggml-large-v3-turbo-q5_0.bin'
        if model.stat().st_size < 500_000_000:
            raise RuntimeError('Model file is incomplete')
        # A foreign server must never be mistaken for our model process.
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',self.config['port']))
        logpath = ROOT/'logs/whisper.log'
        if logpath.exists() and logpath.stat().st_size > 5_000_000:
            logpath.replace(ROOT/'logs/whisper.previous.log')
        self.logfile = logpath.open('ab',buffering=0)
        self.logfile.write(b'\n--- MODEL START ---\n')
        self.process = subprocess.Popen([str(exe),'-m',str(model),'--host','127.0.0.1','--port',str(self.config['port']),'-l','auto','-t','4','-bs','1','-bo','1','-fa'],cwd=exe.parent,stdout=self.logfile,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.attach_job()
            async with httpx.AsyncClient(timeout=2,trust_env=False) as client:
                for _ in range(300):
                    if self.process.poll() is not None:
                        raise RuntimeError('Whisper exited; see logs/whisper.log')
                    try:
                        r=await client.get(self.url+'/health')
                        if r.status_code==200:
                            LOG.info('Whisper ready, PID %s',self.process.pid)
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(1)
            raise RuntimeError('Model loading timed out')
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try: await asyncio.wait_for(asyncio.to_thread(self.process.wait),10)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await asyncio.to_thread(self.process.wait)
            LOG.info('Model stopped; GPU memory released')
            self.process=None
        if self.job:
            k=ctypes.WinDLL('kernel32',use_last_error=True)
            k.CloseHandle.argtypes=[wintypes.HANDLE]
            k.CloseHandle(self.job)
            self.job=None
        if self.logfile:
            self.logfile.close()
            self.logfile=None

    async def unload_if_idle(self):
        if self.lock.locked(): return False
        async with self.lock:
            if not self.busy and self.process and time.monotonic()-self.last_used >= self.config['idle_seconds']:
                await self.stop()
                return True
        return False

    async def convert(self, source, dest):
        limit=self.config['max_duration_seconds']
        proc=await asyncio.create_subprocess_exec(imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-nostdin','-y','-i',str(source),'-map','0:a:0','-vn','-t',str(limit+1),'-ac','1','-ar','16000','-c:a','pcm_s16le',str(dest),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            _,err=await asyncio.wait_for(proc.communicate(),120)
        except BaseException:
            if proc.returncode is None: proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise ValueError('Не удалось извлечь аудио. Пришли аудио или видео со звуковой дорожкой.')
        with wave.open(str(dest),'rb') as wav:
            duration=wav.getnframes()/wav.getframerate()
            samples=array('h',wav.readframes(wav.getnframes()))
            if not samples or max(abs(x) for x in samples)<=2:
                raise ValueError('В записи тишина — речь не обнаружена.')
        if duration > limit:
            raise ValueError(f'Запись длиннее {limit//60} минут. Раздели её на части.')
        if duration < .15:
            raise ValueError('Запись слишком короткая.')
        return duration

    async def transcribe(self, wav, language='auto', mode='quality'):
        async with self.lock:
            self.busy=True
            try:
                await self.ensure_started()
                async with httpx.AsyncClient(timeout=httpx.Timeout(3600,connect=10),trust_env=False) as client:
                    with open(wav,'rb') as f:
                        response=await client.post(self.url+'/inference',files={'file':('audio.wav',f,'audio/wav')},data={'response_format':'json','language':language,'detect_language':'false','temperature':'0.0','translate':'false','beam_size':'5' if mode=='quality' else '1','best_of':'5' if mode=='quality' else '1','split_on_word':'true','max_len':'500','no_timestamps':'true','suppress_nst':'true'})
                    response.raise_for_status()
                    return re.sub(r'\s+', ' ', response.json()['text']).strip()
            except BaseException:
                await self.stop()
                raise
            finally:
                self.busy=False
                self.last_used=time.monotonic()
