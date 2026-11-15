#!/usr/bin/env python3
"""Email the desktop URL using authenticated SMTP over TLS; standard library only."""
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys


def email_address(value):
    if not re.fullmatch(r'[^\s@<>\r\n,;]+@[^\s@<>\r\n,;]+\.[^\s@<>\r\n,;]+', value):
        raise ValueError('Informe um unico endereco de email valido, sem nome de exibicao.')
    return value


def settings(env):
    names = ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD', 'EMAIL_TO')
    if not any(env.get(name) for name in names):
        return None
    if any(not env.get(name, '').strip() for name in names):
        raise ValueError('Email incompleto: configure SMTP_HOST, SMTP_USER, SMTP_PASSWORD e EMAIL_TO.')
    host = env['SMTP_HOST'].strip()
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host):
        raise ValueError('SMTP_HOST deve conter somente o hostname do servidor SMTP.')
    port = int(env.get('SMTP_PORT') or '465')
    if port not in (465, 587):
        raise ValueError('Use SMTP_PORT 465 (TLS) ou 587 (STARTTLS).')
    return {'host': host, 'port': port, 'user': env['SMTP_USER'].strip(),
            'password': env['SMTP_PASSWORD'],
            'sender': email_address((env.get('EMAIL_FROM') or env['SMTP_USER']).strip()),
            'recipient': email_address(env['EMAIL_TO'].strip())}


def make_message(config, url, env):
    if not re.fullmatch(r'https://[a-z0-9-]+\.trycloudflare\.com/vnc\.html\?autoconnect=true&resize=scale', url):
        raise ValueError('Link do desktop ausente ou invalido.')
    message = EmailMessage()
    message['Subject'] = 'Linux Web PC: seu desktop esta pronto'
    message['From'] = config['sender']
    message['To'] = config['recipient']
    message['Date'] = format_datetime(datetime.now(timezone.utc))
    message['Message-ID'] = make_msgid()
    text = ('Seu Linux Web PC esta pronto.\n\n'
            f'Abrir desktop:\n{url}\n\n'
            'Use a senha VNC_PASSWORD que voce configurou no GitHub.\n'
            'O link vale enquanto esta execucao estiver ativa.\n'
            'Cada nova execucao gera outro link.\n')
    repo = env.get('GITHUB_REPOSITORY', '')
    run_id = env.get('GITHUB_RUN_ID', '')
    if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) and run_id.isdigit():
        text += f'\nAcompanhar execucao:\nhttps://github.com/{repo}/actions/runs/{run_id}\n'
    message.set_content(text)
    return message


def make_notice(config, subject, text):
    """Aviso de texto simples (sem link do desktop), usado pelo revezamento e pelo vigia."""
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = config['sender']
    message['To'] = config['recipient']
    message['Date'] = format_datetime(datetime.now(timezone.utc))
    message['Message-ID'] = make_msgid()
    message.set_content(text)
    return message


def notify(env, subject, text):
    """Envia um aviso se o SMTP estiver configurado. Retorna True se enviou."""
    config = settings(env)
    if config is None:
        return False
    send(config, make_notice(config, subject, text))
    return True


def send(config, message):
    context = ssl.create_default_context()
    if config['port'] == 465:
        connection = smtplib.SMTP_SSL(config['host'], 465, timeout=30, context=context)
    else:
        connection = smtplib.SMTP(config['host'], 587, timeout=30)
    with connection as smtp:
        if config['port'] == 587:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(config['user'], config['password'])
        refused = smtp.send_message(message, from_addr=config['sender'], to_addrs=[config['recipient']])
        if refused:
            raise RuntimeError('O servidor recusou o destinatario.')


def main(env=None):
    env = os.environ if env is None else env
    config = settings(env)
    if config is None:
        print('Aviso por email desativado: credenciais SMTP nao configuradas.')
        return
    url_file = Path(env['RUNNER_TEMP']) / 'linux-web-pc' / 'access-url.txt'
    message = make_message(config, url_file.read_text(encoding='utf-8').strip(), env)
    send(config, message)
    print('Servidor SMTP aceitou o aviso com o link do desktop. Confira tambem o spam.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # O erro do servidor pode conter dados da conta; nao o imprimir.
        print(f'::warning::Aviso por email falhou ({type(exc).__name__}). Confira os Secrets SMTP e o provedor.')
        sys.exit(1)
