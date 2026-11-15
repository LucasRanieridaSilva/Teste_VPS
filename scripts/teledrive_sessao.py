#!/usr/bin/env python3
"""Guarda e restaura a sessao do TeleDrive (app desktop) no seu canal do Telegram.

O TeleDrive nao tem conta propria: o login e na sua conta do Telegram e, na
primeira vez, exige um codigo (ou QR) que so voce recebe. Por isso o login
inicial e manual. Para nao repeti-lo a cada sessao (cada sessao e uma VM nova),
a pasta de dados do app (~/.local/share/TeleDrive, que inclui a chave mestra) e
guardada criptografada (ZIP AES-256, senha BACKUP_PASSWORD) no canal e
restaurada na sessao seguinte.

Cada repositorio tem a sua propria sessao salva. Duas VMs nunca usam a mesma
chave de autenticacao do Telegram ao mesmo tempo, o que o Telegram trata como
chave duplicada e encerra.

Uso: teledrive_sessao.py restore | save [--if-due] [--final]
Codigos de saida do restore: 0 restaurada, 3 nada salvo ainda, outro = falha.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import sys
import tempfile
import time

import pyzipper

import teledrive_backup as transport

APP_MARK = '/opt/teledrive/'
KEEP = 3                      # copias mais recentes mantidas no canal
FRESH_INTERVAL = 1800         # sem sessao salva ainda: salva a cada ciclo de 30 min
STEADY_INTERVAL = 7200        # com sessao restaurada: a cada 2 horas
SIZE_GUARD = 0.5              # nao sobrescreve a copia boa por uma muito menor (logout/reset)
MAX_BUNDLE_BYTES = 200 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_FILES = 50000
EXCLUDED = ('cache', 'files/tdlib/files', 'files/imports', 'files/logs')  # caches e temporarios
NO_BUNDLE = 3


class SessionError(Exception):
    pass


def data_dir(env):
    base = env.get('XDG_DATA_HOME', '').strip()
    base = Path(base) if base else Path(env.get('HOME') or Path.home()) / '.local' / 'share'
    return base / 'TeleDrive'


def slot(env):
    return hashlib.sha256(env.get('GITHUB_REPOSITORY', 'local').strip().lower().encode()).hexdigest()[:12]


def bundle_name(env):
    return f'teledrive-sessao-{slot(env)}.zip'


def remote_folder(env):
    folder = env.get('BACKUP_FOLDER', 'Linux Web PC').strip().strip('/')
    if not folder or len(folder) > 120 or any(part in ('', '.', '..') for part in folder.split('/')):
        raise ValueError('BACKUP_FOLDER invalida.')
    return folder + '/_controle/sessao-teledrive'


def state_path(env):
    return Path(env.get('RUNNER_TEMP') or tempfile.gettempdir()) / 'teledrive-sessao.json'


def read_state(env):
    try:
        data = json.loads(state_path(env).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(env, **changes):
    state = read_state(env)
    state.update(changes)
    path = state_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(state, handle)


def is_excluded(relative):
    return any(relative == item or relative.startswith(item + '/') for item in EXCLUDED)


def iter_files(root):
    """Arquivos regulares da pasta de dados, sem seguir links e sem caches."""
    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        relative_dir = here.relative_to(root).as_posix()
        dirs[:] = sorted(d for d in dirs if not is_excluded(
            d if relative_dir == '.' else f'{relative_dir}/{d}') and not (here / d).is_symlink())
        for name in sorted(files):
            path = here / name
            relative = name if relative_dir == '.' else f'{relative_dir}/{name}'
            if path.is_symlink() or not path.is_file() or is_excluded(relative):
                continue
            yield path, relative


def build_bundle(root, dest, secret):
    total, count = 0, 0
    with pyzipper.AESZipFile(dest, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as archive:
        archive.setpassword(secret)
        for path, relative in iter_files(root):
            total += path.stat().st_size
            count += 1
            if total > MAX_UNPACKED_BYTES or count > MAX_FILES:
                raise SessionError('A pasta de dados do TeleDrive e grande demais para guardar.')
            archive.write(path, relative)
    if count == 0:
        raise SessionError('Nada para guardar na pasta de dados do TeleDrive.')
    return count


def safe_extract(bundle, dest, secret):
    """Extrai com protecao contra caminhos maliciosos, links e bombas de descompressao."""
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    total, count = 0, 0
    with pyzipper.AESZipFile(bundle) as archive:
        archive.setpassword(secret)
        for info in archive.infolist():
            name = info.filename
            parts = PurePosixPath(name).parts
            if (not name or '\\' in name or '\x00' in name or name.startswith('/') or '..' in parts
                    or stat.S_ISLNK(info.external_attr >> 16)):
                raise SessionError('Pacote da sessao com caminho invalido.')
            if info.is_dir():
                continue
            total += info.file_size
            count += 1
            if total > MAX_UNPACKED_BYTES or count > MAX_FILES:
                raise SessionError('Pacote da sessao grande demais.')
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            data = archive.read(info)  # pyzipper confere a senha/autenticacao aqui
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
    if count == 0:
        raise SessionError('Pacote da sessao vazio.')
    # mkdir(parents=True, mode=...) so aplica o modo na pasta final; deixa todas privadas.
    for directory in [dest, *(d for d in dest.rglob('*') if d.is_dir())]:
        os.chmod(directory, 0o700)


def digest_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


# ---- processo do app --------------------------------------------------------

def app_pids():
    pids = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / 'cmdline').read_bytes().replace(b'\x00', b' ').decode('utf-8', 'replace')
        except OSError:
            continue
        if command.startswith(APP_MARK) or ' ' + APP_MARK in command[:200]:
            pids.append(int(entry.name))
    return pids


def close_app(wait=25, kill=os.kill, alive=app_pids, sleep=time.sleep, now=time.time):
    """Fecha o app com calma (grava o banco do TDLib). Retorna True se saiu."""
    pids = alive()
    for pid in pids:
        try:
            kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = now() + wait
    while alive() and now() < deadline:
        sleep(1)
    return not alive()


class Frozen:
    """Pausa o app (SIGSTOP) para copiar um estado consistente e sempre o retoma."""
    def __init__(self, kill=os.kill, pids=app_pids):
        self.kill, self.pids, self.stopped = kill, pids, []

    def __enter__(self):
        for pid in self.pids():
            try:
                self.kill(pid, signal.SIGSTOP)
                self.stopped.append(pid)
            except OSError:
                pass
        return self

    def __exit__(self, *args):
        for pid in self.stopped:
            try:
                self.kill(pid, signal.SIGCONT)
            except OSError:
                pass
        return False


# ---- canal do Telegram ------------------------------------------------------

async def find_bundles(client, channel, name, folder):
    """Copias da sessao no canal, da mais nova para a mais antiga."""
    found = []
    async for message in client.iter_messages(channel, search=name, limit=50):
        caption = message.message or ''
        if not caption.startswith('td1:') or not message.document:
            continue
        try:
            meta = json.loads(caption[4:])
        except ValueError:
            continue
        if meta.get('n') == name and meta.get('p') == folder:
            found.append((message, meta))
    return found


async def do_restore(env, connect=None, target=None):
    secret = transport.password()
    name, folder = bundle_name(env), remote_folder(env)
    target = target or data_dir(env)
    if target.exists() and any(target.iterdir()):
        print('A pasta de dados do TeleDrive ja existe; sessao nao restaurada.')
        write_state(env, save_blocked=True)
        return 1
    client, channel = await (connect or transport.connect)()
    try:
        bundles = await find_bundles(client, channel, name, folder)
        if not bundles:
            write_state(env, fresh=True)
            return NO_BUNDLE
        for message, meta in bundles[:KEEP]:
            with tempfile.TemporaryDirectory(prefix='teledrive-sessao-') as tmp:
                path = Path(tmp) / name
                await client.download_media(message, file=str(path))
                if digest_file(path) != meta.get('h'):
                    print('::warning::Copia da sessao com checksum incorreto; tentando a anterior.', flush=True)
                    continue
                staging = Path(tmp) / 'dados'
                try:
                    safe_extract(path, staging, secret)
                except RuntimeError:
                    # pyzipper sinaliza senha errada com RuntimeError: outra copia nao ajudaria.
                    raise SessionError('BACKUP_PASSWORD nao abre a sessao salva do TeleDrive.') from None
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target.rmdir()  # vazia (conferido acima); senao o move iria PARA DENTRO dela
                shutil.move(str(staging), str(target))
                os.chmod(target, 0o700)
            write_state(env, fresh=False, last_save=time.time())
            return 0
        raise SessionError('Nenhuma copia valida da sessao do TeleDrive.')
    except Exception:
        write_state(env, save_blocked=True)  # nao sobrescreve uma copia boa por causa de uma falha
        raise
    finally:
        await client.disconnect()


def due(state, now):
    interval = FRESH_INTERVAL if state.get('fresh') else STEADY_INTERVAL
    return now - float(state.get('last_save') or 0) >= interval


async def do_save(env, if_due=False, final=False, connect=None, root=None, now=time.time,
                  closer=close_app, freezer=Frozen):
    state = read_state(env)
    if state.get('save_blocked'):
        print('::warning::Salvamento da sessao do TeleDrive desativado nesta sessao (falha na restauracao).', flush=True)
        return 0
    root = root or data_dir(env)
    if not (root / 'files').is_dir():
        print('TeleDrive ainda nao foi usado nesta sessao; nada a salvar.')
        return 0
    if if_due and not due(state, now()):
        return 0
    secret = transport.password()
    name, folder = bundle_name(env), remote_folder(env)
    with tempfile.TemporaryDirectory(prefix='teledrive-sessao-') as tmp:
        bundle = Path(tmp) / name
        if final and not closer():
            print('::warning::O TeleDrive nao fechou a tempo; copiando com o app pausado.', flush=True)
        with freezer():  # sem efeito se o app ja fechou; garante estado consistente se ainda roda
            build_bundle(root, bundle, secret)
        size = bundle.stat().st_size
        if size > MAX_BUNDLE_BYTES:
            raise SessionError('Pacote da sessao maior que o limite.')
        client, channel = await (connect or transport.connect)()
        try:
            existing = await find_bundles(client, channel, name, folder)
            if existing and size < SIZE_GUARD * int(existing[0][1].get('s') or 0):
                print('::warning::A sessao atual e muito menor que a salva (logout?); copia boa preservada.', flush=True)
                return 0
            await transport.send_document(client, channel, bundle, folder, digest_file(bundle))
            old = [m.id for m, _ in existing[KEEP - 1:]]
            if old:
                try:
                    await client.delete_messages(channel, old)
                except Exception as exc:  # limpar copias antigas nunca invalida o salvamento
                    print(f'::warning::Nao foi possivel apagar copias antigas da sessao ({type(exc).__name__}).', flush=True)
        finally:
            await client.disconnect()
    write_state(env, last_save=now())
    print('Sessao do TeleDrive guardada no canal.', flush=True)
    return 0


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('restore')
    save = sub.add_parser('save')
    save.add_argument('--if-due', action='store_true', help='So salva se ja passou o intervalo')
    save.add_argument('--final', action='store_true', help='Fecha o app antes de copiar (fim da sessao)')
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        if args.command == 'restore':
            return asyncio.run(do_restore(env))
        return asyncio.run(do_save(env, args.if_due, args.final))
    except Exception as exc:
        detail = str(exc) if isinstance(exc, (SessionError, ValueError)) else type(exc).__name__
        print(f'::warning::Sessao do TeleDrive: {detail}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
