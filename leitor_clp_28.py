import gc
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser

# Tenta importar requests para a transmissão externa
try:
    import requests
    REQUESTS_DISPONIVEL = True
except ImportError:
    REQUESTS_DISPONIVEL = False

# ==========================================
# DESALOCAÇÃO DE THREADS ANTERIORES
# ==========================================
try:
    for thread in threading.enumerate():
        if thread.name.startswith("CLP_Thread") or thread.name.startswith("HTTP_Thread") or thread.name.startswith("Transmissor_Thread"):
            print(f"[RESTART] Encerrando thread antiga: {thread.name}")
except Exception:
    pass
gc.collect()

# Tenta importar pyserial
try:
    import serial
    import serial.tools.list_ports
    PYSERIAL_DISPONIVEL = True
except ImportError:
    PYSERIAL_DISPONIVEL = False

# ==========================================
# CONFIGURAÇÕES DA COMUNICAÇÃO SERIAL (CLP) E NUVEM
# ==========================================
PORTA_COM = "COM3"
BAUDRATE = 38400
DATA_BITS = 7
PARIDADE = "E"
STOP_BITS = 1
USAR_CLP_REAL = True

# URL do seu serviço hospedado no Render
URL_RENDER = "https://supervisorio-robo.onrender.com/api/atualizar"
ENVIAR_PARA_RENDER = True  # Ativa o envio automático para a nuvem quando executado na bancada

# LIMITE DE TEMPO EM SEGUNDOS SEM RECEBER PUSH LOCAL PARA MUDAR PARA SIMULAÇÃO NO RENDER
TIMEOUT_CONEXAO_NUVEM_SEG = 4.0

# ==========================================
# CREDENCIAIS E GERENCIAMENTO DE SESSÕES
# ==========================================
USUARIO_ADMIN = "admin"
SENHA_ADMIN = "mm1234"
SESSIONS_ATIVAS = set()

# ==========================================
# CALIBRAÇÃO DO TRANSDUTOR DE PRESSÃO (D200)
# ==========================================
PRESSAO_MAXIMA_BAR = 10.0
VALOR_ADC_MAX = 3687.0
OFFSET_ZERO = 766
ZONA_MORTA_ADC = 15

# ==========================================
# VARIÁVEIS GLOBAIS DE ESTADO E LOCK
# ==========================================
dados_lock = threading.Lock()

ESTADO_ENTRADAS = {f"X{i}": False for i in list(range(8)) + list(range(20, 28))}
ESTADO_SAIDAS = {f"Y{i}": False for i in range(8)}
VALOR_PRESSAO_BAR = 0.00
POTENCIA_KW = 0.00
DETALHAMENTO_POTENCIA = {
    "eletronica": 18.0,
    "motor_x": 15.0,
    "solenoide_y": 0.0,
    "solenoide_z": 0.0,
    "vacuo": 0.0,
    "reles": 0.0
}
STATUS_COMUNICACAO = "INICIANDO"
ULTIMA_ATUALIZACAO_TIMESTAMP = 0.0  # Guarda o relógio do último POST recebido da bancada

# NOMES DOS ARQUIVOS DE MÍDIA / STL
nome_imagem_parker = "assets/image_e84f27.jpg"
nome_imagem_ipega = "assets/controle_ipega.jpg"
nome_stl_base = "assets/MONTAGEM_BASE.stl"
nome_stl_carro_x = "assets/MONTAGEM_CARRO_X.stl"
nome_stl_carro_y = "assets/MONTAGEM_CARRO_Y.stl"
nome_stl_carro_z = "assets/MONTAGEM_CARRO_Z.stl"

def calcular_checksum(comando_bytes):
    soma = sum(comando_bytes) & 0xFF
    return f"{soma:02X}".encode("ascii")


def criar_comando(payload):
    base = payload + b"\x03"
    chk = calcular_checksum(base)
    return b"\x02" + base + chk


def calcular_pressao_com_zero(val_bruto_d200):
    if val_bruto_d200 <= (OFFSET_ZERO + ZONA_MORTA_ADC):
        return 0.0

    valor_util_adc = val_bruto_d200 - OFFSET_ZERO
    faixa_total_util = VALOR_ADC_MAX - OFFSET_ZERO
    pressao = (valor_util_adc / faixa_total_util) * PRESSAO_MAXIMA_BAR
    return max(0.0, min(pressao, PRESSAO_MAXIMA_BAR))


def calcular_potencia_instantanea():
    detalhe = {
        "eletronica": 18.0,
        "motor_x": 15.0,  # Garante torque de retenção mínimo ativo
        "solenoide_y": 0.0,
        "solenoide_z": 0.0,
        "vacuo": 0.0,
        "reles": 0.0
    }

    with dados_lock:
        if ESTADO_SAIDAS.get("Y0", False): detalhe["motor_x"] = 35.0
        if ESTADO_SAIDAS.get("Y1", False): detalhe["solenoide_y"] = 4.8
        if ESTADO_SAIDAS.get("Y2", False): detalhe["solenoide_z"] = 4.8
        if ESTADO_SAIDAS.get("Y3", False): detalhe["vacuo"] = 4.8

        reles_ativos = sum(1 for y in ["Y0", "Y1", "Y2", "Y3", "Y4", "Y5"] if ESTADO_SAIDAS.get(y, False))
        detalhe["reles"] = round(reles_ativos * 0.5, 1)

    total_watts = sum(detalhe.values())
    return round(total_watts, 1), detalhe


# ==========================================
# THREAD DE LEITURA FÍSICA DO CLP
# ==========================================
def worker_leitura_clp():
    global ESTADO_ENTRADAS, ESTADO_SAIDAS, VALOR_PRESSAO_BAR, POTENCIA_KW, DETALHAMENTO_POTENCIA, STATUS_COMUNICACAO
    ser = None
    cmd_d200 = criar_comando(b"0119002")

    while True:
        # 1. MODO SIMULAÇÃO (Inativo se já estiver conectado ao CLP Real)
        if not USAR_CLP_REAL and STATUS_COMUNICACAO != "CLP_REAL_CONECTADO":
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None

            try:
                segundos = int(time.time())
                with dados_lock:
                    for i in range(8):
                        ESTADO_ENTRADAS[f"X{i}"] = ((segundos + i) % 5) == 0
                    # Emergência X2 ligada por padrão no simulador (Normal Fechada / Ativa)
                    ESTADO_ENTRADAS["X2"] = True

                    for i in range(20, 28):
                        ESTADO_ENTRADAS[f"X{20 + (i-20)}"] = ((segundos + i) % 17) == 0
                    for i in range(8):
                        ESTADO_SAIDAS[f"Y{i}"] = ((segundos + i) % 3) == 0
                    
                    VALOR_PRESSAO_BAR = round((segundos % 100) / 10.0, 2)
                    STATUS_COMUNICACAO = "SIMULADOR_ATIVO_OK"
                
                pot_calc, detalhe_calc = calcular_potencia_instantanea()
                with dados_lock:
                    POTENCIA_KW = pot_calc
                    DETALHAMENTO_POTENCIA = detalhe_calc

                time.sleep(0.15)
                continue
            except Exception:
                pass

        # 2. VERIFICAÇÃO DE SERIAL
        if not PYSERIAL_DISPONIVEL:
            with dados_lock:
                STATUS_COMUNICACAO = "BIBLIOTECA_SERIAL_NAO_INSTALADA"
            time.sleep(1)
            continue

        # 3. COMUNICAÇÃO SERIAL REAL COM CLP
        try:
            if ser is None or not ser.is_open:
                ser = serial.Serial(
                    port=PORTA_COM,
                    baudrate=BAUDRATE,
                    bytesize=DATA_BITS,
                    parity=PARIDADE,
                    stopbits=STOP_BITS,
                    timeout=0.05,
                )
                print(f"\nPorta {PORTA_COM} aberta com sucesso!")

            ser.write(b"\x05")
            _ = ser.read(1)

            # --- 1. LEITURA DAS ENTRADAS X0-X7 ---
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            comando_x0 = b"\x020008001\x035C"
            ser.write(comando_x0)
            resposta_x0 = ser.read(16)

            if len(resposta_x0) > 0 and resposta_x0[0] == 0x02 and len(resposta_x0) >= 4:
                dados_hex_x0 = resposta_x0[1:3].decode("ascii", errors="ignore")
                valor_byte_x0 = int(dados_hex_x0, 16)
                with dados_lock:
                    for bit_idx in range(8):
                        ESTADO_ENTRADAS[f"X{bit_idx}"] = bool(valor_byte_x0 & (1 << bit_idx))
                    STATUS_COMUNICACAO = "CLP_PORTA_OK"
            else:
                with dados_lock:
                    STATUS_COMUNICACAO = "SEM_RESPOSTA_CLP"
                time.sleep(0.1)
                continue

            time.sleep(0.01)

            # --- 2. LEITURA DAS ENTRADAS X20-X27 ---
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            comando_x20 = b"\x020008201\x035E"
            ser.write(comando_x20)
            resposta_x20 = ser.read(16)

            if len(resposta_x20) > 0 and resposta_x20[0] == 0x02 and len(resposta_x20) >= 4:
                dados_hex_x20 = resposta_x20[1:3].decode("ascii", errors="ignore")
                valor_byte_x20 = int(dados_hex_x20, 16)
                with dados_lock:
                    for bit_idx in range(8):
                        ESTADO_ENTRADAS[f"X{20 + bit_idx}"] = bool(valor_byte_x20 & (1 << bit_idx))
                    STATUS_COMUNICACAO = "CLP_PORTA_OK"
            else:
                with dados_lock:
                    STATUS_COMUNICACAO = "SEM_RESPOSTA_CLP"
                time.sleep(0.1)
                continue

            time.sleep(0.01)

            # --- 3. LEITURA DAS SAÍDAS Y0-Y7 ---
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            comando_y = b"\x02000A001\x0365"
            ser.write(comando_y)
            resposta_y = ser.read(16)

            if len(resposta_y) > 0 and resposta_y[0] == 0x02 and len(resposta_y) >= 4:
                dados_hex_y = resposta_y[1:3].decode("ascii", errors="ignore")
                valor_byte_y = int(dados_hex_y, 16)
                with dados_lock:
                    for bit_idx in range(8):
                        ESTADO_SAIDAS[f"Y{bit_idx}"] = bool(valor_byte_y & (1 << bit_idx))
                    STATUS_COMUNICACAO = "CLP_PORTA_OK"
            else:
                with dados_lock:
                    STATUS_COMUNICACAO = "SEM_RESPOSTA_CLP"

            time.sleep(0.01)

            # --- 4. LEITURA DO REGISTRADOR ANALÓGICO D200 ---
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(cmd_d200)
            resposta_d200 = ser.read(16)

            if len(resposta_d200) >= 6 and resposta_d200[0] == 0x02:
                idx_etx = resposta_d200.find(b"\x03")
                if idx_etx > 1:
                    hex_d200 = resposta_d200[1:idx_etx].decode("ascii", errors="ignore")
                    if len(hex_d200) >= 4:
                        try:
                            b_low = int(hex_d200[0:2], 16)
                            b_high = int(hex_d200[2:4], 16)
                            val_bruto_d200 = (b_high << 8) | b_low
                            pressao_calc = round(calcular_pressao_com_zero(val_bruto_d200), 2)
                            with dados_lock:
                                VALOR_PRESSAO_BAR = pressao_calc
                        except ValueError:
                            pass

            pot_calc, detalhe_calc = calcular_potencia_instantanea()
            with dados_lock:
                POTENCIA_KW = pot_calc
                DETALHAMENTO_POTENCIA = detalhe_calc

        except Exception as e:
            print(f" Erro de comunicação serial: {e}")
            with dados_lock:
                STATUS_COMUNICACAO = "ERRO_PORTA"
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None

        time.sleep(0.05)


# ==========================================
# THREAD TRANSMISSORA DE DADOS PARA A NUVEM (RENDER)
# ==========================================
def worker_transmissor_nuvem():
    """Envia leituras locais do CLP em tempo real para a instância do Render."""
    if not REQUESTS_DISPONIVEL:
        print("[TRANSMISSOR] Módulo 'requests' não está instalado. Transmissão para a nuvem desativada.")
        return

    print(f"[TRANSMISSOR] Thread iniciada. Disparando dados para: {URL_RENDER}")

    while True:
        if ENVIAR_PARA_RENDER and USAR_CLP_REAL:
            with dados_lock:
                payload = {
                    "entradas": ESTADO_ENTRADAS,
                    "saidas": ESTADO_SAIDAS,
                    "pressao_bar": VALOR_PRESSAO_BAR,
                    "potencia_kw": POTENCIA_KW,
                    "detalhamento_potencia": DETALHAMENTO_POTENCIA
                }

            try:
                resposta = requests.post(URL_RENDER, json=payload, timeout=0.8)
            except Exception:
                pass

        time.sleep(0.1)


# ==========================================
# SERVIDOR HTTP & ROTEAMENTO DE DADOS
# ==========================================
class CustomCombinedHTTPRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_POST(self):
        global USAR_CLP_REAL, ESTADO_ENTRADAS, ESTADO_SAIDAS, VALOR_PRESSAO_BAR, POTENCIA_KW, DETALHAMENTO_POTENCIA, STATUS_COMUNICACAO, ULTIMA_ATUALIZACAO_TIMESTAMP
        
        # 1. Rota de Autenticação / Login
        if self.path == "/api/login":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                dados = json.loads(body)
                if dados.get("usuario") == USUARIO_ADMIN and dados.get("senha") == SENHA_ADMIN:
                    novo_token = str(uuid.uuid4())
                    SESSIONS_ATIVAS.add(novo_token)
                    
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "sucesso", "token": novo_token}).encode("utf-8"))
                    return
                else:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "erro", "mensagem": "Credenciais invalidas"}).encode("utf-8"))
                    return
            except Exception as e:
                print(f"[ERRO] Falha no endpoint /api/login: {e}")

        # 2. Rota para alternar modo de simulação
        elif self.path == "/toggle_modo":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                dados = json.loads(body)
                if "usar_clp_real" in dados:
                    USAR_CLP_REAL = bool(dados["usar_clp_real"])
                    print(f"\n[SISTEMA] Modo alterado -> CLP Real: {USAR_CLP_REAL}")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ok", "usar_clp_real": USAR_CLP_REAL}).encode("utf-8"))
                    return
            except Exception:
                pass

        # 3. Rota de Recepção de Dados Segura (Chamada na Nuvem/Render)
        elif self.path == "/api/atualizar":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                dados_recebidos = json.loads(body)
                with dados_lock:
                    USAR_CLP_REAL = True
                    ULTIMA_ATUALIZACAO_TIMESTAMP = time.time()  # Reseta o temporizador de heartbeat
                    
                    if "entradas" in dados_recebidos:
                        ESTADO_ENTRADAS.update(dados_recebidos["entradas"])
                    if "saidas" in dados_recebidos:
                        ESTADO_SAIDAS.update(dados_recebidos["saidas"])
                    if "pressao_bar" in dados_recebidos:
                        VALOR_PRESSAO_BAR = dados_recebidos["pressao_bar"]
                    if "potencia_kw" in dados_recebidos:
                        POTENCIA_KW = dados_recebidos["potencia_kw"]
                    if "detalhamento_potencia" in dados_recebidos:
                        DETALHAMENTO_POTENCIA.update(dados_recebidos["detalhamento_potencia"])
                    
                    STATUS_COMUNICACAO = "CLP_REAL_CONECTADO"

                pot_calc, detalhe_calc = calcular_potencia_instantanea()
                with dados_lock:
                    if POTENCIA_KW == 0.0 and pot_calc > 0:
                        POTENCIA_KW = pot_calc
                        DETALHAMENTO_POTENCIA = detalhe_calc

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "recebido"}).encode("utf-8"))
                return
            except Exception as e:
                print(f"[ERRO] Falha em /api/atualizar: {e}")

        self.send_response(400)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        query_params = urllib.parse.parse_qs(parsed_path.query)

        # 1. Rota de Servidão da Tela de Login
        if path == "/login":
            if os.path.exists("assets/login.html"):
                with open("assets/login.html", "rb") as f:
                    conteudo = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(conteudo)
            else:
                self.send_response(404)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"Arquivo assets/login.html nao encontrado.")

        # 2. Rota Hub / Dashboard (Hub de Projetos)
        elif path in ["/", "/dashboard"]:
            token_url = query_params.get("token", [None])[0]
            if not token_url or token_url not in SESSIONS_ATIVAS:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return

            if os.path.exists("assets/dashboard.html"):
                with open("assets/dashboard.html", "rb") as f:
                    conteudo = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(conteudo)
            else:
                self.send_response(404)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"Arquivo assets/dashboard.html nao encontrado.")

        # 3. Rota do Robô Cartesiano 3D
        elif path == "/supervisorio/robo-cartesiano":
            token_url = query_params.get("token", [None])[0]
            if not token_url or token_url not in SESSIONS_ATIVAS:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return

            if os.path.exists("assets/index.html"):
                with open("assets/index.html", "rb") as f:
                    conteudo = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(conteudo)
            else:
                self.send_response(404)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"Arquivo assets/index.html nao encontrado.")

        # 4. Rota de API JSON para o Front-end (COM TIMEOUT PARA O RENDER)
        elif path in ["/dados", "/dados_clp"]:
            with dados_lock:
                # Verifica se estourou o tempo sem dados da bancada
                tempo_decorrido = time.time() - ULTIMA_ATUALIZACAO_TIMESTAMP
                
                # Se estamos usando CLP real, mas passou do tempo limite e o timestamp já foi inicializado:
                if USAR_CLP_REAL and ULTIMA_ATUALIZACAO_TIMESTAMP > 0 and tempo_decorrido > TIMEOUT_CONEXAO_NUVEM_SEG:
                    status_envio = "SEM_RESPOSTA_CLP"
                    modo_real_envio = False
                else:
                    status_envio = STATUS_COMUNICACAO
                    modo_real_envio = USAR_CLP_REAL

                payload = {
                    "entradas": ESTADO_ENTRADAS,
                    "saidas": ESTADO_SAIDAS,
                    "pressao_bar": VALOR_PRESSAO_BAR,
                    "potencia_w": POTENCIA_KW,
                    "potencia_kw": round(POTENCIA_KW / 1000.0, 4),
                    "detalhamento_potencia": DETALHAMENTO_POTENCIA,
                    "status_comunicacao": status_envio,
                    "usar_clp_real": modo_real_envio,
                    "modo_simulacao": not modo_real_envio
                }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        # 5. Servir arquivos estáticos (STL e Imagens)
        elif path.startswith("/assets/"):
            file_path = path.lstrip("/")
            if os.path.exists(file_path):
                self.send_response(200)
                if file_path.endswith(".stl"):
                    self.send_header("Content-Type", "model/stl")
                elif file_path.endswith(".jpg") or file_path.endswith(".jpeg"):
                    self.send_header("Content-Type", "image/jpeg")
                elif file_path.endswith(".png"):
                    self.send_header("Content-Type", "image/png")
                else:
                    self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(file_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

        else:
            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()


def obter_ip_local():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


ip_computador = obter_ip_local()
porta_servidor = int(os.environ.get("PORT", 8085))


def rodar_servidor():
    server_address = ("0.0.0.0", porta_servidor)
    httpd = HTTPServer(server_address, CustomCombinedHTTPRequestHandler)
    print(f"[HTTP] Servidor rodando em http://localhost:{porta_servidor}/")
    httpd.serve_forever()


# ==========================================
# INICIALIZAÇÃO DO SISTEMA
# ==========================================
if __name__ == "__main__":
    if not PYSERIAL_DISPONIVEL:
        USAR_CLP_REAL = False

    # Thread 1: Leitura Serial do CLP / Simulador
    thread_clp = threading.Thread(
        target=worker_leitura_clp, name="CLP_Thread", daemon=True
    )
    thread_clp.start()

    # Thread 2: Servidor Web Local HTTP
    thread_web = threading.Thread(
        target=rodar_servidor, name="HTTP_Thread", daemon=True
    )
    thread_web.start()

    # Thread 3: Transmissor Ativo de Dados para o Render
    thread_transmissor = threading.Thread(
        target=worker_transmissor_nuvem, name="Transmissor_Thread", daemon=True
    )
    thread_transmissor.start()

    try:
        # Abre diretamente na rota de login ao iniciar
        webbrowser.open(f"http://localhost:{porta_servidor}/login")
    except Exception:
        pass

    while True:
        time.sleep(1)