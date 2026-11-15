#!/usr/bin/env python3
"""Join downloaded encrypted parts, validating SHA-256. Does not extract files."""
import argparse
import hashlib
import json
from pathlib import Path
import uuid


def join(folder):
    marker = folder / '_BACKUP_COMPLETO.json'
    if not marker.is_file():
        raise ValueError('Falta _BACKUP_COMPLETO.json. Este backup pode estar incompleto.')
    meta = json.loads(marker.read_text(encoding='utf-8'))
    if meta.get('complete') is not True or not meta.get('parts'):
        raise ValueError('Marcador invalido ou backup incompleto.')
    output = folder / 'arquivos.zip'
    temp = folder / ('arquivos.zip.joining-' + uuid.uuid4().hex)
    total = hashlib.sha256()
    try:
        with temp.open('xb') as dest:
            for index, item in enumerate(meta['parts'], 1):
                expected = f'arquivos.zip.part{index:05d}'
                if item['name'] != expected:
                    raise ValueError('Sequencia de partes invalida.')
                path = folder / expected
                digest = hashlib.sha256()
                size = 0
                with path.open('rb') as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        total.update(chunk)
                        size += len(chunk)
                        dest.write(chunk)
                if size != item['bytes'] or digest.hexdigest() != item['sha256']:
                    raise ValueError(f'Parte corrompida: {expected}')
        if total.hexdigest() != meta['sha256']:
            raise ValueError('Checksum do arquivo completo incorreto.')
        if output.exists():
            raise ValueError('arquivos.zip ja existe. Mova-o antes de juntar novamente.')
        temp.rename(output)
        print(f'Verificado: {output}\nAbra com 7-Zip e a senha BACKUP_PASSWORD.')
        return output
    finally:
        temp.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    join(parser.parse_args().folder)
