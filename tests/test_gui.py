import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gui'))

import control_center as gui


class ValidarVariaveisTests(unittest.TestCase):
    def base(self, **extra):
        dados = {'DISTRO': 'ubuntu', 'DESKTOP_ENV': 'xfce'}
        dados.update(extra)
        return dados

    def test_configuracao_valida(self):
        dados = gui.validar_variaveis(self.base(DOTFILES_URLS='https://github.com/eu/dotfiles'))
        self.assertEqual(dados['DISTRO'], 'ubuntu')
        self.assertEqual(dados['DESKTOP_ENV'], 'xfce')
        self.assertEqual(dados['DOTFILES_URLS'], 'https://github.com/eu/dotfiles')

    def test_arch_aceita_kde_gnome_hyprland(self):
        for desktop in ('kde', 'gnome', 'hyprland'):
            dados = gui.validar_variaveis(self.base(DISTRO='arch', DESKTOP_ENV=desktop))
            self.assertEqual((dados['DISTRO'], dados['DESKTOP_ENV']), ('arch', desktop))

    def test_ubuntu_recusa_kde(self):
        with self.assertRaises(gui.ApiError):
            gui.validar_variaveis(self.base(DISTRO='ubuntu', DESKTOP_ENV='kde'))

    def test_arch_recusa_xfce(self):
        with self.assertRaises(gui.ApiError):
            gui.validar_variaveis(self.base(DISTRO='arch', DESKTOP_ENV='xfce'))

    def test_distro_invalida(self):
        with self.assertRaises(gui.ApiError):
            gui.validar_variaveis(self.base(DISTRO='fedora'))

    def test_varias_urls_e_virgulas(self):
        dados = gui.validar_variaveis(self.base(
            DOTFILES_URLS='https://github.com/eu/dotfiles https://github.com/eu/outros\nhttps://github.com/eu/mais'))
        self.assertEqual(dados['DOTFILES_URLS'].count('https://'), 3)

    def test_url_dotfiles_invalida(self):
        with self.assertRaises(gui.ApiError):
            gui.validar_variaveis(self.base(DOTFILES_URLS='ftp://ruim/x'))
        with self.assertRaises(gui.ApiError):
            gui.validar_variaveis(self.base(DOTFILES_URLS='https://github.com/eu/dotfiles/../../etc'))

    def test_limites_numericos_e_virgula(self):
        dados = gui.validar_variaveis(self.base(RENOVAR='true', RENOVAR_MAX_HORAS='7,5',
                                                RENOVAR_MAX_HORAS_MES='25'))
        self.assertEqual((dados['RENOVAR_MAX_HORAS'], dados['RENOVAR_MAX_HORAS_MES']), ('7.5', '25'))
        with self.assertRaises(gui.ApiError):
            gui.validar_variaveis(self.base(RENOVAR_MAX_HORAS='muito'))

    def test_renovar_so_aceita_true_ou_vazio(self):
        with self.assertRaises(gui.ApiError):
            gui.validar_variaveis(self.base(RENOVAR='sim'))

    def test_backup_paths_json(self):
        dados = gui.validar_variaveis(self.base(BACKUP_PATHS_JSON='["/home/runner/sessao"]'))
        self.assertEqual(dados['BACKUP_PATHS_JSON'], '["/home/runner/sessao"]')
        for ruim in ('caminho', '["relativa"]', '[1]'):
            with self.subTest(ruim=ruim):
                with self.assertRaises(gui.ApiError):
                    gui.validar_variaveis(self.base(BACKUP_PATHS_JSON=ruim))

    def test_instrucao_ia_limitada(self):
        with self.assertRaises(gui.ApiError):
            gui.validar_variaveis(self.base(INSTRUCAO_IA='x' * 60001))


class SecretTests(unittest.TestCase):
    def test_somente_secrets_conhecidos(self):
        with self.assertRaises(gui.ApiError):
            gui.Gui('local').secret_post({'nome': 'SECRET_NOVO', 'valor': 'x'})

    def test_senha_vnc_validada(self):
        with self.assertRaises(gui.ApiError):
            gui.Gui('local').secret_post({'nome': 'VNC_PASSWORD', 'valor': 'curta'})
        with self.assertRaises(gui.ApiError):
            gui.Gui('local').secret_post({'nome': 'VNC_PASSWORD', 'valor': 'novebyte'})

    def test_valor_obrigatorio(self):
        with self.assertRaises(gui.ApiError):
            gui.Gui('local').secret_post({'nome': 'SMTP_HOST', 'valor': ''})


class ConfigLocalTests(unittest.TestCase):
    def test_estado_sem_config_nao_expoe_repo(self):
        estado = gui.Gui('local').estado()
        self.assertEqual(estado['repositorio'], '')
        self.assertFalse(estado['tem_token'])


class ModoSessaoTests(unittest.TestCase):
    def test_le_config_da_sessao(self):
        pasta = unittest.mock.temp = None
        import json as jsonlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            caminho = str(Path(tmp) / 'gui-sessao.json')
            with open(caminho, 'w', encoding='utf-8') as arquivo:
                jsonlib.dump({'modo': 'sessao', 'repositorio': 'dono/projeto',
                              'distro': 'arch', 'desktop_env': 'hyprland'}, arquivo)
            original = gui.os.environ.get('GUI_SESSAO_CONFIG')
            gui.os.environ['GUI_SESSAO_CONFIG'] = caminho
            try:
                instancia = gui.Gui('sessao')
            finally:
                if original is None:
                    gui.os.environ.pop('GUI_SESSAO_CONFIG', None)
                else:
                    gui.os.environ['GUI_SESSAO_CONFIG'] = original
        self.assertEqual(instancia.repo(), 'dono/projeto')
        self.assertEqual(instancia.config_sessao['desktop_env'], 'hyprland')

    def test_exigir_sem_token_falha_claramente(self):
        with self.assertRaises(gui.ApiError):
            gui.Gui('sessao').exigir()


if __name__ == '__main__':
    unittest.main()
