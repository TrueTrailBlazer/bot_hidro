import os
import re
import threading
import time
import uuid
from datetime import datetime

import gspread
import requests
import telebot
import schedule
from dotenv import load_dotenv
from flask import Flask
from google.oauth2.service_account import Credentials
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
os.environ["TZ"] = "America/Campo_Grande"
if hasattr(time, "tzset"):
    time.tzset()

# --- VARIÁVEIS SENSÍVEIS (Vindas do .env) ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")
MEU_CHAT_ID = os.getenv("MEU_CHAT_ID")
NOME_PLANILHA = os.getenv("NOME_PLANILHA", "Monitoramento")

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

def carregar_horarios():
    if os.path.exists("horarios.txt"):
        with open("horarios.txt", "r") as f:
            return f.read().strip().split(",")
    return ["19:00", "21:00", "23:00"]

horarios_noturnos = carregar_horarios()

def salvar_e_recarregar_horarios():
    global horarios_noturnos
    with open("horarios.txt", "w") as f:
        f.write(",".join(horarios_noturnos))
    schedule.clear()
    for hora in horarios_noturnos:
        schedule.every().day.at(hora).do(verificar_e_avisar_noturno)

def gerar_teclado_horarios():
    markup = InlineKeyboardMarkup(row_width=2)
    botoes = []
    for hora in horarios_noturnos:
        botoes.append(InlineKeyboardButton(hora, callback_data=f"sel_hora_{hora}"))
    if botoes:
        markup.add(*botoes)
    markup.add(InlineKeyboardButton("➕ Adicionar Novo", callback_data="add_hora"))
    markup.add(InlineKeyboardButton("❌ Fechar Painel", callback_data="fechar_painel"))
    return markup
TEXTOS_BOTOES = [
    "🟢 Liguei a Água",
    "🔴 Desliguei a Água",
    "📸 Leitura Avulsa",
    "🕒 Configurar Horários",
]

# Variáveis de controle diário e tokens de concorrência
controle_diario = {"data": None, "ligar": None, "hora_ligar": None, "desligar": None, "hora_desligar": None}
token_acao = 0


# --- CONEXÃO E SALVAMENTO (GOOGLE SHEETS) ---
def conectar_planilha(aba):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]

    # Tenta achar o arquivo em locais comuns do Render ou local
    caminhos_tentar = ["credentials.json", "/etc/secrets/credentials.json"]
    path_final = None

    for p in caminhos_tentar:
        if os.path.exists(p):
            path_final = p
            break

    if not path_final:
        raise FileNotFoundError(
            "Arquivo credentials.json não encontrado em nenhum local conhecido."
        )

    creds = Credentials.from_service_account_file(path_final, scopes=scope)
    client = gspread.authorize(creds)
    try:
        planilha = client.open(NOME_PLANILHA)
        return planilha.worksheet(aba)
    except gspread.exceptions.SpreadsheetNotFound:
        raise Exception(f"A planilha '{NOME_PLANILHA}' não foi encontrada ou o e-mail do bot (service account) não tem permissão de Editor nela.")
    except gspread.exceptions.WorksheetNotFound:
        raise Exception(f"A aba '{aba}' não foi encontrada dentro da planilha.")


def salvar_na_planilha(quem, leitura):
    sheet = conectar_planilha("Dados")
    data_atual = datetime.now().strftime("%d/%m/%Y")
    hora_atual = datetime.now().strftime("%H:%M:%S")
    
    # Tenta converter para float para que o gspread envie como número JSON
    try:
        if isinstance(leitura, str):
            leitura = float(leitura.replace(",", "."))
    except ValueError:
        pass
        
    sheet.append_row([data_atual, hora_atual, quem, leitura], table_range="A:D", value_input_option="USER_ENTERED")
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
                timeout=15
            )
        res = r.json()
        if res.get("ParsedResults"):
            # Preserva vírgula ou ponto para separar pretos de vermelhos
            txt = res["ParsedResults"][0]["ParsedText"]
            # Limpa espaços acidentais após vírgula/ponto: "123. 45" -> "123,45"
            txt = re.sub(r'(\d)[\.,]\s+(\d)', r'\1,\2', txt)
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


@bot.message_handler(commands=["cancelar"], is_authorized=True)
def comando_cancelar(message):
    global estado_bot
    estado_bot = "ocioso"
    bot.send_message(message.chat.id, "❌ Ação cancelada.", reply_markup=teclado_principal())

@bot.message_handler(commands=["start"], is_authorized=True)
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
@bot.message_handler(func=lambda m: m.text and "Liguei a Água" in m.text, is_authorized=True)
def botao_liguei(message):
    global controle_diario
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    
    if controle_diario["data"] != data_hoje:
        controle_diario = {"data": data_hoje, "ligar": None, "hora_ligar": None, "desligar": None, "hora_desligar": None}

    if controle_diario["ligar"]:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("✅ Sim, liguei novamente", callback_data="confirmar_ligar"),
            InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_acao")
        )
        bot.reply_to(message, f"⚠️ A água já foi ligada hoje por {controle_diario['ligar']} às {controle_diario['hora_ligar']}!\nTem certeza que deseja registrar outra ligação?", reply_markup=markup)
        return

    executar_ligar(message.chat.id, message.from_user.first_name)

def executar_ligar(chat_id, user_first_name):
    global estado_bot, controle_diario, token_acao
    controle_diario["ligar"] = user_first_name
    controle_diario["hora_ligar"] = datetime.now().strftime("%H:%M")
    estado_bot = "matinal"
    token_acao += 1
    token_atual = token_acao
    
    salvar_log(user_first_name, "Ligou a água")
    bot.send_message(
        chat_id, "✅ Você ligou a água! 📸 Mande a foto ou digite a leitura AGORA."
    )
    threading.Thread(
        target=monitorar_esquecimento,
        args=("ligar", user_first_name, chat_id, token_atual),
    ).start()


@bot.message_handler(func=lambda m: m.text and "Desliguei a Água" in m.text, is_authorized=True)
def botao_desliguei(message):
    global controle_diario
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    
    if controle_diario["data"] != data_hoje:
        controle_diario = {"data": data_hoje, "ligar": None, "hora_ligar": None, "desligar": None, "hora_desligar": None}

    if controle_diario["desligar"]:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("✅ Sim, desliguei novamente", callback_data="confirmar_desligar"),
            InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_acao")
        )
        bot.reply_to(message, f"⚠️ A água já foi desligada hoje por {controle_diario['desligar']} às {controle_diario['hora_desligar']}!\nTem certeza que deseja registrar outro desligamento?", reply_markup=markup)
        return

    executar_desligar(message.chat.id, message.from_user.first_name)

def executar_desligar(chat_id, user_first_name):
    global estado_bot, quem_desligou_hoje, controle_diario, token_acao
    quem_desligou_hoje = user_first_name
    controle_diario["desligar"] = user_first_name
    controle_diario["hora_desligar"] = datetime.now().strftime("%H:%M")
    estado_bot = "noturno"
    token_acao += 1
    token_atual = token_acao
    
    salvar_log(user_first_name, "Desligou a água")
    bot.send_message(
        chat_id,
        "✅ Você desligou a água! 📸 Mande a leitura para o teste de estanqueidade.",
    )
    threading.Thread(
        target=monitorar_esquecimento,
        args=("desligar", user_first_name, chat_id, token_atual),
    ).start()


@bot.message_handler(func=lambda m: m.text and "Leitura Avulsa" in m.text, is_authorized=True)
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


@bot.message_handler(func=lambda m: m.text and "Configurar Horários" in m.text, is_authorized=True)
def botao_configurar(message):
    global estado_bot
    estado_bot = "ocioso"
    texto = "🕒 <b>Painel de Horários de Aviso</b>\n\nSelecione um horário para gerenciar ou adicione um novo:"
    bot.send_message(message.chat.id, texto, parse_mode="HTML", reply_markup=gerar_teclado_horarios())


def monitorar_esquecimento(acao, usuario, chat_id, token_recebido):
    time.sleep(180)
    global estado_bot, token_acao
    if token_recebido != token_acao:
        return # Ação obsoleta
        
    if (acao == "ligar" and estado_bot == "matinal") or (
        acao == "desligar" and estado_bot == "noturno"
    ):
        estado_bot = "ocioso"
        salvar_log(usuario, f"🚨 Esqueceu de anotar a leitura após {acao}.")
        verbo = "ligou" if acao == "ligar" else "desligou"
        for cid in list(CONTATOS_FAMILIA.values()) + [MEU_CHAT_ID]:
            try:
                bot.send_message(
                    cid, f"⚠️ {usuario} {verbo} a água e esqueceu de mandar a leitura!"
                )
            except:
                pass


# --- PROCESSAMENTO DE DADOS ---
@bot.message_handler(
    func=lambda m: (
        (estado_bot in ["matinal", "noturno", "avulso", "editando", "add_horario_wait"] or str(estado_bot).startswith("edit_horario_wait_"))
        and m.content_type == "text"
        and m.text not in TEXTOS_BOTOES
    ),
    is_authorized=True
)
def receber_texto(message):
    global estado_bot, horarios_noturnos
    
    if estado_bot == "add_horario_wait":
        match = re.findall(r"\d{1,2}:\d{2}", message.text)
        if match:
            novo = match[0]
            if novo not in horarios_noturnos:
                horarios_noturnos.append(novo)
                horarios_noturnos.sort()
                salvar_e_recarregar_horarios()
                bot.reply_to(message, f"✅ Horário {novo} adicionado!", reply_markup=gerar_teclado_horarios())
            else:
                bot.reply_to(message, "❌ Esse horário já existe.")
            estado_bot = "ocioso"
        else:
            bot.reply_to(message, "❌ Formato inválido. Tente novamente usando HH:MM.")
        return
        
    if str(estado_bot).startswith("edit_horario_wait_"):
        velho = estado_bot.split("_")[3]
        match = re.findall(r"\d{1,2}:\d{2}", message.text)
        if match:
            novo = match[0]
            if velho in horarios_noturnos:
                idx = horarios_noturnos.index(velho)
                horarios_noturnos[idx] = novo
                horarios_noturnos.sort()
                salvar_e_recarregar_horarios()
                bot.reply_to(message, f"✅ Horário alterado de {velho} para {novo}!", reply_markup=gerar_teclado_horarios())
            estado_bot = "ocioso"
        else:
            bot.reply_to(message, "❌ Formato inválido. Tente novamente usando HH:MM.")
        return

    # Trata erros comuns de digitação com espaços: "123. 45" ou "123, 45" vira "123,45"
    texto_limpo = re.sub(r'(\d)[\.,]\s+(\d)', r'\1,\2', message.text)
    # Regex para aceitar números inteiros ou com separador decimal (vírgula ou ponto)
    numeros = re.findall(r"\d+[\.,]\d+|\d+", texto_limpo)
    if numeros:
        processar_leitura(message, numeros[0])
    else:
        bot.reply_to(
            message, "🤔 Digite apenas os números da leitura (ex: 459 ou 459,123)."
        )


@bot.message_handler(content_types=["photo"], is_authorized=True)
def receber_foto(message):
    if estado_bot in ["matinal", "noturno", "avulso", "editando"]:
        msg_wait = bot.reply_to(message, "⏳ Processando imagem...")
        file_info = bot.get_file(message.photo[-1].file_id)
        raw_data = bot.download_file(file_info.file_path)
        
        # Cria nomes de arquivo únicos para evitar colisão entre usuários
        token_img = str(uuid.uuid4())
        path_orig = f"orig_{token_img}.jpg"
        path_otim = f"otim_{token_img}.jpg"
        
        with open(path_orig, "wb") as f:
            f.write(raw_data)
        comprimir_imagem(path_orig, path_otim)
        leitura = extrair_texto_da_foto(path_otim)
        
        # Limpa os arquivos temporários
        try:
            os.remove(path_orig)
            os.remove(path_otim)
        except:
            pass
            
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

    # Normaliza para usar vírgula como separador visual
    val = leitura_bruta.replace(".", ",")
    
    # Prepara o teclado para edição e exclusão
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✏️ Editar", callback_data="editar_ultima"),
        InlineKeyboardButton("❌ Apagar", callback_data="apagar_ultima")
    )

    # Função auxiliar para editar a mensagem final incluindo o nome e os botões
    def msg_sucesso(texto_base):
        bot.edit_message_text(
            f"{texto_base} salva por {message.from_user.first_name}!",
            message.chat.id,
            msg_wait.message_id,
            reply_markup=markup
        )

    if est_ant == "editando":
        try:
            sheet = conectar_planilha("Dados")
            linhas = sheet.col_values(1)
            ultima_linha = len(linhas)
            if ultima_linha > 1:
                val_float = float(val.replace(",", "."))
                sheet.update_cell(ultima_linha, 4, val_float)
                salvar_log(message.from_user.first_name, f"Editou. Novo Marcador: {val}")
                bot.edit_message_text(
                    f"✅ Leitura editada com sucesso para {val}!", 
                    message.chat.id, 
                    msg_wait.message_id, 
                    reply_markup=markup
                )
            else:
                bot.edit_message_text("❌ Não há dados para editar.", message.chat.id, msg_wait.message_id)
        except Exception as e:
            bot.edit_message_text(f"❌ Erro Técnico ao editar: {str(e)}", message.chat.id, msg_wait.message_id)
    elif est_ant == "noturno":
        salvar_log(message.from_user.first_name, f"Desligou. Marcador: {val}")
        with open("leitura_noturna.txt", "w") as f:
            f.write(val)
        try:
            salvar_na_planilha(message.from_user.first_name, val)
        except:
            pass
        msg_sucesso(f"✅ Leitura Noturna ({val})")
    elif est_ant == "matinal" or est_ant == "avulso":
        try:
            if salvar_na_planilha(message.from_user.first_name, val):
                tipo = "Matinal" if est_ant == "matinal" else "Avulsa"
                salvar_log(message.from_user.first_name, f"{tipo}. Marcador: {val}")
                msg_sucesso(f"✅ Leitura {tipo} ({val})")
        except Exception as e:
            bot.edit_message_text(
                f"❌ Erro Técnico: {str(e)}",
                message.chat.id,
                msg_wait.message_id,
            )
    estado_bot = "ocioso"

@bot.callback_query_handler(func=lambda call: True, is_authorized=True)
def callback_geral(call):
    global estado_bot, horarios_noturnos
    try:
        if call.data == "confirmar_ligar":
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            executar_ligar(call.message.chat.id, call.from_user.first_name)
            return
        elif call.data == "confirmar_desligar":
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            executar_desligar(call.message.chat.id, call.from_user.first_name)
            return
        elif call.data == "cancelar_acao":
            bot.edit_message_text("❌ Ação cancelada.", call.message.chat.id, call.message.message_id)
            return

        # --- Lógica Antiga: Leituras ---
        if call.data == "apagar_ultima":
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            sheet = conectar_planilha("Dados")
            linhas = sheet.col_values(1)
            ultima_linha = len(linhas)
            if ultima_linha > 1:
                sheet.batch_clear([f"A{ultima_linha}:D{ultima_linha}"])
                bot.edit_message_text("🗑️ Última leitura apagada da planilha!", call.message.chat.id, call.message.message_id)
                salvar_log(call.from_user.first_name, "Apagou a última leitura")
            else:
                bot.answer_callback_query(call.id, "Nenhuma leitura encontrada para apagar.")
        elif call.data == "editar_ultima":
            estado_bot = "editando"
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            bot.send_message(call.message.chat.id, "✏️ <b>Modo de Edição</b>\nDigite a leitura correta ou envie a foto corrigida (ou digite /cancelar para sair):", parse_mode="HTML")
            bot.answer_callback_query(call.id, "Aguardando nova leitura...")
            
        # --- Lógica Nova: Painel de Horários ---
        elif call.data == "fechar_painel":
            bot.delete_message(call.message.chat.id, call.message.message_id)
            
        elif call.data.startswith("sel_hora_"):
            hora = call.data.split("_")[2]
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("✏️ Editar", callback_data=f"edit_hora_{hora}"),
                InlineKeyboardButton("❌ Apagar", callback_data=f"del_hora_{hora}")
            )
            markup.add(InlineKeyboardButton("🔙 Voltar", callback_data="voltar_painel"))
            bot.edit_message_text(f"🕒 Gerenciando horário: <b>{hora}</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
            
        elif call.data == "voltar_painel":
            bot.edit_message_text(
                "🕒 <b>Painel de Horários de Aviso</b>\n\nSelecione um horário para gerenciar ou adicione um novo:", 
                call.message.chat.id, 
                call.message.message_id, 
                parse_mode="HTML", 
                reply_markup=gerar_teclado_horarios()
            )
            
        elif call.data.startswith("del_hora_"):
            hora = call.data.split("_")[2]
            if hora in horarios_noturnos:
                horarios_noturnos.remove(hora)
                salvar_e_recarregar_horarios()
                bot.answer_callback_query(call.id, f"Horário {hora} apagado!")
                bot.edit_message_text(
                    "🕒 <b>Painel de Horários de Aviso</b>\n\nSelecione um horário para gerenciar ou adicione um novo:", 
                    call.message.chat.id, 
                    call.message.message_id, 
                    parse_mode="HTML", 
                    reply_markup=gerar_teclado_horarios()
                )
                
        elif call.data == "add_hora":
            estado_bot = "add_horario_wait"
            bot.edit_message_text("➕ Digite o novo horário no formato HH:MM (ex: 18:30) ou envie /cancelar para desistir:", call.message.chat.id, call.message.message_id)
            
        elif call.data.startswith("edit_hora_"):
            hora = call.data.split("_")[2]
            estado_bot = f"edit_horario_wait_{hora}"
            bot.edit_message_text(f"✏️ Digite o novo valor para o horário {hora} (ex: 19:30) ou envie /cancelar para desistir:", call.message.chat.id, call.message.message_id)
            
    except Exception as e:
        bot.answer_callback_query(call.id, f"Erro: {str(e)}")


# --- AGENDAMENTO DE LEMBRETES (Sincronizado com controle_diario) ---
def verificar_e_avisar_noturno():
    global controle_diario
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    
    # Se ainda não desligaram hoje (ou a data virou e não resetou)
    if controle_diario["data"] != data_hoje or not controle_diario["desligar"]:
        texto_aviso = "📢 <b>Lembrete Noturno:</b> Alguém já desligou a água? Se sim, não esqueça de registrar no botão 🔴!"
        for cid in list(CONTATOS_FAMILIA.values()) + [MEU_CHAT_ID]:
            try:
                if cid:
                    bot.send_message(cid, texto_aviso, parse_mode="HTML")
            except:
                pass

def run_scheduler():
    # Configura os horários baseados na variável global
    for hora in horarios_noturnos:
        schedule.every().day.at(hora).do(verificar_e_avisar_noturno)
    
    while True:
        schedule.run_pending()
        time.sleep(60)


# --- HANDLERS DE ACESSO NEGADO ---
@bot.message_handler(is_authorized=False)
def acesso_negado(message):
    bot.reply_to(message, "🚫 Acesso Negado. Você não está autorizado a usar este bot.")

@bot.callback_query_handler(func=lambda call: True, is_authorized=False)
def callback_acesso_negado(call):
    bot.answer_callback_query(call.id, "🚫 Acesso Negado.")

# --- FILTRO CUSTOMIZADO E EXECUÇÃO ---
class IsAuthorizedFilter(telebot.custom_filters.SimpleCustomFilter):
    key = 'is_authorized'
    @staticmethod
    def check(update):
        allowed = [str(v) for v in CONTATOS_FAMILIA.values() if v]
        if MEU_CHAT_ID:
            allowed.append(str(MEU_CHAT_ID))
        
        if type(update) == telebot.types.Message:
            chat_id = str(update.chat.id)
        elif type(update) == telebot.types.CallbackQuery:
            chat_id = str(update.message.chat.id)
        else:
            return False
            
        return chat_id in allowed

if __name__ == "__main__":
    bot.add_custom_filter(IsAuthorizedFilter())
    # Inicia Web Server para Keep-Alive
    threading.Thread(target=run_web, daemon=True).start()
    # Inicia Scheduler de lembretes noturnos
    threading.Thread(target=run_scheduler, daemon=True).start()
    
    print("🚀 Bot iniciado com Lembretes Noturnos ativos!")
    bot.infinity_polling()
