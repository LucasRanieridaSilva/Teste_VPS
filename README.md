# Linux Web PC

Ambiente Linux temporário para desenvolvimento e testes no navegador, com backup incremental no TeleDrive, automações com Qwen + Needle, aviso por e-mail e agendamento pelo Windows.

O projeto inicia um ambiente Ubuntu com XFCE e Google Chrome no GitHub Actions. O acesso é feito pelo noVNC, através de um endereço temporário criado pelo Cloudflare Tunnel.

**O agendador precisa permanecer aberto em um computador Windows ligado, conectado e sem suspensão.** Cada execução usa um ambiente novo; os arquivos salvos no TeleDrive são recuperados manualmente.

## Funcionalidades

- Desktop XFCE em resolução de 1600 × 900, acessível pelo navegador.
- Google Chrome e terminal disponíveis na sessão.
- Link individual para cada execução, protegido por senha VNC.
- Envio opcional do link por e-mail assim que o desktop estiver pronto.
- Backup incremental por conteúdo: primeira cópia, varreduras a cada 30 minutos e varredura final.
- Nomes originais e mesma hierarquia de pastas no TeleDrive, incluindo pastas vazias.
- Criptografia nativa opcional do TeleDrive; índice de controle protegido com ZIP AES-256.
- Divisão nativa de arquivos grandes em partes de até 512 MiB de conteúdo, mais overhead criptográfico, com SHA-256.
- Índice persistente no TeleDrive para reutilizar conteúdo já enviado entre sessões.
- Troca de sessão solicitada a cada cinco horas, aguardando o encerramento da anterior.
- TeleDrive instalado e aberto no desktop a cada sessão, com a sessão guardada e restaurada (veja [TeleDrive no desktop](#teledrive-no-desktop)).
- Opcional: revezamento entre dois repositórios, que abre a próxima sessão 30 minutos antes do fim da atual, sem depender do seu computador (veja [Revezamento](#revezamento-entre-dois-repositórios-sessão-contínua)).
- Opcional: renovação automática em um único repositório, que abre a próxima sessão com as mesmas instruções assim que a atual termina, com limite de uso e vigia de segurança (veja [Renovação automática](#renovação-automática-em-um-único-repositório)).
- **Central de Controle (GUI)** que roda de um pen drive: configure distro, desktop, dotfiles, Secrets, e-mail e comandos para a IA; inicie e pare sessões; e acompanhe o desktop pelo **noVNC embutido** (veja [GUI Central de Controle](#gui-central-de-controle-pen-drive)). A mesma GUI abre dentro de cada sessão, sincronizada pelas variáveis do repositório.
- **Imagens de sistema escolhíveis**: Ubuntu 24.04 com XFCE, ou Arch Linux em container com **KDE Plasma, GNOME ou Hyprland** (veja [Distros e desktops](#distros-desktops-e-dotfiles)).
- **Dotfiles do GitHub** aplicados automaticamente na home a cada sessão.
- Credenciais configuradas como GitHub Actions Secrets, sem gravá-las no código.
- Instruções em `INICIALIZACAO.txt` e `ENCERRAMENTO.txt`: Qwen3.5-2B verifica a cobertura do catálogo, Needle executa rotinas conhecidas e Qwen tenta resolver as restantes.
- Ferramentas aprendidas e verificadas são salvas na branch `ferramentas-aprendidas` e carregadas nas próximas sessões.
- Qwen é descarregado ao terminar cada fase; modo de planejamento sem aplicar ações.

## Requisitos

| Onde | Requisito |
|---|---|
| Computador que agenda | Windows, PowerShell 5.1 ou superior e Python 3.10 ou superior disponível como `python` |
| GitHub | GitHub CLI disponível como `gh`, Actions habilitado e permissão para gerenciar Secrets e executar workflows no repositório |
| Telegram | Conta Telegram, API ID/API Hash próprios e acesso de administração ao canal privado usado pelo TeleDrive |
| TeleDrive | Drive configurado no app [Mahmud0808/TeleDrive](https://github.com/Mahmud0808/TeleDrive) |
| E-mail, opcional | Conta remetente com autenticação SMTP por senha de aplicativo ou credencial aceita pelo provedor |

Confira os programas no PowerShell:

```powershell
python --version
gh --version
```

Se necessário, instale o GitHub CLI:

```powershell
winget install --id GitHub.cli --exact
```

O Python pode ser instalado pelo [site oficial](https://www.python.org/downloads/windows/). Depois de instalar programas, abra uma nova janela do PowerShell para atualizar o PATH.

## Instalação e configuração

Este guia serve para configurar uma cópia do projeto na sua própria conta GitHub. Cada pessoa usa suas próprias credenciais, seu canal Telegram e, se desejar, sua conta SMTP.

**Em todos os comandos, substitua `SEU_USUARIO/SEU_REPOSITORIO` pelo endereço da sua cópia.** Por exemplo: se ela estiver em `https://github.com/seuusuario/linux-web-pc`, use `seuusuario/linux-web-pc`. Esse valor é o identificador do repositório, sem `https://github.com/` e sem `.git`.

### 1. Criar sua cópia e baixar os arquivos

1. No GitHub, clique em **Fork** e crie uma cópia deste repositório na sua conta. Se essa opção não estiver disponível, crie um repositório seu e envie os arquivos do projeto preservando as pastas.
2. Abra a sua cópia e confira o endereço `SEU_USUARIO/SEU_REPOSITORIO` na barra do navegador.
3. Na aba **Actions**, habilite os workflows se o GitHub solicitar confirmação.
4. Na sua cópia, use **Code → Download ZIP** e extraia os arquivos no computador que ficará responsável pelo agendamento.
5. Abra o PowerShell na pasta que contém `Configurar.ps1`.

Configure os Secrets na sua cópia durante o setup abaixo. Um fork não dispensa a configuração das suas próprias contas. Baixar o ZIP, sozinho, não cria um repositório na sua conta.

O workflow precisa estar na branch padrão da sua cópia. Preserve estes caminhos:

| Caminho | Função |
|---|---|
| `.github/workflows/linux-web-pc.yml` | Instala o desktop, envia o aviso e executa o backup |
| `Agendar.ps1` | Mantém o agendamento no Windows |
| `Configurar.ps1` | Prepara as dependências e inicia o configurador |
| `requirements.txt` | Dependências Python do configurador e do backup |
| `requirements-needle.txt` | Versão do runtime local do Needle 3 |
| `scripts/qwen_runtime.py` | Qwen3.5-2B Q4_K_M e llama.cpp CPU com versões e checksums fixados |
| `scripts/hybrid_startup.py` | Cobertura, execução, verificação e aprendizado de ferramentas |
| `scripts/sync_learned_tools.py` | Persiste apenas catálogo e ferramentas na branch dedicada |
| `ENCERRAMENTO.txt` | Tarefas aplicadas antes do backup final |
| `backup/escopo.json` | Diretórios, exclusões e tamanho dos lotes do backup incremental |
| `scripts/incremental_backup.py` | Varredura por SHA-256 e seleção do formato de backup |
| `scripts/original_backup.py` | Arquivos individuais com nomes originais e hierarquia de pastas |
| `scripts/teledrive_native.py` | Metadados nativos, pastas vazias e criptografia do TeleDrive |
| `scripts/restaurar_incremental.py` | Confere/restaura a árvore baixada ou os pacotes do formato anterior |
| `INICIALIZACAO.txt` | Instruções permanentes para cada nova sessão |
| `automacoes/` | Catálogo, listas e scripts das rotinas que o Needle pode selecionar |
| `scripts/needle_startup.py` | Planeja e executa as rotinas cadastradas |
| `scripts/alternar_workflow.py` | Aguarda execuções anteriores antes de solicitar a próxima |
| `scripts/renovar_sessao.py` | Renovação em um único repositório: dispara a próxima sessão e aplica os limites de uso |
| `.github/workflows/renovar.yml` | Vigia da renovação: reabre o ciclo se o disparo falhar |
| `gui/control_center.py` | Central de Controle: GUI em arquivo único (só biblioteca padrão), modos local e sessão |
| `scripts/iniciar-desktop.sh` | Orquestra a imagem da sessão: distro, desktop e dotfiles |
| `scripts/desktops/arch.sh` | Arch Linux em container com KDE/GNOME/Hyprland, noVNC na mesma porta |
| `scripts/aplicar-dotfiles.sh` | Clona os dotfiles do GitHub e aplica na home da sessão |
| `scripts/gui_sessao.sh` | Sobe a GUI dentro da sessão e abre a aba no navegador do desktop |
| `scripts/configurar.py` | Configura Telegram, backup, VNC e e-mail nos Secrets |
| `scripts/start-desktop.sh` | Inicia XFCE, Chrome, VNC, noVNC e o túnel |
| `scripts/enviar_link.py` | Envia o link do desktop por SMTP com TLS |
| `scripts/teledrive_backup.py` | Prepara e envia os backups |
| `scripts/juntar_backup.py` | Reúne e verifica as partes baixadas |

Mantenha a pasta `scripts` ao lado de `Configurar.ps1`, tanto no computador quanto no repositório. O arquivo `linux-web-pc.yml` deve estar dentro de `.github/workflows/`.

### 2. Configurar as contas

```powershell
powershell -ExecutionPolicy Bypass -File .\Configurar.ps1 -Repo SEU_USUARIO/SEU_REPOSITORIO
```

O configurador cria um ambiente Python local, instala as dependências e pede as informações necessárias:

1. **GitHub:** se não houver login válido, siga a autenticação pelo navegador. Use uma conta com permissão para administrar os Secrets e executar Actions na sua cópia.
2. **API ID e API Hash do Telegram:** obtenha em [my.telegram.org](https://my.telegram.org), em **API development tools**.
3. **Login do Telegram:** informe telefone, código recebido e senha de duas etapas, quando solicitada.
4. **Canal de destino:** selecione o canal privado que seu TeleDrive já usa como drive.
5. **Pasta do backup:** pressione Enter para usar `Linux Web PC` ou informe outra pasta.
6. **Senha do backup:** crie uma senha com pelo menos 12 caracteres e guarde-a.
7. **Senha VNC:** crie uma senha com exatamente 8 caracteres ASCII imprimíveis.
8. **Aviso por e-mail:** responda `s` para configurar o envio automático do link.

Senhas digitadas nos campos ocultos não aparecem na tela. O configurador não cria um canal Telegram e não envia um e-mail de teste.

### 3. Iniciar o agendador

> Prefere que as sessões se revezem sozinhas, sem o PC ligado? Use o [revezamento entre dois repositórios](#revezamento-entre-dois-repositórios-sessão-contínua) em vez do agendador periódico.

```powershell
powershell -ExecutionPolicy Bypass -File .\Agendar.ps1 -Repo SEU_USUARIO/SEU_REPOSITORIO
```

Mantenha a janela aberta. A primeira execução é solicitada quando não houver outra anterior ativa. Nas próximas trocas, o horário salvo pelo agendador também é respeitado.

Para parar os próximos disparos, pressione **Ctrl+C**. Isso não encerra automaticamente uma sessão já em execução no GitHub. O script não instala uma tarefa de inicialização do Windows.

Para solicitar somente uma execução, respeitando o próximo horário salvo:

```powershell
powershell -ExecutionPolicy Bypass -File .\Agendar.ps1 -Repo SEU_USUARIO/SEU_REPOSITORIO -UmaVez
```

## Instruções de início e encerramento

Escreva uma tarefa por linha em `INICIALIZACAO.txt`. Exemplo:

```text
Instale os programas da lista.
Prepare minhas pastas de trabalho.
Crie uma pasta chamada demonstracao dentro da minha home.
```

Use `ENCERRAMENTO.txt` para as tarefas de fechamento. Ambos começam apenas com comentários; sem tarefas, o modelo não é carregado.

O Qwen3.5-2B Q4_K_M inspeciona o catálogo e os pedidos. Quando uma rotina cobre o pedido completo, o executor Needle a aplica. Se não houver cobertura, o Needle recusar o pedido ou o script falhar, o Qwen tenta resolver a tarefa pelo terminal. Ele precisa fornecer uma implementação reutilizável e uma verificação de resultado; ambas são executadas antes de cadastrar a ferramenta.

Ferramentas bem-sucedidas são publicadas automaticamente na branch `ferramentas-aprendidas`, no fim do workflow, e carregadas na próxima sessão. A branch principal e seus workflows não são alterados por esse mecanismo. O Qwen é encerrado depois de cada fase, inclusive se uma tarefa falhar. Ele pode ser carregado novamente para abrir aplicativos gráficos ou executar o encerramento.

No **Actions → Linux Web PC → Run workflow**, `startup_instruction` substitui as tarefas de início somente naquela execução. `needle_preview_only` carrega o modelo para mostrar o roteamento, sem executar tarefas nem aprender ferramentas; o desktop e o backup continuam ativos.

Cada pessoa configura suas próprias credenciais em `STARTUP_SECRETS_JSON`, nunca nos textos de tarefas. A instalação usa inferência local, sem chave de API de IA. A primeira carga baixa aproximadamente 1,27 GB de pesos, além do runtime. A velocidade depende da CPU; não há garantia de conclusão de pedidos abertos por um modelo de 2B.

**Em um repo público, ferramentas aprendidas também são públicas.** O projeto exporta apenas scripts e metadados das ferramentas, bloqueando credenciais conhecidas e alguns padrões privados. Essa análise não prova ausência de todo dado privado possível. Não peça para incorporar contas, sessões ou documentos privados aos scripts. Veja os limites e o setup no [guia de automações](automacoes/README.md).

Para a persistência funcionar, em **Settings → Actions → General → Workflow permissions**, permita escrita quando as políticas da sua conta exigirem isso. O token de escrita existe apenas em um job separado, sem Qwen ou scripts gerados. Regras da branch podem impedir a escrita; nesse caso, as ferramentas ficam no artefato `learned-tools-...`, retido por sete dias, e não estarão disponíveis automaticamente na próxima sessão até serem publicadas.

## Acesso ao desktop e envio por e-mail

Quando o desktop estiver pronto, o link aparece em **Actions → Linux Web PC → execução → Summary**. Abra o link e informe a senha VNC escolhida na configuração.

Se o SMTP estiver configurado, a mesma execução envia um e-mail com o link do desktop e o endereço da execução no GitHub. O aviso é enviado **no início da sessão**, assim que o link fica disponível. A senha VNC não é incluída na mensagem.

Cada execução gera um endereço diferente. O link deixa de funcionar quando a sessão termina.

### Configurar somente o e-mail

Para alterar o destinatário ou o remetente sem refazer o login do Telegram:

```powershell
powershell -ExecutionPolicy Bypass -File .\Configurar.ps1 -Repo SEU_USUARIO/SEU_REPOSITORIO -SomenteEmail
```

O destinatário e o remetente podem ser a mesma conta. Para Gmail, use:

| Campo | Valor |
|---|---|
| Servidor SMTP | `smtp.gmail.com` |
| Porta | `465` para TLS desde a conexão, ou `587` para STARTTLS |
| Usuário SMTP | Endereço Gmail completo |
| Remetente | Endereço autorizado da conta remetente |
| Senha SMTP | Senha de aplicativo Google |
| Destinatário | Endereço que receberá os links |

As [senhas de aplicativo Google](https://support.google.com/accounts/answer/185833?hl=pt-BR) exigem verificação em duas etapas e podem não estar disponíveis em algumas contas. Use uma credencial SMTP compatível com seu provedor.

Sem credenciais SMTP, o aviso fica desativado. Uma falha no e-mail não encerra o desktop nem impede o backup. A aceitação da mensagem pelo servidor SMTP não garante que ela chegará à caixa de entrada; confira também o spam.

## Como funciona a troca de sessão

1. O agendador consulta as execuções do workflow selecionado.
2. Se houver alguma ativa ou na fila, aguarda seu encerramento antes de solicitar outra.
3. A sessão carrega ferramentas aprendidas, aplica os pedidos com Qwen + Needle, abre o desktop e envia o link por e-mail, quando configurado.
4. O backup faz a primeira varredura assim que o desktop está pronto e inicia as seguintes nos horários de 30 minutos contados desde o início do job, sem sobreposição.
5. Ao atingir cinco horas desde o início do job, aplica as tarefas de encerramento e tenta uma última cópia incremental. Instalação e preparação contam nesse período.
6. As ferramentas verificadas são publicadas por um job separado. O agendador aguarda o workflow inteiro terminar antes da próxima sessão.

**As cinco horas são uma referência de agendamento, não uma garantia de troca instantânea.** O upload do backup e as filas do GitHub podem aumentar o intervalo. O próximo horário é calculado cinco horas após uma solicitação bem-sucedida.

O agendador não cancela uma sessão à força para abrir outra. Se a anterior continuar ativa, ele aguarda até uma hora por tentativa e volta a verificar depois de 60 segundos. O workflow também mantém `concurrency` com `cancel-in-progress: false` como proteção adicional.

Uma execução encerrada pode ter falhado no backup. O agendador verifica o término do workflow; ele não valida o conteúdo salvo no TeleDrive.

## TeleDrive no desktop

Cada sessão instala o [TeleDrive](https://github.com/Mahmud0808/TeleDrive) automaticamente e o abre no desktop, já logado, para você navegar pelo mesmo drive onde o backup grava.

**O login só é manual na primeira vez.** O TeleDrive não tem conta própria: o login é na sua conta do Telegram e exige um código (ou QR) que só chega a você. Por isso nenhum script consegue entrar sozinho na primeira vez. Entre pelo desktop (API ID e API hash, telefone e código; ou o QR em **Telegram → Configurações → Dispositivos → Conectar dispositivo desktop**, que costuma ser mais prático). Você pode usar os mesmos `TG_API_ID` e `TG_API_HASH` já configurados. A partir daí, a sessão é guardada e restaurada nas sessões seguintes.

### Como funciona

- **Instalação.** O pacote `.deb` oficial (versão e SHA-256 fixados em `automacoes/teledrive.json`) é baixado e instalado só depois de o checksum conferir. Qualquer URL fora das releases do `Mahmud0808/TeleDrive` é recusada.
- **Sessão guardada.** A pasta de dados do app (`~/.local/share/TeleDrive`, que inclui a chave mestra) vai para o canal em um ZIP criptografado (AES-256, senha `BACKUP_PASSWORD`), em `<BACKUP_FOLDER>/_controle/sessao-teledrive`. Caches, mídia baixada e logs ficam de fora. São mantidas as 3 cópias mais recentes.
- **Quando salva.** A cada 30 minutos na primeira sessão (até existir uma cópia), a cada 2 horas depois, e ao encerrar, quando o app é fechado com calma antes da cópia. Nas cópias durante a sessão, o app é pausado por instantes para o banco ficar consistente e sempre é retomado.
- **Quando restaura.** Antes de abrir o app, a cada sessão. Se a cópia mais nova estiver corrompida, usa a anterior.

### Cuidados

- **Cada repositório tem a sua própria sessão.** Duas máquinas usando a mesma chave de autenticação do Telegram ao mesmo tempo contam como chave duplicada, e o Telegram encerra a sessão. No revezamento você entra **uma vez em cada desktop** (A e B).
- **A cópia dá acesso à sua conta do Telegram** a quem tiver o canal e a `BACKUP_PASSWORD`. Use uma senha longa e aleatória. O ZIP cifra o conteúdo, mas os nomes dos arquivos dentro dele ficam legíveis.
- **Proteções contra perder o login.** Se a restauração falhar, o salvamento fica desligado naquela sessão, para não sobrescrever uma cópia boa. Uma sessão atual muito menor que a salva (por exemplo, depois de sair da conta no app) também não substitui a cópia boa. Para recomeçar do zero de propósito, apague as mensagens `teledrive-sessao-*` da pasta `_controle/sessao-teledrive`.
- **O IP muda a cada sessão** (é um datacenter). O Telegram pode pedir confirmação de novo login ou limitar a conta. Se ele encerrar a sessão, entre de novo pelo desktop.
- **A pasta `_controle/sessao-teledrive` aparece no seu drive**, no próprio TeleDrive, como já acontece com o índice do backup.
- **Atualizar a versão:** troque `url` e `sha256` em `automacoes/teledrive.json`. Para obter o checksum, baixe o `.deb` da release e rode `sha256sum`.
- **Desligar:** crie a variável `TELEDRIVE_AUTO=false`.

## Revezamento entre dois repositórios (sessão contínua)

Em vez do agendador no Windows, dois repositórios podem se revezar sozinhos:

1. A sessão do repositório **A** começa.
2. Quando faltam **30 minutos** para as cinco horas (4h30 depois do início do job), A dispara o **mesmo workflow** no repositório **B**.
3. B sobe o desktop e envia **um novo link para o mesmo e-mail**, porque usa os mesmos Secrets SMTP. Os dois desktops ficam abertos por cerca de 30 minutos, para você trocar de um para o outro.
4. A encerra normalmente (tarefas de encerramento e backup final). Quando B chegar aos seus 4h30, ele dispara A, e o ciclo se repete.

### Pré-requisitos

- Dois repositórios com os mesmos arquivos (o segundo é uma cópia do primeiro), com o workflow publicado e **ativo** na branch padrão de ambos.
- Um token do GitHub que enxergue os dois repositórios. O `GITHUB_TOKEN` automático não consegue disparar workflows em outro repositório. Crie um token *fine-grained* em **Settings → Developer settings → Personal access tokens**, com **Repository access** restrito aos dois repositórios e **Actions: Read and write**. Anote a data de validade: com o token vencido, o ciclo para.
- Os mesmos Secrets nos dois repositórios. O configurador grava nos dois de uma vez.

### Configurar

```powershell
powershell -ExecutionPolicy Bypass -File .\Configurar.ps1 -Repo SEU_USUARIO/REPO_A -RepoPar SEU_USUARIO/REPO_B
```

O configurador faz as perguntas de sempre, grava os mesmos Secrets e variáveis nos dois repositórios e, no fim, liga o revezamento: pede o token, **testa o acesso nos dois sentidos antes de gravar qualquer coisa**, guarda-o como Secret `PEER_REPO_TOKEN` nos dois e define a variável `PEER_REPO` em cada um apontando para o outro. Para ligar o revezamento em repositórios que já estão configurados, use `-SomenteRevezamento -RepoPar ...`; os Secrets do par precisam existir (rode a configuração completa com `-RepoPar` se ele for novo).

### Iniciar (uma vez) e desligar o computador

O configurador, no fim, oferece **ligar o vigia** e **iniciar a primeira sessão**. Aceitando os dois, você já pode desligar o computador: as sessões se revezam sozinhas no GitHub, e o vigia reabre o ciclo se ele parar. Sem o configurador, dispare uma execução em qualquer um dos dois repositórios (botão **Run workflow**) e ligue o vigia criando a variável `WATCHDOG_ENABLED=true` em **um** dos repositórios.

Não mantenha o `Agendar.ps1` periódico ligado ao mesmo tempo: ele criaria sessões duplicadas, e por isso se recusa a rodar sem `-UmaVez` quando a variável `PEER_REPO` existe.

### Vigia

O workflow `vigia.yml` roda de tempos em tempos (a cada cerca de 30 minutos) em **um** dos dois repositórios, o que tem `WATCHDOG_ENABLED=true`. Ele consulta as execuções dos dois repositórios:

- Se existe sessão ativa ou na fila em qualquer um deles, não faz nada.
- Se não existe, abre uma nova no repositório onde roda, e o revezamento continua a partir dela. Você recebe um e-mail avisando, além do link da sessão.
- **Disjuntor:** se as 3 execuções mais recentes falharam, ele espera 3 horas antes de tentar de novo, para não gastar minutos reabrindo uma sessão que já nasce quebrada.

Cuidados:

- **O agendamento do GitHub não é exato.** A cada 30 minutos é o nominal; sob carga os disparos atrasam ou são pulados. Conte com uma lacuna de até cerca de uma hora quando o ciclo quebra.
- **Cada checagem custa cerca de 1 minuto de Actions** (o mínimo cobrado por job), ou seja, em torno de 1.400 minutos por mês. Quando `WATCHDOG_ENABLED` não é `true`, o job é ignorado e não consome nada.
- **Em repositório público**, o GitHub desativa workflows agendados após 60 dias sem atividade no repositório (um commit basta para reativar).
- **Rode o vigia em um repositório só.** Com dois vigias ao mesmo tempo, os dois poderiam abrir sessões simultâneas. O configurador já deixa `WATCHDOG_ENABLED=false` no outro.
- **Para parar o ciclo,** primeiro desligue o vigia (variável `WATCHDOG_ENABLED` diferente de `true`, ou o workflow desabilitado na aba Actions) e remova `PEER_REPO` dos dois repositórios; só depois cancele as execuções ativas. Cancelar com o vigia ligado faz ele reabrir o ciclo.

### Como se comporta

- **Aviso por e-mail.** Se o disparo da próxima sessão falhar (token vencido, par fora do ar), você recebe um e-mail com o motivo, usando os mesmos Secrets SMTP. O vigia, se estiver ligado, reabre o ciclo.
- **Falha cedo.** No início de cada sessão, um passo confere o acesso ao par e ao workflow dele. Se falhar, a sessão não abre e o log explica o motivo; é melhor que descobrir às 4h30. Remova `PEER_REPO` para desligar o revezamento.
- **Par ocupado.** Se o par já tem uma sessão ativa ou na fila, nenhuma outra é disparada; a tentativa se repete a cada 60 segundos até o fim da sessão.
- **Mesmas configurações.** O par é o mesmo workflow, com os próprios Secrets e arquivos. Se você iniciou a execução manualmente com `startup_instruction`, esse texto é repassado ao par.
- **Modo de teste.** Uma execução com `needle_preview_only` não faz revezamento.
- **Backup compartilhado.** Os dois repositórios usam o **mesmo índice incremental**: o perfil é calculado a partir do par de repositórios, igual nos dois lados. Assim B reaproveita tudo o que A já enviou, em vez de recomeçar do zero. Para não sobrescreverem o índice um do outro nos 30 minutos de sobreposição, **a sessão mais nova espera a mais antiga terminar o backup** antes de cada varredura (até 45 minutos; se a API do GitHub falhar, o backup segue sem esperar). A sessão antiga nunca espera pela nova.
- **Ao ligar o revezamento, o índice muda.** Quem já tinha backup com um perfil por repositório começa um índice novo e reenvia os arquivos uma vez. Os backups antigos continuam no canal e podem ser restaurados com o índice antigo.

### Limitações

- **O estado não passa de uma sessão para a outra.** Cada sessão é um sistema novo: B não herda arquivos, programas nem configurações de A. O backup agora é compartilhado, mas a restauração automática no início da sessão ainda não existe: ela continua manual, ou fica a cargo das suas rotinas de início.
- **Se o disparo falhar, o ciclo para até o vigia agir.** Token vencido, par indisponível ou workflow desativado geram um `::error::` no log e um e-mail. Com o vigia ligado, o ciclo volta em cerca de uma hora; sem ele, é preciso iniciar uma execução manualmente. O token vencido derruba também o vigia, então anote a validade.
- **Consumo de minutos.** O ciclo mantém um runner ativo praticamente o tempo todo, mais cerca de 30 minutos de sobreposição a cada troca e as checagens do vigia. Repositórios privados têm cota mensal de minutos de Actions que esse uso consome rapidamente; confira seu plano e o consumo em **Settings → Billing**.
- **Termos do GitHub.** Operar 24 horas por dia amplia o risco descrito em [Limites de uso](#limites-de-uso).

## Renovação automática em um único repositório

Diferente do revezamento, a renovação automática usa **um repositório só**: no fim de cada sessão, o passo "Renovar sessão automaticamente" dispara o mesmo workflow de novo, repassando a instrução manual quando a execução foi iniciada com `startup_instruction` (sem ela, a nova sessão usa o `INICIALIZACAO.txt`, como sempre). Como o disparo só acontece depois do backup final, e a concorrência do workflow enfileira a nova execução, **uma sessão nunca sobrepõe a outra** — e cada sessão nova envia o link do desktop por e-mail normalmente.

Para ligar:

1. Crie um **fine-grained personal access token** em [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new): em **Repository access**, escolha somente o seu repositório; em **Permissions → Repository permissions**, conceda **Actions: Read and write** (Metadata: Read já vem junto). Defina uma validade e anote-a — token vencido para o ciclo.
2. No repositório, em **Settings → Secrets and variables → Actions → Secrets**, adicione o token como `RENOVAR_TOKEN`.
3. Em **Variables**, crie `RENOVAR` com o valor `true`.
4. Inicie uma execução normal (botão **Run workflow**). A partir dela, cada sessão abre a próxima sozinha.

Variáveis opcionais:

| Nome | Padrão | Uso |
|---|---|---|
| `RENOVAR_MAX_HORAS` | `20` | Limite de horas de sessão numa janela móvel de 24 h; `0` desliga |
| `RENOVAR_MAX_HORAS_MES` | `0` (desligado) | Limite de horas numa janela móvel de 30 dias |
| `RENOVAR_WORKFLOW` | `linux-web-pc.yml` | Arquivo do workflow a disparar |

Como funciona e como parar:

- **Ritmo.** A nova sessão começa 1 a 2 minutos depois da anterior terminar o backup final. O ciclo roda 24/7 até bater o limite de uso.
- **Limites de uso.** Ao atingir `RENOVAR_MAX_HORAS` ou `RENOVAR_MAX_HORAS_MES`, a renovação pausa e avisa por e-mail; o ciclo volta sozinho quando a janela de tempo alivia. **Atenção ao plano Free em repositório privado: são ~2.000 minutos por mês, cerca de 5 sessões.** Com `RENOVAR_MAX_HORAS=20` o mês inteiro se esgota em pouco mais de um dia. Para caber no plano Free, defina `RENOVAR_MAX_HORAS_MES=25` (ou menos) e deixe o diário em `0`, e confira o consumo em **Settings → Billing**.
- **Disjuntor.** Três execuções seguidas com falha pausam a renovação por 3 horas, para não gastar minutos reabrindo uma sessão que já nasce quebrada.
- **Vigia (`renovar.yml`).** Roda ao fim de cada sessão e de 6 em 6 horas: se não houver sessão ativa (por exemplo, o disparo falhou ou o token venceu), reabre o ciclo e avisa por e-mail. Custa cerca de 1 minuto por checagem.
- **Modo de teste.** Uma execução com `needle_preview_only` não renova; o vigia ainda pode reabrir o ciclo depois dela, se `RENOVAR=true`.
- **Para parar o ciclo:** mude a variável `RENOVAR` para algo diferente de `true` (ou desabilite os workflows na aba Actions) e só então cancele a execução ativa. Cancelar com a renovação ligada faz o vigia reabrir o ciclo.
- **Incompatível com o revezamento.** Se `PEER_REPO` estiver definido, a renovação se recusa a rodar (use um ou outro). Não mantenha o `Agendar.ps1` periódico ligado junto com a renovação: ele criaria sessões duplicadas.
- **Token.** O `RENOVAR_TOKEN` só viaja no cabeçalho Authorization e nunca aparece nos logs. Ao vencer, você recebe um e-mail; renove o Secret e o ciclo volta com o vigia.

## GUI Central de Controle (pen drive)

A pasta `gui/` é uma central de controle completa em **um único arquivo Python** (`control_center.py`), sem nenhuma biblioteca extra — só o Python 3 instalado. Copie a pasta para um pen drive e use `Iniciar-GUI.bat` (Windows) ou `./iniciar-gui.sh` (Linux/macOS); abre em `http://127.0.0.1:8787`.

O que ela faz:

- **Sessão:** iniciar e parar execuções, ver o estado de cada uma e **abrir o desktop noVNC embutido na própria GUI** (o link é baixado do artifact da execução, com token — nada público).
- **Imagem & Desktop:** trocar a "ISO" (Ubuntu/Arch), o ambiente (XFCE/KDE/GNOME/Hyprland) e cadastrar **URLs de dotfiles do GitHub**.
- **Credenciais:** gravar os Secrets (Telegram, VNC, backup, SMTP, e-mail, `RENOVAR_TOKEN`, `STARTUP_SECRETS_JSON`) direto no repositório via GitHub CLI.
- **Automação IA:** comandos de início de sessão para Qwen + Needle (equivalente ao campo manual e ao `INICIALIZACAO.txt`), repassados também pela renovação automática.
- **Renovação:** ligar/desligar e ajustar os limites de uso.

Configuração da primeira vez:

1. Crie um token fine-grained PAT com **Actions, Variables e Secrets (leitura e escrita)** + Metadata (leitura) sobre o seu repositório.
2. Abra a GUI, vá na aba **Sessão**, informe repositório e token e salve.
3. Instale o GitHub CLI se for gravar Secrets pela GUI (`winget install GitHub.cli`).
4. Configure o restante pelas abas e clique em **▶ Iniciar sessão**.

A **GUI da sessão**: cada sessão abre uma aba do navegador com a mesma Central de Controle (modo sessão), lendo a configuração do momento do disparo e sincronizando alterações de volta para as **variáveis do repositório** — a mesma origem da verdade da GUI do pen drive. Nela você altera distro, desktop, dotfiles e instruções para as próximas sessões e encerra a sessão atual, sem sair do desktop.

## Distros, desktops e dotfiles

A sessão roda no GitHub Actions, então a "ISO" é a imagem de sistema do ambiente escolhido (não há boot de ISO real):

| Distro | Desktop | Como roda | Observações |
|---|---|---|---|
| Ubuntu 24.04 | XFCE | Direto no runner (caminho clássico) | TeleDrive aberto no desktop; backup completo |
| Arch Linux | KDE Plasma / GNOME / Hyprland | Container Docker `archlinux:base` | Instala ~1-3 GB de pacotes a cada sessão (10-25 min) |

Para o Arch, a home da sessão (`/root` do container) é o volume `/home/runner/sessao` no runner: configure `BACKUP_PATHS_JSON` como `["/home/runner/sessao"]` (a GUI faz isso na aba Credenciais) para o backup incremental cobrir seus arquivos. Limitações honestas do modo Arch: o app TeleDrive (.deb) não abre no desktop (o backup/restauração continuam funcionando normalmente), e GNOME/KDE podem exigir ajustes em sessões headless — se o desktop não aparecer, veja o log do passo "Abrir desktop".

**Dotfiles:** liste URLs de repositórios git (uma por linha) na variável `DOTFILES_URLS`. A cada início de sessão, cada repositório é clonado e seus arquivos são copiados para a home (`.git`, `README` e `LICENSE` ignorados), preservando a hierarquia. Use os dotfiles para deixar o Hyprland, o KDE ou o GNOME com a sua cara desde o primeiro minuto.

## Backup com nomes e pastas originais

O padrão agora é `layout: "original_names"` em `backup/escopo.json`. Os arquivos ficam navegáveis no TeleDrive com o nome original, sem serem reunidos em um ZIP de pacotes. A hierarquia absoluta da VPS é preservada sob `BACKUP_FOLDER/Arquivos`. Use uma BACKUP_FOLDER diferente para cada repositório ou VPS que grave no mesmo canal, evitando misturar árvores de máquinas diferentes.

Exemplo:

| Na VPS | No TeleDrive, com BACKUP_FOLDER padrão |
|---|---|
| `/home/runner/Projetos/meu-app/config.json` | `Linux Web PC/Arquivos/home/runner/Projetos/meu-app/config.json` |
| `/home/runner/Documentos/anotações.txt` | `Linux Web PC/Arquivos/home/runner/Documentos/anotações.txt` |
| `/home/runner/Pasta vazia/` | `Linux Web PC/Arquivos/home/runner/Pasta vazia/` |

Arquivos com o mesmo nome em pastas diferentes continuam separados. Nomes Unicode, espaços e extensões são mantidos. A árvore de pastas do TeleDrive registra também diretórios vazios; não são criados arquivos marcadores dentro deles. Arquivos grandes são divididos com os metadados nativos de partes: o TeleDrive os apresenta como um só arquivo com o nome original, embora o Telegram mostre as partes.

### Escopo e frequência

Por padrão, `backup/escopo.json` seleciona `/`: a varredura tenta ler arquivos persistentes acessíveis ao usuário `runner`. Exclui `/proc`, `/sys`, `/dev`, `/run`, `/tmp`, `/mnt`, `/media`, arquivos de swap, `RUNNER_TEMP` e caches comuns da home. Não usa sudo para ler arquivos privados do sistema. Links simbólicos são registrados no índice sem seguir seus destinos; sockets e dispositivos não são documentos comuns.

A primeira cópia inicia assim que o desktop está pronto; as seguintes usam os horários de 30 minutos contados desde o início do job, sem sobreposição. Há uma cópia final depois das tarefas de encerramento. O timer chama a ferramenta cadastrada `backup_changed_files` pelo executor do Needle; nenhum modelo lê os bytes dos arquivos.

**Copiar o sistema inteiro como arquivos individuais pode exigir muitos uploads e exceder a duração da sessão.** Para testar, use `BACKUP_PATHS_JSON=["/home/runner"]` ou escolha apenas projetos e documentos. Uma varredura longa adia a seguinte; 30 minutos é o intervalo planejado de início, não uma promessa de upload instantâneo.

### O que é enviado ou pulado

A comparação usa SHA-256 dos bytes. Se o mesmo caminho já tiver o mesmo conteúdo confirmado, não é reenviado. Conteúdo igual em um caminho novo pode ser copiado dentro do Telegram, sem novo upload dos bytes, preservando os dois nomes e lugares.

Quando o conteúdo de um arquivo muda, a versão nova fica no mesmo lugar lógico, com o mesmo nome. A versão anterior é marcada como arquivada no TeleDrive, sem apagar os dados. Não há espelhamento automático de exclusões: apagar um arquivo na VPS não apaga sua cópia anterior no TeleDrive.

A deduplicação usa o índice deste sistema e consegue recuperar arquivos completos enviados recentemente antes de uma interrupção. Ela não converte automaticamente pacotes ZIP antigos nem identifica qualquer arquivo avulso de terceiros que já estivesse no drive. Ao migrar do formato anterior, a primeira cópia com nomes originais precisa enviar os arquivos individuais; os pacotes anteriores são mantidos.

Um arquivo com todas as partes ainda não confirmadas permanece oculto. Arquivos que mudarem durante a leitura ou não puderem ser lidos são registrados como erro, mantendo a última versão confirmada quando existir. A cópia de arquivos ativos não é um snapshot atômico de bancos de dados: salve o trabalho e encerre serviços antes da cópia final.

### Criptografia e credenciais

**Arquivos individuais não usam a senha do ZIP.** Se o drive não tiver a criptografia nativa configurada, seus arquivos e nomes ficam sem essa proteção adicional no canal privado do Telegram, como uploads comuns do app. `BACKUP_PASSWORD` continua protegendo o índice de controle.

Se o seu TeleDrive usa criptografia, configure um Secret adicional chamado `TELEDRIVE_PASSPHRASE`, com a frase de recuperação que você configurou no próprio app. O script restaura a chave existente de `teledrive.keybackup`, usa o formato nativo AES-256-GCM para conteúdo e legendas, e sela a árvore de pastas. Ele não cria nem substitui a chave do drive. O TeleDrive desbloqueado mostra os nomes originais normalmente.

Quando há backup da chave nativa e o Secret está ausente ou incorreto, a verificação inicial bloqueia os envios. Não há fallback para arquivos em claro nessa situação. Alterações da chave exigem outro `BACKUP_FOLDER` ou migração própria. Não publique a frase de recuperação no repo nem a coloque nos pedidos da IA.

Sem criptografia nativa, arquivos de zero bytes são preservados no índice, pois o Telegram não aceita documentos vazios; eles não aparecem como documentos individuais. Com a criptografia nativa, o invólucro autenticado permite armazená-los como arquivos normais. Links simbólicos também ficam no inventário, sem seguir destinos ou substituí-los por conteúdo fictício.

### Abrir e recuperar

Sincronize o TeleDrive e abra `BACKUP_FOLDER/Arquivos`. Os arquivos comuns já podem ser abertos ou baixados diretamente no app, sem juntar pacotes ZIP. Versões anteriores ficam entre os itens arquivados.

O controle técnico fica em `BACKUP_FOLDER/_controle/PERFIL/arvore-PERFIL.zip`, marcado como oculto. Esse índice é protegido por `BACKUP_PASSWORD` e guarda referências, atributos, caminhos, arquivos vazios, links e erros. O Summary mostra as contagens de enviados e reutilizados. `scan_complete: true` dentro do índice significa que a varredura terminou sem erros detectados.

Para conferir o conteúdo baixado, recuperar arquivos vazios e reconstruir os caminhos em uma pasta separada, baixe o índice e a árvore `Arquivos` pelo app. Em Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/restaurar_incremental.py --indice "C:\Backup\arvore-PERFIL.zip" --pacotes "C:\Backup\Arquivos" --destino "C:\Restaurado" --prefixo /home/runner
```

O argumento histórico `--pacotes` aponta para a pasta `Arquivos` nesse formato. O índice fornece os caminhos e hashes; os arquivos de entrada já devem ter sido baixados e descriptografados pelo TeleDrive. A senha do índice é solicitada sem aparecer na tela. O destino precisa estar vazio; o utilitário não sobrescreve o sistema e lista links em `links-nao-restaurados.json`, sem criá-los. Alguns nomes e atributos Unix exigem restauração em Linux.

### Manter o formato de pacotes anterior

Se preferir os pacotes ZIP AES-256 do projeto anterior, defina `layout` como `encrypted_packs` em `backup/escopo.json`. Esse modo continua usando `BACKUP_FOLDER/_incremental/PERFIL`, `indice-PERFIL.zip`, pacotes de partes de até 512 MiB e o utilitário `restaurar_incremental.py` com a pasta de pacotes em `--pacotes`. Não oferece os arquivos originais diretamente navegáveis no app. Backups completos ainda mais antigos continuam usando `juntar_backup.py` e `_BACKUP_COMPLETO.json`.

A restauração dos dados na próxima sessão continua manual. O carregamento automático de ferramentas aprendidas é independente de restaurar arquivos pessoais; nenhuma cópia de arquivos reconstrói RAM, processos ou uma VM inicializável.

## Secrets e variáveis

O configurador grava os valores na cópia indicada em `-Repo`. Eles podem ser gerenciados em **Settings → Secrets and variables → Actions**. Cada usuário deve configurar os próprios Secrets; não reutilize credenciais de outras pessoas.

### Secrets

| Nome | Finalidade |
|---|---|
| `TG_API_ID` | API ID do aplicativo Telegram |
| `TG_API_HASH` | API Hash do aplicativo Telegram |
| `TG_SESSION` | Sessão autenticada da conta Telegram |
| `TG_CHANNEL_ID` | Identificador do canal privado usado pelo TeleDrive |
| `BACKUP_PASSWORD` | Senha do índice e dos pacotes opcionais, com pelo menos 12 caracteres |
| `TELEDRIVE_PASSPHRASE` | Frase de recuperação do app, obrigatória no modo de nomes originais se houver chave nativa do TeleDrive |
| `VNC_PASSWORD` | Senha VNC, com exatamente 8 caracteres ASCII imprimíveis |
| `SMTP_HOST` | Hostname do servidor SMTP |
| `SMTP_PORT` | Porta `465` ou `587`; o script usa `465` se estiver ausente |
| `SMTP_USER` | Usuário da conta remetente |
| `SMTP_PASSWORD` | Senha de aplicativo ou credencial SMTP |
| `EMAIL_FROM` | Remetente; se ausente, o script usa `SMTP_USER` |
| `EMAIL_TO` | Destinatário do aviso |
| `STARTUP_SECRETS_JSON` | Opcional: objeto JSON com as variáveis privadas que suas rotinas precisam |
| `PEER_REPO_TOKEN` | Opcional: token com Actions (leitura e escrita) no repositório par; usado só no revezamento |
| `RENOVAR_TOKEN` | Opcional: token com Actions (leitura e escrita) neste repositório; usado só na renovação automática |

`TG_SESSION` permite acesso à conta Telegram. Não publique credenciais, sessões ou senhas no código, em issues ou em capturas de tela. O configurador envia esses valores diretamente aos Secrets do repositório. Revise mudanças no workflow antes de executá-lo com suas credenciais. Para revogar a sessão, use **Telegram → Configurações → Dispositivos**.

Em repositórios públicos, trate o link do desktop como informação pública: este projeto o escreve no Summary e nos logs da execução — **a menos que você defina a variável `PUBLIC_REPO=true`**, que esconde o link do Summary e do log (ele sai somente por e-mail e pela GUI, via artifact protegido por token). O acesso VNC ainda exige a senha configurada.

### Variables

| Nome | Padrão | Uso |
|---|---|---|
| `BACKUP_FOLDER` | `Linux Web PC` | Pasta lógica no TeleDrive; aceita caminhos como `Backups/Linux Web PC` |
| `PEER_REPO` | vazio (revezamento desligado) | `USUARIO/REPOSITORIO` do par que receberá a próxima sessão |
| `PEER_WORKFLOW` | `linux-web-pc.yml` | Arquivo do workflow a disparar no par |
| `TELEDRIVE_AUTO` | ligado | `false` desliga a instalação, a abertura e o salvamento de sessão do TeleDrive |
| `WATCHDOG_ENABLED` | desligado | `true` liga o vigia neste repositório; use em apenas um dos dois |
| `RENOVAR` | vazio (renovação desligada) | `true` liga a renovação automática neste repositório |
| `RENOVAR_WORKFLOW` | `linux-web-pc.yml` | Arquivo do workflow a disparar na renovação |
| `RENOVAR_MAX_HORAS` | `20` | Limite de horas de sessão em 24 h (janela móvel); `0` desliga |
| `RENOVAR_MAX_HORAS_MES` | `0` (desligado) | Limite de horas em 30 dias (janela móvel) |
| `DISTRO` | `ubuntu` | Imagem da sessão: `ubuntu` ou `arch` |
| `DESKTOP_ENV` | `xfce` | `xfce` (Ubuntu) ou `kde`/`gnome`/`hyprland` (Arch) |
| `DOTFILES_URLS` | vazio | URLs de repositórios de dotfiles aplicados a cada sessão |
| `INSTRUCAO_IA` | vazio | Comandos para Qwen + Needle no início da sessão (usada quando o disparo manual não traz instruções) |
| `PUBLIC_REPO` | vazio | `true` em repositório público: o link do desktop sai só por e-mail e pela GUI (artifact), nunca no Summary ou no log |
| `BACKUP_PATHS_JSON` | `roots` em `backup/escopo.json`, inicialmente `["/"]` | Substitui a lista de caminhos absolutos da varredura incremental |

Exemplo para limitar o volume aos projetos:

```json
["/home/runner/work"]
```

Todas as origens devem existir no runner. Arquivos sem acesso são registrados como erro. As exclusões de sistemas virtuais e staging continuam ativas mesmo ao selecionar `/`. Uma variável `BACKUP_PATHS_JSON` já configurada na versão anterior continua prevalecendo sobre `backup/escopo.json`.

## Solução de problemas

| Situação | O que conferir |
|---|---|
| O workflow não aparece em Actions | Confirme `.github/workflows/linux-web-pc.yml` na branch padrão da sua cópia e habilite Actions se solicitado |
| Erro de arquivo ou script não encontrado | Preserve a pasta `scripts` no repositório e no computador |
| `gh` ou `python` não reconhecido | Instale o programa e abra novamente o PowerShell |
| Erro de permissão no GitHub | Confira a conta ativa com `gh auth status`; use em `-Repo` o endereço da sua cópia e confira suas permissões |
| Nenhum canal Telegram disponível | Configure primeiro um drive no TeleDrive e confira se o canal é privado e administrado por você |
| O e-mail não chega | Confira o passo de e-mail no Actions, os Secrets SMTP, a senha de aplicativo e a pasta de spam |
| O agendador continua esperando | Veja se uma execução anterior ainda está ativa ou na fila; confira o passo de backup |
| Backup ausente ou parcial | Confira contagens no Summary e `errors`/`scan_complete` no índice |
| Erro sobre chave do TeleDrive | Configure TELEDRIVE_PASSPHRASE com a frase de recuperação do próprio app |
| Ferramenta não reaparece na próxima sessão | Confira o job `persist-learned-tools`, as permissões de escrita e a branch `ferramentas-aprendidas` |
| Pedido não foi concluído | Simplifique a tarefa e confira se precisa de autenticação interativa ou credencial ausente |
| Falta de espaço ou upload demorado | Reduza `BACKUP_PATHS_JSON` às pastas necessárias |
| Link antigo parou de funcionar | Use o endereço da execução atual, recebido por e-mail ou disponível no Summary |
| A sessão para em "Verificar repositório par do revezamento" | Confira `PEER_REPO`, o Secret `PEER_REPO_TOKEN` (validade e permissão Actions) e se o workflow do par está ativo; ou remova `PEER_REPO` |
| "Revezamento não concluído" no log | A próxima sessão não foi aberta: corrija o token ou o par e inicie uma nova execução |
| `Agendar.ps1` recusa rodar | O revezamento está ativo; use `-UmaVez` ou remova `PEER_REPO` |
| O vigia não reabre o ciclo | Confira `WATCHDOG_ENABLED=true` (em um só repositório), se `vigia.yml` está na branch padrão e se o workflow agendado não foi desativado por inatividade (repositório público); lembre do disjuntor de 3 horas após 3 falhas seguidas |
| "Renovacao: " com erro no fim da sessão | Confira o Secret `RENOVAR_TOKEN` (validade e permissão Actions: leitura e escrita) e se o workflow está ativo; o vigia tenta reabrir de 6 em 6 horas |
| A renovação não abre nova sessão | Confira `RENOVAR=true`, o limite atingido (`RENOVAR_MAX_HORAS`/`RENOVAR_MAX_HORAS_MES` — o log e o e-mail informam), o disjuntor após 3 falhas e se há outra execução ativa; lembre que `needle_preview_only` não renova |
| O vigia da renovação não reabre o ciclo | Confira se `renovar.yml` está na branch padrão, se `RENOVAR=true` e se o workflow agendado não foi desativado por inatividade; confira também a validade do `RENOVAR_TOKEN` |
| A GUI não abre o desktop (noVNC) | O link fica pronto depois do passo "Abrir desktop"; aguarde e tente de novo. Confira também o token (permissão Actions) |
| A GUI não grava Secrets | Instale o GitHub CLI (`winget install GitHub.cli`), rode `gh auth status` e confira a permissão Secrets do token |
| Desktop Arch em tela preta | Veja o log do passo "Abrir desktop" e `/root/.sessao-de.log` dentro do container; GNOME/KDE em ambiente headless podem exigir ajustes |
| Dotfiles não apareceram | Confira o formato da URL (`https://github.com/usuario/repo`), se o repositório é público e o aviso no log do passo "Abrir desktop" |
| Arch: backup não vê meus arquivos | Configure `BACKUP_PATHS_JSON` como `["/home/runner/sessao"]` (aba Credenciais da GUI) |
| O TeleDrive abre pedindo login em toda sessão | Normal na primeira vez. Se persistir, veja o log do passo "Restaurar sessão e abrir o TeleDrive": `BACKUP_PASSWORD` diferente da usada ao salvar, ou a sessão foi encerrada pelo Telegram |
| "Checksum do TeleDrive não confere" | O `.deb` mudou ou `automacoes/teledrive.json` está desatualizado; confira a release e recalcule o `sha256` |
| Backup "aguardando a sessão anterior" | É a espera normal da sessão nova durante a sobreposição; acaba quando a antiga termina (ou em 45 minutos) |

## Limites de uso

- O ambiente é temporário. Cada execução começa em um sistema novo.
- O backup é incremental e mantém versões anteriores. Não há limpeza automática nem espelhamento de exclusões no TeleDrive.
- O formato de nomes originais prepara uma parte por vez, de até 512 MiB de conteúdo, mais o pequeno overhead da criptografia nativa quando ativa. O modo de pacotes continua exigindo staging do lote, podendo ser maior para um único arquivo grande. A varredura do sistema inteiro pode ser cara em CPU, disco e upload.
- O job está configurado para até **360 minutos**, com até **40 minutos** para o passo de backup. Esses valores não removem os limites do GitHub ou do Telegram.
- Cancelamento forçado, falhas de rede, sessão revogada, arquivos ativos e falta de espaço podem impedir um backup completo. O passo `always()` tenta salvar após falhas, mas não garante tempo para terminar em um cancelamento.
- O índice é criptografado; arquivos, legendas e árvore usam a criptografia nativa apenas quando a chave do app foi recuperada. Sem ela, nomes e bytes dos arquivos individuais ficam acessíveis no canal privado. Tamanhos e horários dos documentos continuam visíveis mesmo com criptografia nativa. Trocar BACKUP_PASSWORD exige uma nova BACKUP_FOLDER ou migração; senha errada não inicia um índice vazio automaticamente.
- O Qwen 2B pode escolher ações ou verificações incorretas. Ter um script que termina com código zero não prova que toda exigência foi atendida. Não são incluídas instalações específicas de agentes ou aplicativos de terceiros.
- A compatibilidade foi inspecionada no código do TeleDrive, commit `7576df8c6ab12f35286a1359b7a88842e92efadb`; o funcionamento com sua conta e versão instalada precisa ser confirmado no primeiro uso.

O GitHub restringe runners hospedados a atividades relacionadas ao desenvolvimento, teste, implantação ou publicação do projeto do repositório. Usar Actions como desktop pessoal ou VPS contínua pode violar seus termos. Consulte os [termos do GitHub Actions](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions), respeite as cotas da conta e use uma máquina própria ou VPS para um desktop permanente.

## Contribuições

Para sugerir uma melhoria ou relatar um erro, abra uma issue com os passos para reproduzir o problema e mensagens de erro sem dados sensíveis. Para propor uma alteração no código, trabalhe em uma branch da sua cópia e abra um pull request.

Ao alterar scripts ou workflows, mantenha a documentação alinhada ao comportamento implementado. Evite incluir senhas, tokens, sessões Telegram ou backups em commits.

## Tecnologias

[Qwen3.5-2B](https://huggingface.co/Qwen/Qwen3.5-2B) · [llama.cpp](https://github.com/ggml-org/llama.cpp) · [Cactus Needle 3](https://github.com/cactus-compute/needle) · [GitHub Actions](https://docs.github.com/en/actions) · [GitHub CLI](https://cli.github.com/) · [XFCE](https://xfce.org/) · [noVNC](https://github.com/novnc/noVNC) · [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) · [TeleDrive](https://github.com/Mahmud0808/TeleDrive) · [Telethon](https://docs.telethon.dev/) · [pyzipper](https://github.com/danifus/pyzipper)
