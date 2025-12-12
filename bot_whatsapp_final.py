"""
Bot WhatsApp com Twilio - FDA Frutos da Amazônia
Reutiliza lógica do bot Telegram para WhatsApp
Extrai coordenadas GPS, valida tags de clientes e gera mapas
"""

import os
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from PIL import Image
import pytesseract
import folium
import requests

# ============================================================================
# CONFIGURAÇÃO DE LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuração do Tesseract
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

# ============================================================================
# CONFIGURAÇÃO DO TWILIO
# ============================================================================

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "your_account_sid")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "your_auth_token")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "+55 91 40403322")

# Inicializar cliente Twilio
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ============================================================================
# CONFIGURAÇÃO DE CLIENTES E GEOFENCES
# ============================================================================

CLIENTES_OURILANDIA = {
    "Oia Giro": {
        "latitude": -6.754173,
        "longitude": -51.071787,
        "raio_metros": 500,
        "cor": "blue",
    },
    "Oia Ideal": {
        "latitude": -6.750542,
        "longitude": -51.080360,
        "raio_metros": 500,
        "cor": "red",
    },
    "Oia Macre": {
        "latitude": -6.759242,
        "longitude": -51.071143,
        "raio_metros": 500,
        "cor": "green",
    },
    "Oia Parazao": {
        "latitude": -6.751243,
        "longitude": -51.078318,
        "raio_metros": 500,
        "cor": "purple",
    },
    "Oia Norte Sul": {
        "latitude": -6.752724,
        "longitude": -51.076518,
        "raio_metros": 500,
        "cor": "orange",
    },
    "Oia Mix": {
        "latitude": -6.730903,
        "longitude": -51.071559,
        "raio_metros": 500,
        "cor": "darkred",
    }
}

# Arquivo para armazenar coordenadas
COORDS_FILE = "coordenadas.json"
MAPA_FILE = "mapa.html"

# Variáveis globais para controlar o delay de geração de mapa
mapa_timer = None

# ============================================================================
# FLASK APP
# ============================================================================

app = Flask(__name__)

# ============================================================================
# FUNÇÕES DE PRÉ-PROCESSAMENTO (REUTILIZADAS DO TELEGRAM)
# ============================================================================

def preprocess_image_for_ocr(image_path: str) -> Image.Image:
    """Abre a imagem sem pré-processamento agressivo que destrói texto pequeno."""
    img = Image.open(image_path)
    return img

def clean_ocr_text(text: str) -> str:
    """Limpa o texto extraído pelo OCR."""
    text = re.sub(r'denov', 'de nov', text, flags=re.IGNORECASE)
    logger.info(f"Texto após a limpeza:\n---\n{text}\n---")
    return text

# ============================================================================
# FUNÇÕES DE PARSING (REUTILIZADAS DO TELEGRAM)
# ============================================================================

def parse_coordinates(coords_str: str) -> tuple:
    """
    Processa coordenadas GPS no formato: -6,6386S -51,9896W
    Retorna uma tupla (latitude, longitude) como números decimais.
    """
    try:
        parts = coords_str.strip().split()
        if len(parts) != 2:
            logger.error(f"Formato de coordenadas inválido: {coords_str}")
            return None
        
        lat_str, lon_str = parts
        
        lat_str = lat_str.replace(',', '.').replace('S', '').replace('N', '')
        latitude = float(lat_str)
        
        lon_str = lon_str.replace(',', '.').replace('W', '').replace('E', '').replace('L', '').replace('O', '')
        longitude = float(lon_str)
        
        if not (-90 <= latitude <= 90):
            logger.error(f"Latitude fora do intervalo válido: {latitude}")
            return None
        if not (-180 <= longitude <= 180):
            logger.error(f"Longitude fora do intervalo válido: {longitude}")
            return None
        
        logger.info(f"Coordenadas processadas com sucesso: Latitude={latitude}, Longitude={longitude}")
        return (latitude, longitude)
    
    except ValueError as e:
        logger.error(f"Erro ao converter coordenadas para números: {e}")
        return None

def find_datetime_in_text(text: str) -> datetime:
    """Busca por data e hora no texto usando várias regras."""
    month_map = {
        'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6, 
        'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12
    }

    # REGRA 1
    match1 = re.search(r'(\d{1,2})\s*(?:de\s*)?([a-z]{3,})\.?\s*(?:de\s*)?(\d{4})\s*.*?(\d{2}:\d{2}(?::\d{2})?)', text, re.IGNORECASE)
    if match1:
        logger.info("Padrão 1 ('DD de Mês de AAAA') encontrado!")
        day, month_str, year, time_str = match1.groups()
        month = month_map.get(month_str.lower()[:3])
        if month:
            try:
                if len(time_str) == 5: time_str += ':00'
                return datetime(int(year), month, int(day), int(time_str[:2]), int(time_str[3:5]), int(time_str[6:]))
            except ValueError:
                logger.error("Valores de data/hora inválidos no Padrão 1.")

    # REGRA 2
    match2 = re.search(r'(\d{2}/\d{2}/\d{4})\s*(\d{2}:\d{2}(?::\d{2})?)', text)
    if match2:
        logger.info("Padrão 2 ('DD/MM/AAAA') encontrado!")
        date_str, time_str = match2.groups()
        try:
            if len(time_str) == 5: time_str += ':00'
            return datetime.strptime(f"{date_str} {time_str}", '%d/%m/%Y %H:%M:%S')
        except ValueError:
            logger.error("Formato de data/hora inválido para DD/MM/AAAA.")

    logger.info("Nenhum padrão de data/hora conhecido foi encontrado no texto.")
    return None

# ============================================================================
# FUNÇÕES DE ARMAZENAMENTO
# ============================================================================

def load_coordinates():
    """Carrega coordenadas do arquivo JSON."""
    if Path(COORDS_FILE).exists():
        with open(COORDS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_coordinates(coords_list):
    """Salva coordenadas no arquivo JSON."""
    with open(COORDS_FILE, 'w', encoding='utf-8') as f:
        json.dump(coords_list, f, ensure_ascii=False, indent=2)

def is_duplicate(coord):
    """Verifica se a coordenada é duplicada."""
    coords_list = load_coordinates()
    for existing in coords_list:
        if (existing.get("timestamp") == coord.get("timestamp") and
            abs(existing.get("latitude", 0) - coord.get("latitude", 0)) < 0.0001 and
            abs(existing.get("longitude", 0) - coord.get("longitude", 0)) < 0.0001):
            return True
    return False

# ============================================================================
# FUNÇÕES DE EXTRAÇÃO DE DADOS
# ============================================================================

def extract_data_from_image(image_path):
    """Extrai data/hora, coordenadas e tag de cliente da imagem usando OCR."""
    try:
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img, lang='por+eng')
        
        logger.info(f"Texto extraído da imagem:\n{text}")
        
        # Extrai data e hora
        dt_object = find_datetime_in_text(text)
        
        if not dt_object:
            logger.warning("Data/hora não encontrada na imagem")
            return None
        
        timestamp = dt_object.strftime('%d/%m/%Y %H:%M:%S')
        
        # Extrai coordenadas
        coords_match = re.search(r'(-?\d+[\.,]\d+[NSns])\s+(-?\d+[\.,]\d+[EWLOwvloe])', text, re.IGNORECASE)
        
        if not coords_match:
            logger.warning("Coordenadas não encontradas na imagem")
            return None
        
        coords_str = f"{coords_match.group(1)} {coords_match.group(2)}"
        parsed_coords = parse_coordinates(coords_str)
        
        if not parsed_coords:
            logger.warning("Coordenadas inválidas")
            return None
        
        latitude, longitude = parsed_coords
        
        # Extrai tag de cliente
        tag_pattern = r'[#t][O0]ia\s+(\w+)'
        tag_match = re.search(tag_pattern, text, re.IGNORECASE)
        
        cliente = None
        if tag_match:
            cliente_name = tag_match.group(1)
            if cliente_name in CLIENTES_OURILANDIA:
                cliente = cliente_name
            else:
                logger.warning(f"Cliente desconhecido: {cliente_name}")
        
        return {
            "timestamp": timestamp,
            "latitude": latitude,
            "longitude": longitude,
            "cliente": cliente,
            "texto_bruto": text
        }
    
    except Exception as e:
        logger.error(f"Erro ao extrair dados da imagem: {e}")
        import traceback
        traceback.print_exc()
        return None

def validate_client_by_geofence(latitude, longitude):
    """Valida qual cliente a coordenada pertence usando geofence."""
    from math import radians, cos, sin, asin, sqrt
    
    def haversine(lat1, lon1, lat2, lon2):
        """Calcula distância entre dois pontos em metros."""
        lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))
        r = 6371000  # Raio da Terra em metros
        return c * r
    
    closest_cliente = None
    min_distance = float('inf')
    
    for cliente_name, cliente_info in CLIENTES_OURILANDIA.items():
        distance = haversine(
            latitude, longitude,
            cliente_info["latitude"], cliente_info["longitude"]
        )
        
        if distance < cliente_info["raio_metros"] and distance < min_distance:
            closest_cliente = cliente_name
            min_distance = distance
    
    return closest_cliente

# ============================================================================
# FUNÇÕES DE MAPA
# ============================================================================

def generate_map() -> bool:
    """Gera um mapa interativo com Folium com todas as coordenadas agrupadas por cliente."""
    try:
        coords_list = load_coordinates()
        
        if not coords_list:
            logger.warning("Nenhuma coordenada para gerar mapa")
            return False
        
        coords_com_cliente = [c for c in coords_list if c.get("cliente")]
        
        if not coords_com_cliente:
            logger.warning("Nenhuma coordenada com cliente para gerar mapa")
            return False
        
        lats = [c["latitude"] for c in coords_com_cliente]
        lons = [c["longitude"] for c in coords_com_cliente]
        center_lat = sum(lats) / len(lats)
        center_lon = sum(lons) / len(lons)
        
        mapa = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=14,
            tiles="OpenStreetMap"
        )
        
        coords_por_cliente = {}
        for coord in coords_com_cliente:
            cliente = coord.get("cliente")
            if cliente not in coords_por_cliente:
                coords_por_cliente[cliente] = []
            coords_por_cliente[cliente].append(coord)
        
        for cliente_name, coords in coords_por_cliente.items():
            if cliente_name in CLIENTES_OURILANDIA:
                cliente_info = CLIENTES_OURILANDIA[cliente_name]
                cor = cliente_info["cor"]
                
                folium.Circle(
                    location=[cliente_info["latitude"], cliente_info["longitude"]],
                    radius=cliente_info["raio_metros"],
                    color=cor,
                    fill=True,
                    fillColor=cor,
                    fillOpacity=0.1,
                    weight=2,
                    popup=f"Geofence: {cliente_name}"
                ).add_to(mapa)
                
                folium.Marker(
                    location=[cliente_info["latitude"], cliente_info["longitude"]],
                    popup=f"<b>Centro: {cliente_name}</b>",
                    icon=folium.Icon(color=cor, icon="star", prefix="fa")
                ).add_to(mapa)
                
                for coord in coords:
                    folium.Marker(
                        location=[coord["latitude"], coord["longitude"]],
                        popup=f"<b>{cliente_name}</b><br>Data: {coord['timestamp']}",
                        tooltip=f"{cliente_name} - {coord['timestamp']}",
                        icon=folium.Icon(color=cor, icon="camera", prefix="fa")
                    ).add_to(mapa)
        
        legend_html = '''
        <div style="position: fixed; 
                    bottom: 50px; right: 50px; width: 280px; height: auto; 
                    background-color: white; border:2px solid grey; z-index:9999; 
                    font-size:13px; padding: 12px; border-radius: 5px;">
            <b style="font-size: 14px;">Clientes - Ourilândia</b><br>
            <hr style="margin: 5px 0;">
        '''
        
        for cliente_name, coords in sorted(coords_por_cliente.items()):
            cor = CLIENTES_OURILANDIA[cliente_name]["cor"]
            contagem = len(coords)
            legend_html += f'<div style="margin: 5px 0;"><i style="background:{cor}; width: 16px; height: 16px; display: inline-block; border-radius: 50%; border: 1px solid black;"></i> <b>{cliente_name}</b>: {contagem} foto(s)</div>'
        
        legend_html += '</div>'
        
        mapa.get_root().html.add_child(folium.Element(legend_html))
        
        mapa.save(MAPA_FILE)
        logger.info(f"Mapa HTML gerado: {MAPA_FILE} com {len(coords_com_cliente)} pontos")
        return True
    
    except Exception as e:
        logger.error(f"Erro ao gerar mapa: {e}")
        import traceback
        traceback.print_exc()
        return False

def schedule_map_generation():
    """Agenda a geração de mapa para 60 segundos após a última foto."""
    global mapa_timer
    
    if mapa_timer:
        mapa_timer.cancel()
    
    mapa_timer = threading.Timer(60.0, lambda: generate_map())
    mapa_timer.daemon = True
    mapa_timer.start()
    logger.info("Mapa agendado para ser gerado em 60 segundos")

# ============================================================================
# HANDLERS DO WHATSAPP
# ============================================================================

@app.route("/webhook", methods=['POST'])
def webhook():
    """Webhook para receber mensagens do WhatsApp."""
    try:
        incoming_msg = request.values.get('Body', '').strip()
        sender = request.values.get('From', '')
        num_media = int(request.values.get('NumMedia', 0))
        
        logger.info(f"Mensagem recebida de {sender}: {incoming_msg}")
        
        resp = MessagingResponse()
        
        if num_media > 0:
            media_url = request.values.get('MediaUrl0')
            logger.info(f"Foto recebida: {media_url}")
            
            response = requests.get(media_url)
            image_path = f"temp_image_{datetime.now().timestamp()}.jpg"
            with open(image_path, 'wb') as f:
                f.write(response.content)
            
            data = extract_data_from_image(image_path)
            
            if not data:
                resp.message("❌ Não consegui extrair dados completos da imagem (data, hora, coordenadas e cliente). Foto ignorada.")
                logger.warning(f"Falha ao extrair dados da imagem")
            else:
                if not data.get("cliente"):
                    data["cliente"] = validate_client_by_geofence(
                        data["latitude"],
                        data["longitude"]
                    )
                
                if not data.get("cliente"):
                    resp.message(f"❌ Coordenadas fora de todas as geofences. Foto ignorada.\n\nCoordenadas: {data['latitude']:.6f}, {data['longitude']:.6f}")
                    logger.warning(f"Coordenadas fora de geofences")
                else:
                    if is_duplicate(data):
                        resp.message("⚠️ Foto duplicada detectada. Ignorada.")
                        logger.warning("Foto duplicada")
                    else:
                        coords_list = load_coordinates()
                        data["id"] = len(coords_list) + 1
                        coords_list.append(data)
                        save_coordinates(coords_list)
                        
                        resp.message(f"✅ Foto processada com sucesso!\n\n📍 Cliente: {data['cliente']}\n📅 Data: {data['timestamp']}\n🗺️ Coordenadas: {data['latitude']:.6f}, {data['longitude']:.6f}\n\nMapa será gerado em 60 segundos...")
                        
                        schedule_map_generation()
                        logger.info(f"Coordenada salva: {data}")
            
            try:
                os.remove(image_path)
            except:
                pass
        
        else:
            if incoming_msg.lower() == "/start":
                resp.message("👋 Bem-vindo ao FDA Bot!\n\n📸 Envie fotos com coordenadas GPS e tags de cliente.\n\nFormato esperado:\n- Linha 1: Data e Hora\n- Linha 2: Coordenadas GPS\n- Linha 3: #Oia NomeCliente")
            elif incoming_msg.lower() == "/mapa":
                if generate_map():
                    resp.message("✅ Mapa gerado com sucesso!")
                else:
                    resp.message("❌ Erro ao gerar mapa")
            else:
                resp.message("📸 Por favor, envie uma foto com coordenadas GPS.\n\nDigite /start para mais informações.")
        
        return str(resp)
    
    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        import traceback
        traceback.print_exc()
        resp = MessagingResponse()
        resp.message("❌ Erro ao processar mensagem")
        return str(resp)

@app.route("/", methods=['GET'])
def index():
    """Página inicial."""
    return "🤖 FDA WhatsApp Bot está funcionando!"

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logger.info(f"Iniciando FDA WhatsApp Bot na porta {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
