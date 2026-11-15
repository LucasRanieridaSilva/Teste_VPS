# Automações com Qwen3.5-2B + Needle 3

O Qwen verifica se as ferramentas existentes cobrem cada pedido. O executor Needle aplica ferramentas conhecidas. Pedidos sem cobertura, recusados pelo Needle ou com execução malsucedida passam para o agente Qwen, que pode usar o terminal para tentar concluir a tarefa.

O modelo usado é Qwen3.5-2B Q4_K_M, servido localmente por llama.cpp em CPU, com contexto de 8192 tokens e saída JSON limitada por schema nas ações do agente. O runtime e o modelo têm revisão e SHA-256 verificados. O servidor escuta apenas em localhost com uma chave temporária. Ele é encerrado ao sair de cada fase, inclusive em falhas normais. Arquivos dos pesos permanecem no staging, mas o processo e sua memória são liberados; páginas de cache do sistema podem permanecer recuperáveis pelo kernel.

## Onde escrever os pedidos

- `INICIALIZACAO.txt`: tarefas de instalação, preparação e abertura de aplicativos.
- `ENCERRAMENTO.txt`: tarefas aplicadas antes da cópia final, como fechar serviços e bancos de dados.

Use uma tarefa por linha. Linhas vazias e comentários com `#` são ignorados. Cada arquivo aceita até 20 tarefas, 2000 caracteres por linha e 12000 caracteres no total. Sem tarefas, o Qwen não é carregado.

Exemplos para início:

```text
Instale os programas da lista.
Prepare minhas pastas de trabalho.
Crie uma pasta chamada demonstracao dentro da minha home.
```

Os exemplos não incluem instalação ou configuração de um agente específico. Cada pessoa define seus próprios pedidos. Um login que dependa de código enviado ao telefone, CAPTCHA ou confirmação interativa pode exigir intervenção prévia e uma sessão restaurável.

No **Actions → Linux Web PC → Run workflow**:

| Campo | Efeito |
|---|---|
| `startup_instruction` | Substitui `INICIALIZACAO.txt` apenas nessa execução |
| `needle_preview_only` | Verifica o roteamento sem aplicar tarefas ou aprender ferramentas |

O preview ainda abre o desktop e executa o backup. Ele não descobre todos os erros que só surgem na execução real. Para usar instruções em todas as sessões, salve-as nos arquivos da branch que o agendador utiliza.

## Como as ações são divididas

1. O Qwen classifica os pedidos de início entre preparação e abertura de aplicativos gráficos.
2. Ele compara cada tarefa com as descrições e listas salvas das ferramentas candidatas.
3. Se houver cobertura completa, o Needle resolve os nomes cadastrados e o executor executa os scripts. Uma seleção incompleta ou de baixa confiança passa ao Qwen.
4. O Qwen usa no máximo dez rodadas de ações, com comando e retorno limitado. O modelo recebe observações com os valores conhecidos dos Secrets substituídos por `[SECRET]`.
5. Para concluir, ele apresenta uma implementação Bash reutilizável e verificações Bash de resultado. O projeto valida a sintaxe, executa a implementação completa e depois as verificações.
6. Somente ferramentas cuja execução final terminou com sucesso são cadastradas para reutilização.
7. Depois de processar a fase, o servidor Qwen é encerrado. Ele pode ser carregado novamente na fase gráfica ou no encerramento.

Ferramentas novas precisam tolerar execução repetida, pois a implementação final é aplicada novamente antes de ser salva. Uma tentativa malsucedida pode deixar alterações parciais na VM; não há rollback automático. Uma verificação incorreta inventada pelo modelo também pode passar: sucesso do script não é uma prova formal de que todo o pedido foi atendido.

Padrões `triggers` das rotinas manuais restringem quais ferramentas são propostas para cada frase. Depois disso, o Qwen avalia a cobertura da proposta.

Na repetição exata de uma tarefa aprendida, o hash normalizado da tarefa identifica a ferramenta cadastrada, e ela é despachada pelo executor sem uma nova inferência do Needle. O Qwen ainda faz a checagem do início da fase. Para frases diferentes, a seleção depende das descrições; até oito candidatas são apresentadas por tarefa, priorizando correspondência exata e palavras compartilhadas. Um catálogo maior não significa que o modelo verá todas as ferramentas simultaneamente.

## Catálogo e ferramentas aprendidas

As ferramentas incluídas ficam em `rotinas.json`. É possível continuar criando scripts manuais em `rotinas/` e cadastrá-los nesse arquivo. As fases são `before_desktop`, `after_desktop` e `before_backup`. A implementação manual aceita até 32 rotinas; o catálogo aprendido aceita até 128 ferramentas.

| Ferramenta incluída | Dados/configuração |
|---|---|
| `install_programs_from_list` | `pacotes.txt`: um pacote apt por linha |
| `prepare_work_folders` | `pastas.json`: lista de diretórios relativos à home |
| `open_programs_from_list` | `programas.json`: listas com executável e argumentos |
| `backup_changed_files` | `../backup/escopo.json`: ferramenta chamada pelo timer fixo |

Pedir um pacote específico não modifica automaticamente a lista de instalação. Se a lista não cobre todo o pedido, o Qwen precisa resolver a tarefa e produzir uma ferramenta própria.

Ferramentas aprendidas usam nomes imutáveis como `learn_...`, scripts em `rotinas/aprendidas/` e metadados em `aprendidas.json`. O texto original do pedido não é salvo no catálogo aprendido; somente um hash e a descrição genérica produzida para a ferramenta.

No fim do workflow, apenas esse catálogo e os scripts referenciados são exportados. Um **job separado**, em um runner novo e sem executar as ferramentas, valida novamente os arquivos contra as credenciais conhecidas e publica um commit na branch `ferramentas-aprendidas`. A branch principal não é alterada. O token de escrita é fornecido somente a esse job, não ao agente. No começo da próxima execução, o projeto baixa a branch dedicada e valida novamente seu conteúdo.

A publicação acontece depois do encerramento, não imediatamente ao terminar cada tarefa. O agendador espera todo o workflow, incluindo a persistência, antes da próxima sessão. Se houver bloqueio de escrita, regras de branch, cancelamento ou falha de rede, o artefato `learned-tools-ID-TENTATIVA` permite recuperar o catálogo por sete dias; a reutilização automática entre sessões exige que a publicação termine.

Permissões: confira **Settings → Actions → General → Workflow permissions** e permita escrita quando a política da conta exigir. O workflow solicita `contents: read` para o desktop e `contents: write` somente para a persistência. Não requer um token pessoal adicional.

## Repo público e credenciais

**A branch de ferramentas é pública quando o repositório é público.** Cada pessoa deve usar sua própria cópia e suas próprias credenciais. Nenhum programa específico é instalado por padrão pelas tarefas comentadas.

Crie um Secret `STARTUP_SECRETS_JSON`, se suas ações precisarem de credenciais:

```json
{
  "APP_API_KEY": "valor-privado",
  "APP_SESSION": "sessao-privada-se-o-aplicativo-permitir"
}
```

Use nomes das variáveis nos pedidos e nos scripts. Os comandos recebem esses valores pelo ambiente; o servidor do modelo recebe apenas o nome das variáveis e observações redigidas. As credenciais do backup e o token de publicação não são fornecidos como ambiente de execução aos comandos do Qwen. Os scripts manuais têm seus valores conhecidos registrados para mascaramento nos logs do Actions.

O filtro de publicação bloqueia valores conhecidos dos Secrets, algumas representações codificadas, chaves privadas, tokens reconhecíveis e determinados caminhos pessoais. Essa análise é uma proteção limitada, **não uma garantia contra todas as formas de vazamento ou código incorreto**. O agente tem acesso ao terminal da VM e pode ler arquivos acessíveis ao usuário; observações privadas desconhecidas pelo filtro continuam sendo sensíveis. Não peça para embutir dados de conta, conteúdo de documentos ou sessões em uma ferramenta pública.

A ferramenta pode usar `$APP_API_KEY`, mas não deve gravar o valor literal da chave no código. Dados privados que um aplicativo salvar em disco entram apenas no backup criptografado, quando estiverem no escopo selecionado; não devem entrar nos arquivos da ferramenta. Revise ferramentas aprendidas no seu repositório e remova as que não desejar reutilizar.

## Backup de 30 minutos

O timer chama a ferramenta `backup_changed_files` diretamente pelo executor. Não mantém Qwen ou Needle carregados para comparar arquivos. Quem detecta alterações é o scanner determinístico por SHA-256, que evita decisões probabilísticas sobre enviar ou pular dados.

O primeiro ciclo cria a árvore de arquivos com nomes originais; os seguintes reutilizam conteúdo confirmado no índice remoto, inclusive entre sessões. Pastas vazias são registradas no formato nativo do TeleDrive. Há uma varredura final depois das tarefas de encerramento. O backup não depende de a IA concluir essas tarefas, mas o GitHub ainda pode encerrar o runner por timeout ou cancelamento.

Se o drive tiver chave de criptografia nativa, configure `TELEDRIVE_PASSPHRASE` nos Secrets. Arquivos individuais sem a criptografia nativa não recebem a senha do ZIP. O índice continua protegido por `BACKUP_PASSWORD`.

O padrão `/` tenta incluir arquivos persistentes legíveis, com exclusões de sistemas virtuais, staging e caches. A primeira varredura pode ser muito pesada; comece com `BACKUP_PATHS_JSON=["/home/runner"]` para testar. Não há cópia da RAM nem reconstrução automática do sistema. Veja o [README principal](../README.md) para escopo, limitações e recuperação.

## Desenvolvimento e testes

Os testes locais do executor antigo não requerem o modelo. Para rodar também os testes incrementais:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

A execução de scripts Bash, o scanner incremental e os testes que usam `fcntl` são destinados ao Linux. No Windows, use Linux/WSL para a suíte completa; o utilitário de restauração pode funcionar nativamente, respeitando as limitações de nomes e atributos.

Os testes automatizados usam respostas simuladas para falhas e roteamento, transportes simulados para uploads e diretórios temporários reais para execução/restauração. Testes manuais com Qwen e Needle reais verificaram seleção e execução de rotina conhecida, aprendizado de uma nova rotina de pastas, reutilização com outra home e descarregamento do Qwen. Não é necessário cadastrar chave de IA.

## Referências

- [Qwen3.5-2B](https://huggingface.co/Qwen/Qwen3.5-2B)
- [Pesos GGUF usados](https://huggingface.co/lmstudio-community/Qwen3.5-2B-GGUF)
- [llama.cpp](https://github.com/ggml-org/llama.cpp)
- [Needle Python Docs](https://www.cactuscompute.com/blog/needle-python-docs)
- [Permissões do GITHUB_TOKEN](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token)
