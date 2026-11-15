#!/usr/bin/env python3
"""Run locally, once. Login to Telegram and write GitHub Secrets via gh stdin."""
import argparse
import asyncio
import getpass
import re
import shutil
import subprocess

from enviar_link import email_address, settings
import handoff


def gh(*args, value=None):
    result = subprocess.run(['gh', *args], input=value, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('GitHub CLI falhou: ' + (result.stderr.strip() or 'sem detalhes'))
    return result.stdout.strip()


async def configure(repos):
    from telethon import TelegramClient, utils
    from telethon.sessions import StringSession
    if not shutil.which('gh'):
        raise ValueError('Instale GitHub CLI e execute gh auth login.')
    gh('auth', 'status')
    print('Use o API ID/API Hash da sua conta em https://my.telegram.org (API development tools).')
    api_id = int(input('API ID: ').strip())
    api_hash = getpass.getpass('API Hash (oculto): ').strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        # O login pode pedir telefone, codigo recebido no Telegram e senha 2FA.
        await client.start()
        choices = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if (getattr(entity, 'broadcast', False) and not getattr(entity, 'username', None)
                and (getattr(entity, 'creator', False) or getattr(entity, 'admin_rights', None))):
                choices.append(entity)
        if not choices:
            raise ValueError('Nenhum canal privado administrado por voce. Crie/abra um drive no TeleDrive primeiro.')
        print('\nEscolha o canal EXATO que o TeleDrive usa como drive:')
        for i, entity in enumerate(choices, 1):
            print(f'{i}. {entity.title}')
        index = int(input('Numero do canal: ')) - 1
        if index < 0 or index >= len(choices):
            raise ValueError('Numero invalido.')
        entity = choices[index]
        print(f'Destino escolhido: {entity.title}')
        folder = input('Pasta dentro do TeleDrive [Linux Web PC]: ').strip() or 'Linux Web PC'
        folder = folder.strip('/')
        if len(folder) > 120 or any(p in ('', '.', '..') for p in folder.split('/')):
            raise ValueError('Pasta invalida.')
        secret = getpass.getpass('Crie uma senha de backup (minimo 12 caracteres): ')
        if len(secret) < 12 or secret != getpass.getpass('Repita a senha do backup: '):
            raise ValueError('Senhas diferentes ou muito curtas.')
        vnc = getpass.getpass('Crie a senha VNC (exatamente 8 caracteres ASCII): ')
        if len(vnc) != 8 or not vnc.isascii() or not vnc.isprintable():
            raise ValueError('A senha VNC precisa ter 8 caracteres ASCII imprimiveis.')
        secrets = {'TG_API_ID': str(api_id), 'TG_API_HASH': api_hash,
                   'TG_SESSION': client.session.save(),
                   'TG_CHANNEL_ID': str(utils.get_peer_id(entity)),
                   'BACKUP_PASSWORD': secret, 'VNC_PASSWORD': vnc}
        for repo in repos:
            for name, value in secrets.items():
                # Valores via stdin: nenhuma credencial na linha de comando ou no terminal.
                gh('secret', 'set', name, '--repo', repo, value=value)
                print(f'[{repo}] Secret {name}: configurado.')
            gh('variable', 'set', 'BACKUP_FOLDER', '--repo', repo, value=folder)
        print('\nConfigurado. Guarde a senha do backup em um local seguro.')
        print('Sessao Telegram salva somente como Secret. Nao publique credenciais no repositorio.')
    finally:
        await client.disconnect()


def configure_email(repos):
    gh('auth', 'status')
    recipient = email_address(input('Seu email para receber o link: ').strip())
    user = input('Email/usuario da conta que envia [mesmo email]: ').strip() or recipient
    default_host = 'smtp.gmail.com' if user.lower().endswith('@gmail.com') else ''
    host = input(f'Servidor SMTP [{default_host or "informe o hostname"}]: ').strip() or default_host
    port = input('Porta SMTP: 465 TLS ou 587 STARTTLS [465]: ').strip() or '465'
    sender = input('Email remetente [mesmo usuario SMTP]: ').strip() or user
    print('Use uma senha de aplicativo/credencial SMTP aceita pelo seu provedor.')
    secret = getpass.getpass('Senha SMTP (oculta): ')
    values = {'SMTP_HOST': host, 'SMTP_PORT': port, 'SMTP_USER': user,
              'SMTP_PASSWORD': secret, 'EMAIL_FROM': sender, 'EMAIL_TO': recipient}
    settings(values)  # Validar todos os valores antes de gravar qualquer Secret.
    for repo in repos:
        for name, value in values.items():
            gh('secret', 'set', name, '--repo', repo, value=value)
            print(f'[{repo}] Secret {name}: configurado.')
    print('Email configurado. O aviso sera enviado quando o proximo desktop ficar pronto.')
    print('Nenhum email de teste foi enviado.')


def configure_handoff(repo, peer):
    """Liga o revezamento: o mesmo PAT nos dois repositorios e cada um apontando para o outro."""
    gh('auth', 'status')
    print('\nRevezamento entre ' + repo + ' e ' + peer + '.')
    print('Crie um token em GitHub > Settings > Developer settings > Fine-grained tokens:')
    print('  - Repository access: SOMENTE os dois repositorios;')
    print('  - Permissions: Actions = Read and write (Metadata: Read e automatico).')
    token = getpass.getpass('Cole o token (oculto): ').strip()
    if not token:
        raise ValueError('Token vazio.')
    # Valida nos dois sentidos antes de gravar qualquer coisa.
    for own, other in ((repo, peer), (peer, repo)):
        cfg = handoff.settings({'PEER_REPO': other, 'PEER_REPO_TOKEN': token, 'GITHUB_REPOSITORY': own})
        try:
            handoff.check(cfg)
        except handoff.HandoffError as exc:
            raise ValueError(f'Token nao serve para {other}: {exc} (o workflow precisa existir e estar ativo la).')
        print(f'Acesso a {other}: ok.')
    for own, other in ((repo, peer), (peer, repo)):
        gh('secret', 'set', 'PEER_REPO_TOKEN', '--repo', own, value=token)
        gh('variable', 'set', 'PEER_REPO', '--repo', own, value=other)
        print(f'[{own}] Revezamento aponta para {other}.')
    print('Revezamento configurado.')
    if input(f'Ligar o vigia em {repo}? Ele reabre o ciclo sozinho se parar. [S/n]: ').strip().lower() not in ('n', 'nao'):
        # O vigia roda em UM so repositorio: dois vigias poderiam abrir sessoes ao mesmo tempo.
        gh('variable', 'set', 'WATCHDOG_ENABLED', '--repo', repo, value='true')
        gh('variable', 'set', 'WATCHDOG_ENABLED', '--repo', peer, value='false')
        status, _ = handoff.api({'token': token}, 'GET', f'/repos/{repo}/actions/workflows/vigia.yml')
        if status != 200:
            print('AVISO: vigia.yml nao foi encontrado na branch padrao; publique o arquivo .github/workflows/vigia.yml.')
        print(f'Vigia ligado em {repo}.')
    if input('Iniciar a primeira sessao agora? [S/n]: ').strip().lower() not in ('n', 'nao'):
        gh('workflow', 'run', 'linux-web-pc.yml', '--repo', repo)
        print(f'Sessao solicitada em {repo}. O link chegara por e-mail em alguns minutos.')
    print('Pronto: voce ja pode desligar este computador. As sessoes se revezam sozinhas no GitHub.')
    print('Nao deixe o Agendar.ps1 periodico rodando ao mesmo tempo (criaria sessoes duplicadas).')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--repo-par', help='Segundo repositorio: recebe os mesmos Secrets e o revezamento')
    parser.add_argument('--email-only', action='store_true', help='Configurar so os avisos por email')
    parser.add_argument('--handoff-only', action='store_true', help='Configurar so o revezamento (exige --repo-par)')
    args = parser.parse_args()
    repo, peer = args.repo, args.repo_par
    for name, value in (('--repo', repo), ('--repo-par', peer)):
        if value and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
            parser.error(f'Use {name} USUARIO/REPOSITORIO')
    if peer and peer.lower() == repo.lower():
        parser.error('--repo-par precisa ser diferente de --repo')
    if args.handoff_only and not peer:
        parser.error('--handoff-only exige --repo-par')
    repos = [repo] + ([peer] if peer else [])
    if args.handoff_only:
        configure_handoff(repo, peer)
    elif args.email_only:
        configure_email(repos)
    else:
        asyncio.run(configure(repos))
        if input('Configurar envio do link por email? [s/N]: ').strip().lower() in ('s', 'sim'):
            configure_email(repos)
        if peer and input('Ligar o revezamento entre os dois repositorios agora? [S/n]: ').strip().lower() not in ('n', 'nao'):
            configure_handoff(repo, peer)
