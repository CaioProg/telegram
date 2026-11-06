# Radar de Promoções — MVP

Bot privado para gerenciar produtos, monitorar canais públicos e consultar ofertas anteriores com SQLite + Telethon. Produtos iniciais: **Motorola Edge 70 Pro** e **Motorola Signature**, sem preço-alvo obrigatório.

## Configuração

Requer Python 3.11+ e uma conta Telegram com acesso aos canais escolhidos.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
# Preencha .env e exporte as variáveis em seu shell ou gerenciador de serviços.
set -a
. ./.env
set +a
python -m radar.login
radar
```

- `TELEGRAM_API_ID` e `TELEGRAM_API_HASH`: credenciais da conta leitora obtidas em https://my.telegram.org.
- `TELEGRAM_BOT_TOKEN`: token fornecido pelo BotFather.
- `TELEGRAM_ADMIN_ID`: ID numérico do único usuário autorizado. Os comandos e botões só funcionam na conversa privada desse usuário com o bot.
- `TELEGRAM_CHANNELS`: **opcional**, apenas para migrar canais legados na primeira inicialização desta versão. Depois, gerencie os canais por `/adicionarcanal`, `/removercanal` e `/listarcanais`. A conta leitora deve participar dos canais públicos. O bot pode iniciar sem nenhum canal cadastrado.
- `RADAR_DB`: caminho SQLite (padrão `data/radar.sqlite3`).
- `TELEGRAM_SESSION`: caminho da sessão Telethon (padrão `data/telegram`).

O comando `python -m radar.login` solicita telefone, código e, quando configurada, senha 2FA da **conta leitora** no terminal. O serviço `radar` exige sessão já autenticada e encerra com instrução de login quando ela estiver ausente ou expirada. Não é a sessão do bot. Resolva esse login antes de executar como serviço. Inicie uma conversa com o bot por `/start` para permitir as notificações. Preserve o diretório `data/` entre reinícios e mantenha apenas uma instância do processo. Nunca versione `.env`, banco ou sessão.

## Comandos

| Comando | Conversa |
| --- | --- |
| `/adicionar` | Nome → cadastrar → importar sim/não → período explícito |
| `/remover` | Produto → confirmar; preserva ofertas e interrompe importações do produto |
| `/listar` | Produtos ativos e preços-alvo opcionais |
| `/preco` | Produto → valor em reais ou `sem alvo` |
| `/historico` | Produto → período → relatório local e preenchimento de lacunas em segundo plano |
| `/menorpreco` | Produto → período → menor preço observado e relatório com contexto |
| `/adicionarcanal` | Username ou link público → validar acesso da conta leitora → ativar sem reiniciar |
| `/removercanal` | Canal → confirmar; desativa monitoramento, cancela buscas pendentes desse canal e preserva histórico |
| `/listarcanais` | Canais ativos cadastrados no SQLite |
| `/buscas` | Últimas dez importações: fila, canais concluídos, canal atual, mensagens candidatas examinadas, ofertas novas e pausas do Telegram |
| `/cancelar` | Encerra a conversa atual e cancela todas as importações pendentes/em andamento |

Períodos: 7, 30, 90 dias, 6 meses, 1 ano e personalizado (`DD/MM/AAAA DD/MM/AAAA`, dia final incluído). Datas e relatórios em UTC. Não há importação automática na inicialização ou no cadastro. Novas consultas sempre pedem o período; somente tarefas previamente autorizadas e interrompidas são retomadas automaticamente.

## Canais pelo bot

Na conversa privada com o bot, use `/adicionarcanal` e envie `@username` ou `https://t.me/username`. O cadastro valida que é um canal público de transmissão e que a conta leitora participa dele. Grupos, usuários, bots, links de mensagens e convites privados não são aceitos. Entre no canal pelo Telegram da conta leitora antes de tentar cadastrar; o bot não entra nem sai de canais automaticamente.

A mudança passa a valer imediatamente. Adicionar não importa mensagens antigas: use `/historico`, selecione o produto e o período para autorizar uma importação. Remover pede confirmação, desativa novas coletas e cancela as tarefas que ainda consultariam aquele canal. Ofertas e coberturas já armazenadas permanecem disponíveis; reativar o mesmo canal reutiliza esse histórico. Relatórios preservam também ofertas de canais removidos.

Ao atualizar uma instalação antiga, os canais de `TELEGRAM_CHANNELS` são validados e importados uma única vez. Falhas de validação são registradas no log e devem ser corrigidas por `/adicionarcanal`; não bloqueiam o início do bot. Depois dessa migração, alterar a variável não altera os canais do banco nem restaura canais removidos. As tabelas novas são criadas sem apagar os dados anteriores.

## Persistência e execução

- Alterações de produtos passam a valer na próxima mensagem, sem reiniciar.
- Um trabalhador de importação usa a busca do Telegram por uma palavra obrigatória do nome (por exemplo, `edge` para Motorola Edge 70 Pro), e valida nome completo e preço localmente. Uma única palavra evita exigir uma frase exata na busca; a regra de reconhecimento do produto permanece igual. Nomes com caracteres não ASCII ou sem palavra adequada usam a leitura integral como alternativa. Há pausas entre lotes. O monitoramento ao vivo usa um handler separado e não aguarda a importação.
- `/buscas` mostra progresso persistido por canal, mensagens candidatas examinadas, ofertas novas e a última data examinada. Não estima porcentagem nem tempo restante, pois o total de resultados é desconhecido. Durante `FloodWait`, mostra o horário previsto de retomada.
- Ao atualizar da leitura integral para a busca, apenas os canais de tarefas ainda incompletos recomeçam do início do intervalo. Ofertas já salvas são deduplicadas e coberturas concluídas são preservadas.
- Cursor persistido por canal/tarefa, inclusive para mensagens sem correspondência. Erros temporários geram nova tentativa após 30 segundos. `FloodWait` pausa somente a importação pelo tempo solicitado pelo Telegram.
- As coberturas são registradas apenas ao concluir cada intervalo. Consultas reutilizam intervalos completos; ofertas possuem chave única por produto/canal/mensagem. Reinícios podem reler a última mensagem, mas não duplicam ofertas. Buscas sobrepostas pendentes podem reler mensagens de intervalos parcialmente cobertos; os dados continuam deduplicados.
- Cancelamento é verificado a cada mensagem. Durante `FloodWait`, o estado é cancelado imediatamente no banco, e o trabalhador o observa ao retomar. Dados parciais permanecem consultáveis, com aviso de incompletude.
- Falha de envio não bloqueia a importação: notificações ficam em uma caixa de saída persistente. Entrega é pelo menos uma vez: uma falha entre envio e confirmação pode repetir uma notificação.

Sem alvo, cada nova oferta reconhecida gera notificação. Com alvo, notificamos valores até o alvo e novos menores preços observados, inclusive acima do alvo. Importações antigas não disparam notificações individuais; enviam um relatório ao terminar. O relatório mostra quantidade, mínimo, média, último valor por data, data/loja/cupom da melhor oferta e até dez links das melhores ofertas.

## Limites do MVP

O menor valor é **observado nos canais configurados**, não o menor preço de todo o mercado. Histórico processado significa que a consulta dos intervalos terminou nos canais configurados. Na busca otimizada, a cobertura depende dos resultados indexados pelo Telegram; mensagens apagadas, indisponíveis ou ausentes do índice não podem ser recuperadas por essa busca. Não há coleta automática de mensagens perdidas durante indisponibilidade: consulte explicitamente o período desejado.

O extrator exige o nome completo do produto e um preço com `R$`; reconhece `Loja:`/`Vendido por:` e `Cupom`. Assume o último valor em reais da mensagem como preço anunciado. Mensagens com parcelas, variantes ou múltiplos produtos podem exigir revisão e adaptação do extrator ao formato dos seus canais; não calcula descontos condicionais nem consulta páginas das lojas. Edições de mensagens não atualizam ofertas existentes. Canais privados não são suportados nesta versão.

## Testes

```sh
python -m pytest -q
```

Cobrem autorização, gerenciamento interativo de canais, validação de acesso, migração e persistência, monitoramento dinâmico, cadastro sem importação implícita, preços, deduplicação, relatórios, notificações, intervalos ausentes, cancelamento e retomada do cursor após falha. Não exigem credenciais. A validação real com o Telegram depende da configuração da conta e dos canais.

## Deploy automático na Oracle

Veja [o guia de CI/CD](deploy/README.md) para configurar o GitHub Actions, os secrets SSH e o primeiro login na VPS. O deploy preserva banco e sessão, verifica a saúde do bot e tenta rollback da imagem em caso de falha.
