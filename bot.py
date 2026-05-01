import os
import re
import threading
import time
from datetime import datetime

import gspread
import requests
import telebot
from dotenv import load_dotenv
from flask import Flask
from oauth2client.service_account import ServiceAccountCredentials
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from telebot import apihelper
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# --- CONFIGURAÇÃO DE AMBIENTE ---
load_dotenv()
os.environ["TZ"] = "America/Grande_Grande"
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
    "🕒 Configurar Horários",
]


# --- CONEXÃO E SALVAMENTO (GOOGLE SHEETS) ---
def conectar_planilha(aba):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    client = gspread.authorize(creds)
    return client.open(NOME_PLANILHA).worksheet(aba)


def salvar_na_planilha(quem, leitura):
    try:
        sheet = conectar_planilha("Dados")
        data_atual = datetime.now().strftime("%d/%m/%Y")
        hora_atual = datetime.now().strftime("%H:%M:%S")
        sheet.append_row([data_atual, hora_atual, quem, leitura], table_range="A:D")
        return True
    except Exception as e:
        print(f"❌ [ERRO PLANILHA DADOS] {e}")
        return False


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


# --- TRATAMENTO DE IMAGEM E OCR ---
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
            # Preserva vírgula ou ponto para separar pretos de vermelhos
            txt = res["ParsedResults"][0]["ParsedText"]
            # Procura por números que podem ter vírgula ou ponto (ex: 459,123)
            # Aceita inteiros de 3 a 6 dígitos ou números com separador decimal
            cand = re.findall(r"\d+[\.,]\d+|\d{3,6}", txt)
            if cand:
                return cand[0].replace(".", ",")
        return "Não lido"
    except:
        return "Erro API"


# --- HUD E MENUS ---
def teclado_principal():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(KeyboardButton("🟢 Liguei a Água"), KeyboardButton("🔴 Desliguei a Água"))
    markup.add(
        KeyboardButton("📸 Leitura Avulsa"), KeyboardButton("🕒 Configurar Horários")
    )
    return markup


@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(
        message.chat.id,
        f"Seu CHAT ID é: <code>{message.chat.id}</code>",
        parse_mode="HTML",
    )
    guia = (
        "💧 <b>GUIA DO SISTEMA DE HIDRÔMETRO</b> 💧\n\n"
        "🚩 <b>COMO USAR:</b>\n"
        "Use os botões no rodapé do Telegram.\n\n"
        "• <b>🟢 Liguei a Água:</b> Clique ao abrir o registro.\n"
        "• <b>🔴 Desliguei a Água:</b> Clique ao fechar o registro.\n\n"
        "📸 <b>REGRA DOS 3 MINUTOS:</b>\n"
        "Após clicar, você tem 3 minutos para mandar a leitura. Se esquecer, o bot avisará a família!"
    )
    bot.send_message(
        message.chat.id, guia, parse_mode="HTML", reply_markup=teclado_principal()
    )


# --- HANDLERS DE AÇÃO ---
@bot.message_handler(func=lambda m: m.text and "Liguei a Água" in m.text)
def botao_liguei(message):
    global estado_bot
    estado_bot = "matinal"
    bot.reply_to(
        message, "✅ Você ligou a água! 📸 Mande a foto ou digite a leitura AGORA."
    )
    threading.Thread(
        target=monitorar_esquecimento,
        args=("ligar", message.from_user.first_name, message.chat.id),
    ).start()


@bot.message_handler(func=lambda m: m.text and "Desliguei a Água" in m.text)
def botao_desliguei(message):
    global estado_bot, quem_desligou_hoje
    quem_desligou_hoje = message.from_user.first_name
    estado_bot = "noturno"
    bot.reply_to(
        message,
        "✅ Você desligou a água! 📸 Mande a leitura para o teste de estanqueidade.",
    )
    threading.Thread(
        target=monitorar_esquecimento,
        args=("desligar", quem_desligou_hoje, message.chat.id),
    ).start()


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


def monitorar_esquecimento(acao, usuario, chat_id):
    time.sleep(180)
    global estado_bot
    if (acao == "ligar" and estado_bot == "matinal") or (
        acao == "desligar" and estado_bot == "noturno"
    ):
        estado_bot = "ocioso"
        salvar_log(usuario, f"🚨 Esqueceu de anotar a leitura após {acao}ar.")
        for cid in list(CONTATOS_FAMILIA.values()) + [MEU_CHAT_ID]:
            try:
                bot.send_message(
                    cid, f"⚠️ {usuario} {acao}u a água e esqueceu de mandar a leitura!"
                )
            except:
                pass


# --- PROCESSAMENTO DE DADOS ---
@bot.message_handler(
    func=lambda m: (
        estado_bot in ["matinal", "noturno", "avulso"]
        and m.content_type == "text"
        and m.text not in TEXTOS_BOTOES
    )
)
def receber_texto(message):
    # Regex para aceitar números inteiros ou com separador decimal (vírgula ou ponto)
    numeros = re.findall(r"\d+[\.,]\d+|\d+", message.text)
    if numeros:
        processar_leitura(message, numeros[0])
    else:
        bot.reply_to(
            message, "🤔 Digite apenas os números da leitura (ex: 459 ou 459,123)."
        )


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
            bot.edit_message_text(
                "❌ Não li a foto. Digite os números.",
                message.chat.id,
                msg_wait.message_id,
            )


def processar_leitura(message, leitura_bruta, msg_wait=None):
    global estado_bot
    est_ant = estado_bot
    estado_bot = "processando"
    if not msg_wait:
        msg_wait = bot.reply_to(message, "⏳ Salvando...")

    # Normaliza para usar vírgula como separador
    val = leitura_bruta.replace(".", ",")

    if est_ant == "noturno":
        salvar_log(message.from_user.first_name, f"Desligou. Marcador: {val}")
        with open("leitura_noturna.txt", "w") as f:
            f.write(val)
        bot.edit_message_text(
            f"✅ Leitura Noturna ({val}) salva!", message.chat.id, msg_wait.message_id
        )
    elif est_ant == "matinal" or est_ant == "avulso":
        if salvar_na_planilha(message.from_user.first_name, val):
            tipo = "Matinal" if est_ant == "matinal" else "Avulsa"
            salvar_log(message.from_user.first_name, f"{tipo}. Marcador: {val}")
            bot.edit_message_text(
                f"✅ Leitura {tipo} ({val}) salva!",
                message.chat.id,
                msg_wait.message_id,
            )
    estado_bot = "ocioso"


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    print("🚀 Bot iniciado!")
    bot.infinity_polling()
