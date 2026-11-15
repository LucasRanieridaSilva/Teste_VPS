#!/usr/bin/env python3
"""Legacy encrypted snapshots and shared TeleDrive td1 transport.

Compatible format inspected at Mahmud0808/TeleDrive commit
7576df8c6ab12f35286a1359b7a88842e92efadb (2026-10-25).
No app database is modified. Original-name trees and native folder metadata are
handled by original_backup.py and teledrive_native.py.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import uuid

import pyzipper
from telethon import TelegramClient, utils
from telethon.sessions import StringSession

PART_BYTES = 512 * 1024 * 1024


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Secret obrigatorio ausente: {name}")
    return value


def password():
    value = os.environ.get("BACKUP_PASSWORD", "")
    if len(value) < 12:
        raise ValueError("BACKUP_PASSWORD deve ter pelo menos 12 caracteres.")
    return value.encode("utf-8")


def roots_from_env():
    values = json.loads(os.environ.get("BACKUP_PATHS_JSON", '["/home/runner"]'))
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        raise ValueError("BACKUP_PATHS_JSON precisa ser uma lista JSON de caminhos absolutos.")
    result = []
    for value in values:
        root = Path(value)
        if not root.is_absolute() or not root.exists() or root.is_symlink():
            raise ValueError(f"Origem invalida: {value}")
        root = root.resolve()
        if root in [Path('/'), Path('/proc'), Path('/sys'), Path('/dev')]:
            raise ValueError("Selecione pastas de arquivos; nao o sistema inteiro.")
        if any(root == other or other in root.parents for other in result):
            continue
        result = [other for other in result if root not in other.parents]
        result.append(root)
    return result


def exclusions(roots, output):
    # Nao incluir o staging, o ambiente de backup nem arquivos transitorios.
    paths = [output.resolve()]
    for name in ('RUNNER_TEMP',):
        if os.environ.get(name):
            paths.append(Path(os.environ[name]).resolve())
    for root in roots:
        paths.append(root / '.cache')
    return paths


def excluded(path, excluded_paths):
    absolute = path.absolute()
    return any(absolute == item or item in absolute.parents for item in excluded_paths)


def create_snapshot(roots, output, secret):
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    dest = output / 'arquivos.zip'
    skip = exclusions(roots, output)
    report = {'format': 1, 'roots': [str(p) for p in roots],
              'excluded': [str(p) for p in skip], 'errors': [], 'special_files': [],
              'files': 0, 'created_at_unix': int(time.time()),
              'note': 'Copia de arquivos ativos; nao e um snapshot atomico do sistema.'}
    seen = set()
    with pyzipper.AESZipFile(str(dest), 'w', compression=pyzipper.ZIP_DEFLATED,
                            compresslevel=1, encryption=pyzipper.WZ_AES,
                            allowZip64=True) as archive:
        archive.setpassword(secret)
        archive.setencryption(pyzipper.WZ_AES, nbits=256)

        def add(path):
            if excluded(path, skip) or path in seen:
                return
            seen.add(path)
            name = path.as_posix().lstrip('/')
            try:
                before = path.lstat()
                if stat.S_ISLNK(before.st_mode):
                    info = archive.zipinfo_cls(name)
                    info.create_system = 3
                    info.external_attr = before.st_mode << 16
                    archive.writestr(info, os.readlink(path).encode('utf-8'))
                elif stat.S_ISDIR(before.st_mode):
                    archive.write(str(path), name + '/')
                elif stat.S_ISREG(before.st_mode):
                    archive.write(str(path), name)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        report['errors'].append({'path': str(path), 'reason': 'Alterado durante a copia'})
                    report['files'] += 1
                else:
                    report['special_files'].append(str(path))
            except OSError as exc:
                # Nao continuar a escrever um ZIP quando o volume ficou sem espaco.
                if exc.errno == 28:
                    raise RuntimeError('Sem espaco para preparar o backup. Reduza BACKUP_PATHS_JSON.') from exc
                report['errors'].append({'path': str(path), 'reason': type(exc).__name__})

        for root in roots:
            add(root)
            if root.is_dir():
                def walk_error(exc):
                    report['errors'].append({'path': str(exc.filename), 'reason': type(exc).__name__})
                for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
                    dirs[:] = [d for d in dirs if not excluded(Path(directory) / d, skip)]
                    for entry in dirs + files:
                        add(Path(directory) / entry)
        archive.writestr('_backup_info.json', json.dumps(report, ensure_ascii=False, indent=2))
    os.chmod(dest, 0o600)
    return dest, report


def caption_for(path, folder, digest):
    now = int(time.time() * 1000)
    payload = {'v': 1, 'id': str(uuid.uuid4()), 'n': path.name, 'p': folder,
               'm': 'application/json' if path.suffix == '.json' else 'application/octet-stream',
               's': path.stat().st_size, 'h': digest, 'e': False, 'ct': now, 'mt': now}
    caption = 'td1:' + json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    # Telegram measures Unicode text in UTF-16 code units.
    if len(caption.encode('utf-16-le')) // 2 > 1024:
        raise ValueError('Nome da pasta grande demais para o formato do TeleDrive.')
    return caption


async def destination(client):
    target = int(required('TG_CHANNEL_ID'))
    # StringSession nao conserva o cache de access_hash das entidades.
    async for dialog in client.iter_dialogs():
        if utils.get_peer_id(dialog.entity) == target:
            entity = dialog.entity
            if not getattr(entity, 'broadcast', False):
                raise ValueError('O destino deve ser o canal privado do TeleDrive.')
            if getattr(entity, 'username', None):
                raise ValueError('Use um canal privado, sem nome de usuario publico.')
            if not (getattr(entity, 'creator', False) or getattr(entity, 'admin_rights', None)):
                raise ValueError('Sem permissao de administracao no canal de destino.')
            return entity
    raise ValueError('Canal nao encontrado entre seus dialogos. Execute configurar.py novamente.')


async def connect():
    password()
    client = TelegramClient(StringSession(required('TG_SESSION')),
                            int(required('TG_API_ID')), required('TG_API_HASH'),
                            flood_sleep_threshold=120, request_retries=3)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ValueError('Sessao Telegram revogada. Execute configurar.py novamente.')
    try:
        channel = await destination(client)
        return client, channel
    except Exception:
        await client.disconnect()
        raise


async def send_document(client, channel, path, folder, digest):
    result = await client.send_file(channel, str(path), force_document=True,
                                    caption=caption_for(path, folder, digest), parse_mode=None)
    if not result or not result.document or result.document.size != path.stat().st_size:
        raise RuntimeError('O Telegram nao confirmou o tamanho do documento enviado.')
    return result.id


async def upload_snapshot(client, channel, archive, report, folder, chunk_size=PART_BYTES):
    parts = []
    full_hash = hashlib.sha256()
    with archive.open('rb') as source:
        index = 0
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            index += 1
            full_hash.update(chunk)
            path = archive.parent / f'arquivos.zip.part{index:05d}'
            path.write_bytes(chunk)
            digest = hashlib.sha256(chunk).hexdigest()
            try:
                message_id = await send_document(client, channel, path, folder, digest)
            finally:
                path.unlink(missing_ok=True)
            parts.append({'name': path.name, 'bytes': len(chunk),
                          'sha256': digest, 'message_id': message_id})
            print(f'Parte {index} confirmada.', flush=True)
    complete = not report['errors']
    marker = archive.parent / ('_BACKUP_COMPLETO.json' if complete else '_BACKUP_PARCIAL.json')
    marker.write_text(json.dumps({'format': 1, 'complete': complete,
                                 'archive': 'arquivos.zip', 'sha256': full_hash.hexdigest(),
                                 'parts': parts, 'file_count': report['files'],
                                 'error_count': len(report['errors']),
                                 'encryption': 'ZIP AES-256 (senha BACKUP_PASSWORD)'},
                                ensure_ascii=False, indent=2), encoding='utf-8')
    # Publicado somente depois de TODAS as partes. Nunca apagar backups anteriores.
    await send_document(client, channel, marker, folder, hashlib.sha256(marker.read_bytes()).hexdigest())
    return complete


async def run(command):
    client, channel = await connect()
    try:
        if command == 'check':
            scope = Path(__file__).resolve().parents[1] / 'backup/escopo.json'
            if scope.exists() and json.loads(scope.read_text(encoding='utf-8')).get('layout') == 'original_names':
                from teledrive_native import existing_content_key, read_folder_tree
                key = await existing_content_key(client, channel, os.environ.get('TELEDRIVE_PASSPHRASE', ''))
                await read_folder_tree(client, channel, key, 'check')
                print('Nomes originais: criptografia nativa ' + ('ativa.' if key else 'desativada; arquivos individuais nao usam a senha do ZIP.'))
            print('Sessao e canal encontrados. A permissao real de upload sera verificada ao enviar.')
            return
        folder = os.environ.get('BACKUP_FOLDER', 'Linux Web PC').strip().strip('/')
        if not folder or len(folder) > 120 or any(p in ('', '.', '..') for p in folder.split('/')):
            raise ValueError('BACKUP_FOLDER invalida; use no maximo 120 caracteres.')
        identity = os.environ.get('GITHUB_RUN_ID', 'local')
        attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '1')
        stamp = time.strftime('%Y-%m-%d_%H-%M-%S_UTC', time.gmtime())
        folder += f'/{stamp}_{identity}-{attempt}'
        roots = roots_from_env()
        with tempfile.TemporaryDirectory(prefix='teledrive-backup-') as tmp:
            archive, report = create_snapshot(roots, Path(tmp), password())
            complete = await upload_snapshot(client, channel, archive, report, folder)
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a', encoding='utf-8') as output:
                output.write(f'\nBackup {"completo" if complete else "PARCIAL"}: `{folder}`\n')
        if not complete:
            raise RuntimeError('Backup parcial: arquivos ilegíveis ou alterados. Veja _backup_info.json no ZIP.')
        print('Backup enviado. Atualize/sincronize o TeleDrive para localizar a pasta.')
    finally:
        await client.disconnect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['check', 'backup'])
    args = parser.parse_args()
    try:
        asyncio.run(run(args.command))
    except Exception as exc:
        # Nao imprimir objetos de requisicao nem credenciais de sessao.
        print(f'Falha no backup: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
