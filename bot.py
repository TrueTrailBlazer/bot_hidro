import os
import re
import threading
import time
from datetime import datetime

import schedule
import gspread
import requests
import telebot
from dotenv import load_dotenv
from flask import Flask
from oauth2client.service_account import ServiceAccountCredentials
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# --- CONFIGURAÇÃO DE AMBIENTE ---
load_dotenv()
os.environ["TZ"] = "America/Campo_Grande"
if hasattr(time, "tzset"):
    time.tzset()

# --- VARIÁVEIS SENSÍVEIS (Vindas do .env) ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")
MEU_CHAT_ID = os.getenv("MEU_CHAT_ID")
NOME_PLANILHA = os.getenv("NOME_PLANILHA", "Monitoramento Agua")

CONTATOS_FAMILIA = {
    "Mãe": os.getenv("ID_MAE"),
    "Luan": os.getenv("ID_LUAN"),
    "Lara": os.getenv("ID_LARA"),
}

# --- INICIALIZAÇÃO DO BOT E FLASK (KEEP ALIVE) ---
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

@app.route("/")
def health_check():
    return "Bot do Hidrômetro: Online e Monitorando!", 200

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- ESTADOS E VARIÁVEIS GLOBAIS ---
estado_bot = "ocioso"
quem_desligou_hoje = None
horarios_noturnos = ["19:00", "21:00", "23:00"]
TEXTOS_BOTOES = [
    "🟢 Liguei a Água",
    "🔴 Desliguei a Água",
    "📸 Leitura Avulsa",
    "⚙️ Configurações",
]

# ==========================================
# CONEXÃO E SALVAMENTO (GOOGLE SHEETS)
# ==========================================
def conectar_planilha(aba):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    caminhos_tentar = ["credentials.json", "/etc/secrets/credentials.json"]
    path_final = None

    for p in caminhos_tentar:
        if os.path.exists(p):
            path_final = p
            break

    if not path_final:
        raise FileNotFoundError("Arquivo credentials.json não encontrado.")

    creds = ServiceAccountCredentials.from_json_keyfile_name(path_final, scope)
    client = gspread.authorize(creds)
    return client.open(NOME_PLANILHA).worksheet(aba)

def salvar_na_planilha(quem, leitura):
    sheet = conectar_planilha("Dados")
    data_atual = datetime.now().strftime("%d/%m/%Y")
    hora_atual = datetime.now().strftime("%H:%M:%S")
    sheet.append_row([data_atual, hora_atual, quem, leitura], table_range="A:D")
    return True

def salvar_log(quem, acao):
    try:
        sheet_logs = conectar_planilha("Logs")
        data_atual = datetime.now().strftime("%d/%m/%Y")
        hora_atual = datetime.now().strftime("%H:%M:%S")
        sheet_logs.append_row([data_atual, hora_atual, quem, acao], table_range="A:D")
        return True
    except Exception as e:
        print(f"❌ [ERRO PLANILHA LOGS] {e}")
        return False

# ==========================================
# TRATAMENTO DE IMAGEM E OCR
# ==========================================
def comprimir_imagem(input_path, output_path):
    try:
        img = Image.open(input_path)
        if img.width > 1500 or img.height > 1500:
            img.thumbnail((1500, 1500))
        img = img.convert("L")
        img = ImageOps.autocontrast(img)
        img = img.filter(ImageFilter.EDGE_ENHANCE_MORE)
        img = ImageEnhance.Contrast(img).enhance(1.5)
        img.save(output_path, "JPEG", quality=95)
    except Exception as e:
        print(f"❌ [ERRO PILLOW] {e}")

def extrair_texto_da_foto(file_path):
    try:
        with open(file_path, "rb") as f:
            r = requests.post(
                "https://api.ocr.space/parse/image",
                files={file_path: f},
                data={"apikey": OCR_API_KEY, "language": "eng", "OCREngine": "2"},
            )
        res = r.json()
        if res.get("ParsedResults"):
            txt = res["ParsedResults"][0]["ParsedText"]
            cand = re.findall(r"\d+[\.,]\d+|\d{3,6}", txt)
            if cand:
                return cand[0].replace(".", ",")
        return "Não lido"
    except:
        return "Erro API"

# ==========================================
# HUD, MENUS E TECLADOS
# ==========================================
def teclado_principal():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(KeyboardButton("🟢 Liguei a Água"), KeyboardButton("🔴 Desliguei a Água"))
    markup.add(KeyboardButton("📸 Leitura Avulsa"), KeyboardButton("⚙️ Configurações"))
    return markup

def teclado_horarios_principal():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("➕ Adicionar", callback_data="add_horario"), InlineKeyboardButton("➖ Remover", callback_data="rem_horario"))
    markup.row(InlineKeyboardButton("🗑️ Limpar Tudo", callback_data="limpar_horarios"), InlineKeyboardButton("❌ Voltar", callback_data="cancelar_acao"))
    return markup

def teclado_continuar_horarios():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("➕ Adicionar Outro", callback_data="add_horario"), InlineKeyboardButton("➖ Remover Outro", callback_data="rem_horario"))
    markup.row(InlineKeyboardButton("❌ Voltar", callback_data="cancelar_acao"))
    return markup

def teclado_cancelar():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_acao"))
    return markup

def teclado_sim_nao():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("✅ Sim, já desliguei!", callback_data="sim_desligado"))
    markup.row(InlineKeyboardButton("❌ Ainda não", callback_data="nao_desligado"))
    return markup

@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, f"Seu CHAT ID é: <code>{message.chat.id}</code>", parse_mode="HTML")
    guia = (
        "💧 <b>GUIA DO SISTEMA DE HIDRÔMETRO</b> 💧\n\n"
        "🚩 <b>COMO USAR:</b>\n"
        "Use os botões no rodapé do Telegram.\n\n"
        "• <b>🟢 Liguei a Água:</b> Clique ao abrir o registro.\n"
        "• <b>🔴 Desliguei a Água:</b> Clique ao fechar o registro.\n\n"
        "📸 <b>REGRA DOS 3 MINUTOS:</b>\n"
        "Após clicar, você tem 3 minutos para mandar a leitura. Se esquecer, o bot avisará a família!"
    )
    bot.send_message(message.chat.id, guia, parse_mode="HTML", reply_markup=teclado_principal())

# ==========================================
# HANDLERS DO TECLADO PRINCIPAL
# ==========================================
@bot.message_handler(func=lambda m: m.text and "Liguei a Água" in m.text)
def botao_liguei(message):
    global estado_bot
    estado_bot = "matinal"
    bot.reply_to(message, "✅ Você ligou a água! 📸 Mande a foto ou digite a leitura AGORA.")
    threading.Thread(target=monitorar_esquecimento, args=("ligar", message.from_user.first_name, message.chat.id)).start()

@bot.message_handler(func=lambda m: m.text and "Desliguei a Água" in m.text)
def botao_desliguei(message):
    global estado_bot, quem_desligou_hoje
    quem_desligou_hoje = message.from_user.first_name
    estado_bot = "noturno"
    bot.reply_to(message, "✅ Você desligou a água! 📸 Mande a leitura para o teste de estanqueidade.")
    threading.Thread(target=monitorar_esquecimento, args=("desligar", quem_desligou_hoje, message.chat.id)).start()

@bot.message_handler(func=lambda m: m.text and "Leitura Avulsa" in m.text)
def botao_avulso(message):
    global estado_bot
    estado_bot = "avulso"
    texto = (
        "📸 <b>Modo de Leitura Avulsa ativado!</b>\n"
        "Você pode mandar uma foto nítida do hidrômetro agora ou digitar a leitura manualmente.\n\n"
        "Se for digitar, separe os números pretos (m³) dos vermelhos (litros) por vírgula. "
        "Ex: se o visor mostra ⚫459 e 🔴123, digite: <code>459,123</code>"
    )
    bot.reply_to(message, texto, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text and "Configurações" in m.text)
def botao_configuracoes(message):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🕒 Configurar Horários", callback_data="menu_horarios"))
    markup.row(InlineKeyboardButton("🗑️ Apagar Últimos Registros", callback_data="menu_apagar"))
    markup.row(InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_acao"))
    texto = "⚙️ <b>Menu de Configurações</b>\n\nEscolha o que deseja fazer:"
    bot.reply_to(message, texto, parse_mode="HTML", reply_markup=markup)

def monitorar_esquecimento(acao, usuario, chat_id):
    time.sleep(180)
    global estado_bot
    if (acao == "ligar" and estado_bot == "matinal") or (acao == "desligar" and estado_bot == "noturno"):
        estado_bot = "ocioso"
        salvar_log(usuario, f"🚨 Esqueceu de anotar a leitura após {acao}ar.")
        for cid in list(CONTATOS_FAMILIA.values()) + [MEU_CHAT_ID]:
            try: bot.send_message(cid, f"⚠️ {usuario} {acao}u a água e esqueceu de mandar a leitura!")
            except: pass

# ==========================================
# CÉREBRO DOS BOTÕES INLINE (CALLBACKS)
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def escutar_botoes_inline(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    nome = call.from_user.first_name

    if call.data == "cancelar_acao":
        bot.clear_step_handler_by_chat_id(chat_id)
        bot.edit_message_text("❌ Ação concluída ou cancelada.", chat_id, msg_id)

    # --- MENU DE APAGAR ---
    elif call.data == "menu_apagar":
        bot.edit_message_text("⏳ Buscando os últimos 5 registros no banco de dados...", chat_id, msg_id)
        try:
            sheet = conectar_planilha("Dados")
            todas_linhas = sheet.get_all_values()
            if len(todas_linhas) <= 1:
                bot.edit_message_text("📭 Nenhum registro encontrado na planilha.", chat_id, msg_id)
                return
            ultimas_linhas = todas_linhas[-5:]
            index_start = len(todas_linhas) - len(ultimas_linhas) + 1
            markup = InlineKeyboardMarkup()
            for i, linha in enumerate(ultimas_linhas):
                real_row = index_start + i
                data, hora, quem, leitura = linha[0], linha[1], linha[2], linha[3]
                markup.row(InlineKeyboardButton(f"{data} {hora} - {leitura} ({quem})", callback_data=f"del_{real_row}"))
            markup.row(InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_acao"))
            bot.edit_message_text("🗑️ <b>Selecione o registro para APAGAR:</b>\n<i>A linha será removida da planilha.</i>", chat_id, msg_id, parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            bot.edit_message_text(f"❌ Erro ao buscar dados: {str(e)}", chat_id, msg_id)

    elif call.data.startswith("del_"):
        row_index = int(call.data.split("_")[1])
        bot.edit_message_text("⏳ Excluindo linha da planilha...", chat_id, msg_id)
        try:
            sheet = conectar_planilha("Dados")
            sheet.delete_rows(row_index)
            bot.edit_message_text("✅ Registro excluído com sucesso!", chat_id, msg_id)
            salvar_log(nome, f"Apagou a linha {row_index} via bot.")
        except Exception as e:
            bot.edit_message_text(f"❌ Erro ao excluir: {str(e)}", chat_id, msg_id)

    # --- MENU DE HORÁRIOS ---
    elif call.data == "menu_horarios":
        lista = "\n".join(f"- {h}" for h in sorted(horarios_noturnos)) if horarios_noturnos else "Nenhum."
        texto = f"🕒 *Horários Atuais:*\n{lista}\n\nO que você quer fazer?"
        bot.edit_message_text(texto, chat_id, msg_id, parse_mode="Markdown", reply_markup=teclado_horarios_principal())

    elif call.data == "add_horario":
        bot.edit_message_text("➕ Digite o horário para adicionar (ex: 18h, 18:30):", chat_id, msg_id, reply_markup=teclado_cancelar())
        bot.register_next_step_handler(call.message, processar_add_horario)

    elif call.data == "rem_horario":
        lista = "\n".join(f"- {h}" for h in sorted(horarios_noturnos)) if horarios_noturnos else "Nenhum."
        bot.edit_message_text(f"➖ Horários:\n{lista}\n\nDigite qual você quer remover:", chat_id, msg_id, reply_markup=teclado_cancelar())
        bot.register_next_step_handler(call.message, processar_rem_horario)

    elif call.data == "limpar_horarios":
        horarios_noturnos.clear()
        aplicar_agendamentos()
        bot.edit_message_text("🗑️ Todos os horários foram removidos.", chat_id, msg_id)

    # --- RESPOSTAS DO LEMBRETE NOTURNO ---
    elif call.data == "sim_desligado":
        global quem_desligou_hoje, estado_bot
        if quem_desligou_hoje:
            bot.answer_callback_query(call.id, f"{quem_desligou_hoje} já confirmou.")
            return
        quem_desligou_hoje = nome
        estado_bot = "noturno"
        bot.edit_message_text(f"✅ Valeu, {nome}! Você desligou a água.\n\n📸 *Mande a foto ou digite a leitura AGORA*.", chat_id, msg_id, parse_mode="Markdown")
        threading.Thread(target=monitorar_esquecimento, args=("desligar", nome, chat_id)).start()

    elif call.data == "nao_desligado":
        bot.edit_message_text("⚠️ Sem problemas! Feche assim que puder.", chat_id, msg_id)

# ==========================================
# FUNÇÕES DE LÓGICA DE HORÁRIOS
# ==========================================
def normalizar_horario(texto):
    padrao = re.match(r'^(\d{1,2})[h:]?(\d{2})?$', texto.strip().lower())
    if padrao:
        h = int(padrao.group(1))
        m = int(padrao.group(2)) if padrao.group(2) else 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    return None

def processar_add_horario(message):
    h = normalizar_horario(message.text)
    if h:
        if h not in horarios_noturnos:
            horarios_noturnos.append(h)
            aplicar_agendamentos()
            bot.send_message(message.chat.id, f"✅ Horário {h} adicionado!\n\nMais alguma ação?", reply_markup=teclado_continuar_horarios())
        else:
            bot.send_message(message.chat.id, "⚠️ Esse horário já está na lista.", reply_markup=teclado_continuar_horarios())
    else:
        bot.send_message(message.chat.id, "❌ Formato inválido.", reply_markup=teclado_continuar_horarios())

def processar_rem_horario(message):
    h = normalizar_horario(message.text)
    if h in horarios_noturnos:
        horarios_noturnos.remove(h)
        aplicar_agendamentos()
        bot.send_message(message.chat.id, f"🗑️ Horário {h} removido.", reply_markup=teclado_continuar_horarios())
    else:
        bot.send_message(message.chat.id, "❌ Horário não encontrado na lista.", reply_markup=teclado_continuar_horarios())

def resetar_status_diario():
    global quem_desligou_hoje
    quem_desligou_hoje = None

def lembrete_noturno():
    global quem_desligou_hoje
    if not quem_desligou_hoje:
        msg = "🌙 O hidrômetro lá fora já foi desligado hoje?"
        for cid in list(CONTATOS_FAMILIA.values()) + [MEU_CHAT_ID]:
            try: bot.send_message(cid, msg, reply_markup=teclado_sim_nao())
            except: pass

def spammer():
    global estado_bot
    estado_bot = "matinal"
    while estado_bot == "matinal":
        try: bot.send_message(MEU_CHAT_ID, "☀️ ACORDA! HIDROMETRO TIMEEE! 💧 Manda foto ou digita a leitura!")
        except: pass
        time.sleep(15)

def aplicar_agendamentos():
    schedule.clear()
    schedule.every().day.at("06:35").do(lambda: threading.Thread(target=spammer).start())
    schedule.every().day.at("18:00").do(resetar_status_diario)
    for h in set(horarios_noturnos):
        schedule.every().day.at(h).do(lambda: threading.Thread(target=lembrete_noturno).start())

def loop_agendamento():
    while True:
        schedule.run_pending()
        time.sleep(1)

# ==========================================
# PROCESSAMENTO DE DADOS (IMAGEM E TEXTO)
# ==========================================
@bot.message_handler(func=lambda m: estado_bot in ["matinal", "noturno", "avulso"] and m.content_type == "text" and m.text not in TEXTOS_BOTOES)
def receber_texto(message):
    numeros = re.findall(r"\d+[\.,]\d+|\d+", message.text)
    if numeros:
        processar_leitura(message, numeros[0])
    else:
        bot.reply_to(message, "🤔 Digite apenas os números da leitura (ex: 459 ou 459,123).")

@bot.message_handler(content_types=["photo"])
def receber_foto(message):
    if estado_bot in ["matinal", "noturno", "avulso"]:
        msg_wait = bot.reply_to(message, "⏳ Processando imagem...")
        file_info = bot.get_file(message.photo[-1].file_id)
        raw_data = bot.download_file(file_info.file_path)
        with open("original.jpg", "wb") as f:
            f.write(raw_data)
        comprimir_imagem("original.jpg", "otimizada.jpg")
        leitura = extrair_texto_da_foto("otimizada.jpg")
        if leitura != "Não lido":
            processar_leitura(message, leitura, msg_wait)
        else:
            bot.edit_message_text("❌ Não li a foto. Digite os números.", message.chat.id, msg_wait.message_id)

def processar_leitura(message, leitura_bruta, msg_wait=None):
    global estado_bot
    est_ant = estado_bot
    estado_bot = "processando"
    if not msg_wait:
        msg_wait = bot.reply_to(message, "⏳ Salvando...")

    val = leitura_bruta.replace(".", ",")

    if est_ant == "noturno":
        salvar_log(message.from_user.first_name, f"Desligou. Marcador: {val}")
        with open("leitura_noturna.txt", "w") as f:
            f.write(val)
        bot.edit_message_text(f"✅ Leitura Noturna ({val}) salva!", message.chat.id, msg_wait.message_id)
    elif est_ant == "matinal" or est_ant == "avulso":
        try:
            if salvar_na_planilha(message.from_user.first_name, val):
                tipo = "Matinal" if est_ant == "matinal" else "Avulsa"
                salvar_log(message.from_user.first_name, f"{tipo}. Marcador: {val}")
                bot.edit_message_text(f"✅ Leitura {tipo} ({val}) salva!", message.chat.id, msg_wait.message_id)
        except Exception as e:
            bot.edit_message_text(f"❌ Erro Técnico: {str(e)}", message.chat.id, msg_wait.message_id)
    estado_bot = "ocioso"

# ==========================================
# INICIALIZAÇÃO MAIN
# ==========================================
if __name__ == "__main__":
    aplicar_agendamentos()
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=loop_agendamento, daemon=True).start()
    print("🚀 Bot iniciado com sucesso!")
    bot.infinity_polling()
