# CI/CD na VPS Oracle

O GitHub Actions testa e constrói a imagem em cada PR e push na `main`. Após os testes, faz deploy por SSH quando a variável `ORACLE_DEPLOY_ENABLED` está em `true`. Também aceita execução manual em Actions → CI and deploy to Oracle → Run workflow (branch `main`).

O deploy usa o commit testado, constrói a imagem na arquitetura da VPS (AMD64 ou ARM64), aguarda o health check por até 120 segundos e volta à última release bem-sucedida se falhar. A imagem anterior deve permanecer na VPS para rollback. Não há alterações nos serviços, portas ou proxy do Clash. O bot precisa apenas de conexões de saída; não publica portas HTTP.

## 1. Requisitos da VPS

- Docker Engine com plugin **Docker Compose v2**, incluindo suporte a `up --wait`.
- SSH, `bash`, `tar` e `flock` (pacote `util-linux`).
- Usuário SSH autorizado a executar Docker sem `sudo` e escrever em sua própria pasta pessoal.
- A conta leitora do Telegram deve participar dos canais públicos configurados.
- Saída de rede para Docker Hub, PyPI e Telegram; entrada SSH acessível ao executor do Actions, conforme suas regras de firewall.

O workflow do Clash sugere que Docker e SSH já estão disponíveis. Confira com `docker info` e `docker compose version`. Não é necessário reinstalar Docker nem alterar o Clash.

## 2. Configurar o GitHub

No repositório **CaioProg/telegram**, abra Settings → Environments e crie `production`.
Em Settings → Secrets and variables → Actions (ou no ambiente `production`), cadastre:

| Secret | Valor |
| --- | --- |
| `ORACLE_HOST` | IP ou hostname da VPS, sem `https://` |
| `ORACLE_USER` | Usuário SSH, por exemplo `ubuntu` |
| `ORACLE_SSH_KEY` | Chave privada SSH autorizada na VPS |
| `ORACLE_KNOWN_HOSTS` | Chave pública do servidor no formato `known_hosts` |

São os mesmos **nomes** usados no Clash. Segredos de um repositório não são herdados por outro; copie-os de onde você os guarda ou disponibilize secrets da organização para ambos. Não é possível recuperar seus valores pela API do GitHub. Use uma chave de deploy dedicada se preferir.

Obtenha `ORACLE_KNOWN_HOSTS` por uma conexão confiável e confira a impressão digital com a VPS; não desative `StrictHostKeyChecking`. O workflow usa SSH na porta 22.

Crie também a **variável** (não secret) `ORACLE_DEPLOY_ENABLED=true` somente após o preparo abaixo. Sem ela o CI continua funcionando e o deploy fica pulado. Esse controle permite concluir o login interativo antes do primeiro restart automático.

## 3. Credenciais e primeiro login na VPS

Conectado por SSH como o mesmo usuário configurado no Actions:

```sh
mkdir -p "$HOME/radar-promocoes/shared/data"
chmod 700 "$HOME/radar-promocoes/shared" "$HOME/radar-promocoes/shared/data"
cd "$HOME/radar-promocoes"
git clone https://github.com/CaioProg/telegram.git bootstrap
cp bootstrap/.env.example shared/.env
chmod 600 shared/.env
nano shared/.env
```

Preencha API ID, API Hash, **novo** token do bot e ID do administrador. Os canais são cadastrados depois pelo bot com `/adicionarcanal`; `TELEGRAM_CHANNELS` é opcional e serve apenas para a migração inicial de instalações antigas. As duas variáveis de caminho são sobrescritas pelo Compose, apontando para o volume persistente. Não envie esse arquivo ao GitHub. O token compartilhado anteriormente precisa ser substituído no BotFather.

```sh
cd "$HOME/radar-promocoes/bootstrap"
export RADAR_SHARED_DIR="$HOME/radar-promocoes/shared"
export RADAR_UID="$(id -u)" RADAR_GID="$(id -g)"
docker compose build radar
docker compose run --rm --no-deps radar python -m radar.login
```

Informe telefone, código e senha 2FA no terminal. Esse comando salva `shared/data/telegram.session`. Não execute o login em paralelo a um bot já rodando. Envie `/start` ao bot pela sua conta autorizada.

Agora ative `ORACLE_DEPLOY_ENABLED=true` no GitHub e execute o workflow manualmente na branch `main`, ou faça um novo push. Não é preciso iniciar o serviço manualmente.

## 4. Estrutura e operação

```text
~/radar-promocoes/
  shared/.env                 # credenciais; nunca sobrescritas pelo deploy
  shared/data/                # SQLite e sessão persistentes
  releases/<commit>/          # código de cada release
  current -> releases/<commit> # última release saudável
  deploy.lock
```

Os deploys usam sempre o projeto Compose `radar-promocoes`, garantindo uma única instância do bot. O usuário do contêiner corresponde ao usuário SSH para permitir escrita no volume. O sistema de arquivos da imagem é somente leitura; dados e `/tmp` são graváveis.

Para consultar o serviço ou parar:

```sh
cd "$HOME/radar-promocoes/current"
export RADAR_SHARED_DIR="$HOME/radar-promocoes/shared"
export RADAR_UID="$(id -u)" RADAR_GID="$(id -g)"
export RADAR_RELEASE="$(basename "$(readlink "$HOME/radar-promocoes/current")")"
docker compose ps
docker compose logs --tail=100 radar
docker compose stop radar
```

O heartbeat só é renovado quando o cliente Telegram está conectado, o polling está em execução e os trabalhadores continuam ativos. Ele verifica a atividade do processo, sem enviar mensagens de teste. O deploy falha se não ficar saudável. Docker reinicia processos que terminam; um contêiner apenas `unhealthy` não é reiniciado automaticamente pelo Docker. Configure monitoramento externo se desejar recuperação automática desse caso.

Rollback de imagem **não** reverte o banco. Este MVP não introduz migrações destrutivas; futuras alterações de schema exigem compatibilidade e backup. Releases e imagens anteriores são preservadas e precisam de limpeza periódica; mantenha a atual e ao menos uma anterior. Faça backups protegidos de `shared/`; pare o serviço ou use a API de backup SQLite para obter uma cópia consistente do banco em WAL.

Se não houver sessão no primeiro deploy, o workflow constrói a imagem e informa o comando de login, sem iniciar o bot. Se a sessão expirar, pare o serviço, refaça o login interativo e execute o workflow novamente.
