#!/usr/bin/env python3
"""Private, short-lived llama.cpp CPU server for Qwen3.5 2B Q4_K_M."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tarfile
import time
import urllib.request

MODEL_REV = 'bb84e11355a036e28f080c7793fa6d22b7c4e344'
MODEL_NAME = 'Qwen3.5-2B-Q4_K_M.gguf'
LLAMA_TAG = 'b11910'
LLAMA_SHA = '811603cdb8e76df28fa8e0a7f179b3b6161e3ee205e1063711e60e49428be0d0'


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def download(url, path, expected=None):
    if path.is_file() and (not expected or sha256(path) == expected):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.download')
    try:
        subprocess.run(['curl', '-fsSL', '--retry', '3', '--connect-timeout', '30',
                        '--max-time', '900', '-o', str(temporary), url], check=True, timeout=930)
        if expected and sha256(temporary) != expected:
            raise RuntimeError('Checksum do runtime ou modelo incorreto.')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def setup(cache):
    archive = cache / f'llama-{LLAMA_TAG}.tar.gz'
    binary_dir = cache / LLAMA_TAG
    download(f'https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/llama-{LLAMA_TAG}-bin-ubuntu-x64.tar.gz', archive, LLAMA_SHA)
    binaries = list(binary_dir.rglob('llama-server')) if binary_dir.exists() else []
    if not binaries:
        binary_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(binary_dir, filter='data')
        binaries = list(binary_dir.rglob('llama-server'))
    if len(binaries) != 1:
        raise RuntimeError('Runtime llama.cpp CPU nao encontrado.')
    binary = binaries[0]
    binary.chmod(0o700)
    model = cache / MODEL_NAME
    # Pin the revision and verify the LFS SHA-256 supplied by the publisher.
    metadata_url = f'https://huggingface.co/api/models/lmstudio-community/Qwen3.5-2B-GGUF/revision/{MODEL_REV}?blobs=true'
    with urllib.request.urlopen(metadata_url, timeout=60) as response:
        meta = json.load(response)
    entry = next(x for x in meta['siblings'] if x['rfilename'] == MODEL_NAME)
    digest = entry['lfs']['sha256']
    download(f'https://huggingface.co/lmstudio-community/Qwen3.5-2B-GGUF/resolve/{MODEL_REV}/{MODEL_NAME}', model, digest)
    return binary, model


class Qwen:
    def __init__(self, cache, prepared=None):
        self.cache = Path(cache)
        self.prepared = prepared
        self.process = None

    def __enter__(self):
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        binary, model = self.prepared or setup(self.cache)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        self.key = secrets.token_hex(32)
        self.log = (self.cache / 'server.log').open('ab')
        os.chmod(self.cache / 'server.log', 0o600)
        safe_env = {k: v for k, v in os.environ.items() if k in
                    ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'LD_LIBRARY_PATH')}
        safe_env['LD_LIBRARY_PATH'] = str(binary.parent)
        self.process = subprocess.Popen([
            str(binary), '-m', str(model), '--host', '127.0.0.1', '--port', str(port),
            '--api-key', self.key, '--ctx-size', '8192', '--parallel', '1',
            '--threads', str(min(4, os.cpu_count() or 2)), '--n-gpu-layers', '0',
            '--jinja', '--no-webui', '--log-disable'],
            env=safe_env, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
            start_new_session=True)
        deadline = time.monotonic() + 180
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('Qwen nao iniciou; veja o log privado do runtime.')
                try:
                    with urllib.request.urlopen(self.url + '/health', timeout=2) as response:
                        if response.status == 200:
                            print('Qwen 3.5 2B carregado (CPU, contexto 8192).', flush=True)
                            return self
                except OSError:
                    time.sleep(1)
            raise RuntimeError('Qwen excedeu o tempo de carregamento.')
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def ask(self, system, messages, schema=None):
        data = json.dumps({'model': 'qwen-local', 'messages': [{'role': 'system', 'content': system}] + messages,
                           'temperature': 0, 'max_tokens': 1600,
                           'response_format': ({'type': 'json_schema', 'json_schema':
                               {'name': 'automation_action', 'strict': True, 'schema': schema}}
                               if schema else {'type': 'json_object'}),
                           'chat_template_kwargs': {'enable_thinking': False}}).encode()
        request = urllib.request.Request(self.url + '/v1/chat/completions', data=data,
                    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.key})
        with urllib.request.urlopen(request, timeout=240) as response:
            result = json.load(response)
        if result['choices'][0].get('finish_reason') == 'length':
            raise RuntimeError('Resposta do Qwen truncada; simplifique a tarefa.')
        content = result['choices'][0]['message']['content']
        if not isinstance(content, str):
            raise RuntimeError('Resposta inesperada do Qwen.')
        result = json.loads(content)
        if not isinstance(result, dict):
            raise RuntimeError('Qwen deve responder com um objeto JSON.')
        return result

    def __exit__(self, *args):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        if hasattr(self, 'log'):
            self.log.close()
        self.process = None
        print('Qwen descarregado; memoria do processo liberada.', flush=True)
