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
radar
```

- `TELEGRAM_API_ID` e `TELEGRAM_API_HASH`: credenciais da conta leitora obtidas em https://my.telegram.org.
- `TELEGRAM_BOT_TOKEN`: token fornecido pelo BotFather.
- `TELEGRAM_ADMIN_ID`: ID numérico do único usuário autorizado. Os comandos e botões só funcionam na conversa privada desse usuário com o bot.
- `TELEGRAM_CHANNELS`: usernames de canais **públicos**, separados por vírgula, sem URLs. A conta leitora deve participar deles para receber novas mensagens em tempo real.
- `RADAR_DB`: caminho SQLite (padrão `data/radar.sqlite3`).
- `TELEGRAM_SESSION`: caminho da sessão Telethon (padrão `data/telegram`).

O primeiro `radar` solicita telefone, código e, quando configurada, senha 2FA da **conta leitora** no terminal. Não é a sessão do bot. Resolva esse login antes de executar como serviço. Inicie uma conversa com o bot por `/start` para permitir as notificações. Preserve o diretório `data/` entre reinícios e mantenha apenas uma instância do processo. Nunca versione `.env`, banco ou sessão.

## Comandos

| Comando | Conversa |
| --- | --- |
| `/adicionar` | Nome → cadastrar → importar sim/não → período explícito |
| `/remover` | Produto → confirmar; preserva ofertas e interrompe importações do produto |
| `/listar` | Produtos ativos e preços-alvo opcionais |
| `/preco` | Produto → valor em reais ou `sem alvo` |
| `/historico` | Produto → período → relatório local e preenchimento de lacunas em segundo plano |
| `/menorpreco` | Produto → período → menor preço observado e relatório com contexto |
| `/buscas` | Estado das últimas dez importações |
| `/cancelar` | Encerra a conversa atual e cancela todas as importações pendentes/em andamento |

Períodos: 7, 30, 90 dias, 6 meses, 1 ano e personalizado (`DD/MM/AAAA DD/MM/AAAA`, dia final incluído). Datas e relatórios em UTC. Não há importação automática na inicialização ou no cadastro. Novas consultas sempre pedem o período; somente tarefas previamente autorizadas e interrompidas são retomadas automaticamente.

## Persistência e execução

- Alterações de produtos passam a valer na próxima mensagem, sem reiniciar.
- Um trabalhador de importação percorre mensagens antigas, com pausas entre lotes. O monitoramento ao vivo usa um handler separado e não aguarda a importação.
- Cursor persistido por canal/tarefa, inclusive para mensagens sem correspondência. Erros temporários geram nova tentativa após 30 segundos. `FloodWait` pausa somente a importação pelo tempo solicitado pelo Telegram.
- As coberturas são registradas apenas ao concluir cada intervalo. Consultas reutilizam intervalos completos; ofertas possuem chave única por produto/canal/mensagem. Reinícios podem reler a última mensagem, mas não duplicam ofertas. Buscas sobrepostas pendentes podem reler mensagens de intervalos parcialmente cobertos; os dados continuam deduplicados.
- Cancelamento é verificado a cada mensagem. Durante `FloodWait`, o estado é cancelado imediatamente no banco, e o trabalhador o observa ao retomar. Dados parciais permanecem consultáveis, com aviso de incompletude.
- Falha de envio não bloqueia a importação: notificações ficam em uma caixa de saída persistente. Entrega é pelo menos uma vez: uma falha entre envio e confirmação pode repetir uma notificação.

Sem alvo, cada nova oferta reconhecida gera notificação. Com alvo, notificamos valores até o alvo e novos menores preços observados, inclusive acima do alvo. Importações antigas não disparam notificações individuais; enviam um relatório ao terminar. O relatório mostra quantidade, mínimo, média, último valor por data, data/loja/cupom da melhor oferta e até dez links das melhores ofertas.

## Limites do MVP

O menor valor é **observado nos canais configurados**, não o menor preço de todo o mercado. Histórico completo significa que os intervalos foram percorridos nos canais configurados; mensagens apagadas ou indisponíveis não podem ser recuperadas. Não há coleta automática de mensagens perdidas durante indisponibilidade: consulte explicitamente o período desejado.

O extrator exige o nome completo do produto e um preço com `R$`; reconhece `Loja:`/`Vendido por:` e `Cupom`. Assume o último valor em reais da mensagem como preço anunciado. Mensagens com parcelas, variantes ou múltiplos produtos podem exigir revisão e adaptação do extrator ao formato dos seus canais; não calcula descontos condicionais nem consulta páginas das lojas. Edições de mensagens não atualizam ofertas existentes. Canais privados não são suportados nesta versão.

## Testes

```sh
python -m pytest -q
```

Cobrem autorização, cadastro sem importação implícita, preços, deduplicação, relatórios, notificações, intervalos ausentes, cancelamento e retomada do cursor após falha. Não exigem credenciais. A validação real com o Telegram depende da configuração da conta e dos canais.
