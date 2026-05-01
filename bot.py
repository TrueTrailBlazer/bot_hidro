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
SENHA_EXCLUSAO = os.getenv("SENHA_EXCLUSAO", "1234")

CONTATOS_FAMILIA = {
    "Mãe": os.getenv("ID_MAE"),
    "Luan": os.getenv("ID_LUAN"),
    "Lara": os.getenv("ID_LARA"),
}

# --- INICIALIZAÇÃO DO BOT E FLASK ---
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
ultima_linha_inserida = None
linha_para_deletar = None
horarios_noturnos = ["19:00", "21:00", "23:00"]
TEXTOS_BOTOES = ["🟢 Liguei a Água", "🔴 Desliguei a Água", "📸 Leitura Avulsa", "⚙️ Configurações"]

# ==========================================
# CONEXÃO E MANIPULAÇÃO DA PLANILHA
# ==========================================
def conectar_planilha(aba):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    caminhos_tentar = ["credentials.json", "/etc/secrets/credentials.json"]
    path_final = next((p for p in caminhos_tentar if os.path.exists(p)), None)

    if not path_final:
        raise FileNotFoundError("Arquivo credentials.json não encontrado.")

    creds = ServiceAccountCredentials.from_json_keyfile_name(path_final, scope)
    client = gspread.authorize(creds)
    return client.open(NOME_PLANILHA).worksheet(aba)

def salvar_na_planilha(quem, leitura):
    sheet = conectar_planilha("Dados")
    data_atual = datetime.now().strftime("%d/%m/%Y")
    hora_atual = datetime.now().strftime("%H:%M:%S")
    
    total_linhas = len(sheet.col_values(1))
    nova_linha = total_linhas + 1
    
    sheet.append_row([data_atual, hora_atual, quem, leitura], table_range="A:D")
    return nova_linha

def deletar_intervalo_safe(row_index):
    """Apaga apenas as colunas A-D e desloca as células para cima, protegendo o painel lateral."""
    sheet = conectar_planilha("Dados")
    
    body = {
        "requests": [
            {
                "deleteRange": {
                    "range": {
                        "sheetId": sheet.id,
                        "startRowIndex": row_index - 1,
                        "endRowIndex": row_index,
                        "startColumnIndex": 0,
                        "endColumnIndex": 4  # Colunas A a D (0, 1, 2, 3)
                    },
                    "shiftDimension": "ROWS"
                }
            }
        ]
    }
    sheet.spreadsheet.batch_update(body)

def salvar_log(quem, acao):
    try:
        sheet_logs = conectar_planilha("Logs")
        data_atual = datetime.now().strftime("%d/%m/%Y")
        hora_atual = datetime.now().strftime("%H:%M:%S")
        sheet_logs.append_row([data_atual, hora_atual, quem, acao], table_range="A:D")
        return True
    except Exception as e:
        print(f"❌ [ERRO LOGS] {e}")
        return False

# ==========================================
# PROCESSAMENTO DE IMAGEM E OCR
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
            r = requests.post("https://api.ocr.space/parse/image", files={file_path: f}, 
                              data={"apikey": OCR_API_KEY, "language": "eng", "OCREngine": "2"})
        res = r.json()
        if res.get('ParsedResults'):
            txt = res['ParsedResults'][0]['ParsedText']
            cand = re.findall(r"\d+[\.,]\s*\d+|\d{3,6}", txt)
            if cand:
                return cand[0].replace(".", ",").replace(" ", "")
        return "Não lido"
    except: return "Erro API"

# ==========================================
# INTERFACE E TECLADOS
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

@bot.message_handler(commands=["start"])
def start(message):
    lista = ", ".join(sorted(horarios_noturnos)) if horarios_noturnos else "Nenhum"
    guia = (
        "💧 <b>SISTEMA HIDRÔMETRO</b> 💧\n\n"
        "• <b>🟢 Liguei / 🔴 Desliguei:</b> Rotinas diárias.\n"
        "• <b>📸 Leitura Avulsa:</b> Registro voluntário.\n"
        f"🌙 <b>Alarmes Noturnos:</b> {lista}\n\n"
        "<i>Use o botão 'Corrigir' após salvar se houver erro de leitura.</i>"
    )
    bot.send_message(message.chat.id, guia, parse_mode="HTML", reply_markup=teclado_principal())

# ==========================================
# LÓGICA DE EXCLUSÃO PROTEGIDA E HANDLERS
# ==========================================
@bot.message_handler(func=lambda m: estado_bot == "aguardando_senha_del" and m.content_type == "text")
def receber_senha_exclusao(message):
    global estado_bot, linha_para_deletar
    if message.text.strip() == SENHA_EXCLUSAO:
        bot.reply_to(message, "⏳ Senha validada. Removendo apenas os dados da tabela...")
        try:
            deletar_intervalo_safe(linha_para_deletar)
            bot.reply_to(message, "✅ Dados removidos. O painel lateral foi preservado.")
            salvar_log(message.from_user.first_name, f"Excluiu dados da linha {linha_para_deletar} (Safe Delete).")
        except Exception as e:
            bot.reply_to(message, f"❌ Erro ao processar exclusão: {str(e)}")
    else:
        bot.reply_to(message, "❌ Senha incorreta. Operação cancelada.")
    estado_bot = "ocioso"
    linha_para_deletar = None

@bot.message_handler(func=lambda m: m.text and "Configurações" in m.text)
def botao_configuracoes(message):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🕒 Configurar Horários", callback_data="menu_horarios"))
    markup.row(InlineKeyboardButton("🗑️ Apagar Últimos Registros", callback_data="menu_apagar"))
    markup.row(InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_acao"))
    bot.reply_to(message, "⚙️ <b>Configurações</b>", parse_mode="HTML", reply_markup=markup)

# ==========================================
# CALLBACKS INLINE (APAGAR, EDITAR, HORARIOS)
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def escutar_callbacks(call):
    chat_id, msg_id, nome = call.message.chat.id, call.message.message_id, call.from_user.first_name

    if call.data == "cancelar_acao":
        bot.edit_message_text("❌ Ação encerrada.", chat_id, msg_id)
        
    elif call.data == "editar_leitura":
        global estado_bot
        estado_bot = "editando"
        bot.edit_message_text("✏️ <b>Modo de Correção</b>\nEnvie o novo valor para sobrescrever a última linha:", chat_id, msg_id, parse_mode="HTML")

    elif call.data == "menu_apagar":
        bot.edit_message_text("⏳ Consultando banco de dados...", chat_id, msg_id)
        try:
            sheet = conectar_planilha("Dados")
            linhas = sheet.get_all_values()
            if len(linhas) <= 1:
                bot.edit_message_text("📭 Sem registros.", chat_id, msg_id)
                return
            ultimas = linhas[-5:]
            start_idx = len(linhas) - len(ultimas) + 1
            markup = InlineKeyboardMarkup()
            for i, L in enumerate(ultimas):
                markup.row(InlineKeyboardButton(f"{L[0]} {L[1]} - {L[3]}", callback_data=f"del_{start_idx+i}"))
            markup.row(InlineKeyboardButton("❌ Voltar", callback_data="cancelar_acao"))
            bot.edit_message_text("🗑️ <b>Selecione para EXCLUIR:</b>", chat_id, msg_id, parse_mode="HTML", reply_markup=markup)
        except Exception as e: bot.edit_message_text(f"❌ Erro: {e}", chat_id, msg_id)

    elif call.data.startswith("del_"):
        global linha_para_deletar, estado_bot
        linha_para_deletar = int(call.data.split("_")[1])
        estado_bot = "aguardando_senha_del"
        bot.edit_message_text("🔒 <b>Ação Restrita</b>\nDigite a Palavra-Chave para confirmar a exclusão parcial da tabela:", chat_id, msg_id, parse_mode="HTML")

    elif call.data == "menu_horarios":
        lista = "\n".join(f"- {h}" for h in sorted(horarios_noturnos)) if horarios_noturnos else "Nenhum."
        bot.edit_message_text(f"🕒 <b>Horários Atuais</b>\n{lista}", chat_id, msg_id, parse_mode="HTML", reply_markup=teclado_horarios_principal())

# ==========================================
# PROCESSAMENTO E AGENDAMENTOS
# ==========================================
@bot.message_handler(func=lambda m: estado_bot in ["matinal", "noturno", "avulso", "editando"] and m.content_type == "text" and m.text not in TEXTOS_BOTOES)
def receber_texto(message):
    nums = re.findall(r"\d+[\.,]\s*\d+|\d+", message.text)
    if nums: processar_leitura(message, nums[0])
    else: bot.reply_to(message, "🤔 Envie os números (Ex: 459 ou 459,123)")

@bot.message_handler(content_types=["photo"])
def receber_foto(message):
    if estado_bot in ["matinal", "noturno", "avulso", "editando"]:
        msg = bot.reply_to(message, "⏳ Lendo imagem...")
        file_info = bot.get_file(message.photo[-1].file_id)
        with open("original.jpg", "wb") as f: f.write(bot.download_file(file_info.file_path))
        comprimir_imagem("original.jpg", "otimizada.jpg")
        leitura = extrair_texto_da_foto("otimizada.jpg")
        if leitura != "Não lido": processar_leitura(message, leitura, msg)
        else: bot.edit_message_text("❌ Falha no OCR. Digite os números.", message.chat.id, msg.message_id)

def processar_leitura(message, bruta, msg_wait=None):
    global estado_bot, ultima_linha_inserida
    ant = estado_bot
    estado_bot = "processando"
    if not msg_wait: msg_wait = bot.reply_to(message, "⏳ Gravando...")

    val = re.sub(r'\s+', '', bruta).replace(".", ",")
    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("✏️ Corrigir", callback_data="editar_leitura"))

    try:
        if ant == "editando":
            if ultima_linha_inserida:
                sheet = conectar_planilha("Dados")
                sheet.update_cell(ultima_linha_inserida, 4, val)
                bot.edit_message_text(f"✅ Corrigido para {val}!", message.chat.id, msg_wait.message_id)
                ultima_linha_inserida = None
            else:
                with open("leitura_noturna.txt", "w") as f: f.write(val)
                bot.edit_message_text(f"✅ Noturna atualizada para {val}!", message.chat.id, msg_wait.message_id)
        elif ant == "noturno":
            with open("leitura_noturna.txt", "w") as f: f.write(val)
            bot.edit_message_text(f"✅ Noturna ({val}) salva!", message.chat.id, msg_wait.message_id, reply_markup=markup)
        else:
            linha = salvar_na_planilha(message.from_user.first_name, val)
            ultima_linha_inserida = linha
            bot.edit_message_text(f"✅ Salvo: {val}!", message.chat.id, msg_wait.message_id, reply_markup=markup)
    except Exception as e: bot.edit_message_text(f"❌ Erro: {e}", message.chat.id, msg_wait.message_id)
    estado_bot = "ocioso"

# --- AGENDAMENTOS ---
def aplicar_agendamentos():
    schedule.clear()
    for h in set(horarios_noturnos):
        schedule.every().day.at(h).do(lambda: threading.Thread(target=lambda: [bot.send_message(c, "🌙 Já desligou a água?", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Sim", callback_data="sim_desligado"), InlineKeyboardButton("❌ Não", callback_data="nao_desligado"))) for c in list(CONTATOS_FAMILIA.values()) + [MEU_CHAT_ID]]).start())

if __name__ == "__main__":
    aplicar_agendamentos()
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=lambda: [schedule.run_pending() or time.sleep(1) for _ in iter(int, 1)], daemon=True).start()
    bot.infinity_polling()
