# Contexto do Projeto: Bot de Hidrômetro (Telegram)

## 📌 Visão Geral
Este é um bot de Telegram desenvolvido em Python para uso familiar. Seu objetivo principal é monitorar o consumo diário de água, registrar as leituras do hidrômetro via texto ou imagem (OCR) e alertar sobre possíveis vazamentos (teste de estanqueidade noturno). O sistema utiliza o Google Sheets como banco de dados e está hospedado no Render.

## 🛠️ Stack Tecnológica
- **Linguagem:** Python 3.x
- **Bot Framework:** `pyTelegramBotAPI` (polling)
- **Banco de Dados:** API do Google Sheets (`gspread`, `oauth2client`)
- **Processamento de Imagem:** `Pillow` (PIL) para tratamento e compressão.
- **OCR:** API externa do OCR.space.
- **Fuso Horário:** Biblioteca `pytz` (`America/Campo_Grande`).
- **Agendamento e Assincronicidade:** Laço customizado em Thread (`threading`).
- **Hospedagem/Deploy:** Render (Web Service) via GitHub.
- **Keep-Alive:** `Flask` e `gunicorn`.

## 🏗️ Arquitetura e Decisões Críticas do Sistema
Ao modificar ou adicionar código a este projeto, RESPEITE estritamente as seguintes regras de arquitetura:

### 1. Máquina de Estados e Persistência na Nuvem
Para evitar conflitos quando múltiplas pessoas usam o bot simultaneamente, o sistema armazena o estado transiente (em andamento) de forma individual por usuário. 
Para resolver o problema do sistema de arquivos efêmero do Render (que apaga os dados locais a cada restart), o `controle_diario` (quem ligou/desligou a água) e os `horarios_noturnos` são **salvos na aba `Config` do próprio Google Sheets**. Caso a aba não exista, o bot cria automaticamente. Os principais estados temporários são:
- `'ocioso'`: Estado padrão. O bot ignora fotos ou textos numéricos soltos para evitar spam.
- `'matinal'`: Ativado ao clicar em "Liguei a Água". Aguarda a leitura para calcular o consumo diário.
- `'noturno'`: Ativado ao clicar em "Desliguei a Água". Aguarda a leitura para marcar o teste de estanqueidade.
- `'processando'`: **MUITO IMPORTANTE.** Estado temporário ativado no milissegundo em que o bot recebe a foto/texto. Serve para travar o "Monitor de Esquecimento" e evitar alertas duplicados enquanto o Google Sheets processa o salvamento (que pode levar até 8 segundos).

### 2. Monitor de Esquecimento (Threads)
Sempre que um usuário inicia uma ação ("Ligar" ou "Desligar"), uma Thread paralela é disparada esperando 3 minutos (`time.sleep(180)`). Se após esse tempo o `estado_bot` ainda for o mesmo (ou seja, a leitura não entrou no estado `'processando'`), o bot dedura o usuário no grupo da família e salva a falha na aba de Logs. **Nunca trave a thread principal (main thread) com `time.sleep`.**

### 3. Conexão com Google Sheets (Trava de Colunas)
As leituras são salvas na planilha "Monitoramento", aba "Dados" ou "Logs". 
- **CRÍTICO:** A função `append_row` DEVE SEMPRE usar o parâmetro `table_range="A:D"`. Existe um painel visual a partir da coluna J na aba "Dados". Se o `table_range` for removido, o `gspread` fará o append das novas leituras no final do painel (linha 10+), quebrando a estrutura de dados.

### 4. Tratamento de Imagens (OCR)
Antes de enviar a imagem para a API do OCR.space, a função `comprimir_imagem` converte a foto para tons de cinza ('L'), aplica autocontraste, realça as bordas (`EDGE_ENHANCE_MORE`) e aumenta o contraste em 1.5x. Não remova esses filtros, pois eles garantem o funcionamento do OCR com fotos tiradas de hidrômetros arranhados ou com pouca luz.

### 5. Coexistência Flask + TeleBot (Deploy no Render)
O arquivo `bot.py` roda dois servidores simultaneamente:
1. Um app Flask rodando na porta dinâmica do Render (`os.environ.get('PORT')`) através de uma Thread daemon (`run_web`). Isso expõe a rota `/` que responde `200 OK`.
2. O bot do Telegram rodando na main thread com `bot.infinity_polling()`.
- **Atenção no Start Command:** O deploy deve ser feito com o comando `python bot.py` e NUNCA `gunicorn bot:app`, pois o Gunicorn ignora o bloco `__main__` e não inicia o polling do Telegram.

## 🎯 Regras para Novas Funcionalidades (Prompt Guidelines)
Se solicitado para criar novas features ou refatorar:
1. **Mantenha o feedback visual imediato:** Sempre que uma requisição à API (Google Sheets ou OCR) for iniciada, responda ao usuário imediatamente com uma mensagem ou emoji de loading (⏳) e atualize essa mensagem com `edit_message_text` quando o processamento terminar. Usuários finais odeiam interagir com bots silenciosos.
2. **Tratamento de Expressões Regulares (RegEx):** Sempre que for capturar números, preveja que os usuários podem enviar texto junto ("A leitura deu 459 hoje"). Extraia os números de forma robusta.
3. **Mantenha variáveis sensíveis no `.env`:** Nunca hardcode URLs, tokens de API ou IDs do Telegram (ex: `MEU_CHAT_ID`, `CONTATOS_FAMILIA`) no código fonte. Mantenha as chamadas via `os.getenv()`.