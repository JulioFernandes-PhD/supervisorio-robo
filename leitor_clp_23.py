import gc
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser

# ==========================================
# DESALOCAÇÃO DE THREADS ANTERIORES
# ==========================================
try:
    for thread in threading.enumerate():
        if thread.name.startswith("CLP_Thread") or thread.name.startswith("HTTP_Thread"):
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
# CONFIGURAÇÕES DA COMUNICAÇÃO SERIAL (CLP)
# ==========================================
PORTA_COM = "COM3"
BAUDRATE = 38400
DATA_BITS = 7
PARIDADE = "E"
STOP_BITS = 1
USAR_CLP_REAL = True

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
POTENCIA_KW = 0.00  # Mantido nome e variável para medidor de potência
STATUS_COMUNICACAO = "INICIANDO"

html_filename = "index.html"
nome_imagem_parker = "image_e84f27.jpg"
nome_imagem_ipega = "controle_ipega.jpg"

# NOMES REAIS DOS ARQUIVOS STL
nome_stl_base = "MONTAGEM_BASE.stl"
nome_stl_carro_x = "MONTAGEM_CARRO_X.stl"
nome_stl_carro_y = "MONTAGEM_CARRO_Y.stl"
nome_stl_carro_z = "MONTAGEM_CARRO_Z.stl"

# AJUSTE DE POSICIONAMENTO 3D (EM MM)
OFFSET_X_CARRO_X = -12
OFFSET_Y_CARRO_X = 203
OFFSET_Z_CARRO_X = 0

OFFSET_X_CARRO_Y = 100
OFFSET_Y_CARRO_Y = 472
OFFSET_Z_CARRO_Y = 44

OFFSET_X_CARRO_Z = 180
OFFSET_Y_CARRO_Z = 440
OFFSET_Z_CARRO_Z = 182

# ROTAÇÃO DOS EIXOS (EM GRAUS)
ROTACAO_X_CARRO_Y = 90
ROTACAO_Y_CARRO_Y = 90
ROTACAO_Z_CARRO_Y = 0
ROTACAO_X_CARRO_Z = 270
ROTACAO_Y_CARRO_Z = 0
ROTACAO_Z_CARRO_Z = 0

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


POTENCIA_WATTS = 50.0  


def calcular_potencia_instantanea():
    """
    Calcula a potência instantânea em Watts (W).
    """
    p_watts = 18.0  # Consumo base da eletrônica em repouso

    with dados_lock:
        driver_desabilitado = ESTADO_SAIDAS.get("Y5", False)
        driver_habilitado = not driver_desabilitado
        em_movimento = ESTADO_SAIDAS.get("Y0", False)
        emergencia_ativa = not ESTADO_ENTRADAS.get("X2", True)

        if driver_habilitado and not emergencia_ativa:
            if em_movimento:
                p_watts += 35.0  # Em movimento
            else:
                p_watts += 15.0  # Parado travado

        # Solenoides 24V
        if ESTADO_SAIDAS.get("Y1", False): p_watts += 4.8
        if ESTADO_SAIDAS.get("Y2", False): p_watts += 4.8
        if ESTADO_SAIDAS.get("Y3", False): p_watts += 4.8

        # Relés do Módulo 5V
        reles_ativos = sum(1 for y in ["Y0", "Y1", "Y2", "Y3", "Y4", "Y5"] if ESTADO_SAIDAS.get(y, False))
        p_watts += reles_ativos * 0.5

    return round(p_watts, 1)


# ==========================================
# THREAD DE LEITURA FÍSICA DO CLP
# ==========================================
def worker_leitura_clp():
    global ESTADO_ENTRADAS, ESTADO_SAIDAS, VALOR_PRESSAO_BAR, POTENCIA_KW, STATUS_COMUNICACAO
    ser = None
    cmd_d200 = criar_comando(b"0119002")

    while True:
        if not PYSERIAL_DISPONIVEL:
            with dados_lock:
                STATUS_COMUNICACAO = "BIBLIOTECA_SERIAL_NAO_INSTALADA"
            time.sleep(1)
            continue

        if not USAR_CLP_REAL:
            try:
                segundos = int(time.time())
                with dados_lock:
                    for i in range(8):
                        ESTADO_ENTRADAS[f"X{i}"] = ((segundos + i) % 5) == 0
                    for i in range(20, 28):
                        ESTADO_ENTRADAS[f"X{i}"] = ((segundos + i) % 17) == 0
                    for i in range(8):
                        ESTADO_SAIDAS[f"Y{i}"] = ((segundos + i) % 3) == 0
                    VALOR_PRESSAO_BAR = round((segundos % 100) / 10.0, 2)
                    STATUS_COMUNICACAO = "SIMULADOR_ATIVO_OK"
                
                pot_calc = calcular_potencia_instantanea()
                with dados_lock:
                    POTENCIA_KW = pot_calc

                time.sleep(0.15)
                continue
            except Exception:
                pass

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

            # Atualiza valor estimado da potência
            pot_calc = calcular_potencia_instantanea()
            with dados_lock:
                POTENCIA_KW = pot_calc

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
# CONSTRUÇÃO DO HTML / THREE.JS / PLOTLY / CHART.JS
# ==========================================
html_base = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Supervisório Gantry 3D</title>
<link href="https://fonts.googleapis.com/css?family=Share+Tech+Mono&display=swap" rel="stylesheet">
<script src="https://cdn.plot.ly/plotly-2.24.1.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/STLLoader.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<style>
* { box-sizing: border-box; }
body { margin: 0; padding: 15px; font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #FFFFFF; overflow-x: hidden; }
.container-principal { display: flex; flex-direction: row; gap: 20px; width: 100%; }
.cabecalho-industrial { font-size: 1.8rem; font-weight: bold; color: #FF4B4B; text-transform: uppercase; margin-top: 0px; margin-bottom: 12px; letter-spacing: -1px; }
.menu-lateral { width: 230px; min-width: 230px; background: #F8FAFC; padding: 15px; border-radius: 8px; border: 1px solid #E2E8F0; height: fit-content; }
.menu-lateral h3 { margin-top: 0; font-size: 1.1rem; color: #334155; }
.btn-menu { display: block; width: 100%; padding: 12px 10px; margin-bottom: 8px; background: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 5px; cursor: pointer; text-align: left; font-weight: bold; color: #475569; font-size: 0.95rem; }
.btn-menu.ativo { background: #FF4B4B; color: #FFFFFF; border-color: #FF4B4B; }
.btn-menu.destaque { border-left: 4px solid #10b981; }
.conteudo-principal { flex-grow: 1; width: 100%; min-width: 0; }
.tela { display: none; width: 100%; }
.tela.ativa { display: block; }
#container-stl-novo { width: 100%; height: 82vh; min-height: 550px; background-color: #0f172a; border-radius: 8px; border: 1px solid #1e293b; position: relative; overflow: hidden; touch-action: none; }
.grid-telemetria { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px; }
.card-grafico { background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 6px; padding: 5px; overflow: hidden; }
.wrapper-manometro-real { position: relative; width: 100%; max-width: 320px; margin: 0 auto; }
.imagem-fundo-manometro { width: 100%; height: auto; display: block; }
.display-digital-sobreposto { 
  position: absolute; 
  top: 11.2%; 
  left: 19.5%; 
  width: 61.5%; 
  height: 10.5%; 
  display: flex; 
  align-items: center; 
  justify-content: center; 
  padding-top: 0px; 
  margin-top: 0px; 
  font-family: 'Share Tech Mono', monospace; 
  font-weight: bold; 
  color: #ff1e1e; 
  font-size: 4.6rem; 
  text-shadow: 0 0 5px rgba(255, 30, 30, 0.85); 
}

.container-monitor-entradas, .container-monitor-saidas { max-width: 650px; margin: 15px auto; }
.grid-leds { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin: 15px 0 25px 0; }
.card-led { background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 6px; padding: 10px; text-align: center; display: flex; flex-direction: column; align-items: center; gap: 6px; }
.led-indicador { width: 40px; height: 40px; border-radius: 50%; background-color: #4b5563; margin: 0 auto; border: 3px solid #cbd5e1; }
.led-nome { font-family: 'Share Tech Mono', monospace; font-size: 15px; font-weight: bold; color: #334155; }
.led-status-texto { font-size: 11px; font-weight: bold; color: #64748b; }
.led-on-entrada { background-color: #10b981 !important; border-color: #d1fae5 !important; }
.led-on-saida { background-color: #f97316 !important; border-color: #ffedd5 !important; }
.led-off { background-color: #374151 !important; }
.led-erro { background-color: #ef4444 !important; }
.painel-footer { text-align: center; border-top: 1px solid #F1F5F9; padding-top: 15px; margin-top: 15px; }
.modo-badge { font-size: 13px; color: #9ca3af; font-family: monospace; }
.secao-titulo-entradas { font-size: 1rem; font-weight: bold; color: #475569; margin: 20px 0 10px 0; border-bottom: 2px solid #E2E8F0; padding-bottom: 5px; text-align: left; }

.card-controle {
  background: #0f172a;
  border-radius: 12px;
  padding: 20px;
  max-width: 750px;
  margin: 0 auto;
  box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3);
  text-align: center;
}
.wrapper-controle-foto {
  position: relative;
  width: 100%;
  max-width: 650px;
  margin: 0 auto;
  display: inline-block;
}
.imagem-fundo-controle {
  width: 100%;
  height: auto;
  display: block;
}

.overlay-btn {
  position: absolute;
  border-radius: 50%;
  pointer-events: none;
  opacity: 0;
  transition: all 0.12s ease-in-out;
  transform: translate(-50%, -50%);
  aspect-ratio: 1 / 1;
}

.overlay-btn.ativo {
  opacity: 1;
  transform: translate(-50%, -50%) scale(1.1);
}

#gp_foto_x20 { top: 51.5%; left: 30.2%; width: 7%; background: rgba(249, 115, 22, 0.55); border: 2px solid #f97316; box-shadow: 0 0 15px #f97316; border-radius: 4px; }
#gp_foto_x21 { top: 58.5%; left: 25.0%; width: 7%; background: rgba(249, 115, 22, 0.55); border: 2px solid #f97316; box-shadow: 0 0 15px #f97316; border-radius: 4px; }
#gp_foto_x22 { top: 65.5%; left: 30.2%; width: 7%; background: rgba(249, 115, 22, 0.55); border: 2px solid #f97316; box-shadow: 0 0 15px #f97316; border-radius: 4px; }
#gp_foto_x23 { top: 58.5%; left: 35.5%; width: 7%; background: rgba(249, 115, 22, 0.55); border: 2px solid #f97316; box-shadow: 0 0 15px #f97316; border-radius: 4px; }

#gp_foto_x24 { top: 43.2%; left: 76.6%; width: 6.8%; background: rgba(16, 185, 129, 0.65); border: 2px solid #10b981; box-shadow: 0 0 18px #10b981; }
#gp_foto_x25 { top: 33.6%; left: 83.2%; width: 6.8%; background: rgba(239, 68, 68, 0.65); border: 2px solid #ef4444; box-shadow: 0 0 18px #ef4444; }
#gp_foto_x26 { top: 33.6%; left: 70.0%; width: 6.8%; background: rgba(56, 189, 248, 0.65); border: 2px solid #38bdf8; box-shadow: 0 0 18px #38bdf8; }
#gp_foto_x27 { top: 24.0%; left: 76.6%; width: 6.8%; background: rgba(245, 158, 11, 0.65); border: 2px solid #f59e0b; box-shadow: 0 0 18px #f59e0b; }

.card-potencia {
  background: #FFFFFF;
  border: 1px solid #E2E8F0;
  border-radius: 12px;
  padding: 20px;
  max-width: 650px;
  margin: 0 auto;
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
  text-align: center;
}

.painel-custo-energia {
  display: flex;
  justify-content: space-around;
  align-items: center;
  background: #F8FAFC;
  border: 1px solid #E2E8F0;
  border-radius: 8px;
  padding: 12px 15px;
  margin-top: 15px;
  margin-bottom: 20px;
}

.box-indicador-custo {
  display: flex;
  flex-direction: column;
  align-items: center;
}

.rotulo-custo {
  font-size: 0.85rem;
  font-weight: bold;
  color: #64748B;
  text-transform: uppercase;
}

.valor-custo {
  font-family: 'Share Tech Mono', monospace;
  font-size: 1.5rem;
  font-weight: bold;
  color: #0F172A;
  margin-top: 4px;
}

.container-chartjs {
  position: relative;
  width: 100%;
  height: 260px;
  margin-top: 10px;
}

@media (max-width: 768px) {
  body { padding: 8px; }
  .container-principal { flex-direction: column; gap: 10px; }
  .menu-lateral { width: 100%; min-width: 100%; padding: 10px; }
  .btn-menu { margin-bottom: 5px; padding: 10px; }
  .cabecalho-industrial { font-size: 1.3rem; margin-bottom: 8px; }
  #container-stl-novo { height: 50vh; min-height: 320px; }
  .grid-leds { grid-template-columns: repeat(2, 1fr); }
  .display-digital-sobreposto { font-size: 3rem; }
  .painel-custo-energia { flex-direction: column; gap: 10px; }
}
</style>
</head>
<body>
<div class="container-principal">
  <div class="menu-lateral">
    <h3>Menu</h3>
    <button class="btn-menu ativo destaque" onclick="mudarTela('base_stl_nova', this)">Estrutura CAD 3D</button>
    <button class="btn-menu" onclick="mudarTela('telemetria', this)">Telemetria</button>
    <button class="btn-menu" onclick="mudarTela('medidor_potencia', this)">Potência Instantânea</button>
    <button class="btn-menu" onclick="mudarTela('pressao_real', this)">Transdutor de Pressão</button>
    <button class="btn-menu" onclick="mudarTela('monitor_entradas', this)">Entradas CLP</button>
    <button class="btn-menu" onclick="mudarTela('monitor_saidas', this)">Saídas CLP</button>
  </div>
  <div class="conteudo-principal">
    <div id="cabecalho" class="cabecalho-industrial">MONTAGEM CAD: GANTRY 3D</div>

    <div id="tela-base_stl_nova" class="tela ativa" style="position: relative;">
      <div id="container-stl-novo"></div>
    </div>

    <div id="tela-telemetria" class="tela">
      <div class="grid-telemetria">
        <div class="card-grafico"><div id="plotX" style="width: 100%; height: 250px;"></div></div>
        <div class="card-grafico"><div id="plotY" style="width: 100%; height: 250px;"></div></div>
        <div class="card-grafico"><div id="plotZ" style="width: 100%; height: 250px;"></div></div>
      </div>
      
      <div style="margin: 30px auto 0 auto; display: flex; align-items: center; justify-content: center; gap: 20px; background: #F8FAFC; padding: 22px 35px; border-radius: 12px; border: 2px solid #E2E8F0; width: fit-content; min-width: 360px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);">
        <div id="led_vacuo_telemetria" class="led-indicador led-off" style="width: 50px; height: 50px; margin: 0;"></div>
        <div>
          <div style="font-weight: bold; font-size: 1.3rem; color: #334155; letter-spacing: 0.5px;">VÁCUO (Saída Y3)</div>
          <div id="status_vacuo_texto" style="font-size: 1.1rem; font-weight: bold; color: #64748b; margin-top: 3px;">DESLIGADO</div>
        </div>
      </div>
    </div>

    <div id="tela-medidor_potencia" class="tela">
      <div class="card-potencia">
        <div id="gaugePotencia" style="width: 100%; height: 320px;"></div>

        <div class="painel-custo-energia">
          <div class="box-indicador-custo">
            <span class="rotulo-custo">Tarifa de Energia</span>
            <span class="valor-custo" style="color: #2563EB;">R$ 0,85 / kWh</span>
          </div>
          <div class="box-indicador-custo">
            <span class="rotulo-custo">Potência Atual</span>
            <span id="valor_potencia_kw_txt" class="valor-custo" style="color: #059669;">0.000 kW</span>
          </div>
          <div class="box-indicador-custo">
            <span class="rotulo-custo">Custo Estimado / Hora</span>
            <span id="valor_custo_hora_txt" class="valor-custo" style="color: #D97706;">R$ 0,000 / h</span>
          </div>
        </div>

        <h4 style="margin: 15px 0 5px 0; color: #334155; font-size: 1rem;">Distribuição de Potência por Atuador/Saída (Watts)</h4>
        <div class="container-chartjs">
          <canvas id="chartBarrasPotencia"></canvas>
        </div>
      </div>
    </div>

    <div id="tela-pressao_real" class="tela">
      <div class="wrapper-manometro-real">
        <img class="imagem-fundo-manometro" src="NOME_DA_IMAGEM_CHAVE" alt="Sensor Parker">
        <div id="valor-display-real" class="display-digital-sobreposto">0.00</div>
      </div>
    </div>

    <div id="tela-controle_bt" class="tela">
      <div class="card-controle">
        <div class="wrapper-controle-foto">
          <img class="imagem-fundo-controle" src="NOME_DA_IMAGEM_IPEGA" alt="Controle Ípega">
          
          <div id="gp_foto_x20" class="overlay-btn"></div>
          <div id="gp_foto_x21" class="overlay-btn"></div>
          <div id="gp_foto_x22" class="overlay-btn"></div>
          <div id="gp_foto_x23" class="overlay-btn"></div>

          <div id="gp_foto_x24" class="overlay-btn"></div>
          <div id="gp_foto_x25" class="overlay-btn"></div>
          <div id="gp_foto_x26" class="overlay-btn"></div>
          <div id="gp_foto_x27" class="overlay-btn"></div>
        </div>

        <div id="status_bt_texto" style="color: #94a3b8; font-family: monospace; margin-top: 15px; font-weight: bold; font-size: 1.1rem;">STATUS: AGUARDANDO COMANDO...</div>
      </div>
    </div>

    <div id="tela-monitor_entradas" class="tela">
      <div style="display: flex; gap: 30px; align-items: flex-start; flex-wrap: wrap;">
        
        <div style="flex: 1; min-width: 320px;">
          <div class="secao-titulo-entradas">Entradas (X0 - X7)</div>
          <div class="grid-leds" style="grid-template-columns: repeat(4, 1fr);">
            <div class="card-led"><span class="led-nome">X0</span><div id="led_X0" class="led-indicador led-off"></div><span id="txt_X0" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X1</span><div id="led_X1" class="led-indicador led-off"></div><span id="txt_X1" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X2</span><div id="led_X2" class="led-indicador led-off"></div><span id="txt_X2" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X3</span><div id="led_X3" class="led-indicador led-off"></div><span id="txt_X3" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X4</span><div id="led_X4" class="led-indicador led-off"></div><span id="txt_X4" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X5</span><div id="led_X5" class="led-indicador led-off"></div><span id="txt_X5" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X6</span><div id="led_X6" class="led-indicador led-off"></div><span id="txt_X6" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X7</span><div id="led_X7" class="led-indicador led-off"></div><span id="txt_X7" class="led-status-texto">OFF</span></div>
          </div>

          <div class="secao-titulo-entradas">Entradas (X20 - X27)</div>
          <div class="grid-leds" style="grid-template-columns: repeat(4, 1fr);">
            <div class="card-led"><span class="led-nome">X20</span><div id="led_X20" class="led-indicador led-off"></div><span id="txt_X20" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X21</span><div id="led_X21" class="led-indicador led-off"></div><span id="txt_X21" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X22</span><div id="led_X22" class="led-indicador led-off"></div><span id="txt_X22" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X23</span><div id="led_X23" class="led-indicador led-off"></div><span id="txt_X23" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X24</span><div id="led_X24" class="led-indicador led-off"></div><span id="txt_X24" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X25</span><div id="led_X25" class="led-indicador led-off"></div><span id="txt_X25" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X26</span><div id="led_X26" class="led-indicador led-off"></div><span id="txt_X26" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">X27</span><div id="led_X27" class="led-indicador led-off"></div><span id="txt_X27" class="led-status-texto">OFF</span></div>
          </div>
          
          <div class="painel-footer"><div id="modo_comunicacao_in" class="modo-badge">Conexão...</div></div>
        </div>

        <div style="flex: 1.2; min-width: 340px; background: #F8FAFC; padding: 15px; border-radius: 8px; border: 1px solid #E2E8F0;">
          <h3 style="margin-top: 0; color: #334155; font-size: 1.1rem; border-bottom: 2px solid #CBD5E1; padding-bottom: 8px;">Mapeamento de Entradas</h3>
          
          <table style="width: 100%; border-collapse: collapse; font-size: 0.88rem; text-align: left;">
            <thead>
              <tr style="background: #E2E8F0; color: #334155;">
                <th style="padding: 8px; border-radius: 4px 0 0 4px;">Tag</th>
                <th style="padding: 8px;">Sinal Físico</th>
                <th style="padding: 8px; border-radius: 0 4px 4px 0;">Descrição / Função</th>
              </tr>
            </thead>
            <tbody style="color: #475569;">
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X0</td>
                <td style="padding: 6px 8px;"><span style="background: #E0F2FE; color: #0369A1; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Fim de Curso</span></td>
                <td style="padding: 6px 8px;">Sensor Recuo Eixo X (X-)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X1</td>
                <td style="padding: 6px 8px;"><span style="background: #FEF3C7; color: #92400E; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Comando</span></td>
                <td style="padding: 6px 8px;">Botão de Homing</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X2</td>
                <td style="padding: 6px 8px;"><span style="background: #FEE2E2; color: #991B1B; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Segurança</span></td>
                <td style="padding: 6px 8px;">Botão de Emergência</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X3</td>
                <td style="padding: 6px 8px;"><span style="background: #E0F2FE; color: #0369A1; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Fim de Curso</span></td>
                <td style="padding: 6px 8px;">Sensor Avanço Eixo X (X+)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X4</td>
                <td style="padding: 6px 8px;"><span style="background: #E0F2FE; color: #0369A1; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Fim de Curso</span></td>
                <td style="padding: 6px 8px;">Sensor Avanço Eixo Y (Y+)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X5</td>
                <td style="padding: 6px 8px;"><span style="background: #E0F2FE; color: #0369A1; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Fim de Curso</span></td>
                <td style="padding: 6px 8px;">Sensor Recuo Eixo Y (Y-)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X6</td>
                <td style="padding: 6px 8px;"><span style="background: #E0F2FE; color: #0369A1; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Fim de Curso</span></td>
                <td style="padding: 6px 8px;">Sensor Eixo Z Recuado / Superior (Z-)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X7</td>
                <td style="padding: 6px 8px;"><span style="background: #E0F2FE; color: #0369A1; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Fim de Curso</span></td>
                <td style="padding: 6px 8px;">Sensor Eixo Z Avançado / Inferior (Z+)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X20</td>
                <td style="padding: 6px 8px;"><span style="background: #FEF3C7; color: #92400E; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Comando</span></td>
                <td style="padding: 6px 8px;">DPAD Cima (Recuo Eixo X)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X21</td>
                <td style="padding: 6px 8px;"><span style="background: #FEF3C7; color: #92400E; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Comando</span></td>
                <td style="padding: 6px 8px;">DPAD Esquerda (Recuo Eixo Y)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X22</td>
                <td style="padding: 6px 8px;"><span style="background: #FEF3C7; color: #92400E; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Comando</span></td>
                <td style="padding: 6px 8px;">DPAD Baixo (Avanço Eixo X)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X23</td>
                <td style="padding: 6px 8px;"><span style="background: #FEF3C7; color: #92400E; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Comando</span></td>
                <td style="padding: 6px 8px;">DPAD Direita (Avanço Eixo Y)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X24</td>
                <td style="padding: 6px 8px;"><span style="background: #FEF3C7; color: #92400E; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Comando</span></td>
                <td style="padding: 6px 8px;">Botão A (Avanço/Recuo Eixo Z)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X25</td>
                <td style="padding: 6px 8px;"><span style="background: #FEF3C7; color: #92400E; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Comando</span></td>
                <td style="padding: 6px 8px;">Botão B (Liga/Desliga Vácuo)</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X26</td>
                <td style="padding: 6px 8px;"><span style="background: #F1F5F9; color: #475569; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Comando</span></td>
                <td style="padding: 6px 8px;">Botão X</td>
              </tr>
              <tr>
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">X27</td>
                <td style="padding: 6px 8px;"><span style="background: #F1F5F9; color: #475569; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Comando</span></td>
                <td style="padding: 6px 8px;">Botão Y</td>
              </tr>
            </tbody>
          </table>
        </div>

      </div>
    </div>

    <div id="tela-monitor_saidas" class="tela">
      <div style="display: flex; gap: 30px; align-items: flex-start; flex-wrap: wrap;">
        
        <div style="flex: 1; min-width: 320px;">
          <div class="secao-titulo-entradas">Saídas (Y0 - Y7)</div>
          <div class="grid-leds" style="grid-template-columns: repeat(4, 1fr);">
            <div class="card-led"><span class="led-nome">Y0</span><div id="led_Y0" class="led-indicador led-off"></div><span id="txt_Y0" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">Y1</span><div id="led_Y1" class="led-indicador led-off"></div><span id="txt_Y1" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">Y2</span><div id="led_Y2" class="led-indicador led-off"></div><span id="txt_Y2" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">Y3</span><div id="led_Y3" class="led-indicador led-off"></div><span id="txt_Y3" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">Y4</span><div id="led_Y4" class="led-indicador led-off"></div><span id="txt_Y4" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">Y5</span><div id="led_Y5" class="led-indicador led-off"></div><span id="txt_Y5" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">Y6</span><div id="led_Y6" class="led-indicador led-off"></div><span id="txt_Y6" class="led-status-texto">OFF</span></div>
            <div class="card-led"><span class="led-nome">Y7</span><div id="led_Y7" class="led-indicador led-off"></div><span id="txt_Y7" class="led-status-texto">OFF</span></div>
          </div>

          <div class="painel-footer"><div id="modo_comunicacao_out" class="modo-badge">Conexão...</div></div>
        </div>

        <div style="flex: 1.2; min-width: 340px; background: #F8FAFC; padding: 15px; border-radius: 8px; border: 1px solid #E2E8F0;">
          <h3 style="margin-top: 0; color: #334155; font-size: 1.1rem; border-bottom: 2px solid #CBD5E1; padding-bottom: 8px;">Mapeamento de Saídas (Y0 - Y7)</h3>
          
          <table style="width: 100%; border-collapse: collapse; font-size: 0.88rem; text-align: left;">
            <thead>
              <tr style="background: #E2E8F0; color: #334155;">
                <th style="padding: 8px; border-radius: 4px 0 0 4px;">Tag</th>
                <th style="padding: 8px;">Tipo Atuador</th>
                <th style="padding: 8px; border-radius: 0 4px 4px 0;">Descrição / Função</th>
              </tr>
            </thead>
            <tbody style="color: #475569;">
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">Y0</td>
                <td style="padding: 6px 8px;"><span style="background: #FFEDD5; color: #C2410C; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Driver / Pulso</span></td>
                <td style="padding: 6px 8px;">Saída de pulsos para motor de passo</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">Y1</td>
                <td style="padding: 6px 8px;"><span style="background: #E0F2FE; color: #0369A1; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Pneumático</span></td>
                <td style="padding: 6px 8px;">Acionamento de avanço do eixo Y</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">Y2</td>
                <td style="padding: 6px 8px;"><span style="background: #E0F2FE; color: #0369A1; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Pneumático</span></td>
                <td style="padding: 6px 8px;">Acionamento de avanço/recuo do eixo Z</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">Y3</td>
                <td style="padding: 6px 8px;"><span style="background: #DCFCE7; color: #15803D; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Vácuo</span></td>
                <td style="padding: 6px 8px;">Acionamento de liga/desliga vácuo</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">Y4</td>
                <td style="padding: 6px 8px;"><span style="background: #FFEDD5; color: #C2410C; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Driver / Direção</span></td>
                <td style="padding: 6px 8px;">Saída de acionamento de direção (DIR-) do motor de passo</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">Y5</td>
                <td style="padding: 6px 8px;"><span style="background: #FFEDD5; color: #C2410C; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Driver / Habilitação</span></td>
                <td style="padding: 6px 8px;">Saída de acionamento de enable (ENA-) do motor de passo</td>
              </tr>
              <tr style="border-bottom: 1px solid #E2E8F0;">
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">Y6</td>
                <td style="padding: 6px 8px;"><span style="background: #F1F5F9; color: #475569; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Reserva</span></td>
                <td style="padding: 6px 8px;">Reservado</td>
              </tr>
              <tr>
                <td style="padding: 6px 8px; font-weight: bold; font-family: monospace;">Y7</td>
                <td style="padding: 6px 8px;"><span style="background: #F1F5F9; color: #475569; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem;">Reserva</span></td>
                <td style="padding: 6px 8px;">Reservado</td>
              </tr>
            </tbody>
          </table>
        </div>

      </div>
    </div>

<script>
let y2Ligado = false;
let telaAtual = 'base_stl_nova';

function mudarTela(tela, botao) {
    telaAtual = tela;
    document.querySelectorAll('.tela').forEach($t => $t.classList.remove('ativa'));
    document.querySelectorAll('.btn-menu').forEach(b => b.classList.remove('ativo'));
    botao.classList.add('ativo');

    if(tela === 'base_stl_nova') {
        document.getElementById('tela-base_stl_nova').classList.add('ativa');
        document.getElementById('cabecalho').innerText = "DIGITAL TWIN: ROBÔ CARTESIANO";
        redimensionarThreeJS();
    } else if(tela === 'telemetria') {
        document.getElementById('tela-telemetria').classList.add('ativa');
        document.getElementById('cabecalho').innerText = "MONITORAMENTO: POSIÇÃO DOS EIXOS";
        setTimeout(() => { Plotly.Plots.resize('plotX'); Plotly.Plots.resize('plotY'); Plotly.Plots.resize('plotZ'); }, 10);
    } else if(tela === 'medidor_potencia') {
        document.getElementById('tela-medidor_potencia').classList.add('ativa');
        document.getElementById('cabecalho').innerText = "MONITOR DE POTÊNCIA INSTANTÂNEA";
        setTimeout(() => { Plotly.Plots.resize('gaugePotencia'); }, 10);
    } else if(tela === 'pressao_real') {
        document.getElementById('tela-pressao_real').classList.add('ativa');
        document.getElementById('cabecalho').innerText = "LEITURA TRANSDUTOR DE PRESSÃO";
    } else if(tela === 'controle_bt') {
        document.getElementById('tela-controle_bt').classList.add('ativa');
        document.getElementById('cabecalho').innerText = "PAINEL INTERATIVO: CONTROLE BLUETOOTH";
    } else if(tela === 'monitor_entradas') {
        document.getElementById('tela-monitor_entradas').classList.add('ativa');
        document.getElementById('cabecalho').innerText = "DIAGNÓSTICO DIGITAL: ENTRADAS X0-X7 E X20-X27";
    } else if(tela === 'monitor_saidas') {
        document.getElementById('tela-monitor_saidas').classList.add('ativa');
        document.getElementById('cabecalho').innerText = "DIAGNÓSTICO DIGITAL: SAÍDAS Y0 - Y7";
    }
}

// THREE.JS SCENE SETUP
const containerNovo = document.getElementById('container-stl-novo');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0f172a);
const camera = new THREE.PerspectiveCamera(45, containerNovo.clientWidth / containerNovo.clientHeight, 0.5, 10000);
camera.position.set(1380, 1280, 1450);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(containerNovo.clientWidth, containerNovo.clientHeight);
renderer.shadowMap.enabled = true;
containerNovo.appendChild(renderer.domElement);
const controls = new THREE.OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

const ambientLight = new THREE.AmbientLight(0xffffff, 0.85);
scene.add(ambientLight);
const dirLight1 = new THREE.DirectionalLight(0xffffff, 1.0);
dirLight1.position.set(300, 500, 400);
scene.add(dirLight1);
const dirLight2 = new THREE.DirectionalLight(0x38bdf8, 0.4);
dirLight2.position.set(-300, 200, -400);
scene.add(dirLight2);

const gridHelper = new THREE.GridHelper(2000, 40, 0x38bdf8, 0x334155);
gridHelper.position.y = -10;
scene.add(gridHelper);

const tamanhoEixo = 150;
const posX = -500, posY = 0, posZ = 500;
const axesHelper = new THREE.AxesHelper(tamanhoEixo);
axesHelper.position.set(posX, posY, posZ);
scene.add(axesHelper);

function criarRotuloTexto(texto, cor) {
    const canvas = document.createElement('canvas');
    canvas.width = 256; canvas.height = 256;
    const ctx = canvas.getContext('2d');
    ctx.font = 'Bold 160px Arial';
    ctx.fillStyle = cor;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(texto, 128, 128);
    const texture = new THREE.CanvasTexture(canvas);
    const spriteMaterial = new THREE.SpriteMaterial({ map: texture });
    const sprite = new THREE.Sprite(spriteMaterial);
    sprite.scale.set(80, 80, 1); 
    return sprite;
}

const labelY = criarRotuloTexto('Y', '#FF4444');
labelY.position.set(posX + tamanhoEixo + 30, posY, posZ);
scene.add(labelY);

const labelZ = criarRotuloTexto('Z', '#44FF44');
labelZ.position.set(posX, posY + tamanhoEixo + 30, posZ);
scene.add(labelZ);

const labelX = criarRotuloTexto('X', '#4488FF');
labelX.position.set(posX, posY, posZ + tamanhoEixo + 30);
scene.add(labelX);

const gantryGroup = new THREE.Group();
scene.add(gantryGroup);

let meshBase = null, meshCarrox = null, meshCarroY = null, meshCarroz = null;

const loaderBase = new THREE.STLLoader();
loaderBase.load('STL_BASE_NOME', function (geometryBase) {
    const materialBase = new THREE.MeshStandardMaterial({ color: 0x94a3b8, roughness: 0.3, metalness: 0.7 });
    geometryBase.computeVertexNormals();
    meshBase = new THREE.Mesh(geometryBase, materialBase);
    gantryGroup.add(meshBase);

    const loaderCarroX = new THREE.STLLoader();
    loaderCarroX.load('STL_CARRO_X_NOME', function (geometryCarroX) {
        const materialCarroX = new THREE.MeshStandardMaterial({ color: 0x38bdf8, roughness: 0.2, metalness: 0.8 });
        geometryCarroX.computeVertexNormals();
        meshCarrox = new THREE.Mesh(geometryCarroX, materialCarroX);
        meshCarrox.position.x += VAR_OFFSET_X_X;
        meshCarrox.position.y += VAR_OFFSET_Y_X;
        meshCarrox.position.z += VAR_OFFSET_Z_X;
        gantryGroup.add(meshCarrox);

        const loaderCarroy = new THREE.STLLoader();
        loaderCarroy.load('STL_CARRO_Y_NOME', function (geometryCarroY) {
            const materialCarroY = new THREE.MeshStandardMaterial({ color: 0x10b981, roughness: 0.2, metalness: 0.8 });
            geometryCarroY.computeVertexNormals();
            meshCarroY = new THREE.Mesh(geometryCarroY, materialCarroY);
            meshCarroY.position.x += VAR_OFFSET_X_Y;
            meshCarroY.position.y += VAR_OFFSET_Y_Y;
            meshCarroY.position.z += VAR_OFFSET_Z_Y;
            meshCarroY.rotation.x = VAR_ROT_X_Y * (Math.PI / 180);
            meshCarroY.rotation.y = VAR_ROT_Y_Y * (Math.PI / 180);
            meshCarroY.rotation.z = VAR_ROT_Z_Y * (Math.PI / 180);
            gantryGroup.add(meshCarroY);

            const loaderCarroz = new THREE.STLLoader();
            loaderCarroz.load('STL_CARRO_Z_NOME', function (geometryCarroz) {
                const materialCarroz = new THREE.MeshStandardMaterial({ color: 0xf59e0b, roughness: 0.2, metalness: 0.8 });
                geometryCarroz.computeVertexNormals();
                meshCarroz = new THREE.Mesh(geometryCarroz, materialCarroz);
                meshCarroz.position.x += VAR_OFFSET_X_Z;
                meshCarroz.position.y += VAR_OFFSET_Y_Z;
                meshCarroz.position.z += VAR_OFFSET_Z_Z;
                meshCarroz.rotation.x = VAR_ROT_X_Z * (Math.PI / 180);
                meshCarroz.rotation.y = VAR_ROT_Y_Z * (Math.PI / 180);
                meshCarroz.rotation.z = VAR_ROT_Z_Z * (Math.PI / 180);
                gantryGroup.add(meshCarroz);

                const box = new THREE.Box3().setFromObject(gantryGroup);
                const center = box.getCenter(new THREE.Vector3());
                const size = box.getSize(new THREE.Vector3());
                gantryGroup.position.x -= center.x;
                gantryGroup.position.z -= center.z;
                gantryGroup.position.y = -box.min.y;
                controls.target.set(0, size.y / 2, 0);
                controls.update();
            });
        });
    });
});

let deslocamentoAtualZ = 0; 
const cursoMaximoZ = 155; 
const velocidadeZ = 1.0;   

let y0Ligado = false;
let y4Ligado = false;
let deslocamentoAtualX = 0;
let alvoX = 0;
const cursoMaximoX = 430; 
const velocidadeX = 1.0;   

let y1Ligado = false;
let deslocamentoAtualY = 0;
let alvoY = 0;
const cursoMaximoY = 430; 
const velocidadeY = 2.5;   

let amostraCount = 0;
const maxPontos = 50;

function atualizarTelemetria() {
    const agora = new Date(); // Objeto Date com suporte completo a milissegundos

    Plotly.extendTraces('plotX', { x: [[agora]], y: [[deslocamentoAtualX]] }, [0], 100);
    Plotly.extendTraces('plotY', { x: [[agora]], y: [[deslocamentoAtualY]] }, [0], 100);
    Plotly.extendTraces('plotZ', { x: [[agora]], y: [[deslocamentoAtualZ]] }, [0], 100);
    
    // Atualiza o texto do eixo X com a hora atual
    const textoHorario = `Horário (${obterHorarioAtualFormatado()})`;
    Plotly.relayout('plotX', { 'xaxis.title.text': textoHorario });
    Plotly.relayout('plotY', { 'xaxis.title.text': textoHorario });
    Plotly.relayout('plotZ', { 'xaxis.title.text': textoHorario });
    
}

setInterval(atualizarTelemetria, 100);

function animateThreeJS() {
    requestAnimationFrame(animateThreeJS);

    let alvoZ = y2Ligado ? cursoMaximoZ : 0;
    if (Math.abs(alvoZ - deslocamentoAtualZ) > velocidadeZ) {
        if (deslocamentoAtualZ < alvoZ) {
            deslocamentoAtualZ += velocidadeZ;
        } else {
            deslocamentoAtualZ -= velocidadeZ;
        }
    } else {
        deslocamentoAtualZ = alvoZ;
    }

    if (y0Ligado && y4Ligado) {
        alvoX = cursoMaximoX; 
    } else if (y0Ligado && !y4Ligado) {
        alvoX = 0;            
    }

    if (Math.abs(alvoX - deslocamentoAtualX) > velocidadeX) {
        if (deslocamentoAtualX < alvoX) {
            deslocamentoAtualX += velocidadeX;
        } else {
            deslocamentoAtualX -= velocidadeX;
        }
    } else {
        deslocamentoAtualX = alvoX;
    }

    alvoY = y1Ligado ? cursoMaximoY : 0;

    if (Math.abs(alvoY - deslocamentoAtualY) > velocidadeY) {
        if (deslocamentoAtualY < alvoY) {
            deslocamentoAtualY += velocidadeY;
        } else {
            deslocamentoAtualY -= velocidadeY;
        }
    } else {
        deslocamentoAtualY = alvoY;
    }

    if (meshCarroz) {
        meshCarroz.position.y = (VAR_OFFSET_Y_Z - deslocamentoAtualZ);
    }

    if (meshCarrox) {
        meshCarrox.position.x = VAR_OFFSET_X_X;
        meshCarrox.position.z = VAR_OFFSET_Z_X + deslocamentoAtualX;
    }
    
    if (meshCarroY) {
        meshCarroY.position.x = VAR_OFFSET_X_Y + deslocamentoAtualY;
        meshCarroY.position.z = VAR_OFFSET_Z_Y + deslocamentoAtualX;
    }
    if (meshCarroz) {
        meshCarroz.position.x = VAR_OFFSET_X_Z + deslocamentoAtualY;
        meshCarroz.position.z = VAR_OFFSET_Z_Z + deslocamentoAtualX;
    }

    controls.update();
    renderer.render(scene, camera);
}
animateThreeJS();

function redimensionarThreeJS() {
    if(!containerNovo) return;
    setTimeout(() => {
        camera.aspect = containerNovo.clientWidth / containerNovo.clientHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(containerNovo.clientWidth, containerNovo.clientHeight);
    }, 100);
}
window.addEventListener('resize', redimensionarThreeJS);

// 1. Função para formatar o horário atual em "HH:MMh"
function obterHorarioAtualFormatado() {
    const agora = new Date();
    const horas = String(agora.getHours()).padStart(2, '0');
    const minutos = String(agora.getMinutes()).padStart(2, '0');
    return `${horas}:${minutos}h`;
}

const layoutPadrao = {
    title: { text: 'Posição Eixo Z (mm)' },
    yaxis: { title: { text: 'mm' } },
    xaxis: {
        type: 'date', // Habilita a interpolação temporal contínua
        tickformat: '%S s', // Exibe apenas os segundos (ex: 35s, 36s, 37s...)
        nticks: 6, // Limita a quantidade de rótulos na tela para não poluir
        title: {
            text: 'Horário (--:--h)',
            standoff: 15 // Empurra o título "Horário" mais para baixo, liberando os números
        }
    },
    margin: { t: 40, b: 60, l: 50, r: 20 }
};

Plotly.newPlot('plotX', [{x: [], y: [], mode: 'lines', line: {color: '#38bdf8', width: 2.5}}], { ...layoutPadrao, title: { text: 'Posição Eixo X (mm)' }, yaxis: { range: [-10, 500], title: { text: 'mm' } } });
Plotly.newPlot('plotY', [{x: [], y: [], mode: 'lines', line: {color: '#10b981', width: 2.5}}], { ...layoutPadrao, title: { text: 'Posição Eixo Y (mm)' }, yaxis: { range: [-10, 500], title: { text: 'mm' } } });
Plotly.newPlot('plotZ', [{x: [], y: [], mode: 'lines', line: {color: '#f59e0b', width: 2.5}}], { ...layoutPadrao, title: { text: 'Posição Eixo Z (mm)' }, yaxis: { range: [-10, 200], title: { text: 'mm' } } });

const dataGaugePotencia = [
  {
    type: "indicator",
    mode: "gauge+number",
    value: 0.0,
    number: { suffix: " W", font: { size: 28, family: "Arial", color: "#1e293b" }, valueformat: ".1f" },
    gauge: {
      axis: { range: [0, 300], tickwidth: 1, tickcolor: "#475569" },
      bar: { color: "#1e293b", width: 4 },
      bgcolor: "white",
      borderwidth: 2,
      bordercolor: "#cbd5e1",
      steps: [
        { range: [0, 60], color: "#006837" },
        { range: [60, 120], color: "#31a354" },
        { range: [120, 200], color: "#fec44f" },
        { range: [200, 300], color: "#de2d26" }
      ]
    }
  }
];

const layoutGaugePotencia = {
  title: { text: "Potência Instantânea", font: { size: 18, family: "Arial", weight: "bold" } },
  margin: { t: 40, r: 35, l: 35, b: 20 }
};

Plotly.newPlot('gaugePotencia', dataGaugePotencia, layoutGaugePotencia);

const ctxBarras = document.getElementById('chartBarrasPotencia').getContext('2d');
const chartBarrasPotencia = new Chart(ctxBarras, {
    type: 'bar',
    data: {
        labels: ['Eletrônica Base', 'Motor X (Y0/Y4/Y5)', 'Solenoide Y (Y1)', 'Solenoide Z (Y2)', 'Vácuo (Y3)'],
        datasets: [{
            label: 'Potência Instantânea (W)',
            data: [18.0, 0.0, 0.0, 0.0, 0.0],
            backgroundColor: [
                'rgba(148, 163, 184, 0.85)',
                'rgba(56, 189, 248, 0.85)',
                'rgba(16, 185, 129, 0.85)',
                'rgba(245, 158, 11, 0.85)',
                'rgba(239, 68, 68, 0.85)'
            ],
            borderColor: [
                '#64748b',
                '#0284c7',
                '#059669',
                '#d97706',
                '#dc2626'
            ],
            borderWidth: 1.5,
            borderRadius: 4
        }]
    },
    options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 150 },
        scales: {
            x: { grid: { display: false } },
            y: {
                beginAtZero: true,
                max: 60,
                ticks: { stepSize: 10 },
                title: { display: true, text: 'Watts (W)' }
            }
        },
        plugins: { legend: { display: false } }
    }
});

const TARIFA_KWH = 0.85;
let buscandoDados = false;

function consultarCLP() {
    if (buscandoDados) return;
    buscandoDados = true;

    fetch('/dados_clp')
    .then(response => response.json())
    .then(dados => {
        buscandoDados = false;
        const modoIn = document.getElementById('modo_comunicacao_in');
        const modoOut = document.getElementById('modo_comunicacao_out');
        const txtStatus = "Canal: " + dados.status_comunicacao;
        if (modoIn) modoIn.innerText = txtStatus;
        if (modoOut) modoOut.innerText = txtStatus;

        const erroConexao = (dados.status_comunicacao === "ERRO_PORTA" || dados.status_comunicacao === "SEM_RESPOSTA_CLP");

        const vPressao = (dados.pressao_bar !== undefined) ? dados.pressao_bar : 0.0;
        const vPotencia = (dados.potencia_w !== undefined) ? dados.potencia_w : 0.0;
        
        Plotly.restyle('gaugePotencia', 'value', [vPotencia]);

        const potenciaKw = vPotencia / 1000.0;
        const custoHora = potenciaKw * TARIFA_KWH;

        const elemKw = document.getElementById('valor_potencia_kw_txt');
        if (elemKw) elemKw.innerText = potenciaKw.toFixed(3) + " kW";

        const elemCusto = document.getElementById('valor_custo_hora_txt');
        if (elemCusto) elemCusto.innerText = "R$ " + custoHora.toFixed(3) + " / h";

        let pBase = 18.0;
        let pMotorX = 0.0;
        let pSolenoideY = 0.0;
        let pSolenoideZ = 0.0;
        let pVacuoY3 = 0.0;

        if (dados.saidas && !erroConexao) {
            const driverDesabilitado = dados.saidas.Y5 || false;
            const emMovimento = dados.saidas.Y0 || false;
            const emEmergencia = dados.entradas ? !dados.entradas.X2 : false;

            if (!driverDesabilitado && !emEmergencia) {
                pMotorX = emMovimento ? 35.0 : 15.0;
            }

            if (dados.saidas.Y1) pSolenoideY = 4.8;
            if (dados.saidas.Y2) pSolenoideZ = 4.8;
            if (dados.saidas.Y3) pVacuoY3 = 4.8;

            const relesAtivos = [dados.saidas.Y0, dados.saidas.Y1, dados.saidas.Y2, dados.saidas.Y3, dados.saidas.Y4, dados.saidas.Y5].filter(Boolean).length;
            pBase += relesAtivos * 0.5;
        }

        chartBarrasPotencia.data.datasets[0].data = [pBase, pMotorX, pSolenoideY, pSolenoideZ, pVacuoY3];
        chartBarrasPotencia.update();

        const elemDisplay = document.getElementById('valor-display-real');
        if (elemDisplay) {
            elemDisplay.innerText = vPressao.toFixed(2);
        }

        if (!erroConexao) {
            if (dados.saidas) {
                if (dados.saidas.Y0 !== undefined) y0Ligado = dados.saidas.Y0;
                if (dados.saidas.Y1 !== undefined) y1Ligado = dados.saidas.Y1;
                if (dados.saidas.Y4 !== undefined) y4Ligado = dados.saidas.Y4;
                if (dados.saidas.Y2 !== undefined) y2Ligado = dados.saidas.Y2;

                if (dados.saidas.Y3 !== undefined) {
                    const vacuoAtivo = dados.saidas.Y3;
                    const ledVacuo = document.getElementById('led_vacuo_telemetria');
                    const txtVacuo = document.getElementById('status_vacuo_texto');
                    
                    if (ledVacuo && txtVacuo) {
                        if (vacuoAtivo) {
                            ledVacuo.className = "led-indicador led-on-entrada";
                            txtVacuo.innerText = "LIGADO (ON)";
                            txtVacuo.style.color = "#10b981";
                        } else {
                            ledVacuo.className = "led-indicador led-off";
                            txtVacuo.innerText = "DESLIGADO (OFF)";
                            txtVacuo.style.color = "#64748b";
                        }
                    }
                }
            }
        }

        const entradasParaAtualizar = [0,1,2,3,4,5,6,7, 20,21,22,23,24,25,26,27];

        let comandoAtivoTexto = "AGUARDANDO COMANDO...";
        entradasParaAtualizar.forEach(i => {
            const tagId = "X" + i;
            const circulo = document.getElementById("led_" + tagId);
            const texto = document.getElementById("txt_" + tagId);
            const ativo = (dados.entradas && !erroConexao) ? dados.entradas[tagId] : false;

            if (circulo && texto) {
                if (erroConexao) {
                    circulo.className = "led-indicador led-erro";
                    texto.innerText = "ERRO";
                    texto.style.color = "#ef4444";
                } else {
                    if (ativo) {
                        circulo.className = "led-indicador led-on-entrada";
                        texto.innerText = "ON";
                        texto.style.color = "#10b981";
                    } else {
                        circulo.className = "led-indicador led-off";
                        texto.innerText = "OFF";
                        texto.style.color = "#64748b";
                    }
                }
            }

            if (i >= 20 && i <= 27) {
                const elemGlowFoto = document.getElementById("gp_foto_x" + i);
                if (elemGlowFoto) {
                    if (ativo) {
                        elemGlowFoto.classList.add("ativo");
                    } else {
                        elemGlowFoto.classList.remove("ativo");
                    }
                }
            }
        });

        if (dados.entradas) {
            if (dados.entradas.X20) comandoAtivoTexto = "DPAD CIMA (RECUO X)";
            else if (dados.entradas.X21) comandoAtivoTexto = "DPAD ESQUERDA (RECUO Y)";
            else if (dados.entradas.X22) comandoAtivoTexto = "DPAD BAIXO (AVANÇO X)";
            else if (dados.entradas.X23) comandoAtivoTexto = "DPAD DIREITA (AVANÇO Y)";
            else if (dados.entradas.X24) comandoAtivoTexto = "BOTÃO A (EIXO Z)";
            else if (dados.entradas.X25) comandoAtivoTexto = "BOTÃO B (VÁCUO)";
            else if (dados.entradas.X26) comandoAtivoTexto = "BOTÃO X";
            else if (dados.entradas.X27) comandoAtivoTexto = "BOTÃO Y";
        }
        
        const txtBt = document.getElementById("status_bt_texto");
        if (txtBt) {
            txtBt.innerText = "STATUS: " + comandoAtivoTexto;
            txtBt.style.color = (comandoAtivoTexto !== "AGUARDANDO COMANDO...") ? "#10b981" : "#94a3b8";
        }

        for (let i = 0; i <= 7; i++) {
            const tagId = "Y" + i;
            const circulo = document.getElementById("led_" + tagId);
            const texto = document.getElementById("txt_" + tagId);
            if (circulo && texto) {
                if (erroConexao) {
                    circulo.className = "led-indicador led-erro";
                    texto.innerText = "ERRO";
                    texto.style.color = "#ef4444";
                } else {
                    const ativo = dados.saidas ? dados.saidas[tagId] : false;
                    if (ativo) {
                        circulo.className = "led-indicador led-on-saida";
                        texto.innerText = "ON";
                        texto.style.color = "#f97316";
                    } else {
                        circulo.className = "led-indicador led-off";
                        texto.innerText = "OFF";
                        texto.style.color = "#64748b";
                    }
                }
            }
        }
    })
    .catch(err => {
        buscandoDados = false;
        console.log("Servidor temporariamente indisponível...");
    });
}
setInterval(consultarCLP, 80);
</script>
</body>
</html>
"""

# Substituições dinâmicas no HTML
html_content = (
    html_base.replace("NOME_DA_IMAGEM_CHAVE", nome_imagem_parker)
    .replace("NOME_DA_IMAGEM_IPEGA", nome_imagem_ipega)
    .replace("STL_BASE_NOME", nome_stl_base)
    .replace("STL_CARRO_X_NOME", nome_stl_carro_x)
    .replace("STL_CARRO_Y_NOME", nome_stl_carro_y)
    .replace("STL_CARRO_Z_NOME", nome_stl_carro_z)
    .replace("VAR_OFFSET_X_X", str(OFFSET_X_CARRO_X))
    .replace("VAR_OFFSET_Y_X", str(OFFSET_Y_CARRO_X))
    .replace("VAR_OFFSET_Z_X", str(OFFSET_Z_CARRO_X))
    .replace("VAR_OFFSET_X_Y", str(OFFSET_X_CARRO_Y))
    .replace("VAR_OFFSET_Y_Y", str(OFFSET_Y_CARRO_Y))
    .replace("VAR_OFFSET_Z_Y", str(OFFSET_Z_CARRO_Y))
    .replace("VAR_ROT_X_Y", str(ROTACAO_X_CARRO_Y))
    .replace("VAR_ROT_Y_Y", str(ROTACAO_Y_CARRO_Y))
    .replace("VAR_ROT_Z_Y", str(ROTACAO_Z_CARRO_Y))
    .replace("VAR_OFFSET_X_Z", str(OFFSET_X_CARRO_Z))
    .replace("VAR_OFFSET_Y_Z", str(OFFSET_Y_CARRO_Z))
    .replace("VAR_OFFSET_Z_Z", str(OFFSET_Z_CARRO_Z))
    .replace("VAR_ROT_X_Z", str(ROTACAO_X_CARRO_Z))
    .replace("VAR_ROT_Y_Z", str(ROTACAO_Y_CARRO_Z))
    .replace("VAR_ROT_Z_Z", str(ROTACAO_Z_CARRO_Z))
)

with open(html_filename, "w", encoding="utf-8") as f:
    f.write(html_content)


# ==========================================
# SERVIDOR HTTP LOCAL INTEGRADO OTIMIZADO
# ==========================================
class CustomCombinedHTTPRequestHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        return

    def do_GET(self):
        path_bruto = self.path.split("?")[0].lstrip("/")
        path_limpo = urllib.parse.unquote(path_bruto)
        diretorio_atual = (
            os.path.dirname(os.path.abspath(__file__))
            if "__file__" in globals()
            else ""
        )
        caminho_arquivo_local = os.path.join(diretorio_atual, path_limpo)

        if path_limpo == "dados_clp":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            
            with dados_lock:
                payload = {
                    "entradas": ESTADO_ENTRADAS.copy(),
                    "saidas": ESTADO_SAIDAS.copy(),
                    "pressao_bar": VALOR_PRESSAO_BAR,
                    "potencia_w": POTENCIA_KW,
                    "status_comunicacao": STATUS_COMUNICACAO,
                }
            
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        elif path_limpo in [nome_imagem_parker, nome_imagem_ipega]:
            if os.path.exists(caminho_arquivo_local):
                self.send_response(200)
                ext = "png" if path_limpo.endswith(".png") else "jpeg"
                self.send_header("Content-type", f"image/{ext}")
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                with open(caminho_arquivo_local, "rb") as img_file:
                    self.wfile.write(img_file.read())
            else:
                self.send_response(404)
                self.end_headers()

        elif path_limpo.endswith(".stl"):
            if os.path.exists(caminho_arquivo_local):
                self.send_response(200)
                self.send_header("Content-type", "model/stl")
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                with open(caminho_arquivo_local, "rb") as stl_file:
                    self.wfile.write(stl_file.read())
            else:
                print(f"\n[ERRO 404] Arquivo não encontrado: {caminho_arquivo_local}")
                self.send_response(404)
                self.end_headers()

        elif path_limpo == "" or path_limpo == "index.html":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_content.encode("utf-8"))

        else:
            self.send_response(404)
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
porta_servidor = 8085


def rodar_servidor():
    server_address = ("0.0.0.0", porta_servidor)
    httpd = HTTPServer(server_address, CustomCombinedHTTPRequestHandler)
    httpd.serve_forever()


# ==========================================
# INICIALIZAÇÃO DO SISTEMA
# ==========================================
if __name__ == "__main__":
    if not PYSERIAL_DISPONIVEL:
        USAR_CLP_REAL = False

    thread_clp = threading.Thread(
        target=worker_leitura_clp, name="CLP_Thread", daemon=True
    )
    thread_clp.start()

    thread_web = threading.Thread(
        target=rodar_servidor, name="HTTP_Thread", daemon=True
    )
    thread_web.start()

    print("-" * 60)
    print("SISTEMA GANTRY 3D COM MEDIDOR DE POTÊNCIA INTEGRADO!")
    print(f"Acesse: http://localhost:{porta_servidor}")
    print("-" * 60)

    time.sleep(1)
    webbrowser.open(f"http://localhost:{porta_servidor}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sys.exit(0)