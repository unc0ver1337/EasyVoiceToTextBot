"""Install dependencies and verified upstream assets; never starts the bot."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import urllib.request
import venv
import zipfile

ROOT = Path(__file__).resolve().parent
ASSETS = [
    ('runtime.zip', 'https://github.com/nyancodex/whisper-server-vulkan-windows/releases/download/v0.1.0/whisper-server-bundle.zip', '587d04a2805737436d134ab9e02c80437f41a7cb64018a7cd4d8f65a76a56af1'),
    ('models/ggml-large-v3-turbo-q5_0.bin', 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin', '394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2'),
]

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def download(relative, url, expected):
    target = ROOT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and digest(target) == expected:
        print(f'Already verified: {relative}', flush=True)
        return target
    partial = target.with_suffix(target.suffix + '.part')
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'EasyVoiceToTextBot-Installer/1'})
        with urllib.request.urlopen(request, timeout=120) as response, partial.open('wb') as out:
            size = int(response.headers.get('Content-Length', 0))
            count = 0
            next_report = 0
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
                count += len(chunk)
                if count >= next_report:
                    print(f'{relative}: {count // (1024*1024)} MB' + (f' / {size // (1024*1024)} MB' if size else ''), flush=True)
                    next_report = count + 50 * 1024 * 1024
        if digest(partial) != expected:
            raise RuntimeError(f'Checksum mismatch for {relative}. File was not installed. Check upstream release before updating SHA256.')
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target

def main():
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        raise SystemExit('This version requires 64-bit Windows and 64-bit Python 3.10+.')
    if sys.version_info < (3, 10):
        raise SystemExit('Install Python 3.10 or newer (64-bit).')
    python = ROOT / '.venv/Scripts/python.exe'
    if not python.exists():
        print('Creating Python environment...', flush=True)
        venv.EnvBuilder(with_pip=True).create(ROOT / '.venv')
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT/'requirements-lock.txt')], check=True)
    for args in ASSETS:
        download(*args)
    runtime = ROOT / 'runtime'
    runtime.mkdir(exist_ok=True)
    with zipfile.ZipFile(ROOT/'runtime.zip') as archive:
        for member in archive.infolist():
            destination = (runtime/member.filename).resolve()
            if not destination.is_relative_to(runtime.resolve()):
                raise RuntimeError('Unsafe path in runtime archive')
        archive.extractall(runtime)
    if not (runtime/'whisper-server.exe').exists():
        raise RuntimeError('Runtime archive layout changed; whisper-server.exe is missing')
    config = ROOT/'config.json'
    if not config.exists():
        shutil.copyfile(ROOT/'config.example.json', config)
    print('Ready. Enter YOUR BotFather token in config.json, then run start.bat.')
    print('No autostart was installed. The bot is not running.')

if __name__ == '__main__':
    main()
