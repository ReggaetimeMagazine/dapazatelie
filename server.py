c:\Users\coyot\Downloads\painel.html c:\Users\coyot\Downloads\server.py#!/usr/bin/env python3
"""
COYOTE CRYPTO - BACKEND SEGURO E COMPLETO
Versão 7.0 - USDT CORE
Sistema baseado em USDT para todos os saldos e contabilidade.
BRL/BTC são apenas views convertidas.
"""

# =========================
# 0) Imports básicos (sempre primeiro)
# =========================
import os
import sys
import json
import time
import shutil
import base64
import hashlib
import secrets
import logging
import threading
import re
from datetime import datetime, timedelta
from collections import deque
from typing import Dict, List, Optional, Tuple, Union

import requests
from dotenv import load_dotenv

from flask import Flask, request, jsonify, send_file, make_response, redirect

from flask_cors import CORS

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Binance (se o pacote estiver instalado)
try:
    from binance.client import Client
except Exception:
    Client = None

# =========================
# 1) BASE_DIR e ENV (antes de qualquer uso)
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv()
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

# =========================
# 2) Logging (antes do app, pra logar tudo desde o boot)
# =========================
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "coyote_admin.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("coyote")

# =========================
# 3) Flask app + CORS (agora pode)
# =========================
app = Flask(__name__)
CORS(app, supports_credentials=True)

# =========================
# 4) Constantes do White Paper (canônicas)
# =========================
MASTER_WALLET_CODES = ["MASTER-COYOTE"]

PROFIT_DISTRIBUTION = {
    "company_fee": 0.50,
    "users_pool": 0.50,
}

# =========================
# 5) Arquivos do sistema
# =========================
WALLETS_FILE = os.path.join(BASE_DIR, "wallets.json")
TRANSACTIONS_FILE = os.path.join(BASE_DIR, "transactions.json")
SYSTEM_CONFIG_FILE = os.path.join(BASE_DIR, "system_config.json")
MASTER_WALLET_FILE = os.path.join(BASE_DIR, "master_wallet.json")
PROFIT_DISTRIBUTION_FILE = os.path.join(BASE_DIR, "profit_distributions.json")
CASHBOX_FILE = os.path.join(BASE_DIR, "cashbox.json")

USERS_FILE = os.path.join(BASE_DIR, "users.json")
ADMIN_LOGS_FILE = os.path.join(BASE_DIR, "admin_access.log.json")
USER_API_KEYS_FILE = os.path.join(BASE_DIR, "user_api_keys.json")
FRAUD_LOG_FILE = os.path.join(BASE_DIR, "fraud_detections.log.json")
SESSIONS_FILE = os.path.join(BASE_DIR, "sessions.json")
USER_STATUS_FILE = os.path.join(BASE_DIR, "user_status.json")

PARTICIPATIONS_FILE = os.path.join(BASE_DIR, "participations.json")
REALTIME_LOGS_FILE = os.path.join(BASE_DIR, "audit_logs.json")

# =========================
# 6) Segurança / chaves
# =========================
ROBO_ADMIN_KEY = "ADMIN-COYOTE-2025-ULTRA"
ADMIN_SECRET_KEY = "ADMIN-COYOTE-2025-ULTRA"
ADMIN_TOKEN_HEADER = "X-ADMIN-KEY"

PASSWORD_SALT = b"coyote_crypto_2025_secure"
SENHA_SERVIDOR = b"COYOTE_ULTRA_SECURE_2025_BACKEND_MASTER_KEY"

def _get_encryption_key() -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=PASSWORD_SALT,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(SENHA_SERVIDOR))

MASTER_ENCRYPTION_KEY = _get_encryption_key()
master_fernet = Fernet(MASTER_ENCRYPTION_KEY)

def encrypt_data(data: str) -> str:
    try:
        return master_fernet.encrypt(data.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"Erro ao criptografar: {e}")
        return data

def decrypt_data(encrypted_data: str) -> str:
    try:
        return master_fernet.decrypt(encrypted_data.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"Erro ao descriptografar: {e}")
        return encrypted_data

# =========================
# 7) Estado em memória (se você usa sessões/locks)
# =========================
REALTIME_LOG_MAX = 50
realtime_logs = deque(maxlen=REALTIME_LOG_MAX)

trade_locks = set()  # se você realmente usa isso em trades

# ─── Lock por usuário (evita race condition em operações concorrentes) ──────
_user_locks: Dict[str, threading.Lock] = {}
_user_locks_meta = threading.Lock()

def get_user_lock(code: str) -> threading.Lock:
    """Retorna (ou cria) um lock exclusivo por código de carteira."""
    with _user_locks_meta:
        if code not in _user_locks:
            _user_locks[code] = threading.Lock()
        return _user_locks[code]


# =========================
# 8) Helpers de arquivo (robustos)
# =========================
def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    # ✅ FIX: retry em caso de leitura durante escrita (race condition)
    for attempt in range(3):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                return default
            return json.loads(content)
        except json.JSONDecodeError:
            if attempt < 2:
                time.sleep(0.05)  # 50ms — aguarda escrita terminar
                continue
            logger.error(f"Erro lendo JSON {path}: conteúdo inválido após 3 tentativas")
            return default
        except Exception as e:
            logger.error(f"Erro lendo JSON {path}: {e}")
            return default
    return default

def _write_json(path: str, data) -> bool:
    # backup simples
    try:
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
    except Exception as e:
        logger.warning(f"Falha ao criar backup de {path}: {e}")

    # ✅ FIX: escrita atômica — grava em .tmp e depois substitui
    # Evita que _read_json leia arquivo parcialmente escrito (race condition)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        logger.error(f"Erro salvando JSON {path}: {e}")
        try:
            os.remove(tmp_path)
        except:
            pass
        return False

# =========================
# 9) Cotações (fallback seguro)
# =========================
DEFAULT_BTC_USDT = 65000.0
DEFAULT_USDT_BRL = 5.20

def _binance_client():
    if Client is None:
        return None
    try:
        # se você usa KEY/SECRET, injete aqui
        if BINANCE_API_KEY and BINANCE_API_SECRET:
            return Client(BINANCE_API_KEY, BINANCE_API_SECRET)
        return Client()  # modo público (pode falhar dependendo da lib/ambiente)
    except Exception as e:
        logger.warning(f"Falha ao criar Client Binance: {e}")
        return None

def get_current_btc_usdt() -> float:
    try:
        c = _binance_client()
        if not c:
            return DEFAULT_BTC_USDT
        ticker = c.get_symbol_ticker(symbol="BTCUSDT")
        return float(ticker["price"])
    except Exception as e:
        logger.warning(f"Erro BTC/USDT, fallback: {e}")
        return DEFAULT_BTC_USDT

def get_current_usdt_brl() -> float:
    try:
        c = _binance_client()
        if not c:
            return DEFAULT_USDT_BRL
        ticker = c.get_symbol_ticker(symbol="USDTBRL")
        return float(ticker["price"])
    except Exception as e:
        logger.warning(f"Erro USDT/BRL, fallback: {e}")
        return DEFAULT_USDT_BRL

# =========================
# 10) Auditoria (mínimo viável, sem inventar regra)
# =========================
def audit_event(action: str, success: bool, user_code: Optional[str] = None, details: str = "", extra: Optional[dict] = None):
    event = {
        "ts": datetime.utcnow().isoformat(),
        "action": action,
        "success": bool(success),
        "user_code": user_code,
        "details": details,
        "extra": extra or {},
    }
    realtime_logs.appendleft(event)

    # persistência
    logs = _read_json(REALTIME_LOGS_FILE, default=[])
    if isinstance(logs, list):
        logs.append(event)
        _write_json(REALTIME_LOGS_FILE, logs)



@app.route('/login')
def login_page():
    return send_file(
        os.path.join(BASE_DIR, 'login.html'),
        mimetype='text/html'
    )
def match_pending_deposit(binance_deposit, pending_transaction):
    """
    Compara um depósito da Binance com uma transação pendente.
    REGRA: tudo comparado EM BTC, com tolerância e confirmações.
    """
    try:
        # ===============================
        # 1. Validar método esperado
        # ===============================
        expected_symbol = str(pending_transaction.get("method", "")).upper()
        if expected_symbol != "BTC":
            logger.warning(f"❌ Método não suportado: {expected_symbol}")
            return False

        # ===============================
        # 2. Validar moeda do depósito Binance
        # ===============================
        binance_coin = str(binance_deposit.get("coin", "")).upper()
        if binance_coin != "BTC":
            logger.warning(f"❌ Moeda Binance inválida: {binance_coin}")
            return False

        # ===============================
        # 3. Valores em BTC (REGRA CRÍTICA)
        # ===============================
        expected_btc = float(pending_transaction.get("expected_crypto", 0))
        actual_btc = float(binance_deposit.get("amount", 0))

        logger.info(
            f"🔍 Comparando BTC | Esperado: {expected_btc:.8f} | Recebido: {actual_btc:.8f}"
        )

        if expected_btc <= 0:
            logger.error("❌ expected_crypto inválido (<= 0)")
            return False

        if actual_btc <= 0:
            logger.warning("⚠️ Depósito inválido (BTC <= 0)")
            return False

        # ===============================
        # 4. Tolerância de valor (5%)
        # ===============================
        tolerance = 0.10  # 10%
        min_btc = expected_btc * (1 - tolerance)
        max_btc = expected_btc * (1 + tolerance)

        if not (min_btc <= actual_btc <= max_btc):
            logger.info(
                f"💰 Valor BTC fora da faixa | "
                f"Esperado: {expected_btc:.8f} | "
                f"Recebido: {actual_btc:.8f} | "
                f"Faixa: {min_btc:.8f} - {max_btc:.8f}"
            )
            return False

        # ===============================
        # 5. Status do depósito Binance
        # ===============================
        # Binance: 1 = sucesso
        if int(binance_deposit.get("status", 0)) != 1:
            logger.warning(
                f"❌ Status Binance inválido: {binance_deposit.get('status')}"
            )
            return False

        # ===============================
        # 6. Confirmações de blockchain
        # ===============================
        confirmations_raw = str(binance_deposit.get("confirmTimes", "0/0"))

        try:
            current_confirmations, required_confirmations = map(
                int, confirmations_raw.split("/")
            )
        except Exception:
            logger.error(f"❌ confirmTimes inválido: {confirmations_raw}")
            return False

        # BTC exige pelo menos 3 confirmações
        if current_confirmations < 3:
            logger.info(
                f"⏳ Aguardando confirmações BTC: {current_confirmations}/3"
            )
            return False

        # ===============================
        # 7. DEPÓSITO CONFIRMADO
        # ===============================
        logger.info("✅✅✅ DEPÓSITO BTC CONFIRMADO")
        logger.info(f"   📌 Valor BTC: {actual_btc:.8f}")
        logger.info(f"   📌 TX Hash: {binance_deposit.get('txId', 'N/A')}")
        logger.info(f"   📌 Confirmações: {confirmations_raw}")

        return True

    except Exception as e:
        logger.error("❌❌❌ ERRO CRÍTICO EM match_pending_deposit")
        logger.exception(e)
        logger.error(f"📊 Binance deposit: {binance_deposit}")
        logger.error(f"📊 Pending transaction: {pending_transaction}")
        return False

def load_participations() -> Dict:
    """
    Carrega participações do arquivo JSON.
    Aceita 2 formatos:
      - dict: { "ROBO-XXXX": {...}, ... }  (formato oficial)
      - list: [ {"wallet_code": "...", ...}, ... ] (formato legado)
    """
    if not os.path.exists(PARTICIPATIONS_FILE):
        logger.info(f"📁 Arquivo {PARTICIPATIONS_FILE} não encontrado, criando novo...")
        with open(PARTICIPATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=2, ensure_ascii=False)
        return {}

    try:
        with open(PARTICIPATIONS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        # Se vier como lista, converte para dict por wallet_code
        if isinstance(raw, list):
            converted = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                wc = (item.get("wallet_code") or "").strip().upper()
                if not wc:
                    continue
                converted[wc] = item
            participations = converted
        elif isinstance(raw, dict):
            participations = raw
        else:
            logger.error("❌ participations.json em formato inválido (não é dict nem list).")
            return {}

        # Normalização/migração USDT CORE (sem quebrar)
        usdt_brl = get_current_usdt_brl()
        migrated = False

        for user_code, participation in participations.items():
            if not isinstance(participation, dict):
                participations[user_code] = {}
                participation = participations[user_code]
                migrated = True

            # Garantir chave wallet_code dentro do registro
            if participation.get("wallet_code") != user_code:
                participation["wallet_code"] = user_code
                migrated = True

            # Campos default
            if "active" not in participation:
                participation["active"] = True
                migrated = True
            if "share" not in participation:
                participation["share"] = 1
                migrated = True

            # Migração BRL → USDT (se existir legado)
            if "virtual_balance" in participation and "virtual_balance_usdt" not in participation:
                participation["virtual_balance_usdt"] = float(participation.get("virtual_balance", 0) or 0) / (usdt_brl or 1)
                migrated = True
            if "total_deposited" in participation and "total_deposited_usdt" not in participation:
                participation["total_deposited_usdt"] = float(participation.get("total_deposited", 0) or 0) / (usdt_brl or 1)
                migrated = True
            if "profit_accumulated" in participation and "profit_accumulated_usdt" not in participation:
                participation["profit_accumulated_usdt"] = float(participation.get("profit_accumulated", 0) or 0) / (usdt_brl or 1)
                migrated = True
            if "total_withdrawn" in participation and "total_withdrawn_usdt" not in participation:
                participation["total_withdrawn_usdt"] = float(participation.get("total_withdrawn", 0) or 0) / (usdt_brl or 1)
                migrated = True

            # Garantir campos USDT
            for k in ["virtual_balance_usdt", "total_deposited_usdt", "profit_accumulated_usdt", "total_withdrawn_usdt"]:
                if k not in participation:
                    participation[k] = 0.0
                    migrated = True

        if migrated:
            save_participations(participations)
            logger.info("✅ Participações normalizadas/migradas para USDT CORE")

        return participations

    except Exception as e:
        logger.error(f"❌ Erro ao carregar participations.json: {e}")
        return {}

def confirm_deposit(transaction_id, binance_deposit):
    """
    Confirma depósito e credita saldo - AGORA APENAS EM PARTICIPATIONS
    wallets.json NÃO É MAIS ATUALIZADO
    """
    try:
        transactions = load_transactions()
        participations = load_participations()

        # Localizar transação
        transaction = None
        index = -1
        for i, t in enumerate(transactions):
            if t.get("id") == transaction_id:
                transaction = t
                index = i
                break

        if not transaction:
            logger.error("❌ Transação não encontrada")
            return False

        if transaction.get("status") == "confirmado":
            logger.warning("⚠️ Transação já confirmada")
            return False

        wallet_code = str(transaction.get("wallet_code", "")).strip().upper()

        if wallet_code not in participations:
            logger.error(f"❌ Carteira não encontrada: {wallet_code}")
            return False

        # 🔒 valor do crédito em USDT
        credit_usdt = float(transaction.get("expected_usdt", 0) or 0)

        # Fallback para BTC
        if credit_usdt <= 0:
            btc_amount = float(binance_deposit.get("amount", 0) or 0)
            btc_usdt = get_current_btc_usdt()
            credit_usdt = btc_amount * btc_usdt

        if credit_usdt <= 0:
            logger.error("❌ Valor de crédito inválido")
            return False

        # ✅ ATUALIZAR APENAS PARTICIPATIONS (FONTE ÚNICA)
        p = participations[wallet_code]
        p["virtual_balance_usdt"] = float(p.get("virtual_balance_usdt", 0)) + credit_usdt
        p["total_deposited_usdt"] = float(p.get("total_deposited_usdt", 0)) + credit_usdt
        p["saldo_disponivel_usdt"] = float(p.get("saldo_disponivel_usdt", 0)) + credit_usdt
        p["updated_at"] = datetime.utcnow().isoformat()

        # Marcar transação
        transactions[index]["status"] = "confirmado"
        transactions[index]["credited"] = True
        transactions[index]["credited_at"] = datetime.utcnow().isoformat()
        transactions[index]["credit_amount_usdt"] = round(credit_usdt, 8)

        # ✅ SALVAR APENAS OS ARQUIVOS NECESSÁRIOS
        save_transactions(transactions)
        save_participations(participations)

        logger.info(f"✅ Depósito creditado: +{credit_usdt:.6f} USDT → {wallet_code}")
        return True

    except Exception as e:
        logger.exception(f"Erro ao confirmar depósito: {e}")
        return False

@app.route("/api/public/wallet-balance", methods=["GET"])
def api_public_wallet_balance():
    """
    /api/public/wallet-balance?code=ROBO-XXXX
    Retorna saldo em USDT e view em BRL.
    """
    try:
        wallet_code = (request.args.get("code") or "").strip().upper()
        if not wallet_code:
            return jsonify({"success": False, "message": "code é obrigatório"}), 400

        participations = load_participations()
        p = participations.get(wallet_code)

        if not p or not bool(p.get("active", True)):
            return jsonify({"success": False, "message": "Carteira sem participação ativa", "wallet_code": wallet_code}), 404

        saldo_usdt     = float(p.get("virtual_balance_usdt", 0) or 0)
        em_posicoes    = float(p.get("saldo_em_posicoes_usdt", 0) or 0)
        # ✅ FIX: se saldo_em_posicoes > saldo total (inconsistência), usa 0 para não mostrar negativo
        if em_posicoes > saldo_usdt:
            em_posicoes = 0.0
        saldo_disp     = round(max(0.0, saldo_usdt - em_posicoes), 6)
        total_dep_usdt = float(p.get("total_deposited_usdt", 0) or 0)
        lucro_usdt     = float(p.get("profit_accumulated_usdt", 0) or 0)

        usdt_brl = get_current_usdt_brl()
        return jsonify({
            "success": True,
            "wallet_code": wallet_code,
            "currency_source": "USDT_CORE",
            "brl_rate": round(usdt_brl, 6),
            "balance": {"usdt": round(saldo_usdt, 6), "brl": round(saldo_usdt * usdt_brl, 2)},
            "saldo_disponivel_usdt": saldo_disp,
            "total_deposited": {"usdt": round(total_dep_usdt, 6), "brl": round(total_dep_usdt * usdt_brl, 2)},
            "profit_accumulated": {"usdt": round(lucro_usdt, 6), "brl": round(lucro_usdt * usdt_brl, 2)},
            "profit_accumulated_usdt": round(lucro_usdt, 6),  # campo flat para compatibilidade com painel
            "status": p.get("status", "active"),
            "last_update": p.get("updated_at", ""),
            "share_percent": p.get("share_percent", 0.0),
        }), 200

    except Exception as e:
        logger.error(f"Erro /api/public/wallet-balance: {e}")
        return jsonify({"success": False, "message": "Erro interno"}), 500


@app.route("/api/participation/dashboard", methods=["GET"])
def api_participation_dashboard():
    """
    /api/participation/dashboard?code=ROBO-XXXX
    """
    try:
        wallet_code = (request.args.get("code") or "").strip().upper()
        if not wallet_code:
            return jsonify({"success": False, "message": "code é obrigatório"}), 400

        participations = load_participations()
        p = participations.get(wallet_code)
        if not p or not bool(p.get("active", True)):
            return jsonify({"success": False, "message": "Carteira sem participação ativa", "wallet_code": wallet_code}), 404

        return jsonify({"success": True, "wallet_code": wallet_code, "participation": p, "currency": "USDT"}), 200
    except Exception as e:
        logger.error(f"Erro /api/participation/dashboard: {e}")
        return jsonify({"success": False, "message": "Erro interno"}), 500


        
def validate_system_consistency():
    """Valida consistência do sistema - TRAVAS DE SEGURANÇA (APENAS PARTICIPATIONS)"""
    try:
        logger.info("🔍 Validando consistência do sistema...")
        
        transactions = load_transactions()
        participations = load_participations()
        
        issues = []
        
        # 🔒 TRAVA 1: Verificar se trade foi liquidado mais de uma vez
        trade_ids = []
        for tx in transactions:
            if tx.get("trade_id"):
                if tx["trade_id"] in trade_ids:
                    issues.append({
                        "type": "TRADE_DUPLICADO",
                        "trade_id": tx["trade_id"],
                        "severity": "CRITICAL"
                    })
                trade_ids.append(tx["trade_id"])
        
        # 🔒 TRAVA 2: Verificar saques sem lucro suficiente (AGORA BASEADO APENAS EM PARTICIPATIONS)
        for tx in transactions:
            if tx.get("type") == "withdraw_user" and tx.get("status") == "completado":
                user_code = tx.get("user_code")
                if user_code and user_code in participations:
                    participation = participations[user_code]
                    profit_before = participation.get("profit_accumulated_usdt", 0) + tx.get("value", 0)
                    if profit_before < tx.get("value", 0):
                        issues.append({
                            "type": "SAQUE_SEM_LUCRO",
                            "user_code": user_code,
                            "transaction_id": tx.get("id"),
                            "severity": "HIGH"
                        })
        
        # 🔒 TRAVA 3: Verificar saldo virtual (APENAS PARTICIPATIONS - CONSISTÊNCIA INTERNA)
        for user_code, participation in participations.items():
            virtual_balance = participation.get("virtual_balance_usdt", 0)
            total_deposited = participation.get("total_deposited_usdt", 0)
            
            # Regra: virtual_balance NUNCA pode ser negativo
            if virtual_balance < 0:
                issues.append({
                    "type": "SALDO_NEGATIVO",
                    "user_code": user_code,
                    "virtual_balance_usdt": virtual_balance,
                    "severity": "CRITICAL"
                })
        
        if issues:
            logger.error(f"🚨 {len(issues)} problemas de segurança encontrados")
            for issue in issues:
                logger.error(f"   • {issue['type']}: {issue}")
            
            audit_event(
                action="SECURITY_CHECK_FAILED",
                success=False,
                details=f"Encontrados {len(issues)} problemas de segurança",
                extra={"issues": issues}
            )
            
            return {
                "success": False,
                "issues": issues,
                "message": f"Encontrados {len(issues)} problemas de segurança"
            }
        else:
            logger.info("✅ Sistema seguro e consistente")
            
            audit_event(
                action="SECURITY_CHECK_PASSED",
                success=True,
                details="Todas as travas de segurança validadas"
            )
            
            return {
                "success": True,
                "issues": [],
                "message": "Sistema seguro e consistente"
            }
            
    except Exception as e:
        logger.error(f"❌ Erro na validação de segurança: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Erro na validação"
        }


# ============================================
# 🔥 ESSENCIAL: BASE_DIR (SEMPRE PRIMEIRO)
# ============================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))



# ============================================
# API PÚBLICA
# ============================================

@app.route("/health", methods=["GET"])
def health():
    """Health check"""
    cleanup_expired_sessions()
    return jsonify({
        "status": "ok", 
        "timestamp": datetime.now().isoformat(),
        "service": "Coyote Crypto API",
        "users_count": len(users),
        "sessions_count": len(active_sessions)
    })




# ============================================
# 🔥 IMPORTS ADICIONAIS DO SISTEMA
# ============================================

from flask_cors import CORS
import time
import threading
import logging
import json
import shutil
from datetime import datetime, timedelta
import hashlib
import secrets
import re
from typing import Dict, List, Union, Optional, Tuple
from collections import defaultdict, deque
import requests
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import pytz
import sys
from binance.client import Client

# ============================================
# 🔥 ARQUIVOS DE DADOS DO SISTEMA DE CARTEIRAS
# ============================================

WALLETS_FILE = os.path.join(BASE_DIR, "wallets.json")
TRANSACTIONS_FILE = os.path.join(BASE_DIR, "transactions.json")
SYSTEM_CONFIG_FILE = os.path.join(BASE_DIR, "system_config.json")
MASTER_WALLET_FILE = os.path.join(BASE_DIR, "master_wallet.json")
PROFIT_DISTRIBUTION_FILE = os.path.join(BASE_DIR, "profit_distributions.json")
CASHBOX_FILE = os.path.join(BASE_DIR, "cashbox.json")

# 🔥 CARTEIRAS BASE DO SISTEMA
BASE_WALLETS = ["raiz01", "raiz02", "raiz03"]

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('coyote_admin.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

CORS(app, supports_credentials=True)

# ============================================
# 🔒 CHAVE DE CRIPTOGRAFIA ÚNICA
# ============================================
PASSWORD_SALT = b'coyote_crypto_2025_secure'
SENHA_SERVIDOR = b'COYOTE_ULTRA_SECURE_2025_BACKEND_MASTER_KEY'

def get_encryption_key():
    """Gera a chave de criptografia única do sistema"""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=PASSWORD_SALT,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(SENHA_SERVIDOR))
    return key

MASTER_ENCRYPTION_KEY = get_encryption_key()
master_fernet = Fernet(MASTER_ENCRYPTION_KEY)

def encrypt_data(data: str) -> str:
    """Criptografa dados com a chave mestra"""
    try:
        encrypted = master_fernet.encrypt(data.encode('utf-8'))
        return encrypted.decode('utf-8')
    except Exception as e:
        logger.error(f"❌ Erro ao criptografar: {e}")
        return data

def decrypt_data(encrypted_data: str) -> str:
    """Descriptografa dados com a chave mestra"""
    try:
        decrypted = master_fernet.decrypt(encrypted_data.encode('utf-8'))
        return decrypted.decode('utf-8')
    except Exception as e:
        logger.error(f"❌ Erro ao descriptografar: {e}")
        return encrypted_data

ROBO_ADMIN_KEY = "ADMIN-COYOTE-2025-ULTRA"

# 🔒 TOKENS DE ADMIN
ADMIN_SECRET_KEY = "ADMIN-COYOTE-2025-ULTRA"
ADMIN_TOKEN_HEADER = "X-ADMIN-KEY"

# 🔒 ARQUIVOS DE DADOS
USERS_FILE = os.path.join(BASE_DIR, "users.json")
ADMIN_LOGS_FILE = os.path.join(BASE_DIR, "admin_access.log.json")
USER_API_KEYS_FILE = os.path.join(BASE_DIR, "user_api_keys.json")
FRAUD_LOG_FILE = os.path.join(BASE_DIR, "fraud_detections.log.json")
SESSIONS_FILE = os.path.join(BASE_DIR, "sessions.json")
USER_STATUS_FILE = os.path.join(BASE_DIR, "user_status.json")

# ============================================
# 🔥 SISTEMA DE AUDITORIA EM TEMPO REAL
# ============================================
REALTIME_LOG_MAX = 50
realtime_logs = deque(maxlen=REALTIME_LOG_MAX)
REALTIME_LOGS_FILE = os.path.join(BASE_DIR, "audit_logs.json")

# ============================================
# 🔥 NOVA ESTRUTURA PARA SISTEMA DE PARTICIPAÇÕES (USDT CORE)
# ============================================

PARTICIPATIONS_FILE = os.path.join(BASE_DIR, "participations.json")

# 🔥 TAXAS DE CONVERSÃO (FALLBACK SEGURO)
DEFAULT_BTC_USDT = 65000.0  # 1 BTC = 65,000 USDT
DEFAULT_USDT_BRL = 5.20      # 1 USDT = 5.20 BRL

def get_current_btc_usdt() -> float:
    """Obtém cotação BTC/USDT da Binance ou fallback"""
    try:
        from binance.client import Client
        client = Client()
        ticker = client.get_symbol_ticker(symbol="BTCUSDT")
        return float(ticker['price'])
    except Exception as e:
        logger.warning(f"⚠️ Erro ao buscar BTC/USDT, usando fallback: {e}")
        return DEFAULT_BTC_USDT



def load_participations():
    """Carrega participações - FONTE ÚNICA DE VERDADE"""
    try:
        if not os.path.exists(PARTICIPATIONS_FILE):
            return {}

        with open(PARTICIPATIONS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()

            if not content:
                return {}

            data = json.loads(content)

            # 🔒 VALIDAÇÃO ESTRUTURAL CRÍTICA
            for code, p in data.items():
                # Campos obrigatórios
                p.setdefault("virtual_balance_usdt", 0.0)
                p.setdefault("total_deposited_usdt", 0.0)
                p.setdefault("profit_accumulated_usdt", 0.0)
                p.setdefault("total_withdrawn_usdt", 0.0)
                p.setdefault("share_percent", 0.0)
                p.setdefault("status", "active")
                p.setdefault("created_at", datetime.utcnow().isoformat())
                p.setdefault("updated_at", datetime.utcnow().isoformat())
                p.setdefault("last_profit_distribution", None)
                p.setdefault("master_wallet", MASTER_WALLET_CODES[0] if MASTER_WALLET_CODES else "MASTER")
                
                # ✅ VALIDAÇÃO DE INTEGRIDADE: virtual_balance_usdt NUNCA pode ser negativo
                if p["virtual_balance_usdt"] < 0:
                    logger.error(f"🚨 SALDO NEGATIVO DETECTADO EM {code}: {p['virtual_balance_usdt']} - CORRIGINDO PARA 0")
                    p["virtual_balance_usdt"] = 0.0
                    p["updated_at"] = datetime.utcnow().isoformat()

            return data

    except Exception as e:
        logger.error(f"❌ Erro ao carregar participations.json: {e}")
        return {}

def save_participations(participations: Dict[str, dict]) -> bool:
    return _write_json(PARTICIPATIONS_FILE, participations)

def garantir_participacao(wallet_code, wallets, participations):
    """Cria participação automaticamente se não existir"""
    if wallet_code not in participations:
        participations[wallet_code] = {
            "virtual_balance_usdt": wallets[wallet_code]["balanceUSDT"],
            "profit_accumulated_usdt": 0.0,
            "created_at": datetime.utcnow().isoformat(),
            "last_update": datetime.utcnow().isoformat()
        }

def validate_system_consistency() -> dict:
    try:
        transactions = load_transactions()
        participations = load_participations()
        issues = []

        # trava: trade duplicado
        seen_trades = set()
        for tx in transactions:
            tid = tx.get("trade_id")
            if tid:
                if tid in seen_trades:
                    issues.append({"type": "TRADE_DUPLICADO", "trade_id": tid, "severity": "CRITICAL"})
                seen_trades.add(tid)

        # trava: saldo negativo
        for code, p in participations.items():
            if float(p.get("virtual_balance_usdt", 0) or 0) < 0:
                issues.append({"type": "SALDO_NEGATIVO", "user_code": code, "severity": "CRITICAL"})

        if issues:
            audit_event("SECURITY_CHECK_FAILED", False, details=f"{len(issues)} issues", extra={"issues": issues})
            return {"success": False, "issues": issues, "message": "Inconsistências encontradas"}
        audit_event("SECURITY_CHECK_PASSED", True, details="OK")
        return {"success": True, "issues": [], "message": "Sistema consistente"}
    except Exception as e:
        return {"success": False, "error": str(e), "message": "Erro na validação"}

def get_master_wallet_balance_usdt() -> float:
    """Retorna saldo total da carteira master em USDT"""
    wallets = load_wallets()
    total_balance = 0.0
    
    for wallet_code, wallet in wallets.items():
        if wallet_code in MASTER_WALLET_CODES:
            total_balance += wallet.get('balanceUSDT', 0.0)
    
    return total_balance





def get_master_wallet_balance(confirmed_only=True) -> float:
    """Compatibilidade: retorna saldo master em BRL convertido"""
    usdt_balance = get_master_wallet_balance_usdt()
    usdt_brl = get_current_usdt_brl()
    return usdt_balance * usdt_brl



def calculate_share_percent(user_deposit_usdt: float, total_master_balance_usdt: float) -> float:
    """Calcula percentual de participação do usuário baseado em USDT"""
    if total_master_balance_usdt <= 0:
        return 0.0
    return (user_deposit_usdt / total_master_balance_usdt) * 100

def create_or_update_participation(user_code: str, deposit_amount_usdt: float) -> bool:
    """Cria ou atualiza participação do usuário após depósito confirmado pelo admin"""
    try:
        participations = load_participations()
        master_balance_usdt = get_master_wallet_balance_usdt()
        
        if user_code in participations:
            # Atualizar participação existente
            participation = participations[user_code]
            participation["total_deposited_usdt"] += deposit_amount_usdt
            participation["virtual_balance_usdt"] += deposit_amount_usdt
            participation["share_percent"] = calculate_share_percent(
                participation["total_deposited_usdt"], 
                master_balance_usdt + deposit_amount_usdt
            )
            participation["updated_at"] = datetime.now().isoformat()
        else:
            # Criar nova participação
            participations[user_code] = {
                "user_code": user_code,
                "master_wallet": MASTER_WALLET_CODES[0],
                "virtual_balance_usdt": deposit_amount_usdt,
                "total_deposited_usdt": deposit_amount_usdt,
                "profit_accumulated_usdt": 0.0,
                "share_percent": calculate_share_percent(deposit_amount_usdt, master_balance_usdt + deposit_amount_usdt),
                "joined_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "status": "active",
                "last_profit_distribution": None,
                "total_withdrawn_usdt": 0.0
            }
        
        # Recalcular shares de todos os usuários após novo depósito
        recalculate_all_shares_usdt()
        
        # Auditoria
        audit_event(
            action="PARTICIPATION_CREATED_UPDATED",
            success=True,
            user_code=user_code,
            details=f"Participação criada/atualizada com depósito de {deposit_amount_usdt:.2f} USDT",
            extra={
                "deposit_amount_usdt": deposit_amount_usdt,
                "share_percent": participations[user_code]["share_percent"]
            }
        )
        
        return save_participations(participations)
        
    except Exception as e:
        logger.error(f"❌ Erro ao criar/atualizar participação: {e}")
        return False

def require_robo_key(request):
    # robotic.py envia X-ADMIN-KEY; aceita ambos
    key = request.headers.get("X-ROBO-KEY") or request.headers.get("X-ADMIN-KEY")
    return key == ROBO_ADMIN_KEY

def recalculate_all_shares_usdt():
    """Recalcula percentuais de participação de todos os usuários baseado em USDT"""
    try:
        participations = load_participations()

        # ✅ DENOMINADOR = soma dos virtual_balance_usdt de todos os participantes ativos
        # NÃO usa wallets.json nem master_wallet.json — esses valores oscilam a cada fee/trade
        # e causavam share_percent > 100% (ex: 1997%, 8888%) quando o saldo do wallets.json
        # era menor que o total_deposited_usdt dos usuários
        total_pool_usdt = sum(
            float(p.get("virtual_balance_usdt", 0))
            for p in participations.values()
            if p.get("status") == "active" and float(p.get("virtual_balance_usdt", 0)) > 0
        )

        if total_pool_usdt <= 0:
            return

        for user_code, participation in participations.items():
            if participation.get("status") == "active":
                bal    = float(participation.get("virtual_balance_usdt", 0))
                em_pos = float(participation.get("saldo_em_posicoes_usdt", 0))
                participation["share_percent"]      = round((bal / total_pool_usdt) * 100, 6)
                # Garantir que saldo_disponivel sempre reflete a realidade
                participation["saldo_disponivel_usdt"] = round(max(0.0, bal - em_pos), 8)
                participation["updated_at"] = datetime.now().isoformat()

        save_participations(participations)
        logger.info(f"📊 Shares recalculados — pool total: {total_pool_usdt:.4f} USDT")

    except Exception as e:
        logger.error(f"❌ Erro ao recalcular shares: {e}")

def recalculate_all_shares():
    """Compatibilidade: chama a versão USDT"""
    recalculate_all_shares_usdt()

def get_user_equity_summary(user_code: str) -> Dict:
    """
    🔒 FONTE ÚNICA DE VERDADE:
    APENAS participation.virtual_balance_usdt
    """
    try:
        participations = load_participations()

        if user_code not in participations:
            return {"has_equity": False}

        p = participations[user_code]

        # 🔒 SALDO OFICIAL (ÚNICA FONTE)
        balance_usdt = float(p.get("virtual_balance_usdt", 0))

        # conversões apenas para visualização
        btc_rate = get_current_btc_usdt()
        usdt_brl = get_current_usdt_brl()

        balance_brl = balance_usdt * usdt_brl
        balance_btc = balance_usdt / btc_rate if btc_rate else 0

        return {
            "has_equity": True,
            "equity": {
                "virtual_balance_usdt": round(balance_usdt, 6),
                "virtual_balance_brl": round(balance_brl, 2),
                "virtual_balance_btc": round(balance_btc, 8),
                "available_withdraw_usdt": round(balance_usdt, 6),  # 100% disponível
                "available_withdraw_brl": round(balance_usdt * usdt_brl, 2),
                "withdraw_percent": 100
            },
            "participation": {
                "total_deposited_usdt": p.get("total_deposited_usdt", 0),
                "profit_accumulated_usdt": p.get("profit_accumulated_usdt", 0),
                "total_withdrawn_usdt": p.get("total_withdrawn_usdt", 0),
                "share_percent": p.get("share_percent", 0)
            },
            "system_mode": "usdt_core_single_source"
        }

    except Exception as e:
        logger.error(f"❌ equity summary error: {e}")
        return {"has_equity": False}

def get_system_operations_status():
    """Retorna status das operações do sistema (sistema livre)"""
    try:
        transactions = load_transactions()
        participations = load_participations()
        
        # Verificar última distribuição
        distribution_transactions = [
            tx for tx in transactions 
            if tx.get("type") == "profit_distribution" and tx.get("status") == "completado"
        ]
        
        last_distribution = None
        if distribution_transactions:
            distribution_transactions.sort(key=lambda x: x.get("date", ""), reverse=True)
            last_distribution = distribution_transactions[0]
        
        # Carregar dados para estatísticas
        active_participations = [p for p in participations.values() if p.get("status") == "active"]
        
        wallets = load_wallets()
        master_balance_usdt = 0
        for wallet_code in MASTER_WALLET_CODES:
            if wallet_code in wallets:
                master_balance_usdt += wallets[wallet_code].get('balanceUSDT', 0)
        
        # Calcular lucro potencial
        potential_profit_usdt = 0
        for wallet_code in MASTER_WALLET_CODES:
            if wallet_code in wallets:
                wallet = wallets[wallet_code]
                profit = wallet.get('balanceUSDT', 0) - wallet.get('totalDepositedUSDT', 0)
                if profit > 0:
                    potential_profit_usdt += profit
        
        # Sistema sempre operacional
        today = datetime.now().date()
        
        return {
            "success": True,
            "status": {
                "system_mode": "unrestricted",
                "operations_enabled": True,
                "deposit_enabled": True,
                "withdraw_enabled": True,
                "reinvest_enabled": True,
                "active_participants": len(active_participations),
                "master_balance_usdt": round(master_balance_usdt, 2),
                "potential_profit_usdt": round(potential_profit_usdt, 2),
                "total_participations": len(participations),
                "last_distribution": {
                    "date": last_distribution.get("date") if last_distribution else None,
                    "id": last_distribution.get("id") if last_distribution else None,
                    "profit_usdt": last_distribution.get("details", {}).get("master_profit_usdt", 0) if last_distribution else 0
                } if last_distribution else None,
                "server_time": datetime.now().isoformat(),
                "message": "Sistema operando normalmente - Operações livres",
                "currency": "USDT"
            }
        }
        
    except Exception as e:
        logger.error(f"❌ Erro ao obter status do sistema: {e}")
        return {
            "success": False, 
            "error": str(e),
            "status": {
                "system_mode": "unrestricted",
                "operations_enabled": True,
                "message": "Sistema operacional"
            }
        }

def get_last_cycle_info():
    """Retorna informações do último ciclo processado"""
    try:
        transactions = load_transactions()
        cycle_transactions = [
            tx for tx in transactions 
            if tx.get("type") == "monthly_cycle" and tx.get("status") == "completado"
        ]
        
        if not cycle_transactions:
            return {"last_cycle": None, "count": 0}
        
        # Ordenar por data (mais recente primeiro)
        cycle_transactions.sort(key=lambda x: x.get("date", ""), reverse=True)
        last_cycle = cycle_transactions[0]
        
        return {
            "last_cycle": {
                "date": last_cycle.get("date"),
                "id": last_cycle.get("id"),
                "profit_usdt": last_cycle.get("details", {}).get("master_profit_usdt", 0),
                "participants": last_cycle.get("details", {}).get("active_participations", 0)
            },
            "total_cycles": len(cycle_transactions)
        }
    except Exception as e:
        logger.error(f"❌ Erro ao obter info do último ciclo: {e}")
        return {"error": str(e)}

def validate_withdrawal_conditions(user_code: str, amount_usdt: float) -> Tuple[bool, str]:
    """Valida condições para saque - SEM DIA 7, baseado em USDT"""
    try:
        # Carregar participação
        participations = load_participations()
        if user_code not in participations:
            return False, "Usuário não possui participação ativa"
        
        participation = participations[user_code]
        
        # Verificar status
        if participation.get("status") != "active":
            return False, "Participação inativa"
        
        # Verificar saldo virtual em USDT
        virtual_balance_usdt = participation.get("virtual_balance_usdt", 0)
        if amount_usdt > virtual_balance_usdt:
            return False, f"Saldo virtual insuficiente. Disponível: {virtual_balance_usdt:.2f} USDT"
        
        
        
        return True, "Validação aprovada"
        
    except Exception as e:
        logger.error(f"❌ Erro na validação de saque: {e}")
        return False, f"Erro na validação: {str(e)}"

def corrigir_deposito_btc(transaction_id, btc_real_recebido, btc_usdt_rate=None):
    """Corrige um depósito BTC - COM IDEMPOTÊNCIA ABSOLUTA - Converte para USDT"""
    try:
        if btc_usdt_rate is None:
            btc_usdt_rate = get_current_btc_usdt()
        
        # Carregar transações
        transactions = load_transactions()
        
        # 🔥 VERIFICAÇÃO DE IDEMPOTÊNCIA ANTES DE QUALQUER COISA
        for tx in transactions:
            if tx.get("id") == transaction_id and tx.get("status") == "confirmado":
                print(f"🚨 TRANSAÇÃO JÁ CONFIRMADA: {transaction_id}")
                print(f"   Status: {tx.get('status')}")
                print(f"   Confirmado em: {tx.get('confirmed_at')}")
                print(f"   Creditado: {tx.get('credited', False)}")
                return False
        
        # Encontrar a transação
        for i, tx in enumerate(transactions):
            if tx.get("id") == transaction_id:
                print(f"🔧 Corrigindo transação: {transaction_id}")
                print(f"   BTC errado registrado: {tx.get('expected_crypto', 0)}")
                print(f"   BTC real recebido: {btc_real_recebido}")
                print(f"   BRL errado: R$ {tx.get('value', 0):,.2f}")
                
                # Calcular valor real em USDT
                valor_real_usdt = btc_real_recebido * btc_usdt_rate
                print(f"   USDT real: {valor_real_usdt:.2f}")
                
                # 🔥 VERIFICAR SE JÁ FOI CORRIGIDA
                if tx.get("corrigida") is True:
                    print(f"🚨 TRANSAÇÃO JÁ CORRIGIDA ANTERIORMENTE!")
                    return False
                
                # Corrigir valores
                transactions[i]["value_usdt"] = valor_real_usdt
                transactions[i]["value_btc"] = btc_real_recebido
                transactions[i]["expected_amount_usdt"] = valor_real_usdt
                transactions[i]["expected_crypto"] = btc_real_recebido
                transactions[i]["btc_price_at_deposit"] = btc_usdt_rate
                
                # Marcar como confirmado
                transactions[i]["status"] = "confirmado"
                transactions[i]["confirmed_at"] = datetime.now().isoformat()
                transactions[i]["description"] = f"Depósito BTC corrigido: {btc_real_recebido:.8f} BTC"
                
                # 🔥 MARCAÇÃO DE IDEMPOTÊNCIA
                transactions[i]["corrigida"] = True
                transactions[i]["corrigida_em"] = datetime.now().isoformat()
                transactions[i]["btc_real_recebido"] = btc_real_recebido
                transactions[i]["btc_usdt_rate_aplicado"] = btc_usdt_rate
                
                # Salvar
                save_transactions(transactions)
                
                # Agora confirmar o depósito
                return confirmar_deposito_corrigido(
                    transaction_id, 
                    btc_real_recebido, 
                    valor_real_usdt,
                    tx.get("wallet_code"),
                    tx_hash=tx.get("tx_hash")
                )
        
        print(f"❌ Transação não encontrada: {transaction_id}")
        return False
        
    except Exception as e:
        print(f"💥 Erro ao corrigir: {e}")
        return False

def confirmar_deposito_corrigido(transaction_id, btc_amount, usdt_amount, wallet_code, tx_hash=None):
    """Confirma depósito corrigido - COM IDEMPOTÊNCIA - Credita em USDT"""
    try:
        # Carregar wallets
        wallets = load_wallets()
        
        if wallet_code not in wallets:
            print(f"❌ Carteira não encontrada: {wallet_code}")
            return False
        
        wallet = wallets[wallet_code]
        
        # 🔥 VERIFICAÇÃO DE IDEMPOTÊNCIA CRÍTICA
        transactions = load_transactions()
        for tx in transactions:
            if (tx.get("id") == transaction_id and 
                tx.get("status") == "confirmado" and
                tx.get("credited") is True):
                print(f"🚨 CRÉDITO JÁ APLICADO PARA ESTA TRANSAÇÃO!")
                print(f"   Transação: {transaction_id}")
                print(f"   Creditado em: {tx.get('credited_at')}")
                return False
        
        print(f"💰 Creditando carteira: {wallet_code}")
        print(f"   Saldo USDT antes: {wallet.get('balanceUSDT', 0):.2f}")
        print(f"   BTC antes: {wallet.get('balanceBTC', 0):.8f}")
        
        # Creditar valores corrigidos em USDT
        wallet["balanceUSDT"] = wallet.get("balanceUSDT", 0) + usdt_amount
        wallet["balanceBTC"] = wallet.get("balanceBTC", 0) + btc_amount
        wallet["totalDepositedUSDT"] = wallet.get("totalDepositedUSDT", 0) + usdt_amount
        wallet["totalDepositedBTC"] = wallet.get("totalDepositedBTC", 0) + btc_amount
        wallet["updated_at"] = datetime.now().isoformat()
        
        # 🔥 MARCAÇÃO DE IDEMPOTÊNCIA NA WALLET
        if "creditos_aplicados" not in wallet:
            wallet["creditos_aplicados"] = []
        
        # Verificar se crédito JÁ está na lista
        credito_existente = False
        for credito in wallet["creditos_aplicados"]:
            if (credito.get("transaction_id") == transaction_id and 
                credito.get("tipo") == "deposito_corrigido"):
                credito_existente = True
                break
        
        if not credito_existente:
            wallet["creditos_aplicados"].append({
                "transaction_id": transaction_id,
                "tipo": "deposito_corrigido",
                "btc_amount": btc_amount,
                "usdt_amount": usdt_amount,
                "data": datetime.now().isoformat(),
                "tx_hash": tx_hash
            })
        
        # Salvar
        save_wallets(wallets)
        
        # 🔥 ATUALIZAR TRANSAÇÃO COM MARCAÇÃO DE CRÉDITO
        for i, tx in enumerate(transactions):
            if tx.get("id") == transaction_id:
                transactions[i]["credited"] = True
                transactions[i]["credited_at"] = datetime.now().isoformat()
                transactions[i]["credit_amount_usdt"] = usdt_amount
                transactions[i]["credit_amount_btc"] = btc_amount
                break
        
        save_transactions(transactions)
        
        print(f"✅ Depositado: {btc_amount:.8f} BTC ({usdt_amount:.2f} USDT)")
        print(f"   Saldo USDT depois: {wallet['balanceUSDT']:.2f}")
        print(f"   BTC depois: {wallet['balanceBTC']:.8f}")
        
        # Sincronizar com participação
        sync_wallet_balance_to_participation(wallet_code)
        
        # Auditoria
        audit_event(
            action="DEPOSITO_CORRIGIDO_CONFIRMADO",
            success=True,
            user_code=wallet_code,
            details=f"Depósito BTC corrigido: {btc_amount:.8f} BTC = {usdt_amount:.2f} USDT",
            extra={
                "transaction_id": transaction_id,
                "btc_amount": btc_amount,
                "usdt_amount": usdt_amount,
                "btc_usdt_rate": usdt_amount / btc_amount if btc_amount > 0 else 0,
                "idempotent_check": "unique_credit_applied"
            }
        )
        
        return True
        
    except Exception as e:
        print(f"💥 Erro ao confirmar: {e}")
        return False

def emergency_credit(wallet_code, amount_usdt):
    """Crédito emergencial manual - COM TRAVAS DE IDEMPOTÊNCIA - Em USDT"""
    try:
        wallets = load_wallets()
        
        if wallet_code not in wallets:
            print(f"❌ Carteira não encontrada: {wallet_code}")
            return False
        
        wallet = wallets[wallet_code]
        
        # 🔥 TRAVA 1: Verificar se JÁ houve crédito emergencial HOJE
        last_credit = wallet.get("last_emergency_credit", "")
        if last_credit:
            last_date = datetime.fromisoformat(last_credit).date()
            today = datetime.now().date()
            if last_date == today:
                print(f"🚨 CRÉDITO EMERGENCIAL JÁ APLICADO HOJE PARA {wallet_code}")
                print(f"   Último crédito: {last_credit}")
                return False
        
        # 🔥 TRAVA 2: Verificar limite diário (em USDT)
        daily_limit_usdt = 2000.00  # ~R$ 10,000
        if amount_usdt > daily_limit_usdt:
            print(f"🚨 VALOR EXCEDE LIMITE DIÁRIO: {daily_limit_usdt:.2f} USDT")
            return False
        
        # 🔥 TRAVA 3: Verificar na lista de créditos aplicados
        if "creditos_emergenciais" not in wallet:
            wallet["creditos_emergenciais"] = []
        
        # Verificar se crédito IDÊNTICO já foi aplicado
        for credito in wallet["creditos_emergenciais"]:
            if (credito.get("amount_usdt") == amount_usdt and 
                credito.get("date") == datetime.now().date().isoformat()):
                print(f"🚨 CRÉDITO IDÊNTICO JÁ APLICADO HOJE")
                return False
        
        saldo_antes_usdt = wallet.get("balanceUSDT", 0.0)
        deposito_antes_usdt = wallet.get("totalDepositedUSDT", 0.0)
        
        # Atualizar saldo
        wallet["balanceUSDT"] = saldo_antes_usdt + amount_usdt
        wallet["totalDepositedUSDT"] = deposito_antes_usdt + amount_usdt
        wallet["updated_at"] = datetime.now().isoformat()
        wallet["last_emergency_credit"] = datetime.now().isoformat()
        
        # 🔥 REGISTRAR NA LISTA DE CRÉDITOS
        wallet["creditos_emergenciais"].append({
            "amount_usdt": amount_usdt,
            "date": datetime.now().date().isoformat(),
            "timestamp": datetime.now().isoformat(),
            "balance_before_usdt": saldo_antes_usdt,
            "balance_after_usdt": wallet["balanceUSDT"]
        })
        
        # Manter apenas últimos 10 créditos
        if len(wallet["creditos_emergenciais"]) > 10:
            wallet["creditos_emergenciais"] = wallet["creditos_emergenciais"][-10:]
        
        # Registrar transação
        register_transaction(
            user_code=wallet_code,
            tx_type="emergency_credit",
            amount_usdt=amount_usdt,
            currency="USDT",
            status="confirmed",
            note=f"Crédito emergencial manual - ID: {datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        
        # Salvar alterações
        save_wallets(wallets)
        
        # Auditoria
        audit_event(
            action="EMERGENCY_CREDIT",
            success=True,
            user_code=wallet_code,
            details=f"Crédito emergencial aplicado: {amount_usdt:.2f} USDT",
            extra={
                "amount_usdt": amount_usdt,
                "balance_before_usdt": saldo_antes_usdt,
                "balance_after_usdt": wallet["balanceUSDT"],
                "total_deposited_before_usdt": deposito_antes_usdt,
                "total_deposited_after_usdt": wallet["totalDepositedUSDT"],
                "idempotent_check": "daily_limit_enforced"
            }
        )
        
        print(f"✅ CRÉDITO EMERGENCIAL APLICADO COM SUCESSO: {wallet_code}")
        print(f"   📊 Saldo USDT antes: {saldo_antes_usdt:.2f}")
        print(f"   📊 Saldo USDT depois: {wallet['balanceUSDT']:.2f}")
        print(f"   📊 Total depositado USDT: {wallet['totalDepositedUSDT']:.2f}")
        print(f"   ⚠️  TRAVAS ATIVAS: Limite diário {daily_limit_usdt:.2f} USDT")
        
        # Sincronizar com participação
        sync_wallet_balance_to_participation(wallet_code)
        
        return True
        
    except Exception as e:
        print(f"💥 ERRO CRÍTICO em emergency_credit: {e}")
        audit_event(
            action="EMERGENCY_CREDIT_ERROR",
            success=False,
            user_code=wallet_code,
            details=f"Erro ao aplicar crédito: {str(e)}"
        )
        return False

# ============================================
# 🔥 NOVOS ENDPOINTS PARA SISTEMA DE PARTICIPAÇÕES (USDT)
# ============================================




@app.route("/debug/participation/<user_code>")
def debug_participation(user_code):
    participations = load_participations()
    return jsonify(participations.get(user_code, {}))


@app.route('/api/participation/withdraw', methods=['POST'])
def api_user_withdraw():
    """Endpoint para saque do usuário - COM VALIDAÇÃO DE EQUITY em USDT"""
    try:
        data = request.get_json(silent=True) or {}
        
        user_code = data.get('user_code', '').strip().upper()
        amount_usdt = float(data.get('amount_usdt', 0))
        btc_address = data.get('btc_address', '').strip()
        
        if not user_code:
            return jsonify({"success": False, "message": "Código do usuário é obrigatório"}), 400
        
        if amount_usdt <= 0:
            return jsonify({"success": False, "message": "Valor inválido"}), 400
        
        if not btc_address:
            return jsonify({"success": False, "message": "Endereço BTC é obrigatório"}), 400
        
        # 🔥 Usar função corrigida com validação de equity
        result = process_user_withdrawal_usdt(user_code, amount_usdt, btc_address)
        
        if result["success"]:
            return jsonify({
                "success": True,
                "message": result["message"],
                "transaction_id": result["transaction_id"],
                "virtual_balance_usdt": result["virtual_balance_usdt"],
                "equity_check": result.get("equity_check", {})
            })
        else:
            return jsonify({
                "success": False,
                "message": result["message"]
            }), 400
        
    except Exception as e:
        logger.error(f"💥 Erro em /api/participation/withdraw: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500

@app.route('/api/participation/reinvest', methods=['POST'])
def api_user_reinvest():
    """Endpoint para reinvestimento de lucros - SEM DIA 7, em USDT"""
    try:
        data = request.get_json(silent=True) or {}
        
        user_code = data.get('user_code', '').strip().upper()
        amount_usdt = float(data.get('amount_usdt', 0))
        
        if not user_code:
            return jsonify({"success": False, "message": "Código do usuário é obrigatório"}), 400
        
        if amount_usdt <= 0:
            return jsonify({"success": False, "message": "Valor inválido"}), 400
        
        participations = load_participations()
        
        if user_code not in participations:
            return jsonify({"success": False, "message": "Participação não encontrada"}), 404
        
        participation = participations[user_code]
        
        # Verificar saldo disponível para reinvestir (profit_accumulated_usdt)
        available_for_reinvestment_usdt = participation.get("profit_accumulated_usdt", 0)
        
        if amount_usdt > available_for_reinvestment_usdt:
            return jsonify({
                "success": False, 
                "message": f"Saldo de lucro insuficiente. Disponível: {available_for_reinvestment_usdt:.2f} USDT"
            }), 400
        
        # Atualizar participação
        participation["profit_accumulated_usdt"] -= amount_usdt
        participation["total_deposited_usdt"] += amount_usdt
        participation["virtual_balance_usdt"] += amount_usdt
        participation["updated_at"] = datetime.now().isoformat()
        
        # Recalcular share percent
        master_balance_usdt = get_master_wallet_balance_usdt()
        participation["share_percent"] = calculate_share_percent(
            participation["total_deposited_usdt"],
            master_balance_usdt
        )
        
        # Registrar transação
        transactions = load_transactions()
        transaction_id = f"REINV-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        reinvest_transaction = {
            "id": transaction_id,
            "user_code": user_code,
            "wallet_code": user_code,
            "type": "reinvestment",
            "amount_usdt": amount_usdt,
            "date": datetime.now().isoformat(),
            "status": "completed",
            "description": f"Reinvestimento de lucros",
            "currency": "USDT"
        }
        
        transactions.append(reinvest_transaction)
        
        # Salvar alterações
        save_participations(participations)
        save_transactions(transactions)
        
        # Auditoria
        audit_event(
            action="REINVESTMENT",
            success=True,
            user_code=user_code,
            details=f"Reinvestimento de {amount_usdt:.2f} USDT",
            extra={
                "transaction_id": transaction_id,
                "new_share_percent": participation["share_percent"],
                "currency": "USDT"
            }
        )
        
        return jsonify({
            "success": True,
            "message": "Reinvestimento realizado com sucesso",
            "transaction_id": transaction_id,
            "new_share_percent": participation["share_percent"],
            "profit_accumulated_usdt": participation["profit_accumulated_usdt"]
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /api/participation/reinvest: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500

@app.route('/favicon.ico')
def favicon():
    """Serve o favicon para evitar erro 404"""
    try:
        favicon_path = os.path.join(BASE_DIR, 'favicon.ico')
        if os.path.exists(favicon_path):
            return send_file(favicon_path, mimetype='image/vnd.microsoft.icon')
        else:
            return '', 204
    except:
        return '', 204

@app.route("/api/admin/participations", methods=["GET"])
def api_admin_participations():
    if not verify_admin_key(request):
        return jsonify({"success": False, "message": "Acesso não autorizado"}), 403

    participations = load_participations()
    usdt_brl = get_current_usdt_brl()

    out = []
    total_virtual_usdt = 0.0
    for code, p in participations.items():
        v = float(p.get("virtual_balance_usdt", 0) or 0)
        total_virtual_usdt += v
        out.append({
            "user_code": code,
            "virtual_balance_usdt": round(v, 6),
            "virtual_balance_brl": round(v * usdt_brl, 2),
            "total_deposited_usdt": round(float(p.get("total_deposited_usdt", 0) or 0), 6),
            "profit_accumulated_usdt": round(float(p.get("profit_accumulated_usdt", 0) or 0), 6),
            "share_percent": round(float(p.get("share_percent", 0) or 0), 6),
            "status": p.get("status", "active"),
            "updated_at": p.get("updated_at", ""),
        })

    out.sort(key=lambda x: x["virtual_balance_usdt"], reverse=True)

    return jsonify({
        "success": True,
        "count": len(out),
        "total_virtual_balance_usdt": round(total_virtual_usdt, 6),
        "total_virtual_balance_brl": round(total_virtual_usdt * usdt_brl, 2),
        "participations": out,
    }), 200
@app.route('/api/process-monthly-cycle', methods=['POST'])
def api_process_admin_cycle():
    """Endpoint para forçar processamento do ciclo mensal (apenas admin)"""
    try:
        # Verificar admin
        if not verify_admin_key(request):
            return jsonify({"success": False, "message": "Acesso não autorizado"}), 403
        
        success = process_monthly_cycle()
        
        if success:
            return jsonify({
                "success": True,
                "message": "Ciclo mensal processado com sucesso"
            })
        else:
            return jsonify({
                "success": False,
                "message": "Não foi possível processar o ciclo mensal (verifique logs)"
            }), 400
            
    except Exception as e:
        logger.error(f"💥 Erro em /api/process-monthly-cycle: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500

# ============================================
# 🔥 FUNÇÕES PARA GESTÃO DE CARTEIRAS (USDT CORE)
# ============================================

def load_wallets() -> Dict:
    """Carrega todas as carteiras do arquivo com migração automática para USDT"""
    if not os.path.exists(WALLETS_FILE):
        logger.info(f"📁 Arquivo {WALLETS_FILE} não encontrado, criando novo...")
        # Criar carteiras base iniciais
        base_wallets = {}
        for wallet_id in BASE_WALLETS:
            base_wallets[wallet_id] = {
                "id": wallet_id,
                "code": wallet_id,
                "name": f"Carteira Base {wallet_id}",
                "balanceUSDT": 0.0,
                "balanceBTC": 0.0,
                "totalDepositedUSDT": 0.0,
                "totalDepositedBTC": 0.0,
                "totalProfitUSDT": 0.0,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "phrase_hash": "",
                "active": True,
                "is_base_wallet": True
            }
        
        with open(WALLETS_FILE, 'w', encoding='utf-8') as f:
            json.dump(base_wallets, f, indent=2, ensure_ascii=False)
        return base_wallets
    
    try:
        with open(WALLETS_FILE, 'r', encoding='utf-8') as f:
            wallets = json.load(f)
        
        # 🔥 MIGRAÇÃO AUTOMÁTICA BRL → USDT
        usdt_brl = get_current_usdt_brl()
        migrated = False
        
        for code, wallet in wallets.items():
            if "balanceBRL" in wallet and "balanceUSDT" not in wallet:
                wallet["balanceUSDT"] = wallet.get("balanceBRL", 0) / usdt_brl
                migrated = True
            if "totalDeposited" in wallet and "totalDepositedUSDT" not in wallet:
                wallet["totalDepositedUSDT"] = wallet.get("totalDeposited", 0) / usdt_brl
                migrated = True
            if "totalProfit" in wallet and "totalProfitUSDT" not in wallet:
                wallet["totalProfitUSDT"] = wallet.get("totalProfit", 0) / usdt_brl
                migrated = True
            
            # Garantir campos USDT
            if "balanceUSDT" not in wallet:
                wallet["balanceUSDT"] = 0.0
                migrated = True
            if "totalDepositedUSDT" not in wallet:
                wallet["totalDepositedUSDT"] = 0.0
                migrated = True
            if "totalProfitUSDT" not in wallet:
                wallet["totalProfitUSDT"] = 0.0
                migrated = True
        
        if migrated:
            save_wallets(wallets)
            logger.info("✅ Carteiras migradas para USDT CORE")
        
        return wallets
    except Exception as e:
        logger.error(f"❌ Erro ao carregar wallets.json: {e}")
        return {}

def save_wallets(wallets: Dict) -> bool:
    """Salva todas as carteiras no arquivo"""
    backup_path = WALLETS_FILE + ".bak"
    
    if os.path.exists(WALLETS_FILE):
        try:
            shutil.copy2(WALLETS_FILE, backup_path)
        except Exception as e:
            logger.error(f"❌ Erro ao criar backup: {e}")
    
    try:
        with open(WALLETS_FILE, 'w', encoding='utf-8') as f:
            json.dump(wallets, f, indent=2, ensure_ascii=False)
        logger.info(f"💾 {len(wallets)} carteiras salvas")
        return True
    except Exception as e:
        logger.error(f"❌ Erro ao salvar carteiras: {e}")
        return False

# ============================================
# 🔥 FUNÇÕES PARA MASTER WALLET (USDT CORE)
# ============================================

def load_master_wallet_data() -> Dict:
    """Carrega dados da carteira master em USDT"""
    default_master = {
        "total_profit_received_usdt": 0.0,
        "balance_available_usdt": 0.0,
        "total_deposited_usdt": 0.0,
        "total_withdrawn_usdt": 0.0,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "profit_distributions": [],
        "rules": [
            "Recebe EXATAMENTE 50% do lucro líquido",
            "Pode crescer, sacar, reinvestir",
            "NÃO mistura com carteiras de usuários",
            "CURRENCY: USDT"
        ]
    }
    
    if not os.path.exists(MASTER_WALLET_FILE):
        logger.info("📁 Criando arquivo master_wallet.json...")
        save_master_wallet_data(default_master)
        return default_master
    
    try:
        with open(MASTER_WALLET_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # Garantir campos obrigatórios em USDT
            for key in default_master.keys():
                if key not in data:
                    data[key] = default_master[key]
            return data
    except Exception as e:
        logger.error(f"❌ Erro ao carregar master_wallet: {e}")
        return default_master

def save_master_wallet_data(master_data: Dict) -> bool:
    """Salva dados da carteira master"""
    try:
        master_data["updated_at"] = datetime.now().isoformat()
        with open(MASTER_WALLET_FILE, 'w', encoding='utf-8') as f:
            json.dump(master_data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"❌ Erro ao salvar master_wallet: {e}")
        return False

# ============================================
# 🔥 SISTEMA DE DISTRIBUIÇÃO DE LUCRO (USDT CORE)
# ============================================

def calculate_profit_distribution(profit_amount_usdt: float) -> Dict:
    """Calcula distribuição do lucro conforme regras"""
    return {
        "total_profit_usdt": profit_amount_usdt,
        "master_share_usdt": profit_amount_usdt * PROFIT_DISTRIBUTION["company_fee"],
        "users_share_usdt": profit_amount_usdt * PROFIT_DISTRIBUTION["users_pool"],
        "distribution_date": datetime.now().isoformat(),
        "currency": "USDT"
    }

def register_profit_distribution(profit_data: Dict):
    """Registra uma distribuição de lucro no histórico"""
    try:
        distributions = []
        if os.path.exists(PROFIT_DISTRIBUTION_FILE):
            with open(PROFIT_DISTRIBUTION_FILE, 'r', encoding='utf-8') as f:
                try:
                    distributions = json.load(f)
                except:
                    distributions = []
        
        distributions.append(profit_data)
        
        # Manter apenas últimas 100 distribuições
        if len(distributions) > 100:
            distributions = distributions[-100:]
        
        with open(PROFIT_DISTRIBUTION_FILE, 'w', encoding='utf-8') as f:
            json.dump(distributions, f, indent=2, ensure_ascii=False)
        
        logger.info(f"📊 Distribuição registrada: {profit_data['total_profit_usdt']:.2f} USDT")
        return True
    except Exception as e:
        logger.error(f"❌ Erro ao registrar distribuição: {e}")
        return False

def load_transactions() -> List[dict]:
    data = _read_json(TRANSACTIONS_FILE, default=[])
    return data if isinstance(data, list) else []

def save_transactions(transactions: List[dict]) -> bool:
    return _write_json(TRANSACTIONS_FILE, transactions)

def generate_wallet_code(phrase: str) -> str:
    """Gera um código único para a carteira a partir da frase"""
    phrase_hash = hashlib.sha256(phrase.encode('utf-8')).hexdigest()
    code = phrase_hash[:12].upper()
    return f"ROBO-{code}"

def load_participations() -> Dict[str, dict]:
    """
    Aceita:
      - dict: {"ROBO-XXXX": {...}}
      - list: [{"wallet_code": "...", ...}] (legado)
    Normaliza para dict, aplica defaults USDT CORE e corrige saldo negativo.
    """
    raw = _read_json(PARTICIPATIONS_FILE, default={})

    # legado: lista -> dict
    if isinstance(raw, list):
        converted = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            wc = (item.get("wallet_code") or item.get("user_code") or "").strip().upper()
            if not wc:
                continue
            converted[wc] = item
        raw = converted

    if not isinstance(raw, dict):
        logger.error("participations.json inválido (não é dict/list).")
        return {}

    usdt_brl = get_current_usdt_brl() or 1.0
    migrated = False

    for code, p in raw.items():
        if not isinstance(p, dict):
            raw[code] = {}
            p = raw[code]
            migrated = True

        # garantir wallet_code interno
        code_up = str(code).strip().upper()
        if p.get("wallet_code") != code_up:
            p["wallet_code"] = code_up
            migrated = True

        # defaults
        p.setdefault("active", True)
        p.setdefault("status", "active")
        p.setdefault("share", 1)
        p.setdefault("share_percent", p.get("share_percent", 0.0))
        p.setdefault("created_at", datetime.utcnow().isoformat())
        p.setdefault("updated_at", datetime.utcnow().isoformat())
        p.setdefault("last_profit_distribution", None)
        p.setdefault("master_wallet", MASTER_WALLET_CODES[0] if MASTER_WALLET_CODES else "MASTER")

        # migração BRL -> USDT (se existirem campos legados)
        if "virtual_balance" in p and "virtual_balance_usdt" not in p:
            p["virtual_balance_usdt"] = float(p.get("virtual_balance", 0) or 0) / usdt_brl
            migrated = True
        if "total_deposited" in p and "total_deposited_usdt" not in p:
            p["total_deposited_usdt"] = float(p.get("total_deposited", 0) or 0) / usdt_brl
            migrated = True
        if "profit_accumulated" in p and "profit_accumulated_usdt" not in p:
            p["profit_accumulated_usdt"] = float(p.get("profit_accumulated", 0) or 0) / usdt_brl
            migrated = True
        if "total_withdrawn" in p and "total_withdrawn_usdt" not in p:
            p["total_withdrawn_usdt"] = float(p.get("total_withdrawn", 0) or 0) / usdt_brl
            migrated = True

        # garantir campos USDT
        for k in ["virtual_balance_usdt", "total_deposited_usdt", "profit_accumulated_usdt", "total_withdrawn_usdt"]:
            if k not in p:
                p[k] = 0.0
                migrated = True

        # integridade: saldo nunca negativo
        if float(p.get("virtual_balance_usdt", 0) or 0) < 0:
            logger.error(f"SALDO NEGATIVO em {code_up}. Corrigindo para 0.")
            p["virtual_balance_usdt"] = 0.0
            p["updated_at"] = datetime.utcnow().isoformat()
            migrated = True

    if migrated:
        save_participations(raw)
        logger.info("Participations normalizado/migrado (USDT CORE).")

    return raw

# (primeira definição de distribuir_lucro_proporcional e função órfã
#  distribuir_prejuizo_proporcional removidas — usada apenas a versão
#  completa com audit_event definida abaixo, linha ~3207)






def is_day_7() -> bool:
    """Verifica se hoje é dia 7 (horário de São Paulo) - MANTIDO PARA COMPATIBILIDADE"""
    try:
        now = datetime.now()
        sao_paulo = pytz.timezone('America/Sao_Paulo')
        now_sp = now.astimezone(sao_paulo)
        return now_sp.day == 7
    except:
        return datetime.now().day == 7

def verify_admin_key(request) -> bool:
    """Verifica se a requisição tem a chave de admin válida"""
    admin_key = request.headers.get(ADMIN_TOKEN_HEADER)
    if not admin_key:
        admin_key = request.args.get('key')
    
    return admin_key == ADMIN_SECRET_KEY

def audit_event(action: str, success: bool = True, user_code: str = "", details: str = "", extra: dict | None = None):
    """Registra evento de auditoria no buffer em tempo real e persiste em arquivo"""
    extra_data = extra or {}
    if user_code and user_code in active_sessions:
        extra_data["session_id"] = active_sessions[user_code].get("session_id", "")
    
    entry = {
        "timestamp": datetime.now().isoformat(),
        "ip": request.remote_addr if request else "unknown",
        "action": action,
        "success": bool(success),
        "user_code": (user_code or "").strip().upper(),
        "details": details or "",
        "user_agent": request.headers.get("User-Agent", "Desconhecido") if request else "unknown",
        "extra": extra_data
    }

    # 1) buffer em memória (últimos 50)
    realtime_logs.append(entry)

    # 2) persistência em arquivo
    try:
        data = []
        if os.path.exists(REALTIME_LOGS_FILE):
            with open(REALTIME_LOGS_FILE, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f) or []
                except:
                    data = []
        data.append(entry)
        if len(data) > 2000:
            data = data[-2000:]
        with open(REALTIME_LOGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Erro ao persistir audit_event: {e}")

# ============================================
# 🔥 SISTEMA DE SESSÕES DETALHADAS
# ============================================

active_sessions = {}
SESSION_TIMEOUT = 300
sessions_history = defaultdict(list)

def load_sessions():
    """Carrega histórico de sessões do arquivo"""
    if not os.path.exists(SESSIONS_FILE):
        return {}
    try:
        with open(SESSIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"❌ Erro ao carregar sessions.json: {e}")
        return {}

def save_sessions():
    """Salva histórico de sessões no arquivo"""
    try:
        with open(SESSIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(sessions_history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"❌ Erro ao salvar sessions.json: {e}")

def register_session_event(user_code: str, event_type: str, session_data: dict):
    """Registra evento de sessão"""
    try:
        user_code = user_code.upper()
        
        session_data_to_save = {
            "session_id": session_data.get("session_id", ""),
            "login_time": session_data.get("login_time", time.time()),
            "last_ping": session_data.get("last_ping", time.time()),
            "ip_address": session_data.get("ip_address", request.remote_addr if request else "unknown"),
            "user_agent": session_data.get("user_agent", request.headers.get("User-Agent", "Desconhecido") if request else "unknown"),
            "plan": session_data.get("plan", "trial")
        }
        
        event = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "session_data": session_data_to_save,
            "ip": request.remote_addr if request else "unknown",
            "user_agent": request.headers.get("User-Agent", "Desconhecido") if request else "unknown"
        }
        
        sessions_history[user_code].append(event)
        
        if len(sessions_history[user_code]) > 100:
            sessions_history[user_code] = sessions_history[user_code][-100:]
        
        if len(sessions_history[user_code]) % 10 == 0:
            save_sessions()
            
        audit_event(
            action=f"SESSION_{event_type}",
            success=True,
            user_code=user_code,
            details=f"Sessão {event_type.lower()}",
            extra=session_data_to_save
        )
        
    except Exception as e:
        logger.error(f"Erro ao registrar evento de sessão: {e}")

def get_active_sessions_list():
    """Retorna lista de sessões ativas formatada"""
    cleanup_expired_sessions()
    
    sessions_list = []
    now = time.time()
    
    for code, sess in active_sessions.items():
        login_time = sess.get("login_time", now)
        last_ping = sess.get("last_ping", now)
        minutes_since_ping = (now - last_ping) / 60
        
        status = "online"
        if minutes_since_ping > 5:
            status = "expired"
        elif minutes_since_ping > 2:
            status = "inactive"
        
        sessions_list.append({
            "user_code": code,
            "session_id": sess.get("session_id", ""),
            "ip_address": sess.get("ip_address", request.remote_addr if request else "unknown"),
            "user_agent": sess.get("user_agent", request.headers.get("User-Agent", "Desconhecido") if request else "unknown"),
            "login_time": login_time,
            "last_ping": last_ping,
            "status": status,
            "plan": sess.get("plan", "trial")
        })
    
    return sessions_list

def cleanup_expired_sessions() -> int:
    """Remove sessões expiradas"""
    now = time.time()
    expired = []
    
    for code, sess in active_sessions.items():
        last_ping = sess.get("last_ping", now)
        if (now - last_ping) > SESSION_TIMEOUT:
            expired.append(code)
    
    for code in expired:
        if code in active_sessions:
            register_session_event(
                user_code=code,
                event_type="EXPIRED",
                session_data=active_sessions[code]
            )
            audit_event(
                action="SESSION_EXPIRED",
                success=False,
                user_code=code,
                details="Sessão expirada por inatividade",
                extra={"last_ping": active_sessions[code].get("last_ping", now)}
            )
            del active_sessions[code]
        logger.info(f"🧹 Sessão expirada: {code}")
    
    return len(expired)

def is_session_active(code: str, session_id: str = None) -> bool:
    """Verifica se sessão está ativa"""
    cleanup_expired_sessions()
    
    if code not in active_sessions:
        return False
    
    sess = active_sessions[code]
    
    if session_id and sess.get("session_id") != session_id:
        return False
    
    last_ping = sess.get("last_ping", 0)
    if (time.time() - last_ping) > SESSION_TIMEOUT:
        register_session_event(
            user_code=code,
            event_type="EXPIRED",
            session_data=sess
        )
        audit_event(
            action="SESSION_EXPIRED",
            success=False,
            user_code=code,
            details="Sessão expirada na verificação",
            extra={"last_ping": last_ping}
        )
        del active_sessions[code]
        return False
    
    return True

# ============================================
# 🔥 SISTEMA DE STATUS DO USUÁRIO
# ============================================

def load_user_status():
    """Carrega status dos usuários do arquivo"""
    if not os.path.exists(USER_STATUS_FILE):
        return {}
    try:
        with open(USER_STATUS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"❌ Erro ao carregar user_status.json: {e}")
        return {}

def save_user_status():
    """Salva status dos usuários no arquivo"""
    try:
        with open(USER_STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(user_status, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"❌ Erro ao salvar user_status.json: {e}")

user_status = load_user_status()

def update_user_status(user_code: str, update_data: dict):
    """Atualiza status do usuário"""
    try:
        user_code = user_code.upper()
        
        if user_code not in user_status:
            user_status[user_code] = {
                "user_code": user_code,
                "last_access": "",
                "sessions": [],
                "recent_searches": [],
                "favorites": [],
                "invalid_attempts": [],
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }
        
        current_status = user_status[user_code]
        for field in ["recent_searches", "favorites", "invalid_attempts", "sessions"]:
            if field not in current_status:
                current_status[field] = []
        
        for key, value in update_data.items():
            if key == "recent_searches":
                if isinstance(value, dict):
                    search_entry = {
                        "symbol": value.get("symbol", ""),
                        "timestamp": value.get("timestamp", datetime.now().isoformat()),
                        "ip": value.get("ip", request.remote_addr if request else "unknown"),
                        "source": value.get("source", "unknown")
                    }
                    current_status[key].insert(0, search_entry)
                    if len(current_status[key]) > 20:
                        current_status[key] = current_status[key][:20]
            elif key == "favorites":
                if isinstance(value, dict):
                    action = value.get("action")
                    symbol = value.get("symbol", "")
                    if action == "add" and symbol:
                        exists = any(fav.get("symbol") == symbol for fav in current_status[key])
                        if not exists:
                            current_status[key].append({
                                "symbol": symbol,
                                "added_at": value.get("added_at", datetime.now().isoformat()),
                                "ip": value.get("ip", request.remote_addr if request else "unknown")
                            })
                    elif action == "remove" and symbol:
                        current_status[key] = [fav for fav in current_status[key] if fav.get("symbol") != symbol]
            elif key == "invalid_attempts":
                if isinstance(value, dict):
                    attempt_entry = {
                        "reason": value.get("reason", ""),
                        "timestamp": value.get("timestamp", datetime.now().isoformat()),
                        "ip": value.get("ip", request.remote_addr if request else "unknown")
                    }
                    current_status[key].append(attempt_entry)
                    if len(current_status[key]) > 10:
                        current_status[key] = current_status[key][:10]
            elif key == "sessions":
                if isinstance(value, dict):
                    session_summary = {
                        "session_id": value.get("session_id", ""),
                        "login_time": value.get("login_time", datetime.now().isoformat()),
                        "logout_time": value.get("logout_time", ""),
                        "ip_address": value.get("ip_address", request.remote_addr if request else "unknown"),
                        "status": value.get("status", "unknown")
                    }
                    current_status[key].append(session_summary)
                    if len(current_status[key]) > 50:
                        current_status[key] = current_status[key][:50]
            else:
                current_status[key] = value
        
        current_status["updated_at"] = datetime.now().isoformat()
        
        if len(user_status) % 5 == 0:
            save_user_status()
            
    except Exception as e:
        logger.error(f"Erro ao atualizar status do usuário: {e}")

def get_user_status(user_code: str) -> dict:
    """Retorna status completo do usuário"""
    user_code = user_code.upper()
    
    if user_code not in user_status:
        return {
            "user_code": user_code,
            "last_access": "",
            "sessions": [],
            "recent_searches": [],
            "favorites": [],
            "invalid_attempts": [],
            "created_at": "",
            "updated_at": ""
        }
    
    status = user_status[user_code].copy()
    for field in ["recent_searches", "favorites", "invalid_attempts", "sessions"]:
        if field not in status:
            status[field] = []
    
    return status
trade_locks = set()

def acquire_trade_lock(trade_id):
    if trade_id in trade_locks:
        return False
    trade_locks.add(trade_id)
    return True


def release_trade_lock(trade_id):
    trade_locks.discard(trade_id)

def trade_ja_processado(trade_id, transactions):
    # Verificação 1: trade_id exato já processado
    if any(
        tx.get("trade_id") == trade_id and
        tx.get("type") in ["profit_distribution", "loss_distribution"]
        for tx in transactions
    ):
        return True

    # Verificação 2: mesmo símbolo+timestamp_entrada fechado nos últimos 60 segundos
    # Evita que o scanner envie o mesmo fechamento com IDs ligeiramente diferentes
    # trade_id formato: SYMBOLUSDT_ENTRADA_SAIDA ex: SOLUSDT_20260219_081952_20260219_161149
    try:
        parts = trade_id.split("_")
        if len(parts) >= 4:
            # símbolo + data_entrada + hora_entrada = chave base do trade
            chave_base = "_".join(parts[:3])  # ex: SOLUSDT_20260219_081952
            from datetime import datetime, timedelta
            agora = datetime.now()
            janela = timedelta(seconds=90)
            for tx in transactions:
                tid2 = tx.get("trade_id", "")
                if (tx.get("type") in ["profit_distribution", "loss_distribution"]
                        and tid2.startswith(chave_base)
                        and tid2 != trade_id):
                    # Verificar se foi processado recentemente
                    try:
                        dt2 = datetime.fromisoformat(tx.get("date", ""))
                        if agora - dt2 < janela:
                            logger.warning(
                                f"⚠️ Trade duplicado bloqueado: {trade_id} "
                                f"(já processado como {tid2} em {tx.get('date','')})"
                            )
                            return True
                    except Exception:
                        pass
    except Exception:
        pass

    return False



def auditoria_divergencia(wallets, participations):
    divergencias = []

    for code, p in participations.items():
        saldo_participacao = p.get("virtual_balance_usdt", 0)
        saldo_wallet = wallets.get(code, {}).get("balanceUSDT", 0)

        if abs(saldo_participacao - saldo_wallet) > 0.01:
            divergencias.append({
                "wallet": code,
                "wallet_balance": saldo_wallet,
                "participation_balance": saldo_participacao
            })

    if divergencias:
        logger.error("🚨 Divergência detectada")
        audit_event(
            action="DIVERGENCIA_SALDO",
            success=False,
            details="Diferença entre wallets e participations",
            extra={"divergencias": divergencias}
        )


# ============================================
# 🔥 SISTEMA ANTIFRAUDE
# ============================================

def normalize_email(email: str) -> str:
    """Normaliza email: lowercase, trim"""
    if not email:
        return ""
    return email.strip().lower()

def normalize_phone(phone: str) -> str:
    """Normaliza telefone: apenas números, padrão E.164 se possível"""
    if not phone:
        return ""
    
    numbers = re.sub(r'\D', '', phone)
    
    if numbers.startswith('0'):
        numbers = numbers[1:]
    
    if len(numbers) == 11 and numbers[2] == '9':
        return f"+55{numbers}"
    elif len(numbers) == 10:
        return f"+55{numbers}"
    elif phone.startswith('+'):
        return phone
    
    return numbers

def generate_fingerprint(request) -> str:
    """Gera fingerprint SHA256"""
    try:
        user_agent = request.headers.get('User-Agent', 'unknown')
        ip_address = request.remote_addr or '0.0.0.0'
        accept_language = request.headers.get('Accept-Language', 'unknown')
        
        ip_parts = ip_address.split('.')
        if len(ip_parts) == 4:
            ip_partial = f"{ip_parts[0]}.{ip_parts[1]}.x.x"
        else:
            ip_partial = ip_address
        
        data_to_hash = f"{user_agent}|{ip_partial}|{accept_language}"
        fingerprint = hashlib.sha256(data_to_hash.encode('utf-8')).hexdigest()
        
        return fingerprint
        
    except Exception as e:
        logger.error(f"❌ Erro ao gerar fingerprint: {e}")
        fallback_data = f"fallback|{time.time()}|{secrets.token_hex(8)}"
        return hashlib.sha256(fallback_data.encode('utf-8')).hexdigest()

def final_system_check():
    """Verificação final do sistema antes de fechar"""
    try:
        logger.info("=" * 70)
        logger.info("🔍 VERIFICAÇÃO FINAL DO SISTEMA")
        logger.info("=" * 70)
        
        share_check = verify_and_fix_share_percent()
        if not share_check.get("success", False):
            logger.error("❌ FALHA: Problema com share_percent")
            return False
        
        save_check = verify_save_order()
        if not save_check:
            logger.error("❌ FALHA: Problema com salvamento")
            return False
        
        participations = load_participations()
        wallets = load_wallets()
        transactions = load_transactions()
        
        logger.info("📊 ESTATÍSTICAS ATUAIS (USDT):")
        logger.info(f"   👤 Participações ativas: {len([p for p in participations.values() if p.get('status') == 'active'])}")
        logger.info(f"   💰 Carteiras: {len(wallets)}")
        logger.info(f"   📝 Transações: {len(transactions)}")
        
        trade_ids = []
        duplicates = []
        for tx in transactions:
            if tx.get("type") == "trade_closed":
                trade_id = tx.get("trade_id")
                if trade_id in trade_ids:
                    duplicates.append(trade_id)
                trade_ids.append(trade_id)
        
        if duplicates:
            logger.error(f"❌ ATENÇÃO: {len(duplicates)} trades duplicados encontrados!")
            for dup in duplicates[:5]:
                logger.error(f"   • {dup}")
        else:
            logger.info("✅ Nenhum trade duplicado encontrado")
        
        logger.info("📋 MODELO FINAL DE RATEIO (USDT):")
        logger.info("   ✅ Usuários usam share_percent da participação")
        logger.info("   ✅ share_percent em % (15.5 = 15.5%)")
        logger.info("   ✅ Se share_percent = 0, fallback proporcional ao virtual_balance_usdt")
        logger.info("   ✅ 50% admin / 50% usuários")
        logger.info("   ✅ PnL negativo descontado do virtual_balance_usdt")
        logger.info("   ✅ CURRENCY: USDT")
        
        logger.info("=" * 70)
        logger.info("✅✅✅ SISTEMA VERIFICADO E PRONTO PARA PRODUÇÃO (USDT CORE)")
        logger.info("=" * 70)
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Erro na verificação final: {e}")
        return False

def verify_save_order():
    """Verifica se a ordem de salvamento está correta"""
    try:
        logger.info("🔍 Verificando ordem de salvamento...")
        
        correct_order = ["participations", "wallets", "transactions"]
        
        logger.info("✅ Ordem correta de salvamento:")
        logger.info(f"   1. {correct_order[0]} - Primeiro saldos virtuais")
        logger.info(f"   2. {correct_order[1]} - Depois carteiras master") 
        logger.info(f"   3. {correct_order[2]} - Por último histórico")
        
        test_data = {"test": "data", "timestamp": datetime.now().isoformat()}
        
        # Testar save_participations
        try:
            original_participations = load_participations()
            save_participations({"TEST_USER": test_data})
            restored = load_participations()
            save_participations(original_participations)
            
            if "TEST_USER" not in restored:
                logger.error("❌ save_participations não está funcionando!")
                return False
            else:
                logger.info("✅ save_participations funciona corretamente")
        except Exception as e:
            logger.error(f"❌ save_participations tem erro: {e}")
            return False
        
        # Testar save_wallets
        try:
            original_wallets = load_wallets()
            test_wallet = {"TEST_WALLET": test_data}
            save_wallets(test_wallet)
            restored = load_wallets()
            save_wallets(original_wallets)
            
            if "TEST_WALLET" not in restored:
                logger.error("❌ save_wallets não está funcionando!")
                return False
            else:
                logger.info("✅ save_wallets funciona corretamente")
        except Exception as e:
            logger.error(f"❌ save_wallets tem erro: {e}")
            return False
        
        # Testar save_transactions
        try:
            original_transactions = load_transactions()
            test_tx = [test_data]
            save_transactions(test_tx)
            restored = load_transactions()
            save_transactions(original_transactions)
            
            if len(restored) == 0:
                logger.error("❌ save_transactions não está funcionando!")
                return False
            else:
                logger.info("✅ save_transactions funciona corretamente")
        except Exception as e:
            logger.error(f"❌ save_transactions tem erro: {e}")
            return False
        
        logger.info("✅✅✅ TODAS AS FUNÇÕES DE SAVE FUNCIONAM CORRETAMENTE")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erro na verificação de salvamento: {e}")
        return False

def is_duplicate_user(
    email: str,
    phone: str,
    fingerprint: str,
    exclude_user_code: str = None,
    is_admin_creation: bool = False
) -> Tuple[bool, str]:
    """Verifica duplicidade de usuário"""
    try:
        users = load_users()
        email_norm = normalize_email(email)
        phone_norm = normalize_phone(phone)
        
        for code, user in users.items():
            if exclude_user_code and code == exclude_user_code:
                continue
            
            if email_norm and normalize_email(user.get('email', '')) == email_norm:
                return True, "email"
            
            if phone_norm and normalize_phone(user.get('phone', '')) == phone_norm:
                return True, "telefone"
            
            if not is_admin_creation:
                if fingerprint and user.get('fingerprint') == fingerprint:
                    return True, "fingerprint"
        
        return False, ""
        
    except Exception as e:
        logger.error(f"Erro antifraude: {e}")
        return False, ""

def log_fraud_detection(detection_type: str, details: str, data: Dict = None):
    """Registra detecção de fraude para auditoria"""
    try:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": detection_type,
            "details": details,
            "data": data or {},
            "ip": request.remote_addr if request else "unknown",
            "user_agent": request.headers.get('User-Agent', 'unknown') if request else "unknown"
        }
        
        logs = []
        if os.path.exists(FRAUD_LOG_FILE):
            with open(FRAUD_LOG_FILE, 'r', encoding='utf-8') as f:
                try:
                    logs = json.load(f)
                except:
                    logs = []
        
        logs.append(log_entry)
        
        if len(logs) > 1000:
            logs = logs[-1000:]
        
        with open(FRAUD_LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(logs, f, indent=2, ensure_ascii=False)
        
        logger.warning(f"🚨 FRAUDE DETECTADA: {detection_type} - {details}")
        
    except Exception as e:
        logger.error(f"❌ Erro ao registrar detecção de fraude: {e}")

# ============================================
# 🔥 VALIDAÇÃO AUTOMÁTICA DE USUÁRIO
# ============================================

def is_user_valid(user: Dict) -> bool:
    """Verifica se o usuário ainda é válido"""
    try:
        if not user.get("active", True):
            logger.warning(f"❌ Usuário inativo detectado")
            return False
        
        expires_at_str = user.get("expires_at", "1970-01-01")
        try:
            exp_date = parse_date_ymd(expires_at_str)
            today = datetime.now().date()
            
            if today > exp_date:
                logger.warning(f"❌ Plano expirado: {expires_at_str}")
                return False
        except Exception as e:
            logger.error(f"❌ Erro ao verificar data de expiração: {e}")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Erro na validação do usuário: {e}")
        return False

def get_binance_positions_snapshot(user_code: str) -> List[Dict]:
    """Sistema NÃO consulta posições abertas na Binance - Retorna lista vazia"""
    try:
        logger.info(f"⚠️ Snapshot Binance solicitado para {user_code}: SISTEMA NÃO IMPLEMENTADO")
        
        audit_event(
            action="BINANCE_SNAPSHOT_REQUESTED",
            success=False,
            user_code=user_code,
            details="Sistema não suporta snapshot de posições Binance",
            extra={
                "return_value": "[]",
                "positions_count": 0,
                "system_mode": "no_open_positions"
            }
        )
        
        return []
        
    except Exception as e:
        logger.error(f"❌ Erro em get_binance_positions_snapshot: {e}")
        return []

# ============================================
# 🔥 CRIPTOGRAFIA PARA CHAVES DO USUÁRIO
# ============================================

def generate_encryption_key():
    """Gera uma chave de criptografia segura baseada no ADMIN_SECRET_KEY"""
    salt = b'coyote_salt_2025'
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(ADMIN_SECRET_KEY.encode()))
    return key

ENCRYPTION_KEY = generate_encryption_key()
cipher_suite = Fernet(ENCRYPTION_KEY)

def encrypt_secret(secret: str) -> str:
    """Criptografa a API Secret do usuário"""
    try:
        encrypted = cipher_suite.encrypt(secret.encode())
        return encrypted.decode('utf-8')
    except Exception as e:
        logger.error(f"❌ Erro ao criptografar secret: {e}")
        return secret

def decrypt_secret(encrypted_secret: str) -> str:
    """Descriptografa a API Secret do usuário"""
    try:
        decrypted = cipher_suite.decrypt(encrypted_secret.encode())
        return decrypted.decode('utf-8')
    except Exception as e:
        logger.error(f"❌ Erro ao descriptografar secret: {e}")
        return encrypted_secret

# ============================================
# 🔥 FUNÇÕES PARA CHAVES DO USUÁRIO
# ============================================

def load_user_api_keys() -> Dict:
    """Carrega todas as chaves de usuários do arquivo"""
    if not os.path.exists(USER_API_KEYS_FILE):
        return {}
    try:
        with open(USER_API_KEYS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"❌ Erro ao carregar user_api_keys.json: {e}")
        return {}

def save_user_api_keys(keys: Dict):
    """Salva todas as chaves de usuários no arquivo"""
    try:
        with open(USER_API_KEYS_FILE, 'w', encoding='utf-8') as f:
            json.dump(keys, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"❌ Erro ao salvar user_api_keys.json: {e}")

def save_user_keys(user_id: str, data: Dict) -> bool:
    """Salva as chaves do usuário (criptografando a secret)"""
    try:
        keys = load_user_api_keys()
        
        user_data = {
            "market_mode": data.get("market_mode", "spot"),
            "api_key": data.get("api_key", ""),
            "api_secret": encrypt_secret(data.get("api_secret", "")),
            "created_at": data.get("created_at", datetime.now().isoformat()),
            "updated_at": datetime.now().isoformat()
        }
        
        keys[user_id] = user_data
        save_user_api_keys(keys)
        logger.info(f"✅ Chaves salvas para usuário: {user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Erro ao salvar chaves do usuário {user_id}: {e}")
        return False

def load_user_keys(user_id: str) -> Optional[Dict]:
    """Carrega as chaves do usuário (descriptografando a secret)"""
    try:
        keys = load_user_api_keys()
        if user_id not in keys:
            return None
        
        user_data = keys[user_id].copy()
        user_data["api_secret_decrypted"] = decrypt_secret(user_data["api_secret"])
        return user_data
    except Exception as e:
        logger.error(f"❌ Erro ao carregar chaves do usuário {user_id}: {e}")
        return None

def delete_user_keys(user_id: str) -> bool:
    """Remove as chaves do usuário"""
    try:
        keys = load_user_api_keys()
        if user_id in keys:
            del keys[user_id]
            save_user_api_keys(keys)
            logger.info(f"✅ Chaves removidas para usuário: {user_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"❌ Erro ao remover chaves do usuário {user_id}: {e}")
        return False

def user_has_keys(user_id: str) -> bool:
    """Verifica se o usuário tem chaves salvas"""
    keys = load_user_api_keys()
    return user_id in keys

def distribuir_lucro_proporcional(trade_id: str, pnl_usdt: float, description: str = "") -> Dict:
    """Distribuição de lucro/prejuízo em USDT"""
    try:
        # ===============================
        # 1. IDEMPOTÊNCIA
        # ===============================
        transactions = load_transactions()
        # Verificação exata por trade_id
        if any(t.get("trade_id") == trade_id for t in transactions):
            logger.warning(f"⚠️ Trade {trade_id} já processado (idempotência)")
            return {"success": True, "duplicate": True, "message": "Trade já processado anteriormente"}

        # Verificação por símbolo+entrada (bloqueia IDs ligeiramente diferentes do mesmo trade)
        try:
            parts = trade_id.split("_")
            if len(parts) >= 3:
                chave_base = "_".join(parts[:3])
                from datetime import datetime, timedelta
                janela = timedelta(seconds=90)
                agora = datetime.now()
                for t in transactions:
                    tid2 = t.get("trade_id", "")
                    if tid2.startswith(chave_base) and tid2 != trade_id:
                        try:
                            dt2 = datetime.fromisoformat(t.get("date", ""))
                            if agora - dt2 < janela:
                                logger.warning(f"⚠️ Trade duplicado bloqueado: {trade_id} (base={chave_base} já em {tid2})")
                                return {"success": True, "duplicate": True, "message": "Trade duplicado bloqueado (mesmo símbolo/entrada)"}
                        except Exception:
                            pass
        except Exception:
            pass

        # ===============================
        # 2. TRADE COM PREJUÍZO
        # ===============================
        if pnl_usdt < 0:
            perda_total_usdt = abs(pnl_usdt)
            logger.info(f"📉 Trade {trade_id} com PREJUÍZO: {perda_total_usdt:.2f} USDT")

            participations = load_participations()
            master_wallet  = load_master_wallet_data()

            # ── Monta o pool: usuários ativos + master ────────────────────────────
            # Regra: a perda é dividida PROPORCIONALMENTE entre todos que têm saldo
            # no pool (usuários + master). Sem penalidade extra. Sem fee.
            # master absorve a parte dela, usuários só perdem o proporcional.
            # ─────────────────────────────────────────────────────────────────────
            penalidade_total_master = 0.0   # sem penalidade adicional no modelo atual
            ativos_com_saldo = []
            for user_code, p in participations.items():
                if (
                    p.get("status") == "active" and
                    p.get("virtual_balance_usdt", 0) > 0
                ):
                    p["user_code"] = user_code
                    ativos_com_saldo.append(p)

            saldo_master         = float(master_wallet.get("balance_available_usdt", 0))
            total_usuarios_usdt  = sum(p["virtual_balance_usdt"] for p in ativos_com_saldo)
            total_pool_usdt      = total_usuarios_usdt + saldo_master  # usuários + master

            if total_pool_usdt > 0:
                perda_distribuida = 0.0

                # ── Débito proporcional para cada usuário ──
                for p in ativos_com_saldo:
                    proporcao          = p["virtual_balance_usdt"] / total_pool_usdt
                    perda_usuario      = round(perda_total_usdt * proporcao, 8)

                    if perda_usuario > 0:
                        saldo_antes = p.get("virtual_balance_usdt", 0)
                        novo_saldo  = max(0.0, round(saldo_antes - perda_usuario, 8))
                        p["virtual_balance_usdt"]    = novo_saldo
                        p["profit_accumulated_usdt"] = round(
                            float(p.get("profit_accumulated_usdt", 0)) - perda_usuario, 8
                        )
                        p["updated_at"] = datetime.now().isoformat()
                        perda_distribuida += perda_usuario

                        tx_id = f"LOSS-{trade_id}-{p['user_code']}"
                        # Extrair símbolo do trade_id (ex: SOLUSDT_20260219_...)
                        _symbol_parts = trade_id.split("_")
                        _symbol = _symbol_parts[0] if (_symbol_parts and _symbol_parts[0].endswith("USDT")) else ""
                        transactions.append({
                            "id":                          tx_id,
                            "trade_id":                    trade_id,
                            "user_code":                   p["user_code"],
                            "wallet_code":                 p["user_code"],
                            "type":                        "loss_distribution",
                            "amount_usdt":                 round(-perda_usuario, 8),
                            "perda_bruta_usdt":            round(perda_usuario, 8),
                            "penalidade_usdt":             0.0,
                            "symbol":                      _symbol,
                            "date":                        datetime.now().isoformat(),
                            "status":                      "completed",
                            "proporcao_saldo_atual":        round(proporcao, 8),
                            "description":                 f"Perda proporcional trade {trade_id}: {description}",
                            "virtual_balance_before_usdt": saldo_antes,
                            "virtual_balance_after_usdt":  novo_saldo,
                            "currency":                    "USDT",
                            "cycle_id":                    datetime.utcnow().strftime("%Y-%m-%d")
                        })

                        logger.info(
                            f"   👤 {p['user_code']}: proporção={proporcao*100:.2f}% "
                            f"perda={perda_usuario:.4f} USDT | saldo {saldo_antes:.4f}→{novo_saldo:.4f}"
                        )

                # ── Débito proporcional da master ──────────────────────────────────
                proporcao_master   = saldo_master / total_pool_usdt
                perda_master       = round(perda_total_usdt * proporcao_master, 8)
                if perda_master > 0:
                    saldo_antes_master = saldo_master
                    novo_saldo_master  = max(0.0, round(saldo_master - perda_master, 8))
                    master_wallet["balance_available_usdt"] = novo_saldo_master
                    master_wallet["updated_at"]             = datetime.now().isoformat()
                    master_wallet.setdefault("profit_distributions", []).append({
                        "date":        datetime.now().isoformat(),
                        "amount_usdt": -perda_master,
                        "trade_id":    trade_id,
                        "description": f"Perda proporcional master trade {trade_id}",
                        "type":        "loss_share"
                    })
                    save_master_wallet_data(master_wallet)
                    perda_distribuida += perda_master

                    transactions.append({
                        "id":                         f"LOSS-MASTER-{trade_id}",
                        "trade_id":                   trade_id,
                        "type":                       "loss_master_share",
                        "amount_usdt":                round(-perda_master, 8),
                        "date":                       datetime.now().isoformat(),
                        "status":                     "completed",
                        "description":                f"Perda proporcional master trade {trade_id}",
                        "master_balance_before_usdt": saldo_antes_master,
                        "master_balance_after_usdt":  novo_saldo_master,
                        "currency":                   "USDT"
                    })

                    logger.info(
                        f"   🏢 MASTER: proporção={proporcao_master*100:.2f}% "
                        f"perda={perda_master:.4f} USDT | saldo {saldo_antes_master:.4f}→{novo_saldo_master:.4f}"
                    )

                save_participations(participations)
                logger.info(
                    f"   📊 Perda total distribuída: {perda_distribuida:.4f} / {perda_total_usdt:.4f} USDT"
                )
            
            _tc_symbol_parts = trade_id.split("_")
            _tc_symbol = _tc_symbol_parts[0] if (_tc_symbol_parts and _tc_symbol_parts[0].endswith("USDT")) else ""
            transactions.append({
                "id":                       f"TRADE-LOSS-{trade_id}",
                "trade_id":                 trade_id,
                "type":                     "trade_closed_loss",
                "symbol":                   _tc_symbol,
                "pnl_usdt":                 pnl_usdt,
                "perda_total_usdt":         perda_total_usdt,
                "penalidade_master_usdt":   round(penalidade_total_master, 8),
                "date":                     datetime.now().isoformat(),
                "status":                   "completed",
                "description":              f"Perda proporcional sem penalidade: {description}",
                "active_users_with_balance":len(ativos_com_saldo),
                "total_capital_atual_usdt": total_pool_usdt if total_pool_usdt else 0,
                "currency":                 "USDT",
                "cycle_id":                 datetime.utcnow().strftime("%Y-%m-%d")
            })
            
            save_transactions(transactions)

            audit_event(
                action="TRADE_LOSS_DISTRIBUTED",
                success=True,
                details=f"Trade {trade_id} prejuízo distribuído com penalidade 10%",
                extra={
                    "trade_id":              trade_id,
                    "perda_total_usdt":      perda_total_usdt,
                    "penalidade_master_usdt":round(penalidade_total_master, 8),
                    "active_users":          len(ativos_com_saldo),
                    "distribution_base":     "virtual_balance_current_usdt"
                }
            )

            logger.info("=" * 60)
            logger.info(f"📉 TRADE {trade_id} PREJUÍZO DISTRIBUÍDO!")
            logger.info(f"   📊 Perda total do trade: {perda_total_usdt:.4f} USDT")
            logger.info(f"   🏢 Penalidade master (10%): {penalidade_total_master:.4f} USDT")
            logger.info(f"   👥 Usuários com saldo: {len(ativos_com_saldo)}")
            logger.info("=" * 60)
            
            return {
                "success":              True,
                "message":              "Prejuízo distribuído com penalidade de 10%",
                "trade_id":             trade_id,
                "perda_total_usdt":     perda_total_usdt,
                "penalidade_master_usdt": round(penalidade_total_master, 8),
                "total_creditado_usdt": -(perda_total_usdt + penalidade_total_master),
                "total_fee_usdt":       round(penalidade_total_master, 8),
                "distribution_base":    "virtual_balance_current_usdt",
                "currency":             "USDT"
            }
        
        # ===============================
        # 3. TRADE COM LUCRO
        # ===============================
        elif pnl_usdt > 0:
            logger.info(f"💰 Processando trade {trade_id} | Lucro TOTAL: {pnl_usdt:.2f} USDT")
            
            participations = load_participations()
            master_wallet_profit = load_master_wallet_data()
            saldo_master_profit  = float(master_wallet_profit.get("balance_available_usdt", 0))
            
            ativos_com_saldo = []
            for user_code, p in participations.items():
                if (
                    p.get("status") == "active" and
                    p.get("virtual_balance_usdt", 0) > 0
                ):
                    p["user_code"] = user_code
                    ativos_com_saldo.append(p)
            
            # Pool total = saldo dos usuários + saldo da master
            total_usuarios_capital_usdt = sum(p["virtual_balance_usdt"] for p in ativos_com_saldo)
            total_capital_atual_usdt = total_usuarios_capital_usdt + saldo_master_profit
            
            if total_capital_atual_usdt <= 0:
                logger.warning("⚠️ Nenhum saldo ativo encontrado")
                transactions.append({
                    "id": f"TRADE-NO-CAPITAL-{trade_id}",
                    "trade_id": trade_id,
                    "type": "trade_closed_no_capital",
                    "pnl_usdt": pnl_usdt,
                    "date": datetime.now().isoformat(),
                    "status": "completed",
                    "description": f"Trade sem capital ativo: {description}",
                    "currency": "USDT"
                })
                save_transactions(transactions)
                return {
                    "success": False,
                    "message": "Nenhum capital ativo para distribuir lucro"
                }
            
            # ✅ REGRA WHITE PAPER — Lucro:
            #   O pool é dividido proporcionalmente pelo saldo (usuários + master).
            #   De cada parcela de usuário: 50% vai para fee (master) e 50% para o usuário.
            #   A master recebe: sua parcela proporcional bruta + fee dos usuários.
            #   ─────────────────────────────────────────────────────────────────────
            # Proporção da master no pool total
            proporcao_master_profit = (saldo_master_profit / total_capital_atual_usdt) if total_capital_atual_usdt > 0 else 0
            lucro_bruto_master      = round(pnl_usdt * proporcao_master_profit, 8)   # parcela proporcional bruta
            lucro_bruto_usuarios    = pnl_usdt - lucro_bruto_master                  # parcela proporcional dos usuários

            # 50% fee do bloco de usuários → vai para master
            fee_empresa   = round(lucro_bruto_usuarios * 0.50, 8)
            lucro_usuarios = round(lucro_bruto_usuarios * 0.50, 8)   # o que os usuários de fato recebem
            master_split  = round(lucro_bruto_master + fee_empresa, 8)  # master recebe proporcional + fee
            
            master_wallet = load_master_wallet_data()
            saldo_antes_master = master_wallet.get("balance_available_usdt", 0)
            
            master_wallet["balance_available_usdt"] += master_split
            master_wallet["total_profit_received_usdt"] += master_split
            master_wallet["updated_at"] = datetime.now().isoformat()
            master_wallet["profit_distributions"].append({
                "date": datetime.now().isoformat(),
                "amount_usdt": master_split,
                "trade_id": trade_id,
                "description": f"50% do lucro do trade {trade_id} (master/robô)",
                "type": "trading_fee"
            })
            
            save_master_wallet_data(master_wallet)
            
            logger.info(f"🏢 Fee empresa: {fee_empresa:.2f} USDT → master_wallet.json")
            
            transactions.append({
                "id": f"FEE-{trade_id}",
                "trade_id": trade_id,
                "type": "company_fee_income",
                "amount_usdt": fee_empresa,
                "date": datetime.now().isoformat(),
                "status": "completed",
                "description": f"Fee empresa trade {trade_id}",
                "master_balance_before_usdt": saldo_antes_master,
                "master_balance_after_usdt": master_wallet["balance_available_usdt"],
                "source": "trade_profit",
                "wallet_file": "master_wallet.json",
                "currency": "USDT"
            })
            
            lucro_distribuido_total = 0.0
            
            for p in ativos_com_saldo:
                proporcao_saldo_atual = p["virtual_balance_usdt"] / total_capital_atual_usdt
                lucro_usuario = lucro_usuarios * proporcao_saldo_atual
                
                if lucro_usuario > 0:
                    saldo_antes = p.get("virtual_balance_usdt", 0)
                    p["virtual_balance_usdt"] += lucro_usuario
                    p["profit_accumulated_usdt"] += lucro_usuario
                    p["last_profit_distribution"] = datetime.now().isoformat()
                    p["updated_at"] = datetime.now().isoformat()
                    
                    lucro_distribuido_total += lucro_usuario
                    
                    tx_id = f"PROF-{trade_id}-{p['user_code']}"
                    # Extrair símbolo do trade_id (ex: SOLUSDT_20260219_...)
                    _sym_parts = trade_id.split("_")
                    _sym = _sym_parts[0] if (_sym_parts and _sym_parts[0].endswith("USDT")) else ""
                    transactions.append({
                        "id": tx_id,
                        "trade_id": trade_id,
                        "user_code": p["user_code"],
                        "wallet_code": p["user_code"],
                        "type": "profit_distribution",
                        "amount_usdt": lucro_usuario,
                        "symbol": _sym,
                        "date": datetime.now().isoformat(),
                        "status": "completed",
                        "proporcao_saldo_atual": proporcao_saldo_atual,
                        "description": f"Lucro trade {trade_id}: {description}",
                        "virtual_balance_before_usdt": saldo_antes,
                        "virtual_balance_after_usdt": p["virtual_balance_usdt"],
                        "currency": "USDT",
                        "cycle_id": datetime.utcnow().strftime("%Y-%m-%d")
                    })
                    
                    logger.info(f"   👤 {p['user_code']}: {proporcao_saldo_atual*100:.1f}% → {lucro_usuario:.2f} USDT")
            
            ajuste = lucro_usuarios - lucro_distribuido_total
            if abs(ajuste) > 0.01:
                logger.info(f"🔧 Ajuste arredondamento: {ajuste:.4f} USDT")
                master_wallet["balance_available_usdt"] += ajuste
                master_wallet["total_profit_received_usdt"] += ajuste
                save_master_wallet_data(master_wallet)
                
                transactions.append({
                    "id": f"AJUSTE-{trade_id}",
                    "trade_id": trade_id,
                    "type": "rounding_adjustment",
                    "amount_usdt": ajuste,
                    "date": datetime.now().isoformat(),
                    "status": "completed",
                    "description": f"Ajuste arredondamento trade {trade_id}",
                    "currency": "USDT"
                })
            
            save_participations(participations)
            
            _tp_symbol_parts = trade_id.split("_")
            _tp_symbol = _tp_symbol_parts[0] if (_tp_symbol_parts and _tp_symbol_parts[0].endswith("USDT")) else ""
            transactions.append({
                "id": f"TRADE-{trade_id}",
                "trade_id": trade_id,
                "type": "trade_closed_profit",
                "symbol": _tp_symbol,
                "pnl_usdt": pnl_usdt,
                "fee_empresa_usdt": fee_empresa,
                "lucro_usuarios_distribuido_usdt": lucro_distribuido_total,
                "date": datetime.now().isoformat(),
                "status": "completed",
                "active_users_with_balance": len(ativos_com_saldo),
                "total_capital_atual_usdt": total_capital_atual_usdt,
                "description": f"Trade {trade_id}: {description}",
                "distribution_model": "proportional_pool_50pct_fee",
                "currency": "USDT",
                "cycle_id": datetime.utcnow().strftime("%Y-%m-%d")
            })
            
            save_transactions(transactions)
            
            audit_event(
                action="TRADE_PROFIT_DISTRIBUTED_CORRECT",
                success=True,
                details=f"Trade {trade_id} distribuído corretamente",
                extra={
                    "trade_id": trade_id,
                    "pnl_usdt": pnl_usdt,
                    "fee_empresa_usdt": fee_empresa,
                    "lucro_usuarios_usdt": lucro_distribuido_total,
                    "active_users": len(ativos_com_saldo),
                    "total_capital_atual_usdt": total_capital_atual_usdt,
                    "distribution_base": "virtual_balance_current_usdt",
                    "master_wallet_file": "master_wallet.json",
                    "currency": "USDT"
                }
            )
            
            logger.info("=" * 60)
            logger.info(f"✅ TRADE {trade_id} DISTRIBUÍDO CORRETAMENTE!")
            logger.info(f"   📊 Lucro total do trade: {pnl_usdt:.2f} USDT")
            logger.info(f"   🏢 50% Master/Robô: {master_split:.2f} USDT → master_wallet.json")
            logger.info(f"   👥 50% Usuários (sem fee): {lucro_distribuido_total:.2f} USDT")
            logger.info(f"   👤 Usuários com saldo: {len(ativos_com_saldo)}")
            logger.info(f"   💰 Capital atual total: {total_capital_atual_usdt:.2f} USDT")
            logger.info("=" * 60)
            
            return {
                "success": True,
                "trade_id": trade_id,
                "pnl_usdt": pnl_usdt,
                "fee_empresa_usdt": fee_empresa,
                "lucro_usuarios_distribuido_usdt": lucro_distribuido_total,
                "active_users": len(ativos_com_saldo),
                "total_capital_atual_usdt": total_capital_atual_usdt,
                "distribution_base": "virtual_balance_current_usdt",
                "currency": "USDT",
                "message": f"Trade {trade_id} distribuído: Empresa {fee_empresa:.2f} USDT, Usuários {lucro_distribuido_total:.2f} USDT"
            }
        
        else:
            transactions.append({
                "id": f"TRADE-BREAKEVEN-{trade_id}",
                "trade_id": trade_id,
                "type": "trade_closed_breakeven",
                "pnl_usdt": 0,
                "date": datetime.now().isoformat(),
                "status": "completed",
                "description": f"Trade sem lucro/prejuízo: {description}",
                "currency": "USDT"
            })
            save_transactions(transactions)
            
            logger.info(f"⚖️ Trade {trade_id} sem lucro/prejuízo")
            
            return {
                "success": True,
                "message": "Trade encerrado sem lucro",
                "processed": False
            }

    except Exception as e:
        logger.error(f"❌ Erro CRÍTICO na distribuição do trade {trade_id}: {e}", exc_info=True)
        audit_event(
            action="TRADE_PROFIT_DISTRIBUTION_ERROR",
            success=False,
            details=f"Erro trade {trade_id}: {str(e)}",
            extra={"trade_id": trade_id, "error": str(e)}
        )
        return {
            "success": False,
            "error": str(e)
        }

# ============================================
# 🔥 FUNÇÕES PARA TRADE COM CHAVE DO USUÁRIO
# ============================================

def validate_binance_credentials(api_key: str, api_secret: str) -> bool:
    """Valida se as credenciais da Binance são válidas"""
    try:
        from binance.client import Client
        from binance.exceptions import BinanceAPIException
        
        client = Client(api_key, api_secret)
        account_info = client.get_account()
        
        permissions = account_info.get('permissions', [])
        if 'SPOT' not in permissions:
            logger.warning(f"❌ Usuário sem permissão SPOT")
            return False
            
        logger.info(f"✅ Credenciais Binance validadas com sucesso")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erro ao validar credenciais Binance: {e}")
        return False

def executarOrdemUsuario(user_id: str, symbol: str, side: str) -> Dict:
    """
    Executa ordem de compra/venda usando chaves do usuário.
    BUY  → limita pelo aporte_usdt configurado, registra posição aberta e debita virtual_balance_usdt.
    SELL → calcula PnL real, aplica fee 10%, atualiza virtual_balance_usdt (lucro ou prejuízo).
    """
    try:
        user_keys = load_user_keys(user_id)
        if not user_keys:
            raise Exception("Usuário não possui chaves salvas")

        api_key = user_keys.get("api_key")
        api_secret = user_keys.get("api_secret_decrypted")

        if not api_key or not api_secret:
            raise Exception("Chaves API não encontradas")

        from binance.client import Client
        from binance.exceptions import BinanceAPIException

        client = Client(api_key, api_secret)

        market_mode = user_keys.get("market_mode", "spot")
        if market_mode != "spot":
            raise Exception("Apenas trading spot é permitido")

        # ── Carregar participação do usuário ──────────────────────────────
        participations = load_participations()
        p = participations.get(user_id)
        if p:
            p = _ensure_user_bot_fields(p)

        if side.upper() == "BUY":
            quote_asset = symbol[-4:] if symbol.endswith("USDT") else "USDT"
            balance = client.get_asset_balance(asset=quote_asset)
            if not balance:
                raise Exception(f"Saldo de {quote_asset} não encontrado")

            free_balance = float(balance['free'])
            if free_balance <= 0:
                raise Exception(f"Saldo insuficiente de {quote_asset}")

            ticker = client.get_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])

            # ✅ Limitar pelo aporte_usdt configurado pelo usuário (não usar tudo)
            aporte_usdt = float(p.get("aporte_usdt", 0)) if p else 0
            if aporte_usdt > 0:
                valor_entrada_usdt = min(aporte_usdt, free_balance)
            else:
                # Fallback: usa 99.9% do saldo se não tiver aporte configurado
                valor_entrada_usdt = free_balance * 0.999

            max_qty = valor_entrada_usdt / current_price

            symbol_info = client.get_symbol_info(symbol)
            step_size = 0.000001
            for filt in symbol_info['filters']:
                if filt['filterType'] == 'LOT_SIZE':
                    step_size = float(filt['stepSize'])
                    break

            max_qty = max_qty - (max_qty % step_size)

            if max_qty <= 0:
                raise Exception("Quantidade muito pequena para comprar")

            order = client.order_market_buy(
                symbol=symbol,
                quantity=max_qty
            )

            # ✅ Calcular valor real executado a partir dos fills
            fills = order.get('fills', [])
            if fills:
                valor_gasto_usdt = sum(float(f['qty']) * float(f['price']) for f in fills)
                qty_comprada = sum(float(f['qty']) for f in fills)
                preco_medio = valor_gasto_usdt / qty_comprada if qty_comprada > 0 else current_price
            else:
                qty_comprada = float(order.get('executedQty', max_qty))
                valor_gasto_usdt = round(qty_comprada * current_price, 4)
                preco_medio = current_price

            # ✅ Registrar posição aberta e debitar virtual_balance_usdt
            if p:
                nova_posicao = {
                    "id": f"{user_id}_{symbol}_{int(time.time())}",
                    "usuario_id": user_id,
                    "symbol": symbol,
                    "preco_entrada": round(preco_medio, 8),
                    "quantidade": round(qty_comprada, 8),
                    "valor_entrada_usdt": round(valor_gasto_usdt, 4),
                    "origem": "MANUAL",
                    "timestamp": datetime.now().isoformat(),
                    "status": "aberta",
                }
                if "user_positions" not in p:
                    p["user_positions"] = []
                p["user_positions"].append(nova_posicao)

                saldo_antes = float(p.get("virtual_balance_usdt", 0))
                p["virtual_balance_usdt"] = max(0.0, round(saldo_antes - valor_gasto_usdt, 4))
                p["saldo_disponivel_usdt"] = max(0.0, round(
                    float(p.get("saldo_disponivel_usdt", 0)) - valor_gasto_usdt, 4
                ))
                p["saldo_em_posicoes_usdt"] = round(
                    float(p.get("saldo_em_posicoes_usdt", 0)) + valor_gasto_usdt, 4
                )
                p["updated_at"] = datetime.now().isoformat()
                participations[user_id] = p
                save_participations(participations)

                logger.info(
                    f"📈 [{user_id}] BUY {symbol} | qty={qty_comprada:.6f} | "
                    f"preco={preco_medio:.6f} | gasto=${valor_gasto_usdt:.2f} | "
                    f"saldo_virtual: {saldo_antes:.4f} → {p['virtual_balance_usdt']:.4f}"
                )

        elif side.upper() == "SELL":
            base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol
            balance = client.get_asset_balance(asset=base_asset)
            if not balance:
                raise Exception(f"Saldo de {base_asset} não encontrado")

            free_balance = float(balance['free'])
            if free_balance <= 0:
                raise Exception(f"Saldo insuficiente de {base_asset}")

            symbol_info = client.get_symbol_info(symbol)
            step_size = 0.000001
            for filt in symbol_info['filters']:
                if filt['filterType'] == 'LOT_SIZE':
                    step_size = float(filt['stepSize'])
                    break

            qty = free_balance - (free_balance % step_size)

            if qty <= 0:
                raise Exception("Quantidade muito pequena para vender")

            order = client.order_market_sell(
                symbol=symbol,
                quantity=qty
            )

            # ✅ Calcular valor real recebido a partir dos fills
            fills = order.get('fills', [])
            if fills:
                valor_recebido_usdt = sum(float(f['qty']) * float(f['price']) for f in fills)
                qty_vendida = sum(float(f['qty']) for f in fills)
                preco_saida = valor_recebido_usdt / qty_vendida if qty_vendida > 0 else 0
            else:
                ticker = client.get_symbol_ticker(symbol=symbol)
                preco_saida = float(ticker['price'])
                qty_vendida = float(order.get('executedQty', qty))
                valor_recebido_usdt = round(qty_vendida * preco_saida, 4)

            # ✅ Encontrar posição aberta para calcular PnL real
            if p:
                posicoes = p.get("user_positions", [])
                pos_idx = next((i for i, pos in enumerate(posicoes) if pos.get("symbol") == symbol), None)

                if pos_idx is not None:
                    pos = posicoes[pos_idx]
                    valor_entrada = float(pos.get("valor_entrada_usdt", 0))
                    preco_entrada = float(pos.get("preco_entrada", 0))

                    pnl_bruto = round(valor_recebido_usdt - valor_entrada, 4)
                    pnl_pct = round(((preco_saida - preco_entrada) / preco_entrada * 100) if preco_entrada > 0 else 0, 2)

                    # LUCRO: 50% fee → 50% pro usuário | PREJUÍZO: 10% do investido como fee
                    FEE_PREJUIZO = 0.10
                    SPLIT_LUCRO  = 0.50
                    if pnl_bruto >= 0:
                        fee_aplicada = round(pnl_bruto * SPLIT_LUCRO, 4)       # 50% do lucro vai pro fundo
                        pnl_liquido  = round(pnl_bruto * SPLIT_LUCRO, 4)       # 50% fica pro usuário
                    else:
                        fee_aplicada = round(abs(pnl_bruto) * FEE_PREJUIZO, 4)  # 10% da PERDA como fee (não do investido)
                        pnl_liquido  = round(pnl_bruto - fee_aplicada, 4)      # prejuízo + 10% da perda

                    fechado = {
                        **pos,
                        "preco_saida": preco_saida,
                        "valor_saida_usdt": valor_recebido_usdt,
                        "pnl_usdt": pnl_bruto,
                        "pnl_liquido_usdt": pnl_liquido,
                        "pnl_percent": pnl_pct,
                        "fee_usdt": fee_aplicada,
                        "motivo_saida": "MANUAL",
                        "status": "fechada",
                        "timestamp_saida": datetime.now().isoformat(),
                    }
                    p["user_positions"].pop(pos_idx)
                    if "user_closed_trades" not in p:
                        p["user_closed_trades"] = []
                    p["user_closed_trades"].append(fechado)

                    # Atualizar saldo virtual com PnL líquido
                    saldo_antes = float(p.get("virtual_balance_usdt", 0))
                    saldo_novo = max(0.0, round(saldo_antes + pnl_liquido, 4))
                    p["virtual_balance_usdt"] = saldo_novo

                    p["saldo_disponivel_usdt"] = round(
                        float(p.get("saldo_disponivel_usdt", 0)) + valor_recebido_usdt, 4
                    )
                    p["saldo_em_posicoes_usdt"] = max(0.0, round(
                        float(p.get("saldo_em_posicoes_usdt", 0)) - valor_entrada, 4
                    ))

                    # Acumula sempre (positivo ou negativo)
                    p["profit_accumulated_usdt"] = round(
                        float(p.get("profit_accumulated_usdt", 0)) + pnl_liquido, 4
                    )

                    fechado["virtual_balance_antes"] = saldo_antes
                    fechado["virtual_balance_depois"] = saldo_novo

                    logger.info(
                        f"📉 [{user_id}] SELL {symbol} | PnL bruto={pnl_bruto:+.4f} "
                        f"| Fee={fee_aplicada:.4f} (só no prejuízo) "
                        f"| PnL líquido={pnl_liquido:+.4f} | "
                        f"saldo_virtual: {saldo_antes:.4f} → {saldo_novo:.4f}"
                    )
                else:
                    # Posição aberta não encontrada — credita o valor recebido diretamente
                    logger.warning(f"⚠️ [{user_id}] SELL {symbol}: posição aberta não encontrada, creditando valor recebido")
                    p["virtual_balance_usdt"] = round(
                        float(p.get("virtual_balance_usdt", 0)) + valor_recebido_usdt, 4
                    )
                    p["saldo_disponivel_usdt"] = round(
                        float(p.get("saldo_disponivel_usdt", 0)) + valor_recebido_usdt, 4
                    )

                p["updated_at"] = datetime.now().isoformat()
                participations[user_id] = p
                save_participations(participations)

        else:
            raise Exception("Side inválido. Use 'BUY' ou 'SELL'")

        registrarTradeUsuario(user_id, symbol, side, order)

        return {
            "success": True,
            "order": order,
            "message": f"Ordem de {side} executada com sucesso"
        }

    except Exception as e:
        logger.error(f"❌ Erro ao executar ordem para {user_id}: {e}")
        return {
            "success": False,
            "error": str(e)
        }

def get_last_transaction_balance(user_code: str) -> float:
    """
    Retorna o saldo mais recente baseado nas transações.
    Usado apenas para sincronização inicial.
    """
    transactions = load_transactions()

    last_balance = None

    # Ordena da mais recente para a mais antiga
    for tx in sorted(transactions, key=lambda x: x.get("date", ""), reverse=True):
        if tx.get("wallet_code") == user_code:
            if "virtual_balance_after" in tx:
                last_balance = float(tx["virtual_balance_after"])
                break

    return last_balance if last_balance is not None else 0.0

def sync_participation_balance(user_code: str):
    """
    Sincroniza o saldo oficial com base nas transações.
    Deve ser executado uma vez ou em manutenção.
    """
    participations = load_participations()

    if user_code not in participations:
        return False

    last_balance = get_last_transaction_balance(user_code)

    participations[user_code]["virtual_balance"] = round(last_balance, 2)

    # atualizar USDT também
    usdt_brl = get_current_usdt_brl()
    participations[user_code]["virtual_balance_usdt"] = round(last_balance / usdt_brl, 6)

    participations[user_code]["updated_at"] = datetime.utcnow().isoformat()

    save_participations(participations)

    logger.info(f"🔄 Saldo sincronizado para {user_code}: {last_balance}")

    return True


def apply_profit_to_user(user_code: str, profit_brl: float):
    """
    Aplica lucro ao saldo oficial do usuário.
    """
    participations = load_participations()

    if user_code not in participations:
        logger.error("Usuário não encontrado para aplicar lucro")
        return False

    p = participations[user_code]

    saldo_antes = float(p.get("virtual_balance", 0))

    novo_saldo = saldo_antes + profit_brl

    # Atualiza saldos
    p["virtual_balance"] = round(novo_saldo, 2)

    usdt_brl = get_current_usdt_brl()
    p["virtual_balance_usdt"] = round(novo_saldo / usdt_brl, 6)

    # Atualiza lucro acumulado
    p["profit_accumulated"] = round(p.get("profit_accumulated", 0) + profit_brl, 2)
    p["profit_accumulated_usdt"] = round(
        p.get("profit_accumulated_usdt", 0) + (profit_brl / usdt_brl), 6
    )

    p["updated_at"] = datetime.utcnow().isoformat()

    save_participations(participations)

    logger.info(
        f"💰 Lucro aplicado: {profit_brl:.2f} → Saldo: {novo_saldo:.2f} ({user_code})"
    )

    return True

def apply_loss_to_user(user_code: str, loss_brl: float):
    """
    Aplica prejuízo ao saldo oficial do usuário.
    """
    participations = load_participations()

    if user_code not in participations:
        return False

    p = participations[user_code]

    saldo_antes = float(p.get("virtual_balance", 0))
    novo_saldo = max(0, saldo_antes - abs(loss_brl))

    p["virtual_balance"] = round(novo_saldo, 2)

    usdt_brl = get_current_usdt_brl()
    p["virtual_balance_usdt"] = round(novo_saldo / usdt_brl, 6)

    p["updated_at"] = datetime.utcnow().isoformat()

    save_participations(participations)

    logger.info(
        f"📉 Prejuízo aplicado: {loss_brl:.2f} → Saldo: {novo_saldo:.2f} ({user_code})"
    )

    return True


# ============================================
# 🔥 FUNÇÃO PARA RESETAR E DISTRIBUIR SALDO
# ============================================

def reset_and_distribute_balance(total_balance_usdt: float):
    """Reseta posições antigas e distribui o saldo em 3 carteiras base (USDT)"""
    try:
        wallets = load_wallets()
        transactions = load_transactions()
        
        for wallet_code, wallet in wallets.items():
            if not wallet.get('is_base_wallet', True):
                wallet['balanceUSDT'] = 0.0
                wallet['balanceBTC'] = 0.0
                wallet['totalDepositedUSDT'] = 0.0
                wallet['totalDepositedBTC'] = 0.0
                wallet['totalProfitUSDT'] = 0.0
                wallet['updated_at'] = datetime.now().isoformat()
        
        base_wallets_count = len(BASE_WALLETS)
        if base_wallets_count == 0:
            base_wallets_count = 3
            BASE_WALLETS = ["raiz01", "raiz02", "raiz03"]
        
        balance_per_wallet_usdt = total_balance_usdt / base_wallets_count
        
        btc_usdt = get_current_btc_usdt()
        balance_btc_per_wallet = balance_per_wallet_usdt / btc_usdt if btc_usdt > 0 else 0
        
        for wallet_id in BASE_WALLETS:
            if wallet_id not in wallets:
                wallets[wallet_id] = {
                    "id": wallet_id,
                    "code": wallet_id,
                    "name": f"Carteira Base {wallet_id}",
                    "balanceUSDT": balance_per_wallet_usdt,
                    "balanceBTC": balance_btc_per_wallet,
                    "totalDepositedUSDT": balance_per_wallet_usdt,
                    "totalDepositedBTC": balance_btc_per_wallet,
                    "totalProfitUSDT": 0.0,
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                    "phrase_hash": "",
                    "active": True,
                    "is_base_wallet": True
                }
            else:
                wallets[wallet_id]['balanceUSDT'] = balance_per_wallet_usdt
                wallets[wallet_id]['balanceBTC'] = balance_btc_per_wallet
                wallets[wallet_id]['totalDepositedUSDT'] = balance_per_wallet_usdt
                wallets[wallet_id]['totalDepositedBTC'] = balance_btc_per_wallet
                wallets[wallet_id]['updated_at'] = datetime.now().isoformat()
        
        transaction_id = f"RESET-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        reset_transaction = {
            "id": transaction_id,
            "type": "system_reset",
            "description": f"Reset do sistema e distribuição de {total_balance_usdt:.2f} USDT",
            "total_balance_usdt": total_balance_usdt,
            "balance_per_wallet_usdt": balance_per_wallet_usdt,
            "date": datetime.now().isoformat(),
            "status": "completed",
            "currency": "USDT"
        }
        
        transactions.append(reset_transaction)
        
        save_wallets(wallets)
        save_transactions(transactions)
        
        audit_event(
            action="SYSTEM_RESET",
            success=True,
            details=f"Reset do sistema - Distribuído {total_balance_usdt:.2f} USDT em {base_wallets_count} carteiras",
            extra={
                "total_balance_usdt": total_balance_usdt,
                "balance_per_wallet_usdt": balance_per_wallet_usdt,
                "base_wallets": BASE_WALLETS,
                "currency": "USDT"
            }
        )
        
        logger.info(f"🔄 Sistema resetado: {total_balance_usdt:.2f} USDT distribuído em {base_wallets_count} carteiras")
        
        return True, f"Saldo distribuído com sucesso: {balance_per_wallet_usdt:.2f} USDT por carteira"
        
    except Exception as e:
        logger.error(f"❌ Erro no reset: {str(e)}")
        return False, f"Erro no reset: {str(e)}"

def registrarTradeUsuario(user_id: str, symbol: str, side: str, order_data: Dict):
    """Registra o trade executado no log"""
    try:
        log_entry = {
            "user_id": user_id,
            "symbol": symbol,
            "side": side,
            "order_id": order_data.get("orderId"),
            "executed_qty": order_data.get("executedQty"),
            "cummulative_quote_qty": order_data.get("cummulativeQuoteQty"),
            "status": order_data.get("status"),
            "transact_time": order_data.get("transactTime"),
            "timestamp": datetime.now().isoformat()
        }
        
        trade_log_file = os.path.join(BASE_DIR, "user_trades.log.json")
        trades = []
        
        if os.path.exists(trade_log_file):
            with open(trade_log_file, 'r', encoding='utf-8') as f:
                try:
                    trades = json.load(f)
                except:
                    trades = []
        
        trades.append(log_entry)
        
        if len(trades) > 1000:
            trades = trades[-1000:]
        
        with open(trade_log_file, 'w', encoding='utf-8') as f:
            json.dump(trades, f, indent=2, ensure_ascii=False)
        
        logger.info(f"📝 Trade registrado: {user_id} {side} {symbol}")
        
    except Exception as e:
        logger.error(f"❌ Erro ao registrar trade: {e}")

# ============================================
# 🔥 ESTRUTURA GLOBAL DE MÉDIAS DE FLUXO
# ============================================

flow_averages = {
    "BTCUSDT": {
        "daily": {"value": "Neutro", "updated_at": 0},
        "weekly": {"value": "Neutro", "updated_at": 0},
        "monthly": {"value": "Neutro", "updated_at": 0}
    }
}

flow_events_buffer = defaultdict(list)
flow_lock = threading.Lock()

# ============================================
# CONFIGURAÇÃO DE PLANOS
# ============================================

MAX_COINS_MAP = {
    "trial": 3,
    "basic": 10,
    "pro": 15,
    "vip": 10000
}

TRIAL_DEFAULT_COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# ============================================
# FUNÇÕES AUXILIARES GERAIS
# ============================================

def verify_and_fix_share_percent():
    """Verifica e corrige share_percent de todos os usuários"""
    try:
        logger.info("🔍 Verificando share_percent de todos os usuários...")
        
        participations = load_participations()
        master_balance_usdt = get_master_wallet_balance_usdt()
        
        problemas = []
        corrigidos = 0
        
        for user_code, participation in participations.items():
            if participation.get("status") == "active":
                if "user_code" not in participation:
                    participation["user_code"] = user_code
                    problemas.append(f"ADD user_code: {user_code}")
                    corrigidos += 1
                
                share_percent = participation.get("share_percent", 0)
                total_deposited_usdt = participation.get("total_deposited_usdt", 0)
                
                if share_percent == 0 and total_deposited_usdt > 0 and master_balance_usdt > 0:
                    new_share = (total_deposited_usdt / master_balance_usdt) * 100
                    participation["share_percent"] = round(new_share, 4)
                    problemas.append(f"FIX share_percent: {user_code} = {new_share:.2f}%")
                    corrigidos += 1
                
                if 0 < share_percent < 1:
                    participation["share_percent"] = share_percent * 100
                    problemas.append(f"CONVERT decimal to %: {user_code} {share_percent*100:.2f}%")
                    corrigidos += 1
        
        if corrigidos > 0:
            save_participations(participations)
            logger.info(f"✅ {corrigidos} correções aplicadas:")
            for problema in problemas:
                logger.info(f"   • {problema}")
        else:
            logger.info("✅ Todos os share_percent estão corretos")
        
        return {
            "success": True,
            "corrigidos": corrigidos,
            "problemas": problemas,
            "modelo": "share_percent da participação (em %)",
            "fallback": "proporcional ao virtual_balance_usdt se share_percent = 0"
        }
        
    except Exception as e:
        logger.error(f"❌ Erro na verificação: {e}")
        return {"success": False, "error": str(e)}

def normalize_coins_allowed(coins_allowed: Union[str, List, None]) -> Union[str, List]:
    """Normaliza coins_allowed para evitar problemas de formato"""
    if not coins_allowed:
        return []
    
    if coins_allowed == 'ALL' or coins_allowed == ["ALL"]:
        return "ALL"
    
    if isinstance(coins_allowed, str):
        cleaned = coins_allowed.replace('，', ',')
        parts = cleaned.replace(' ', ',').split(',')
        result = [p.strip().upper() for p in parts if p.strip()]
        return result if result else []
    
    if isinstance(coins_allowed, list):
        result = []
        for item in coins_allowed:
            if isinstance(item, str):
                if ' ' in item or ',' in item:
                    sub_parts = item.replace('，', ',').replace(' ', ',').split(',')
                    for sub in sub_parts:
                        if sub.strip():
                            result.append(sub.strip().upper())
                else:
                    result.append(item.strip().upper())
        return result if result else []
    
    return []

def parse_date_ymd(date_str: str):
    """Converte string YYYY-MM-DD para objeto date"""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except:
        return datetime.strptime("1970-01-01", "%Y-%m-%d").date()

# ============================================
# 🔥 FUNÇÕES PARA MÉDIAS DE FLUXO
# ============================================

def flow_type_to_score(flow_type: str) -> int:
    """Converte tipo de fluxo para pontuação numérica"""
    score_map = {
        "Entrada Forte": 2,
        "Entrada": 1,
        "Neutro": 0,
        "Saída": -1,
        "Saída Forte": -2
    }
    return score_map.get(flow_type, 0)

def score_to_flow_type(score: float) -> str:
    """Converte pontuação média para tipo de fluxo"""
    if score >= 1.5:
        return "Entrada Forte"
    elif score > 0.3:
        return "Entrada"
    elif score <= -1.5:
        return "Saída Forte"
    elif score < -0.3:
        return "Saída"
    else:
        return "Neutro"

def register_flow_event(symbol: str, flow_type: str):
    """Registra um evento de fluxo no buffer"""
    with flow_lock:
        timestamp = time.time()
        score = flow_type_to_score(flow_type)
        
        flow_events_buffer[symbol].append({
            "score": score,
            "timestamp": timestamp,
            "type": flow_type
        })
        
        if len(flow_events_buffer[symbol]) > 1000:
            flow_events_buffer[symbol] = flow_events_buffer[symbol][-1000:]

def calculate_averages():
    """Calcula médias de fluxo (diária, semanal, mensal)"""
    global flow_averages
    
    current_time = time.time()
    time_limits = {
        "daily": 24 * 60 * 60,
        "weekly": 7 * 24 * 60 * 60,
        "monthly": 30 * 24 * 60 * 60
    }
    
    with flow_lock:
        new_averages = {}
        
        for symbol, events in flow_events_buffer.items():
            if not events:
                continue
                
            symbol_averages = {}
            
            for period, limit in time_limits.items():
                period_events = [
                    e for e in events 
                    if current_time - e["timestamp"] <= limit
                ]
                
                if not period_events:
                    symbol_averages[period] = {
                        "value": "Neutro",
                        "updated_at": current_time
                    }
                    continue
                
                avg_score = sum(e["score"] for e in period_events) / len(period_events)
                flow_type = score_to_flow_type(avg_score)
                
                symbol_averages[period] = {
                    "value": flow_type,
                    "updated_at": current_time
                }
            
            new_averages[symbol] = symbol_averages
        
        flow_averages.update(new_averages)
        
        if new_averages:
            logger.info(f"📊 Médias atualizadas para {len(new_averages)} símbolos")

def update_flow_averages_thread():
    """Thread de atualização das médias de fluxo"""
    while True:
        try:
            calculate_averages()
        except Exception as e:
            logger.error(f"❌ Erro ao calcular médias: {e}")
        
        time.sleep(900)

# ============================================
# ENDPOINTS PARA MÉDIAS DE FLUXO
# ============================================

@app.route("/flow-event", methods=["POST"])
def post_flow_event():
    """Endpoint para registrar eventos de fluxo"""
    try:
        data = request.get_json(silent=True) or {}
        symbol = data.get("symbol", "").upper()
        flow_type = data.get("flowType", "")
        
        if not symbol or not flow_type:
            return jsonify({"error": "Parâmetros inválidos"}), 400
        
        valid_types = ["Entrada Forte", "Entrada", "Neutro", "Saída", "Saída Forte"]
        if flow_type not in valid_types:
            return jsonify({"error": f"Tipo de fluxo inválido: {flow_type}"}), 400
        
        register_flow_event(symbol, flow_type)
        
        return jsonify({"success": True})
        
    except Exception as e:
        logger.error(f"💥 Erro em /flow-event: {str(e)}")
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

@app.route("/flow-averages", methods=["GET"])
def get_flow_averages():
    """Endpoint de leitura das médias de fluxo"""
    try:
        with flow_lock:
            return jsonify(flow_averages)
    except Exception as e:
        logger.error(f"💥 Erro em /flow-averages: {str(e)}")
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

def get_master_balance_from_confirmed_transactions_usdt() -> float:
    """FONTE ÚNICA: Calcula saldo master APENAS de transações CONFIRMADAS em USDT"""
    try:
        transactions = load_transactions()
        
        master_deposits = [
            tx for tx in transactions 
            if tx.get("type") == "deposit" 
            and tx.get("status") == "confirmado"
            and tx.get("wallet_code") in MASTER_WALLET_CODES
        ]
        
        master_withdrawals = [
            tx for tx in transactions 
            if tx.get("type") == "withdrawal"
            and tx.get("status") == "confirmado"
            and tx.get("wallet_code") in MASTER_WALLET_CODES
        ]
        
        total_deposited_usdt = sum(tx.get("amount_usdt", tx.get("value_usdt", 0)) for tx in master_deposits)
        total_withdrawn_usdt = sum(tx.get("amount_usdt", tx.get("value_usdt", 0)) for tx in master_withdrawals)
        
        real_balance_usdt = total_deposited_usdt - total_withdrawn_usdt
        
        if len(master_deposits) == 0:
            real_balance_usdt = 0.0
            
        logger.info(f"💰 Master balance REAL (transações confirmadas): {real_balance_usdt:.2f} USDT")
        
        return real_balance_usdt
        
    except Exception as e:
        logger.error(f"❌ Erro ao calcular saldo master real: {e}")
        return 0.0

def validate_financial_consistency() -> dict:
    """Valida se os valores no sistema são consistentes em USDT"""
    try:
        logger.info("🔍 VALIDAÇÃO DE CONSISTÊNCIA FINANCEIRA")
        
        transactions = load_transactions()
        wallets = load_wallets()
        
        confirmed_deposits = [
            tx for tx in transactions 
            if tx.get("type") == "deposit" and tx.get("status") == "confirmado"
        ]
        
        real_total_invested_usdt = sum(tx.get("amount_usdt", tx.get("value_usdt", 0)) for tx in confirmed_deposits)
        real_master_balance_usdt = get_master_balance_from_confirmed_transactions_usdt()
        
        current_master_balance_usdt = 0.0
        for wallet_code, wallet in wallets.items():
            if wallet_code in MASTER_WALLET_CODES:
                current_master_balance_usdt += wallet.get("balanceUSDT", 0.0)
        
        inconsistencies = []
        
        if len(confirmed_deposits) == 0:
            if real_total_invested_usdt != 0:
                inconsistencies.append({
                    "rule": "CONFIRMED_ZERO_BUT_TOTAL_NOT_ZERO",
                    "expected": 0,
                    "actual": real_total_invested_usdt,
                    "severity": "CRITICAL"
                })
            
            if real_master_balance_usdt != 0:
                inconsistencies.append({
                    "rule": "CONFIRMED_ZERO_BUT_MASTER_NOT_ZERO", 
                    "expected": 0,
                    "actual": real_master_balance_usdt,
                    "severity": "CRITICAL"
                })
        
        if real_master_balance_usdt > real_total_invested_usdt + 0.01:
            inconsistencies.append({
                "rule": "MASTER_BALANCE_EXCEEDS_TOTAL_INVESTED",
                "total_invested_usdt": real_total_invested_usdt,
                "master_balance_usdt": real_master_balance_usdt,
                "difference": real_master_balance_usdt - real_total_invested_usdt,
                "severity": "CRITICAL"
            })
        
        if abs(current_master_balance_usdt - real_master_balance_usdt) > 0.01:
            inconsistencies.append({
                "rule": "WALLET_BALANCE_MISMATCH_REAL_BALANCE",
                "wallet_balance_usdt": current_master_balance_usdt,
                "real_balance_usdt": real_master_balance_usdt,
                "difference": current_master_balance_usdt - real_master_balance_usdt,
                "severity": "HIGH"
            })
        
        result = {
            "valid": len(inconsistencies) == 0,
            "real_values": {
                "confirmed_deposits": len(confirmed_deposits),
                "total_invested_real_usdt": real_total_invested_usdt,
                "master_balance_real_usdt": real_master_balance_usdt
            },
            "current_values": {
                "master_balance_current_usdt": current_master_balance_usdt
            },
            "inconsistencies": inconsistencies,
            "timestamp": datetime.now().isoformat(),
            "currency": "USDT"
        }
        
        if result["valid"]:
            logger.info("✅ SISTEMA CONSISTENTE - Todos os valores são REAIS em USDT")
        else:
            logger.error(f"🚨 {len(inconsistencies)} INCONSISTÊNCIAS ENCONTRADAS:")
            for inc in inconsistencies:
                logger.error(f"   • {inc['rule']}: {inc}")
        
        return result
        
    except Exception as e:
        logger.error(f"❌ Erro na validação de consistência: {e}")
        return {
            "valid": False,
            "error": str(e)
        }

# ============================================
# VERIFICAÇÃO SIMPLES DE ADMIN (PARA FINANCEIRO)
# ============================================

def verify_finance_admin():
    """Verificação simples de admin para o financeiro"""
    key = request.args.get("key")
    if not key:
        key = request.headers.get(ADMIN_TOKEN_HEADER)
    
    if not key:
        return False
    
    return key == ADMIN_SECRET_KEY

# ============================================
# AUTENTICAÇÃO ADMIN
# ============================================

def require_admin_auth():
    """VERIFICAÇÃO DE ADMIN - MANTIDA PARA COMPATIBILIDADE"""
    admin_key = request.headers.get(ADMIN_TOKEN_HEADER)
    
    if not admin_key:
        admin_key = request.args.get('key')
    
    if not admin_key:
        log_admin_access(
            ip=request.remote_addr,
            action="TENTATIVA_ACESSO_SEM_TOKEN",
            success=False,
            details="Token não fornecido"
        )
        return False, "Token de administração não fornecido"
    
    if admin_key != ADMIN_SECRET_KEY:
        log_admin_access(
            ip=request.remote_addr,
            action="TENTATIVA_ACESSO_TOKEN_INVALIDO",
            success=False,
            details=f"Token fornecido: {admin_key[:10]}..."
        )
        return False, "Token de administração inválido"
    
    return True, "OK"

def log_admin_access(ip: str, action: str, success: bool, details: str = ""):
    """Registra acesso ao admin para auditoria de segurança"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "ip": ip,
        "action": action,
        "success": success,
        "details": details,
        "user_agent": request.headers.get('User-Agent', 'Desconhecido')
    }
    
    try:
        logs = []
        if os.path.exists(ADMIN_LOGS_FILE):
            with open(ADMIN_LOGS_FILE, 'r', encoding='utf-8') as f:
                try:
                    logs = json.load(f)
                except:
                    logs = []
        
        logs.append(log_entry)
        if len(logs) > 1000:
            logs = logs[-1000:]
        
        with open(ADMIN_LOGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(logs, f, indent=2, ensure_ascii=False)
        
        logger.info(f"🔒 LOG ADMIN: {ip} - {action} - Sucesso: {success}")
        
        user_code = ""
        if success and "USUARIO" in action and "CRIAR" not in action:
            import re
            match = re.search(r'Usuário\s+([A-Z0-9]+)', details)
            if match:
                user_code = match.group(1)
        
        audit_event(
            action=f"ADMIN_{action}",
            success=success,
            user_code=user_code,
            details=details,
            extra={"ip": ip}
        )
        
    except Exception as e:
        logger.error(f"Erro ao salvar log: {e}")

# ============================================
# 🔥 ESTRUTURA MASTER WALLET SEPARADA (OBRIGATÓRIA)
# ============================================

def admin_auth_ok(request):
    """Função única de autenticação admin - ACEITA HEADER OU QUERY PARAM"""
    key = request.args.get("key") or request.headers.get("X-ADMIN-KEY")
    return key == ADMIN_SECRET_KEY

def load_master_funds() -> Dict:
    """Carrega os componentes da master wallet separadamente (USDT)"""
    default_master = {
        "binance_balance_usdt": 0.0,
        "profit_usdt": 0.0,
        "updated_at": datetime.now().isoformat(),
        "rules": [
            "binance_balance_usdt: APENAS informativo (nunca usado para cálculos)",
            "profit_usdt: onde entra 50% do lucro + taxas de saque",
            "NUNCA misturar com ledger de usuários",
            "NÃO há reserva fixa - todo saldo está disponível",
            "CURRENCY: USDT"
        ]
    }
    
    master_file = os.path.join(BASE_DIR, "master_funds.json")
    
    if not os.path.exists(master_file):
        save_master_funds(default_master)
        return default_master
    
    try:
        with open(master_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for key in default_master.keys():
                if key not in data:
                    data[key] = default_master[key]
            return data
    except Exception as e:
        logger.error(f"❌ Erro ao carregar master_funds: {e}")
        return default_master

def save_master_funds(master_data: Dict) -> bool:
    """Salva os dados da master wallet separada"""
    try:
        master_data["updated_at"] = datetime.now().isoformat()
        master_file = os.path.join(BASE_DIR, "master_funds.json")
        with open(master_file, 'w', encoding='utf-8') as f:
            json.dump(master_data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"❌ Erro ao salvar master_funds: {e}")
        return False

def update_master_binance_balance_info(balance_info: Dict):
    """Atualiza APENAS o campo informativo binance_balance_usdt"""
    try:
        master_data = load_master_funds()
        master_data["binance_balance_usdt"] = balance_info.get("total_balance", 0)
        master_data["updated_at"] = datetime.now().isoformat()
        save_master_funds(master_data)
        logger.info(f"📊 Binance balance atualizado (informativo): {master_data['binance_balance_usdt']:.2f} USDT")
        return True
    except Exception as e:
        logger.error(f"❌ Erro ao atualizar binance_balance_usdt: {e}")
        return False

# ============================================
# GESTÃO DE USUÁRIOS
# ============================================

def load_users() -> Dict:
    """Carrega usuários do arquivo JSON de forma segura"""
    if not os.path.exists(USERS_FILE):
        logger.info(f"📁 Arquivo {USERS_FILE} não encontrado, criando novo...")
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f, indent=2, ensure_ascii=False)
        return {}
    
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            users_data = json.load(f)
        
        for code, user in users_data.items():
            if 'coins_allowed' in user:
                user['coins_allowed'] = normalize_coins_allowed(user['coins_allowed'])
            
            if user.get('plan') == 'trial':
                coins = user.get('coins_allowed', [])
                if isinstance(coins, list) and len(coins) < 3:
                    user['coins_allowed'] = TRIAL_DEFAULT_COINS.copy()
                user['max_coins'] = 3
        
        logger.info(f"✅ {len(users_data)} usuários carregados e normalizados")
        return users_data
    except Exception as e:
        logger.error(f"❌ Erro ao carregar users.json: {e}")
        backup_file = USERS_FILE + ".backup"
        if os.path.exists(backup_file):
            try:
                shutil.copy2(backup_file, USERS_FILE)
                logger.info("🔄 Restaurado de backup")
                return load_users()
            except:
                pass
        return {}

def save_users(users: Dict) -> bool:
    """Salva usuários com backup automático"""
    backup_path = USERS_FILE + ".bak"
    
    if os.path.exists(USERS_FILE):
        try:
            shutil.copy2(USERS_FILE, backup_path)
            logger.info(f"📂 Backup criado: {backup_path}")
        except Exception as e:
            logger.error(f"❌ Erro ao criar backup: {e}")
    
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, indent=2, ensure_ascii=False)
        
        logger.info(f"💾 {len(users)} usuários salvos com sucesso")
        return True
    except Exception as e:
        logger.error(f"❌ Erro ao salvar usuários: {e}")
        return False

users = load_users()

# ============================================
# VALIDAÇÃO DE USUÁRIOS
# ============================================

def validate_user_code(code: str):
    """Valida código do usuário"""
    code_upper = code.upper()
    
    if code_upper not in users:
        logger.warning(f"Código não encontrado: {code_upper}")
        return False, "Código não encontrado", None
    
    user = users[code_upper]
    
    if not user.get("active", True):
        logger.warning(f"Código inativo: {code_upper}")
        return False, "Código desativado", None
    
    try:
        exp_date = parse_date_ymd(user.get("expires_at", "1970-01-01"))
        today = datetime.now().date()
        
        if today > exp_date:
            logger.warning(f"Código expirado: {code_upper} (expirou em {exp_date})")
            return False, "Código expirado", None
    except Exception as e:
        logger.error(f"Erro na data de expiração: {e}")
        return False, "Erro na data de expiração", None
    
    logger.info(f"✅ Código válido: {code_upper} - Plano: {user.get('plan', 'trial')}")
    return True, "Código válido", user

def get_user_by_phone(phone: str) -> Optional[str]:
    """Encontra usuário pelo telefone"""
    phone_norm = normalize_phone(phone)
    if not phone_norm:
        return None
    
    for code, user in users.items():
        user_phone_norm = normalize_phone(user.get('phone', ''))
        if user_phone_norm == phone_norm and user.get('active', True):
            return code
    return None

def get_user_by_email(email: str) -> Optional[str]:
    """Encontra usuário pelo email"""
    email_norm = normalize_email(email)
    if not email_norm:
        return None
    
    for code, user in users.items():
        user_email_norm = normalize_email(user.get('email', ''))
        if user_email_norm == email_norm and user.get('active', True):
            return code
    return None

# ============================================
# 🔥 ENDPOINTS CRÍTICOS - CORRIGIDOS
# ============================================

@app.route("/login-page")
def serve_login():
    """Página de login"""
    try:
        return send_file(os.path.join(BASE_DIR, "login.html"))
    except:
        return jsonify({"error": "Página não encontrada"}), 404

@app.route("/scanner")
def serve_scanner():
    """Scanner - COM VALIDAÇÃO VIA COOKIE"""
    code = request.cookies.get("session_code", "").strip().upper()
    
    if not code:
        return redirect("/login-page")
    
    if not is_session_active(code):
        return redirect("/login-page")
    
    if code not in users:
        return redirect("/login-page")
    
    user = users[code]
    if not user.get("active", True):
        if code in active_sessions:
            del active_sessions[code]
        return redirect("/login-page")
    
    if not is_user_valid(user):
        if code in active_sessions:
            del active_sessions[code]
        return redirect("/login-page")
    
    active_sessions[code]["last_ping"] = time.time()
    
    response = make_response(send_file(os.path.join(BASE_DIR, "scanner_public.html")))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# =====================================================================
# API SCANNER DATA — usada pelo robotic.py (modo scanner)
# Retorna as moedas com scores calculados pela API da Binance
# O robotic.py consulta este endpoint para decidir qual moeda comprar
#
# CORREÇÃO v7.1: o cálculo agora roda em background thread a cada 60s,
# mantendo o cache sempre quente. O endpoint responde em < 50ms sem
# nunca fazer chamadas HTTP dentro da requisição Flask (elimina timeout).
# =====================================================================
_scanner_cache = {"data": [], "ts": 0}

# Lista das moedas que o scanner monitora (top altcoins por liquidez)
_SCANNER_SYMBOLS = [
    "BNBUSDT","SOLUSDT","ADAUSDT","XRPUSDT","DOTUSDT",
    "LINKUSDT","MATICUSDT","LTCUSDT","AVAXUSDT","ATOMUSDT",
    "NEARUSDT","ALGOUSDT","FTMUSDT","SANDUSDT","MANAUSDT",
    "GALAUSDT","AXSUSDT","LRCUSDT","ONEUSDT","CELOUSDT",
    "HBARUSDT","CHZUSDT","ENJUSDT","BATUSDT","ZRXUSDT",
    "COMPUSDT","AAVEUSDT","UNIUSDT","SUSHIUSDT","CRVUSDT",
]


def _calcular_scanner_moedas():
    """
    Executa o cálculo de score para todas as moedas e retorna a lista
    ordenada por score desc. Chamada exclusivamente pela thread de background.
    Nunca deve ser chamada dentro de uma requisição Flask.
    """
    import requests as _req
    moedas_result = []

    for sym in _SCANNER_SYMBOLS:
        try:
            # Candles 1h (últimas 50)
            url_k = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&limit=50"
            r_k = _req.get(url_k, timeout=5)
            if r_k.status_code != 200:
                continue
            candles = r_k.json()
            if len(candles) < 20:
                continue

            closes = [float(c[4]) for c in candles]
            highs  = [float(c[2]) for c in candles]
            lows   = [float(c[3]) for c in candles]
            vols   = [float(c[5]) for c in candles]

            price = closes[-1]
            if price <= 0:
                continue

            # ── Score expandido 0–17 (compatível com scanner_public.html) ──
            # Score mínimo recomendado para entrada no robô: 7
            score = 0

            # --- Indicadores base ---
            mm20 = sum(closes[-20:]) / 20
            mm9  = sum(closes[-9:])  / 9
            mm50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else mm20

            change_1h = ((price - closes[-2]) / closes[-2]) * 100 if closes[-2] > 0 else 0

            vol_media = sum(vols[-20:]) / 20
            vol_ratio = vols[-1] / vol_media if vol_media > 0 else 1

            deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains  = [d for d in deltas[-14:] if d > 0]
            losses = [-d for d in deltas[-14:] if d < 0]
            avg_g  = sum(gains)  / 14 if gains  else 0
            avg_l  = sum(losses) / 14 if losses else 0.0001
            rs     = avg_g / avg_l
            rsi    = 100 - (100 / (1 + rs))

            open_last = float(candles[-1][1])

            # 1. Tendência MM20 (+2)
            if price > mm20:
                score += 2

            # 2. Tendência MM50 — prazo maior (+1)
            if price > mm50:
                score += 1

            # 3. MM9 acima de MM20 — estrutura bullish curto prazo (+1)
            if mm9 > mm20:
                score += 1

            # 4. Momentum 1h (+2/+1)
            if change_1h > 0.5:
                score += 2
            elif change_1h > 0:
                score += 1

            # 5. Volume relativo (+2/+1)
            if vol_ratio > 1.5:
                score += 2
            elif vol_ratio > 1.0:
                score += 1

            # 6. RSI zona de entrada (+2/+1)
            if 50 <= rsi <= 65:
                score += 2
            elif 45 <= rsi < 50 or 65 < rsi <= 70:
                score += 1

            # 7. RSI subindo nas últimas 3 velas (+1)
            if len(deltas) >= 3 and sum(1 for d in deltas[-3:] if d > 0) >= 2:
                score += 1

            # 8. Candle atual positivo (+1)
            if price > open_last:
                score += 1

            # 9. Candle anterior também positivo — momentum (+1)
            if len(candles) >= 2:
                open_prev  = float(candles[-2][1])
                close_prev = float(candles[-2][4])
                if close_prev > open_prev:
                    score += 1

            # 10. Não em overbought extremo RSI < 75 (+1)
            if rsi < 75:
                score += 1

            # 11. Variação 4h positiva (+1)
            if len(closes) >= 4:
                change_4h = ((price - closes[-4]) / closes[-4]) * 100 if closes[-4] > 0 else 0
                if change_4h > 0:
                    score += 1

            # Suporte e resistência (últimas 50 velas)
            window_n = min(50, len(closes))
            sorted_c = sorted(closes[-window_n:])
            low_n    = max(1, window_n // 20)
            support    = sum(sorted_c[:low_n]) / low_n
            resistance = sum(sorted_c[-low_n:]) / low_n

            # Variação 24h
            url_t = f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}"
            r_t   = _req.get(url_t, timeout=4)
            change_24h = 0.0
            volume_24h = 0.0
            if r_t.status_code == 200:
                td = r_t.json()
                change_24h = float(td.get("priceChangePercent", 0))
                volume_24h = float(td.get("quoteVolume", 0))

            moedas_result.append({
                "symbol":     sym,
                "price":      price,
                "score":      score,
                "rsi":        round(rsi, 1),
                "change_1h":  round(change_1h, 2),
                "change":     round(change_24h, 2),
                "volume":     volume_24h,
                "vol_ratio":  round(vol_ratio, 2),
                "support":    round(support, 8),
                "resistance": round(resistance, 8),
                "suporte":    round(support, 8),
                "resistencia":round(resistance, 8),
                "mm20":       round(mm20, 8),
                "tendencia":  "bullish" if price > mm20 else "bearish"
            })

        except Exception:
            continue  # Pula símbolos com erro

    moedas_result.sort(key=lambda x: x["score"], reverse=True)
    return moedas_result


def _scanner_background_loop():
    """
    Thread de background que atualiza o cache do scanner a cada 60s.
    Iniciada automaticamente no boot do servidor.
    Roda completamente fora das threads Flask — nunca bloqueia requisições.
    """
    global _scanner_cache
    logger.info("🔄 Scanner background thread iniciada")
    while True:
        try:
            moedas = _calcular_scanner_moedas()
            _scanner_cache["data"] = moedas
            _scanner_cache["ts"]   = time.time()
            logger.info(f"✅ Scanner cache atualizado: {len(moedas)} moedas")
        except Exception as e:
            logger.error(f"❌ Erro no scanner background loop: {e}")
        time.sleep(60)  # Atualiza a cada 60 segundos


# ============================================================
# 🤖 LOOP DE BOT INDIVIDUAL — executa entradas para usuários
#    com bot_active=True usando o cache do scanner
# ============================================================

def _executar_compra_usuario(user_id: str, p: dict, symbol: str, valor_usdt: float) -> bool:
    """
    Executa compra via API da Binance usando chaves do usuário.
    Registra posição e debita virtual_balance_usdt.
    Retorna True se sucesso.
    """
    try:
        user_keys = load_user_keys(user_id)
        if not user_keys:
            logger.warning(f"[{user_id}] Sem chaves API salvas")
            return False

        api_key    = user_keys.get("api_key")
        api_secret = user_keys.get("api_secret_decrypted")
        if not api_key or not api_secret:
            logger.warning(f"[{user_id}] Chaves API incompletas")
            return False

        from binance.client import Client
        cli = Client(api_key, api_secret)

        # Verificar saldo real na Binance
        bal = cli.get_asset_balance(asset="USDT")
        free_usdt = float(bal["free"]) if bal else 0.0
        if free_usdt < valor_usdt:
            # Ajusta para o disponível real (mínimo $10)
            valor_usdt = round(free_usdt * 0.99, 2)
            if valor_usdt < 10.0:
                logger.warning(f"[{user_id}] Saldo real insuficiente: ${free_usdt:.2f}")
                return False

        # Obter preço e calcular quantidade
        ticker = cli.get_symbol_ticker(symbol=symbol)
        preco  = float(ticker["price"])

        symbol_info = cli.get_symbol_info(symbol)
        step_size   = 0.000001
        for filt in (symbol_info or {}).get("filters", []):
            if filt["filterType"] == "LOT_SIZE":
                step_size = float(filt["stepSize"])
                break

        qty = (valor_usdt / preco)
        qty = qty - (qty % step_size)
        qty = round(qty, 8)

        if qty <= 0:
            logger.warning(f"[{user_id}] Quantidade calculada zero para {symbol}")
            return False

        # Executar ordem de compra
        order = cli.order_market_buy(symbol=symbol, quantity=qty)

        # Valor real executado via fills
        fills = order.get("fills", [])
        if fills:
            valor_gasto  = sum(float(f["qty"]) * float(f["price"]) for f in fills)
            qty_comprada = sum(float(f["qty"]) for f in fills)
            preco_medio  = valor_gasto / qty_comprada if qty_comprada > 0 else preco
        else:
            qty_comprada = float(order.get("executedQty", qty))
            valor_gasto  = round(qty_comprada * preco, 4)
            preco_medio  = preco

        # Registrar posição aberta
        nova_posicao = {
            "id":                f"{user_id}_{symbol}_{int(time.time())}",
            "usuario_id":        user_id,
            "symbol":            symbol,
            "preco_entrada":     round(preco_medio, 8),
            "quantidade":        round(qty_comprada, 8),
            "valor_entrada_usdt": round(valor_gasto, 4),
            "origem":            "USER_BOT_SCANNER",
            "timestamp":         datetime.now().isoformat(),
            "status":            "aberta",
        }
        if "user_positions" not in p:
            p["user_positions"] = []
        p["user_positions"].append(nova_posicao)

        # Debitar saldo virtual
        saldo_antes = float(p.get("virtual_balance_usdt", 0))
        p["virtual_balance_usdt"]   = max(0.0, round(saldo_antes - valor_gasto, 4))
        p["saldo_disponivel_usdt"]  = max(0.0, round(float(p.get("saldo_disponivel_usdt", 0)) - valor_gasto, 4))
        p["saldo_em_posicoes_usdt"] = round(float(p.get("saldo_em_posicoes_usdt", 0)) + valor_gasto, 4)
        p["updated_at"]             = datetime.now().isoformat()

        logger.info(
            f"✅ [USER_BOT] [{user_id}] COMPROU {symbol} | "
            f"qty={qty_comprada:.6f} | preco={preco_medio:.6f} | "
            f"gasto=${valor_gasto:.2f} | saldo_virtual: {saldo_antes:.2f}→{p['virtual_balance_usdt']:.2f}"
        )
        return True

    except Exception as e:
        logger.error(f"❌ [USER_BOT] [{user_id}] Erro ao comprar {symbol}: {e}")
        return False


def _executar_venda_usuario(user_id: str, p: dict, pos: dict, motivo: str = "USER_BOT") -> bool:
    """
    Executa venda de uma posição aberta do usuário.
    Calcula PnL real, aplica fee 10% e atualiza virtual_balance_usdt.
    Retorna True se sucesso.
    """
    try:
        user_keys = load_user_keys(user_id)
        if not user_keys:
            return False

        api_key    = user_keys.get("api_key")
        api_secret = user_keys.get("api_secret_decrypted")
        if not api_key or not api_secret:
            return False

        from binance.client import Client
        cli = Client(api_key, api_secret)

        symbol     = pos["symbol"]
        base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol

        bal      = cli.get_asset_balance(asset=base_asset)
        free_qty = float(bal["free"]) if bal else 0.0
        if free_qty <= 0:
            logger.warning(f"[{user_id}] Saldo de {base_asset} zero — posição já vendida?")
            return False

        symbol_info = cli.get_symbol_info(symbol)
        step_size   = 0.000001
        for filt in (symbol_info or {}).get("filters", []):
            if filt["filterType"] == "LOT_SIZE":
                step_size = float(filt["stepSize"])
                break

        qty = free_qty - (free_qty % step_size)
        if qty <= 0:
            return False

        order = cli.order_market_sell(symbol=symbol, quantity=qty)

        fills = order.get("fills", [])
        if fills:
            valor_recebido = sum(float(f["qty"]) * float(f["price"]) for f in fills)
            qty_vendida    = sum(float(f["qty"]) for f in fills)
            preco_saida    = valor_recebido / qty_vendida if qty_vendida > 0 else 0
        else:
            ticker         = cli.get_symbol_ticker(symbol=symbol)
            preco_saida    = float(ticker["price"])
            qty_vendida    = float(order.get("executedQty", qty))
            valor_recebido = round(qty_vendida * preco_saida, 4)

        valor_entrada = float(pos.get("valor_entrada_usdt", 0))
        preco_entrada = float(pos.get("preco_entrada", 0))
        pnl_bruto     = round(valor_recebido - valor_entrada, 4)
        pnl_pct       = round(((preco_saida - preco_entrada) / preco_entrada * 100) if preco_entrada > 0 else 0, 2)

        # LUCRO:    50% fee cobrado → usuário recebe 50% do lucro bruto
        # PREJUÍZO: 10% da PERDA cobrado como fee → usuário absorve prejuízo + 10% da perda
        FEE_PREJUIZO = 0.10
        SPLIT_LUCRO  = 0.50
        if pnl_bruto >= 0:
            fee_aplicada  = round(pnl_bruto * SPLIT_LUCRO, 4)      # 50% do lucro vai pro fundo
            pnl_liquido   = round(pnl_bruto * SPLIT_LUCRO, 4)      # 50% fica pro usuário
        else:
            fee_aplicada  = round(abs(pnl_bruto) * FEE_PREJUIZO, 4)  # 10% da PERDA como fee (não do investido)
            pnl_liquido   = round(pnl_bruto - fee_aplicada, 4)      # prejuízo + 10% da perda

        # Fechar posição no histórico
        fechado = {
            **pos,
            "preco_saida":        preco_saida,
            "valor_saida_usdt":   valor_recebido,
            "pnl_usdt":           pnl_bruto,
            "pnl_liquido_usdt":   pnl_liquido,
            "pnl_percent":        pnl_pct,
            "fee_usdt":           fee_aplicada,
            "motivo_saida":       motivo,
            "status":             "fechada",
            "timestamp_saida":    datetime.now().isoformat(),
        }

        posicoes = p.get("user_positions", [])
        p["user_positions"] = [x for x in posicoes if x.get("id") != pos.get("id")]
        if "user_closed_trades" not in p:
            p["user_closed_trades"] = []
        p["user_closed_trades"].append(fechado)

        saldo_antes = float(p.get("virtual_balance_usdt", 0))
        saldo_novo  = max(0.0, round(saldo_antes + pnl_liquido, 4))
        p["virtual_balance_usdt"]   = saldo_novo
        p["saldo_disponivel_usdt"]  = round(float(p.get("saldo_disponivel_usdt", 0)) + valor_recebido, 4)
        p["saldo_em_posicoes_usdt"] = max(0.0, round(float(p.get("saldo_em_posicoes_usdt", 0)) - valor_entrada, 4))

        # Acumula sempre (positivo ou negativo)
        p["profit_accumulated_usdt"] = round(float(p.get("profit_accumulated_usdt", 0)) + pnl_liquido, 4)

        fechado["virtual_balance_antes"]  = saldo_antes
        fechado["virtual_balance_depois"] = saldo_novo
        p["updated_at"] = datetime.now().isoformat()

        emoji = "🟢" if pnl_bruto >= 0 else "🔴"
        logger.info(
            f"{emoji} [USER_BOT] [{user_id}] VENDEU {symbol} | "
            f"PnL bruto={pnl_bruto:+.4f} | Fee={fee_aplicada:.4f} (só no prejuízo) | "
            f"PnL líquido={pnl_liquido:+.4f} | motivo={motivo} | saldo: {saldo_antes:.2f}→{saldo_novo:.2f}"
        )
        return True

    except Exception as e:
        logger.error(f"❌ [USER_BOT] [{user_id}] Erro ao vender {pos.get('symbol')}: {e}")
        return False


def _executar_venda_virtual(user_id: str, p: dict, pos: dict, preco_saida: float, motivo: str) -> bool:
    """
    Fecha uma posição virtual do usuário sem executar ordem real na Binance.
    Aplica regra de fee: lucro → 50% sem fee | prejuízo → +10% fee.
    Atualiza saldo virtual, saldo disponível e acumula PnL.
    """
    try:
        symbol        = pos.get("symbol", "")
        preco_entrada = float(pos.get("preco_entrada", 0))
        quantidade    = float(pos.get("quantidade", 0))
        valor_entrada = float(pos.get("valor_entrada_usdt", 0))

        if preco_entrada <= 0 or quantidade <= 0:
            return False

        valor_saida = round(preco_saida * quantidade, 4)
        pnl_bruto   = round(valor_saida - valor_entrada, 4)
        pnl_pct     = round(((preco_saida - preco_entrada) / preco_entrada * 100), 2)

        # LUCRO:    50% fee cobrado → usuário recebe 50% do lucro bruto
        # PREJUÍZO: 10% da PERDA cobrado como fee → usuário absorve prejuízo + 10% da perda
        FEE_PREJUIZO = 0.10
        SPLIT_LUCRO  = 0.50
        if pnl_bruto >= 0:
            fee_aplicada = round(pnl_bruto * SPLIT_LUCRO, 4)      # 50% do lucro vai pro fundo
            pnl_liquido  = round(pnl_bruto * SPLIT_LUCRO, 4)      # 50% fica pro usuário
        else:
            fee_aplicada = round(abs(pnl_bruto) * FEE_PREJUIZO, 4)  # 10% da PERDA como fee (não do investido)
            pnl_liquido  = round(pnl_bruto - fee_aplicada, 4)      # prejuízo + 10% da perda

        # Fechar posição
        now_iso  = datetime.now().isoformat()
        trade_id = pos.get("id", f"{user_id}_{symbol}_{int(time.time())}")
        cycle_id = datetime.utcnow().strftime("%Y-%m-%d")

        fechado = {
            **pos,
            "preco_saida":        preco_saida,
            "valor_saida_usdt":   valor_saida,
            "pnl_usdt":           pnl_bruto,
            "pnl_liquido_usdt":   pnl_liquido,
            "pnl_percent":        pnl_pct,
            "fee_usdt":           fee_aplicada,
            "motivo_saida":       motivo,
            "exit_reason":        motivo,
            "status":             "fechada",
            "timestamp_saida":    now_iso,
            "exit_time":          now_iso,
            "origem":             "USER_BOT_VIRTUAL",
            "cycle_id":           cycle_id,
        }

        posicoes = p.get("user_positions", [])
        p["user_positions"] = [x for x in posicoes if x.get("id") != pos.get("id")]
        if "user_closed_trades" not in p:
            p["user_closed_trades"] = []
        p["user_closed_trades"].append(fechado)

        saldo_antes = float(p.get("virtual_balance_usdt", 0))
        p["virtual_balance_usdt"]    = max(0.0, round(saldo_antes + pnl_liquido, 4))
        p["saldo_disponivel_usdt"]   = round(float(p.get("saldo_disponivel_usdt", 0)) + valor_saida, 4)
        p["saldo_em_posicoes_usdt"]  = max(0.0, round(float(p.get("saldo_em_posicoes_usdt", 0)) - valor_entrada, 4))
        p["profit_accumulated_usdt"] = round(float(p.get("profit_accumulated_usdt", 0)) + pnl_liquido, 4)
        p["updated_at"] = now_iso

        # ── Registrar transação em transactions.json ──────────────────────────
        try:
            tx_type = "profit_distribution" if pnl_bruto >= 0 else "loss_distribution"
            transactions = load_transactions()
            transactions.append({
                "id":                          f"VB-{trade_id}",
                "trade_id":                    trade_id,
                "user_code":                   user_id,
                "wallet_code":                 user_id,
                "type":                        tx_type,
                "symbol":                      symbol,
                "amount_usdt":                 round(pnl_liquido, 8),
                "pnl_bruto_usdt":              round(pnl_bruto, 8),
                "pnl_liquido_usdt":            round(pnl_liquido, 8),
                "fee_usdt":                    round(fee_aplicada, 8),
                "exit_reason":                 motivo,
                "motivo_saida":                motivo,
                "date":                        now_iso,
                "exit_time":                   now_iso,
                "status":                      "completed",
                "virtual_balance_before_usdt": round(saldo_antes, 8),
                "virtual_balance_after_usdt":  round(p["virtual_balance_usdt"], 8),
                "description":                 f"{symbol} {motivo} | bruto={pnl_bruto:+.4f} líquido={pnl_liquido:+.4f}",
                "origem":                      "USER_BOT_VIRTUAL",
                "currency":                    "USDT",
                "cycle_id":                    cycle_id,
            })
            save_transactions(transactions)
        except Exception as e_tx:
            logger.warning(f"[VIRTUAL] Falha ao salvar transação: {e_tx}")

        # ── Creditar fee na master_wallet (apenas lucro) ──────────────────────
        if fee_aplicada > 0 and pnl_bruto >= 0:
            try:
                add_profit_to_master(
                    amount_usdt=fee_aplicada,
                    description=f"Fee 50% trade virtual {symbol} [{user_id}]"
                )
            except Exception as e_fee:
                logger.warning(f"[VIRTUAL] Falha ao creditar fee na master: {e_fee}")

        emoji = "🟢" if pnl_bruto >= 0 else "🔴"
        logger.info(
            f"{emoji} [VIRTUAL] [{user_id}] FECHOU {symbol} | "
            f"PnL bruto={pnl_bruto:+.4f} | líquido={pnl_liquido:+.4f} | fee={fee_aplicada:.4f} | "
            f"saldo: {saldo_antes:.2f}→{p['virtual_balance_usdt']:.2f}"
        )
        return True

    except Exception as e:
        logger.error(f"❌ [VIRTUAL] [{user_id}] Erro ao fechar {pos.get('symbol')}: {e}")
        return False


def _abrir_posicao_virtual(user_id: str, p: dict, symbol: str, preco: float, valor_usdt: float, score: int, moeda_info: dict) -> bool:
    """
    Abre posição virtual para o usuário sem ordem real Binance.
    Usa preço atual do scanner e debita saldo virtual disponível.
    TP/SL vêm dos dados do scanner (se disponíveis) ou fallback %.
    """
    try:
        saldo_antes = float(p.get("virtual_balance_usdt", 0))
        saldo_disp  = float(p.get("saldo_disponivel_usdt", 0))

        if saldo_disp < valor_usdt:
            return False

        quantidade = round(valor_usdt / preco, 8)
        valor_real = round(quantidade * preco, 4)

        # TP/SL do scanner se disponíveis, senão percentuais padrão
        tp_pct  = float(moeda_info.get("take_profit_pct", 1.5) or 1.5)
        sl_pct  = float(moeda_info.get("stop_loss_pct",  0.8) or 0.8)
        tp_preco = round(preco * (1 + tp_pct / 100), 8)
        sl_preco = round(preco * (1 - sl_pct / 100), 8)

        nova_pos = {
            "id":                   f"{user_id}_{symbol}_{int(time.time())}",
            "usuario_id":           user_id,
            "symbol":               symbol,
            "preco_entrada":        round(preco, 8),
            "quantidade":           quantidade,
            "valor_entrada_usdt":   valor_real,
            "take_profit":          tp_preco,
            "stop_loss":            sl_preco,
            "take_profit_percent":  tp_pct,
            "stop_loss_percent":    sl_pct,
            "score_at_entry":       score,
            "origem":               "USER_BOT_VIRTUAL",
            "timestamp":            datetime.now().isoformat(),
            "status":               "aberta",
        }

        if "user_positions" not in p:
            p["user_positions"] = []
        p["user_positions"].append(nova_pos)

        p["virtual_balance_usdt"]   = max(0.0, round(saldo_antes - valor_real, 4))
        p["saldo_disponivel_usdt"]  = max(0.0, round(saldo_disp - valor_real, 4))
        p["saldo_em_posicoes_usdt"] = round(float(p.get("saldo_em_posicoes_usdt", 0)) + valor_real, 4)
        p["updated_at"]             = datetime.now().isoformat()

        logger.info(
            f"✅ [VIRTUAL] [{user_id}] ABRIU {symbol} | "
            f"qty={quantidade:.6f} @ {preco:.6f} | ${valor_real:.2f} | score={score} | "
            f"TP={tp_preco:.6f} SL={sl_preco:.6f} | saldo: {saldo_antes:.2f}→{p['virtual_balance_usdt']:.2f}"
        )
        return True

    except Exception as e:
        logger.error(f"❌ [VIRTUAL] [{user_id}] Erro ao abrir {symbol}: {e}")
        return False


def _user_bot_loop():
    """
    Bot individual virtual — a cada 30s monitora posições abertas dos usuários com
    bot_active=True e verifica TP/SL baseado no scanner cache (preços reais).
    NÃO executa ordens Binance — opera 100% virtualmente no saldo disponível do usuário.
    O robotic.py abre as posições via /robo/user/open-position.
    Este loop apenas monitora TP/SL e fecha virtualmente via _executar_venda_virtual.
    """
    logger.info("🤖 User Bot Loop VIRTUAL iniciado — monitorando TP/SL das posições individuais")
    time.sleep(20)  # aguarda scanner popular cache

    while True:
        try:
            moedas = _scanner_cache.get("data", [])
            participations = load_participations()
            dirty = False

            for user_id, participation in participations.items():
                if not participation.get("bot_active", False):
                    continue
                if participation.get("status") != "active":
                    continue

                p = _ensure_user_bot_fields(participation)
                posicoes = list(p.get("user_positions", []))

                if not posicoes:
                    continue

                cfg           = p.get("scanner_config", {})
                score_minimo  = int(cfg.get("score_minimo", 7))

                # ── 1. MONITORAR TP/SL de posições abertas ─────────────────
                for pos in posicoes:
                    symbol        = pos.get("symbol", "")
                    preco_entrada = float(pos.get("preco_entrada", 0))
                    tp_preco      = float(pos.get("take_profit", 0))
                    sl_preco      = float(pos.get("stop_loss", 0))

                    if not symbol or preco_entrada <= 0:
                        continue

                    # Buscar preço atual do cache scanner
                    moeda_info  = next((m for m in moedas if m.get("symbol") == symbol), None)
                    preco_atual = float(moeda_info.get("preco_atual", 0) or moeda_info.get("price", 0)) if moeda_info else 0

                    if preco_atual <= 0:
                        continue

                    variacao_pct = ((preco_atual - preco_entrada) / preco_entrada) * 100
                    motivo_saida = None

                    # TP/SL por preço absoluto (se definido pelo robô)
                    if tp_preco > 0 and preco_atual >= tp_preco:
                        motivo_saida = f"TAKE_PROFIT ({variacao_pct:+.2f}%)"
                    elif sl_preco > 0 and preco_atual <= sl_preco:
                        motivo_saida = f"STOP_LOSS ({variacao_pct:+.2f}%)"
                    # Fallback por percentual se TP/SL não definidos
                    elif tp_preco == 0 and sl_preco == 0:
                        tp_pct = float(pos.get("take_profit_percent", 1.5) or 1.5)
                        sl_pct = float(pos.get("stop_loss_percent",  0.8) or 0.8)
                        if variacao_pct >= tp_pct:
                            motivo_saida = f"TAKE_PROFIT ({variacao_pct:+.2f}%)"
                        elif variacao_pct <= -sl_pct:
                            motivo_saida = f"STOP_LOSS ({variacao_pct:+.2f}%)"

                    if motivo_saida:
                        ok = _executar_venda_virtual(user_id, p, pos, preco_atual, motivo_saida)
                        if ok:
                            dirty = True
                            logger.info(f"✅ [USER_BOT_VIRTUAL] [{user_id}] {symbol} fechado: {motivo_saida}")

                # ── 2. VERIFICAR NOVAS ENTRADAS VIRTUAIS ───────────────────
                # Só entra se há oportunidade no scanner com score >= configurado
                # e o saldo virtual comporta a entrada
                posicoes_abertas_atual = p.get("user_positions", [])
                max_pos    = int(cfg.get("max_posicoes", 3))
                valor_ent  = float(cfg.get("valor_entrada", 10.0))
                saldo_disp = float(p.get("saldo_disponivel_usdt", 0))

                if len(posicoes_abertas_atual) >= max_pos:
                    continue
                if saldo_disp < valor_ent:
                    logger.debug(f"[USER_BOT] [{user_id}] Saldo virtual insuficiente ${saldo_disp:.2f}")
                    continue

                symbols_abertos = {pos["symbol"] for pos in posicoes_abertas_atual}

                candidatas = [
                    m for m in moedas
                    if int(m.get("score", 0)) >= score_minimo
                    and m.get("symbol") not in symbols_abertos
                ]
                if not candidatas:
                    continue

                melhor = max(candidatas, key=lambda m: int(m.get("score", 0)))
                symbol_novo = melhor.get("symbol", "")
                preco_novo  = float(melhor.get("preco_atual", 0) or melhor.get("price", 0))
                score_novo  = int(melhor.get("score", 0))

                if not symbol_novo or preco_novo <= 0:
                    continue

                # Abrir posição virtual
                ok = _abrir_posicao_virtual(user_id, p, symbol_novo, preco_novo, valor_ent, score_novo, melhor)
                if ok:
                    dirty = True
                    logger.info(f"🎯 [USER_BOT_VIRTUAL] [{user_id}] Entrada virtual: {symbol_novo} score={score_novo} ${valor_ent:.2f}")

            if dirty:
                try:
                    save_participations(participations)
                except Exception as e:
                    logger.error(f"❌ [USER_BOT] Erro ao salvar: {e}")

        except Exception as e:
            logger.error(f"❌ [USER_BOT] Erro no loop: {e}")

        time.sleep(30)


# Inicia a thread de background imediatamente ao importar/iniciar o módulo.
# daemon=True garante que não impede o processo de encerrar.
_scanner_bg_thread = threading.Thread(target=_scanner_background_loop, daemon=True)
_scanner_bg_thread.start()

_user_bot_thread = threading.Thread(target=_user_bot_loop, daemon=True)
_user_bot_thread.start()


@app.route("/api/scanner-data", methods=["GET"])
def api_scanner_data():
    """
    Endpoint interno para o robô consultar os dados do scanner.
    Pode ser chamado com header X-Scanner-Internal: robotic
    Responde em < 50ms lendo o cache pré-aquecido pela background thread.
    """
    if not _scanner_cache["data"]:
        # Cache ainda não foi populado (primeiros segundos após o boot)
        return jsonify({
            "success": False,
            "moedas": [],
            "message": "Cache aquecendo, tente novamente em 30s",
            "ts": _scanner_cache["ts"]
        }), 503

    age = round(time.time() - _scanner_cache["ts"], 1)
    return jsonify({
        "success": True,
        "moedas": _scanner_cache["data"],
        "cached": True,
        "total":  len(_scanner_cache["data"]),
        "ts":     _scanner_cache["ts"],
        "age_seconds": age
    })


def get_system_info():
    """Informações do sistema"""
    try:
        participations = load_participations()
        active_participations = [p for p in participations.values() if p.get("status") == "active"]
        
        wallets = load_wallets()
        master_balance_usdt = 0
        for wallet_code in MASTER_WALLET_CODES:
            if wallet_code in wallets:
                master_balance_usdt += wallets[wallet_code].get('balanceUSDT', 0)
        
        return {
            "success": True,
            "system_info": {
                "active_participants": len(active_participations),
                "master_balance_usdt": round(master_balance_usdt, 2),
                "total_participations": len(participations),
                "operations_status": get_system_operations_status(),
                "last_profit_distribution": None,
                "server_time": datetime.now().isoformat(),
                "currency": "USDT"
            }
        }
        
    except Exception as e:
        logger.error(f"❌ Erro ao obter info do sistema: {e}")
        return {"success": False, "error": str(e)}

def get_system_operations_status() -> Dict:
    """Retorna status das operações do sistema (sempre livre)"""
    return {
        "deposit_enabled": True,
        "withdraw_enabled": True,
        "reinvest_enabled": True,
        "system_mode": "unrestricted",
        "last_updated": datetime.now().isoformat(),
        "message": "Sistema operando normalmente - Operações livres",
        "currency": "USDT"
    }

@app.route('/api/system/status', methods=['GET'])
def api_system_status():
    """Status do sistema - Sempre operacional"""
    return jsonify({
        "success": True,
        "system": get_system_operations_status(),
        "server_time": datetime.now().isoformat(),
        "version": "7.0"
    })

@app.route("/login", methods=["POST"])
def api_login():
    """Autenticação de usuário - COM REDIRECIONAMENTO PARA PAINEL"""
    cleanup_expired_sessions()
    
    try:
        data = request.get_json(silent=True) or {}
        
        code = (data.get("code") or "").strip().upper()
        session_id = (data.get("session_id") or "").strip()
        
        if not code or not session_id:
            audit_event(
                action="LOGIN_TENTATIVA",
                success=False,
                user_code=code,
                details="Código ou session_id faltando"
            )
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Código ou session_id faltando"
            }), 400
        
        logger.info(f"🔐 Tentativa login: {code}")
        
        audit_event(
            action="LOGIN_TENTATIVA",
            success=True,
            user_code=code,
            details="Tentativa de login"
        )
        
        is_valid, message, user = validate_user_code(code)
        
        if not is_valid:
            logger.warning(f"❌ Login falhou: {code} - {message}")
            
            update_user_status(code, {
                "invalid_attempts": {
                    "timestamp": datetime.now().isoformat(),
                    "reason": message,
                    "ip": request.remote_addr
                }
            })
            
            audit_event(
                action="LOGIN_ERRO_DATA",
                success=False,
                user_code=code,
                details=message
            )
            return jsonify({
                "status": "invalid",
                "success": False,
                "message": message
            }), 400
        
        existing_session = active_sessions.get(code)
        
        if existing_session and existing_session.get("session_id") != session_id:
            logger.warning(f"⛔ Código já em uso: {code}")
            audit_event(
                action="LOGIN_CODIGO_EM_USO",
                success=False,
                user_code=code,
                details="Código já está sendo usado em outra sessão"
            )
            return jsonify({
                "status": "blocked",
                "success": False,
                "message": "Este código já está sendo usado em outra sessão"
            }), 403
        
        try:
            exp_date = parse_date_ymd(user.get("expires_at", "1970-01-01"))
            today = datetime.now().date()
            
            if today > exp_date:
                update_user_status(code, {
                    "invalid_attempts": {
                        "timestamp": datetime.now().isoformat(),
                        "reason": "Código expirado",
                        "ip": request.remote_addr
                    }
                })
                
                audit_event(
                    action="LOGIN_CHAVE_EXPIRADA",
                    success=False,
                    user_code=code,
                    details=f"Código expirado em {exp_date}"
                )
                return jsonify({
                    "status": "expired",
                    "success": False,
                    "message": "Código expirado",
                    "expires_at": str(exp_date)
                }), 403
        except Exception as e:
            logger.error(f"Erro na verificação de data: {e}")
            audit_event(
                action="LOGIN_ERRO_DATA",
                success=False,
                user_code=code,
                details=f"Erro na verificação de data: {e}"
            )
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Erro na data de expiração"
            }), 500
        
        now = time.time()
        session_data = {
            "session_id": session_id,
            "last_ping": now,
            "login_time": now,
            "ip_address": request.remote_addr,
            "user_agent": request.headers.get('User-Agent', 'Desconhecido'),
            "plan": user.get("plan", "trial"),
            "has_participation": False
        }
        
        participations = load_participations()
        if code in participations:
            participation = participations[code]
            if participation.get("status") == "active":
                session_data["has_participation"] = True
                session_data["virtual_balance_usdt"] = participation.get("virtual_balance_usdt", 0)
                session_data["share_percent"] = participation.get("share_percent", 0)
        
        elif code.startswith('ROBO-'):
            wallets = load_wallets()
            if code in wallets:
                wallet = wallets[code]
                session_data["has_participation"] = True
                session_data["virtual_balance_usdt"] = wallet.get("balanceUSDT", 0)
                session_data["share_percent"] = 0
        
        active_sessions[code] = session_data
        
        register_session_event(
            user_code=code,
            event_type="LOGIN",
            session_data=session_data
        )
        
        update_user_status(code, {
            "last_access": datetime.now().isoformat(),
            "sessions": {
                "session_id": session_id,
                "login_time": datetime.now().isoformat(),
                "ip_address": request.remote_addr,
                "status": "active",
                "has_participation": session_data["has_participation"]
            }
        })
        
        logger.info(f"✅ Nova sessão registrada em active_sessions: {code} - Plano: {user.get('plan', 'trial')} - Participação: {session_data['has_participation']}")
        
        audit_event(
            action="LOGIN_OK",
            success=True,
            user_code=code,
            details=f"Plano={user.get('plan','')}, IP={request.remote_addr}, Participação={session_data['has_participation']}",
            extra={
                "session_id": session_id,
                "has_participation": session_data["has_participation"]
            }
        )
        
        coins_allowed = normalize_coins_allowed(user.get("coins_allowed", ["BTCUSDT"]))
        
        resp_data = {
            "status": "ok",
            "success": True,
            "plan": user["plan"],
            "coins_allowed": coins_allowed,
            "expires_at": user["expires_at"],
            "session_id": session_id,
            "max_coins": user.get("max_coins", 1),
            "has_participation": session_data["has_participation"],
            "virtual_balance_usdt": session_data.get("virtual_balance_usdt", 0),
            "share_percent": session_data.get("share_percent", 0),
            "redirect": "/painel.html" if session_data["has_participation"] else "/dashboard.html",
            "currency": "USDT"
        }
        
        if session_data["has_participation"]:
            resp_data["participation_data"] = {
                "virtual_balance_usdt": session_data["virtual_balance_usdt"],
                "share_percent": session_data["share_percent"],
                "operations_info": {
                    "deposit_enabled": True,
                    "withdraw_enabled": True,
                    "reinvest_enabled": True,
                    "system_status": "operational",
                    "system_mode": "unrestricted"
                }
            }
        
        resp_data["operations_info"] = {
            "deposit_enabled": True,
            "withdraw_enabled": True,
            "reinvest_enabled": True,
            "system_status": "operational"
        }
        
        resp = make_response(jsonify(resp_data))
        
        resp.set_cookie(
            "session_code",
            code,
            httponly=True,
            samesite="Lax",
            max_age=60*60*6
        )
        
        resp.set_cookie(
            "session_id",
            session_id,
            httponly=True,
            samesite="Lax",
            max_age=60*60*6
        )
        
        resp.set_cookie(
            "has_participation",
            str(session_data["has_participation"]).lower(),
            httponly=False,
            samesite="Lax",
            max_age=60*60*6
        )
        
        resp.set_cookie(
            "operations_enabled",
            "true",
            httponly=False,
            samesite="Lax",
            max_age=60*60*6
        )
        
        return resp
        
    except Exception as e:
        logger.error(f"💥 Erro no login: {str(e)}")
        audit_event(
            action="LOGIN_ERRO_INTERNO",
            success=False,
            user_code=data.get("code", "").strip().upper() if data else "",
            details=f"Erro interno: {str(e)}"
        )
        return jsonify({
            "status": "error",
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/device-console")
def device_console():
    """Página do device console - PROTEGIDA POR TOKEN"""
    admin_key = request.args.get('key')
    
    if not admin_key or admin_key != ADMIN_SECRET_KEY:
        return jsonify({
            "error": "Acesso negado",
            "message": "Token de administração necessário. Use: /device-console?key=SEU_TOKEN"
        }), 403
    
    log_admin_access(
        ip=request.remote_addr,
        action="ACESSO_DEVICE_CONSOLE",
        success=True,
        details="Acesso via token URL"
    )
    
    return send_file(os.path.join(BASE_DIR, "device_console.html"))

# ============================================
# 🔥 NOVOS ENDPOINTS PARA CONTROLE DE USUÁRIOS
# ============================================

@app.route("/user/search", methods=["POST"])
def user_search():
    """Registra uma busca de moeda pelo usuário"""
    try:
        data = request.get_json(silent=True) or {}
        
        user_code = (data.get("code") or "").strip().upper()
        session_id = (data.get("session_id") or "").strip()
        symbol = (data.get("symbol") or "").strip().upper()
        source = data.get("source", "scanner")
        
        if not user_code or not session_id or not symbol:
            return jsonify({"error": "Código, session_id e símbolo são obrigatórios"}), 400
        
        if user_code not in users:
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        user = users[user_code]
        if not is_user_valid(user):
            if user_code in active_sessions:
                del active_sessions[user_code]
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        search_record = {
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "ip": request.remote_addr,
            "source": source
        }
        
        update_user_status(user_code, {"recent_searches": search_record})
        
        audit_event(
            action="BUSCA_MOEDA",
            success=True,
            user_code=user_code,
            details=f"Busca por {symbol}",
            extra={"symbol": symbol, "source": source}
        )
        
        return jsonify({
            "success": True,
            "message": "Busca registrada"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /user/search: {str(e)}")
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

@app.route("/user/favorite/add", methods=["POST"])
def user_favorite_add():
    """Adiciona uma moeda aos favoritos do usuário"""
    try:
        data = request.get_json(silent=True) or {}
        
        user_code = (data.get("code") or "").strip().upper()
        session_id = (data.get("session_id") or "").strip()
        symbol = (data.get("symbol") or "").strip().upper()
        
        if not user_code or not session_id or not symbol:
            return jsonify({"error": "Código, session_id e símbolo são obrigatórios"}), 400
        
        if user_code not in users:
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        user = users[user_code]
        if not is_user_valid(user):
            if user_code in active_sessions:
                del active_sessions[user_code]
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        update_user_status(user_code, {
            "favorites": {
                "action": "add",
                "symbol": symbol,
                "added_at": datetime.now().isoformat(),
                "ip": request.remote_addr
            }
        })
        
        audit_event(
            action="FAVORITO_ADICIONADO",
            success=True,
            user_code=user_code,
            details=f"Favorito adicionado: {symbol}",
            extra={"symbol": symbol}
        )
        
        return jsonify({
            "success": True,
            "message": "Favorito adicionado"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /user/favorite/add: {str(e)}")
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

@app.route("/user/favorite/remove", methods=["POST"])
def user_favorite_remove():
    """Remove uma moeda dos favoritos do usuário"""
    try:
        data = request.get_json(silent=True) or {}
        
        user_code = (data.get("code") or "").strip().upper()
        session_id = (data.get("session_id") or "").strip()
        symbol = (data.get("symbol") or "").strip().upper()
        
        if not user_code or not session_id or not symbol:
            return jsonify({"error": "Código, session_id e símbolo são obrigatórios"}), 400
        
        if user_code not in users:
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        user = users[user_code]
        if not is_user_valid(user):
            if user_code in active_sessions:
                del active_sessions[user_code]
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        update_user_status(user_code, {
            "favorites": {
                "action": "remove",
                "symbol": symbol
            }
        })
        
        audit_event(
            action="FAVORITO_REMOVIDO",
            success=True,
            user_code=user_code,
            details=f"Favorito removido: {symbol}",
            extra={"symbol": symbol}
        )
        
        return jsonify({
            "success": True,
            "message": "Favorito removido"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /user/favorite/remove: {str(e)}")
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

# ============================================
# 🔥 NOVOS ENDPOINTS ADMIN PARA CONTROLE
# ============================================

@app.route("/admin/user/status/<code>", methods=["GET"])
def admin_user_status(code):
    """Retorna o status completo do usuário para o device_console"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    code = code.upper()
    
    if code not in users:
        return jsonify({
            "success": False,
            "message": "Usuário não encontrado"
        }), 404
    
    user = users[code]
    
    status = get_user_status(code)
    
    status["name"] = user.get("name", "")
    status["email"] = user.get("email", "")
    status["phone"] = user.get("phone", "")
    status["plan"] = user.get("plan", "trial")
    status["active"] = user.get("active", True)
    status["expires_at"] = user.get("expires_at", "")
    status["max_coins"] = user.get("max_coins", 1)
    status["coins_allowed"] = user.get("coins_allowed", [])
    status["status_pagamento"] = user.get("status_pagamento", "pendente")
    status["data_pagamento"] = user.get("data_pagamento", "")
    
    status["sessions"] = []
    if code in active_sessions:
        sess = active_sessions[code]
        status["sessions"].append({
            "session_id": sess.get("session_id", ""),
            "ip_address": sess.get("ip_address", ""),
            "user_agent": sess.get("user_agent", ""),
            "login_time": sess.get("login_time", time.time()),
            "last_ping": sess.get("last_ping", time.time()),
            "status": "online"
        })
    
    return jsonify({
        "success": True,
        "user_status": status
    })

@app.route("/admin/sessions", methods=["GET"])
def admin_list_sessions():
    """Lista todas as sessões ativas com detalhes - PROTEGIDO"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    cleanup_expired_sessions()
    
    try:
        sessions_list = get_active_sessions_list()
        
        log_admin_access(
            ip=request.remote_addr,
            action="LISTAR_SESSOES_ATIVAS",
            success=True,
            details=f"{len(sessions_list)} sessões listadas"
        )
        
        return jsonify({
            "status": "ok",
            "success": True,
            "sessions": sessions_list
        })
    except Exception as e:
        logger.error(f"💥 Erro listar sessões: {str(e)}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/admin/audit-logs", methods=["GET"])
def admin_audit_logs():
    """Obtém logs de auditoria em tempo real - PROTEGIDO"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    try:
        logs = list(realtime_logs)
        logs.reverse()
        
        return jsonify({
            "status": "ok",
            "success": True,
            "logs": logs
        })
    except Exception as e:
        logger.error(f"Erro ao obter logs de auditoria: {e}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": "Erro ao obter logs de auditoria"
        }), 500

@app.route("/admin/api-keys-status", methods=["GET"])
def admin_api_keys_status():
    """Retorna o status das chaves API de todos os usuários - PROTEGIDO"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    try:
        keys = load_user_api_keys()
        status_list = []
        for user_code, key_data in keys.items():
            status_list.append({
                "user_code": user_code,
                "market_mode": key_data.get("market_mode", "spot"),
                "created_at": key_data.get("created_at", ""),
                "updated_at": key_data.get("updated_at", "")
            })
        
        return jsonify({
            "status": "ok",
            "success": True,
            "count": len(status_list),
            "keys": status_list
        })
    except Exception as e:
        logger.error(f"💥 Erro ao listar chaves API: {str(e)}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/admin/user/force-logout", methods=["POST"])
def admin_force_logout():
    """Força o logout de um usuário (remove a sessão ativa) - PROTEGIDO"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").upper().strip()
        session_id = data.get("session_id")
        
        if not code:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Código do usuário é obrigatório"
            }), 400
        
        if code in active_sessions:
            if session_id and active_sessions[code].get("session_id") != session_id:
                return jsonify({
                    "status": "error",
                    "success": False,
                    "message": "Session_id não corresponde"
                }), 400
            
            register_session_event(
                user_code=code,
                event_type="FORCED_LOGOUT",
                session_data=active_sessions[code]
            )
            
            audit_event(
                action="FORCE_LOGOUT",
                success=True,
                user_code=code,
                details="Logout forçado pelo administrador",
                extra={"admin_ip": request.remote_addr}
            )
            
            del active_sessions[code]
            logger.info(f"🚫 Logout forçado: {code}")
            
            log_admin_access(
                ip=request.remote_addr,
                action="FORCE_LOGOUT",
                success=True,
                details=f"Logout forçado para o usuário {code}"
            )
            
            return jsonify({
                "status": "ok",
                "success": True,
                "message": "Logout forçado com sucesso"
            })
        else:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Usuário não possui sessão ativa"
            }), 404
        
    except Exception as e:
        logger.error(f"💥 Erro forçar logout: {str(e)}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/admin/user/delete-keys", methods=["POST"])
def admin_delete_keys():
    """Remove as chaves API de um usuário - PROTEGIDO"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").upper().strip()
        
        if not code:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Código do usuário é obrigatório"
            }), 400
        
        if delete_user_keys(code):
            logger.info(f"🗑️ Chaves API removidas pelo admin: {code}")
            
            log_admin_access(
                ip=request.remote_addr,
                action="DELETE_API_KEYS",
                success=True,
                details=f"Chaves API removidas para o usuário {code}"
            )
            
            return jsonify({
                "status": "ok",
                "success": True,
                "message": "Chaves API removidas com sucesso"
            })
        else:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Usuário não possui chaves API ou erro ao remover"
            }), 404
        
    except Exception as e:
        logger.error(f"💥 Erro remover chaves API: {str(e)}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

# ============================================
# 🔥 ROTAS PARA CHAVES DO USUÁRIO COM VALIDAÇÃO
# ============================================

@app.route("/user/api/save", methods=["POST"])
def user_api_save():
    """Salva as chaves API do usuário"""
    try:
        data = request.get_json(silent=True) or {}
        
        user_code = (data.get("code") or "").strip().upper()
        session_id = (data.get("session_id") or "").strip()
        
        if not user_code or not session_id:
            return jsonify({"error": "Código e session_id são obrigatórios"}), 400
        
        if user_code not in users:
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        user = users[user_code]
        if not is_user_valid(user):
            if user_code in active_sessions:
                del active_sessions[user_code]
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        api_key = data.get("api_key", "").strip()
        api_secret = data.get("api_secret", "").strip()
        market_mode = data.get("market_mode", "spot").strip()
        
        if not api_key or not api_secret:
            return jsonify({"error": "API Key e Secret são obrigatórios"}), 400
        
        if market_mode not in ["spot"]:
            return jsonify({"error": "Modo de mercado inválido. Use 'spot'"}), 400
        
        if not validate_binance_credentials(api_key, api_secret):
            return jsonify({"error": "Credenciais inválidas ou sem permissão de trade"}), 400
        
        save_data = {
            "market_mode": market_mode,
            "api_key": api_key,
            "api_secret": api_secret,
            "created_at": datetime.now().isoformat()
        }
        
        if save_user_keys(user_code, save_data):
            audit_event(
                action="USER_API_SAVE",
                success=True,
                user_code=user_code,
                details="Chaves API salvas com sucesso",
                extra={"market_mode": market_mode}
            )
            
            return jsonify({
                "success": True,
                "message": "Chaves salvas com sucesso"
            })
        else:
            audit_event(
                action="USER_API_SAVE",
                success=False,
                user_code=user_code,
                details="Erro ao salvar chaves API"
            )
            return jsonify({"error": "Erro ao salvar chaves"}), 500
            
    except Exception as e:
        logger.error(f"💥 Erro em /user/api/save: {str(e)}")
        audit_event(
            action="USER_API_SAVE_ERROR",
            success=False,
            user_code=data.get("code", "").strip().upper() if data else "",
            details=f"Erro interno: {str(e)}"
        )
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

@app.route("/user/api/delete", methods=["POST"])
def user_api_delete():
    """Remove as chaves API do usuário"""
    try:
        data = request.get_json(silent=True) or {}
        
        user_code = (data.get("code") or "").strip().upper()
        session_id = (data.get("session_id") or "").strip()
        
        if not user_code or not session_id:
            return jsonify({"error": "Código e session_id são obrigatórios"}), 400
        
        if user_code not in users:
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        user = users[user_code]
        if not is_user_valid(user):
            if user_code in active_sessions:
                del active_sessions[user_code]
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        if delete_user_keys(user_code):
            audit_event(
                action="USER_API_DELETE",
                success=True,
                user_code=user_code,
                details="Chaves API removidas com sucesso"
            )
            
            return jsonify({
                "success": True,
                "message": "Chaves removidas com sucesso"
            })
        else:
            audit_event(
                action="USER_API_DELETE",
                success=False,
                user_code=user_code,
                details="Usuário não possui chaves salvas"
            )
            return jsonify({"error": "Usuário não possui chaves salvas"}), 404
            
    except Exception as e:
        logger.error(f"💥 Erro em /user/api/delete: {str(e)}")
        audit_event(
            action="USER_API_DELETE_ERROR",
            success=False,
            user_code=data.get("code", "").strip().upper() if data else "",
            details=f"Erro interno: {str(e)}"
        )
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

@app.route("/user/api/status", methods=["GET"])
def user_api_status():
    """Verifica status das chaves do usuário"""
    try:
        user_code = request.args.get("code", "").strip().upper()
        session_id = request.args.get("session_id", "").strip()
        
        if not user_code or not session_id:
            return jsonify({"error": "Código e session_id são obrigatórios"}), 400
        
        if user_code not in users:
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        user = users[user_code]
        if not is_user_valid(user):
            if user_code in active_sessions:
                del active_sessions[user_code]
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        has_keys = user_has_keys(user_code)
        market_mode = None
        
        if has_keys:
            user_keys = load_user_keys(user_code)
            market_mode = user_keys.get("market_mode") if user_keys else None
        
        return jsonify({
            "has_keys": has_keys,
            "market_mode": market_mode
        })
            
    except Exception as e:
        logger.error(f"💥 Erro em /user/api/status: {str(e)}")
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

# ============================================
# 🔥 ROTAS PARA TRADE DO USUÁRIO COM VALIDAÇÃO
# ============================================

@app.route("/user/trade/buy", methods=["POST"])
def user_trade_buy():
    """Executa ordem de compra com chaves do usuário"""
    try:
        data = request.get_json(silent=True) or {}
        
        user_code = (data.get("code") or "").strip().upper()
        session_id = (data.get("session_id") or "").strip()
        symbol = (data.get("symbol") or "").strip().upper()
        
        if not user_code or not session_id or not symbol:
            return jsonify({"error": "Código, session_id e símbolo são obrigatórios"}), 400
        
        if user_code not in users:
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        user = users[user_code]
        if not is_user_valid(user):
            if user_code in active_sessions:
                del active_sessions[user_code]
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        if not user_has_keys(user_code):
            return jsonify({"error": "Usuário não possui chaves API configuradas"}), 400
        
        result = executarOrdemUsuario(user_code, symbol, "BUY")
        
        if result.get("success"):
            audit_event(
                action="USER_TRADE_BUY",
                success=True,
                user_code=user_code,
                details=f"Compra executada: {symbol}",
                extra={
                    "symbol": symbol,
                    "order_id": result.get("order", {}).get("orderId"),
                    "executed_qty": result.get("order", {}).get("executedQty")
                }
            )
            
            return jsonify({
                "success": True,
                "message": result.get("message"),
                "order_id": result.get("order", {}).get("orderId")
            })
        else:
            audit_event(
                action="USER_TRADE_BUY",
                success=False,
                user_code=user_code,
                details=f"Falha na compra: {symbol} - {result.get('error')}",
                extra={"symbol": symbol, "error": result.get("error")}
            )
            return jsonify({"error": result.get("error")}), 400
            
    except Exception as e:
        logger.error(f"💥 Erro em /user/trade/buy: {str(e)}")
        audit_event(
            action="USER_TRADE_BUY_ERROR",
            success=False,
            user_code=data.get("code", "").strip().upper() if data else "",
            details=f"Erro interno: {str(e)}",
            extra={"symbol": data.get("symbol", "") if data else ""}
        )
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

@app.route("/user/trade/sell", methods=["POST"])
def user_trade_sell():
    """Executa ordem de venda com chaves do usuário"""
    try:
        data = request.get_json(silent=True) or {}
        
        user_code = (data.get("code") or "").strip().upper()
        session_id = (data.get("session_id") or "").strip()
        symbol = (data.get("symbol") or "").strip().upper()
        
        if not user_code or not session_id or not symbol:
            return jsonify({"error": "Código, session_id e símbolo são obrigatórios"}), 400
        
        if user_code not in users:
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        user = users[user_code]
        if not is_user_valid(user):
            if user_code in active_sessions:
                del active_sessions[user_code]
            return jsonify({
                "status": "expired",
                "message": "Plano expirado ou usuário desativado"
            }), 403
        
        if not user_has_keys(user_code):
            return jsonify({"error": "Usuário não possui chaves API configuradas"}), 400
        
        result = executarOrdemUsuario(user_code, symbol, "SELL")
        
        if result.get("success"):
            audit_event(
                action="USER_TRADE_SELL",
                success=True,
                user_code=user_code,
                details=f"Venda executada: {symbol}",
                extra={
                    "symbol": symbol,
                    "order_id": result.get("order", {}).get("orderId"),
                    "executed_qty": result.get("order", {}).get("executedQty")
                }
            )
            
            return jsonify({
                "success": True,
                "message": result.get("message"),
                "order_id": result.get("order", {}).get("orderId")
            })
        else:
            audit_event(
                action="USER_TRADE_SELL",
                success=False,
                user_code=user_code,
                details=f"Falha na venda: {symbol} - {result.get('error')}",
                extra={"symbol": symbol, "error": result.get("error")}
            )
            return jsonify({"error": result.get("error")}), 400
            
    except Exception as e:
        logger.error(f"💥 Erro em /user/trade/sell: {str(e)}")
        audit_event(
            action="USER_TRADE_SELL_ERROR",
            success=False,
            user_code=data.get("code", "").strip().upper() if data else "",
            details=f"Erro interno: {str(e)}",
            extra={"symbol": data.get("symbol", "") if data else ""}
        )
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

# ============================================
# ROTAS FINANCEIRO SIMPLIFICADAS
# ============================================

@app.route("/finance-data", methods=["GET"])
def finance_data():
    """Retorna dados financeiros básicos"""
    if not verify_finance_admin():
        audit_event(
            action="FINANCE_DATA_ACCESS",
            success=False,
            details="Tentativa de acesso não autorizado ao financeiro"
        )
        return jsonify({"error": "unauthorized"}), 401
    
    try:
        audit_event(
            action="FINANCE_DATA_ACCESS",
            success=True,
            details="Acesso aos dados financeiros"
        )
        
        users_list = []
        for code, user in users.items():
            if "status_pagamento" not in user:
                user["status_pagamento"] = "pendente"
            if "data_pagamento" not in user:
                user["data_pagamento"] = ""
            if "valor_plano" not in user:
                plan_values = {
                    "trial": 0.0,
                    "basic": 49.90,
                    "pro": 99.90,
                    "vip": 199.90
                }
                user["valor_plano"] = plan_values.get(user.get("plan", "trial"), 0.0)
            
            users_list.append({
                "code": code,
                "name": user.get("name", ""),
                "phone": user.get("phone", ""),
                "email": user.get("email", ""),
                "plan": user.get("plan", "trial"),
                "expires_at": user.get("expires_at", ""),
                "active": user.get("active", True),
                "status_pagamento": user["status_pagamento"],
                "valor_plano": float(user["valor_plano"])
            })
        
        payments_list = []
        for code, user in users.items():
            user_payments = user.get("payments", [])
            for payment in user_payments:
                if isinstance(payment, dict) and "amount" in payment:
                    payments_list.append({
                        "id": f"{code}_{payment.get('date', '')}",
                        "user_code": code,
                        "amount": float(payment["amount"]),
                        "due_date": payment.get("date", ""),
                        "status": "paid",
                        "paid_at": payment.get("date", ""),
                        "description": payment.get("notes", "Pagamento")
                    })
        
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        
        arrecadacao_mensal = 0.0
        clientes_ativos = 0
        vencimentos_hoje = 0
        vence_amanha = 0
        
        for code, user in users.items():
            if user.get("active", True):
                clientes_ativos += 1
                
                user_payments = user.get("payments", [])
                for payment in user_payments:
                    if isinstance(payment, dict) and "date" in payment:
                        payment_date = payment.get("date", "")
                        if payment_date.startswith(today.strftime("%Y-%m")):
                            arrecadacao_mensal += float(payment.get("amount", 0))
                
                expires_at = user.get("expires_at", "")
                if expires_at:
                    try:
                        exp_date = parse_date_ymd(expires_at)
                        if exp_date == today:
                            vencimentos_hoje += 1
                        elif exp_date == tomorrow:
                            vence_amanha += 1
                    except:
                        pass
        
        return jsonify({
            "ok": True,
            "users": users_list,
            "payments": payments_list,
            "kpis": {
                "arrecadacao_mensal": round(arrecadacao_mensal, 2),
                "clientes_ativos": clientes_ativos,
                "vencimentos_hoje": vencimentos_hoje,
                "vence_amanha": vence_amanha
            }
        })
        
    except Exception as e:
        logger.error(f"💥 Erro finance-data: {str(e)}")
        audit_event(
            action="FINANCE_DATA_ERROR",
            success=False,
            details=f"Erro interno: {str(e)}"
        )
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

@app.route("/mark-paid", methods=["POST"])
def mark_paid():
    """Marca um pagamento como pago"""
    if not verify_finance_admin():
        audit_event(
            action="MARK_PAID_ACCESS",
            success=False,
            details="Tentativa de acesso não autorizado ao mark-paid"
        )
        return jsonify({"error": "unauthorized"}), 401
    
    try:
        data = request.get_json(silent=True) or {}
        
        user_code = (data.get("user_code") or "").strip().upper()
        
        if not user_code:
            return jsonify({"error": "user_code é obrigatório"}), 400
        
        if user_code not in users:
            return jsonify({"error": "Usuário não encontrado"}), 404
        
        if "payments" not in users[user_code]:
            users[user_code]["payments"] = []
        
        amount = users[user_code].get("valor_plano", 0)
        if not amount or amount <= 0:
            plan_values = {
                "trial": 0.0,
                "basic": 49.90,
                "pro": 99.90,
                "vip": 199.90
            }
            amount = plan_values.get(users[user_code].get("plan", "trial"), 0.0)
        
        payment_record = {
            "amount": float(amount),
            "date": datetime.now().date().isoformat(),
            "notes": "Pagamento marcado como pago via financeiro",
            "registered_at": datetime.now().isoformat(),
            "registered_by": "finance_admin"
        }
        
        users[user_code]["payments"].append(payment_record)
        
        users[user_code]["status_pagamento"] = "pago"
        users[user_code]["data_pagamento"] = datetime.now().date().isoformat()
        users[user_code]["updated_at"] = datetime.now().isoformat()
        
        if save_users(users):
            audit_event(
                action="MARK_PAID",
                success=True,
                user_code=user_code,
                details="Pagamento marcado como pago",
                extra={
                    "amount": float(amount),
                    "payment_date": datetime.now().date().isoformat()
                }
            )
            
            logger.info(f"✅ Pagamento marcado como pago: {user_code}")
            return jsonify({"ok": True})
        else:
            audit_event(
                action="MARK_PAID",
                success=False,
                user_code=user_code,
                details="Erro ao salvar pagamento"
            )
            return jsonify({"error": "Erro ao salvar pagamento"}), 500
        
    except Exception as e:
        logger.error(f"💥 Erro mark-paid: {str(e)}")
        audit_event(
            action="MARK_PAID_ERROR",
            success=False,
            user_code=data.get("user_code", "").strip().upper() if data else "",
            details=f"Erro interno: {str(e)}"
        )
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

@app.route("/register-payment", methods=["POST"])
def register_payment():
    """Registra um pagamento manual"""
    if not verify_finance_admin():
        audit_event(
            action="REGISTER_PAYMENT_ACCESS",
            success=False,
            details="Tentativa de acesso não autorizado ao register-payment"
        )
        return jsonify({"error": "unauthorized"}), 401
    
    try:
        data = request.get_json(silent=True) or {}
        
        user_code = (data.get("user_code") or "").strip().upper()
        amount = data.get("amount")
        
        if not user_code:
            return jsonify({"error": "user_code é obrigatório"}), 400
        
        if not amount or float(amount) <= 0:
            return jsonify({"error": "amount é obrigatório e deve ser maior que zero"}), 400
        
        if user_code not in users:
            return jsonify({"error": "Usuário não encontrado"}), 404
        
        if "payments" not in users[user_code]:
            users[user_code]["payments"] = []
        
        payment_record = {
            "amount": float(amount),
            "date": data.get("payment_date", datetime.now().date().isoformat()),
            "notes": data.get("notes", "Pagamento manual registrado"),
            "registered_at": datetime.now().isoformat(),
            "registered_by": "finance_admin"
        }
        
        users[user_code]["payments"].append(payment_record)
        
        users[user_code]["status_pagamento"] = "pago"
        users[user_code]["data_pagamento"] = payment_record["date"]
        
        users[user_code]["updated_at"] = datetime.now().isoformat()
        
        if save_users(users):
            audit_event(
                action="REGISTER_PAYMENT",
                success=True,
                user_code=user_code,
                details="Pagamento manual registrado",
                extra={
                    "amount": float(amount),
                    "payment_date": payment_record["date"],
                    "notes": data.get("notes", "Pagamento manual registrado")
                }
            )
            
            logger.info(f"💰 Pagamento registrado: {user_code} - R${amount}")
            return jsonify({"ok": True})
        else:
            audit_event(
                action="REGISTER_PAYMENT",
                success=False,
                user_code=user_code,
                details="Erro ao salvar pagamento"
            )
            return jsonify({"error": "Erro ao salvar pagamento"}), 500
        
    except Exception as e:
        logger.error(f"💥 Erro register-payment: {str(e)}")
        audit_event(
            action="REGISTER_PAYMENT_ERROR",
            success=False,
            user_code=data.get("user_code", "").strip().upper() if data else "",
            details=f"Erro interno: {str(e)}"
        )
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

# ============================================
# 🔥 ENDPOINT PARA LOGS EM TEMPO REAL
# ============================================

@app.route("/finance-realtime-logs", methods=["GET"])
def finance_realtime_logs():
    """Retorna os últimos 50 logs de auditoria em tempo real"""
    key = request.args.get("key")
    if key != "ADMIN-COYOTE-2025-ULTRA":
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    logs = list(realtime_logs)
    logs.reverse()
    
    return jsonify({
        "ok": True,
        "logs": logs[-50:] if len(logs) > 50 else logs
    })

# ============================================
# ROTAS DO ADMIN ORIGINAL
# ============================================

@app.route("/")
def serve_index():
    """Página inicial"""
    try:
        return send_file(os.path.join(BASE_DIR, "index.html"))
    except:
        return jsonify({"error": "Página não encontrada"}), 404

@app.route("/admin")
def serve_admin():
    """Painel Admin - PROTEGIDO POR TOKEN"""
    admin_key = request.args.get('key')
    
    if not admin_key or admin_key != ADMIN_SECRET_KEY:
        return jsonify({
            "error": "Acesso negado",
            "message": "Token de administração necessário. Use: /admin?key=SEU_TOKEN"
        }), 403
    
    log_admin_access(
        ip=request.remote_addr,
        action="ACESSO_PAINEL_ADMIN",
        success=True,
        details="Acesso via token URL"
    )
    
    try:
        return send_file(os.path.join(BASE_DIR, "admin.html"))
    except:
        return jsonify({"error": "Página não encontrada"}), 404

@app.route("/finance-admin")
def finance_admin():
    try:
        return send_file(os.path.join(BASE_DIR, "finance_admin.html"))
    except Exception as e:
        return jsonify({"error": "Página não encontrada", "detail": str(e)}), 404



@app.route("/binance-24h", methods=["GET"])
def binance_24h_proxy():
    """Proxy para dados de 24h da Binance"""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            timeout=10
        )
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        logger.error(f"❌ Erro Binance proxy: {e}")
        return jsonify({"error": "Falha ao buscar dados da Binance"}), 500

@app.route("/ping", methods=["POST"])
def api_ping():
    """Mantém sessão ativa - COM ATUALIZAÇÃO DE LAST_PING"""
    cleanup_expired_sessions()
    
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip().upper()
        session_id = (data.get("session_id") or "").strip()
        
        if not code or not session_id:
            return jsonify({"status": "error", "message": "Dados faltando"}), 400
        
        if code in users:
            user = users[code]
            if not is_user_valid(user):
                if code in active_sessions:
                    del active_sessions[code]
                return jsonify({
                    "status": "expired",
                    "message": "Plano expirado ou usuário desativado"
                }), 403
        
        now = time.time()
        if code in active_sessions:
            active_sessions[code]["last_ping"] = now
            
            if int(now) % 30 == 0:
                register_session_event(
                    user_code=code,
                    event_type="PING",
                    session_data=active_sessions[code]
                )
        
        return jsonify({
            "status": "alive",
            "last_ping": now,
            "session_timeout": SESSION_TIMEOUT
        })
        
    except Exception as e:
        logger.error(f"Erro no ping: {str(e)}")
        return jsonify({"status": "error", "message": "Erro interno"}), 500

@app.route("/verify-session", methods=["POST"])
def api_verify_session():
    """Verifica sessão"""
    cleanup_expired_sessions()
    
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip().upper()
        session_id = (data.get("session_id") or "").strip()
        
        if not code or not session_id:
            return jsonify({"status": "error", "message": "Dados faltando"}), 400
        
        is_active = is_session_active(code, session_id)
        is_valid = False
        
        if is_active and code in users:
            user = users[code]
            is_valid = is_user_valid(user)
            
            if is_active and not is_valid:
                if code in active_sessions:
                    del active_sessions[code]
                is_active = False
        
        if is_active and is_valid:
            return jsonify({
                "status": "valid", 
                "valid": True,
                "plan": users[code].get("plan", "trial"),
                "expires_at": users[code].get("expires_at", "")
            }), 200
        else:
            return jsonify({
                "status": "invalid", 
                "valid": False,
                "message": "Sessão expirada ou plano inválido"
            }), 200
            
    except Exception as e:
        logger.error(f"Erro verify-session: {str(e)}")
        return jsonify({"status": "error", "valid": False}), 500

@app.route("/logout", methods=["POST"])
def api_logout():
    """Logout - COM REGISTRO COMPLETO"""
    cleanup_expired_sessions()
    
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip().upper()
        session_id = (data.get("session_id") or "").strip()
        
        logger.info(f"🚪 Logout: {code}")
        
        if not code or not session_id:
            return jsonify({"status": "error", "message": "Dados faltando"}), 400
        
        if code in active_sessions and active_sessions[code].get("session_id") == session_id:
            register_session_event(
                user_code=code,
                event_type="LOGOUT",
                session_data=active_sessions[code]
            )
            
            update_user_status(code, {
                "sessions": {
                    "session_id": session_id,
                    "logout_time": datetime.now().isoformat(),
                    "status": "logged_out"
                }
            })
            
            audit_event(
                action="LOGOUT",
                success=True,
                user_code=code,
                details="Sessão finalizada pelo usuário",
                extra={"session_id": session_id}
            )
            
            del active_sessions[code]
            logger.info(f"✅ Sessão finalizada: {code}")
            
            return jsonify({"status": "ok", "message": "Sessão finalizada"})
        
        logger.warning(f"❌ Sessão não encontrada: {code}")
        return jsonify({"status": "not_found", "message": "Sessão não encontrada"}), 404
        
    except Exception as e:
        logger.error(f"Erro logout: {e}")
        return jsonify({"status": "error", "message": "Erro interno"}), 500

# ============================================
# API ADMIN ORIGINAL
# ============================================

@app.route("/admin/users", methods=["GET"])
def admin_list_users():
    """Lista todos os usuários - PROTEGIDO"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    cleanup_expired_sessions()
    
    try:
        user_list = []
        for code, user_data in users.items():
            coins_allowed = normalize_coins_allowed(user_data.get("coins_allowed", []))
            
            user_info = {
                "code": code,
                "name": user_data.get("name", ""),
                "phone": user_data.get("phone", ""),
                "email": user_data.get("email", ""),
                "plan": user_data.get("plan", "trial"),
                "expires_at": user_data.get("expires_at", "1970-01-01"),
                "active": user_data.get("active", True),
                "max_coins": user_data.get("max_coins", 1),
                "coins_allowed": coins_allowed,
                "status_pagamento": user_data.get("status_pagamento", "pendente"),
                "data_pagamento": user_data.get("data_pagamento", ""),
                "observacoes_admin": user_data.get("observacoes_admin", ""),
                "created_at": user_data.get("created_at", ""),
                "fingerprint": user_data.get("fingerprint", "")[:16] + "..." if user_data.get("fingerprint") else ""
            }
            user_list.append(user_info)
        
        log_admin_access(
            ip=request.remote_addr,
            action="LISTAR_USUARIOS",
            success=True,
            details=f"{len(user_list)} usuários listados"
        )
        
        return jsonify({
            "status": "ok",
            "success": True,
            "users": user_list
        })
    except Exception as e:
        logger.error(f"💥 Erro listar usuários: {str(e)}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/admin/logs", methods=["GET"])
def admin_get_logs():
    """Obtém logs de acesso ao admin - PROTEGIDO"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    try:
        logs = []
        if os.path.exists(ADMIN_LOGS_FILE):
            with open(ADMIN_LOGS_FILE, 'r', encoding='utf-8') as f:
                try:
                    logs = json.load(f)
                except:
                    logs = []
        
        logs = logs[-50:]
        
        return jsonify({
            "status": "ok",
            "success": True,
            "logs": logs
        })
    except Exception as e:
        logger.error(f"Erro ao obter logs: {e}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": "Erro ao obter logs"
        }), 500

@app.route("/admin/user/create", methods=["POST"])
def admin_create_user():
    """Cria novo usuário - COM PARTICIPAÇÃO INICIAL em USDT"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403

    cleanup_expired_sessions()

    try:
        data = request.get_json(silent=True) or {}

        code = (data.get("code") or "").upper().strip()
        name = data.get("name", "").strip()
        phone = data.get("phone", "").strip()
        email = data.get("email", "").strip()
        plan = data.get("plan", "trial")
        expires_at = data.get("expires_at")
        coins_allowed = data.get("coins_allowed", [])
        status_pagamento = data.get("status_pagamento", "pendente")
        data_pagamento = data.get("data_pagamento", "")
        observacoes_admin = data.get("observacoes_admin", "")
        active = data.get("active", True)
        initial_deposit_usdt = float(data.get("initial_deposit_usdt", 0))

        logger.info(f"🆕 Criar usuário: {code} - {name}")

        if not code or not expires_at:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Código e data de expiração são obrigatórios"
            }), 400

        if code in users:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Usuário já existe"
            }), 400

        if plan == "trial":
            normalized_coins = TRIAL_DEFAULT_COINS.copy()
            max_coins = 3
        else:
            normalized_coins = normalize_coins_allowed(coins_allowed)
            max_coins = MAX_COINS_MAP.get(plan, 1)

        plan_values = {
            "trial": 0.0,
            "basic": 49.90,
            "pro": 99.90,
            "vip": 199.90
        }
        valor_plano = plan_values.get(plan, 0.0)

        created_at = datetime.now().isoformat()

        users[code] = {
            "name": name,
            "phone": phone,
            "email": email,
            "plan": plan,
            "expires_at": expires_at,
            "max_coins": max_coins,
            "coins_allowed": normalized_coins,
            "status_pagamento": status_pagamento,
            "data_pagamento": data_pagamento,
            "observacoes_admin": observacoes_admin,
            "valor_plano": valor_plano,
            "active": active,
            "created_at": created_at,
            "updated_at": created_at,
            "payments": [],
            "initial_deposit_usdt": initial_deposit_usdt
        }

        if save_users(users):
            participations = load_participations()
            if code not in participations:
                master_balance_usdt = get_master_wallet_balance_usdt()
                participations[code] = {
                    "user_code": code,
                    "master_wallet": MASTER_WALLET_CODES[0],
                    "virtual_balance_usdt": initial_deposit_usdt,
                    "total_deposited_usdt": initial_deposit_usdt,
                    "profit_accumulated_usdt": 0.0,
                    "share_percent": calculate_share_percent(initial_deposit_usdt, master_balance_usdt + initial_deposit_usdt),
                    "joined_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                    "status": "active",
                    "last_profit_distribution": None,
                    "total_withdrawn_usdt": 0.0,
                    "created_by_admin": True
                }
                save_participations(participations)
                
                if initial_deposit_usdt > 0:
                    wallets = load_wallets()
                    master_wallet_code = MASTER_WALLET_CODES[0]
                    
                    if master_wallet_code in wallets:
                        master_wallet = wallets[master_wallet_code]
                        master_wallet["balanceUSDT"] += initial_deposit_usdt
                        master_wallet["totalDepositedUSDT"] += initial_deposit_usdt
                        master_wallet["updated_at"] = datetime.now().isoformat()
                        
                        transactions = load_transactions()
                        transaction_id = f"INIT-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                        
                        init_transaction = {
                            "id": transaction_id,
                            "wallet_code": master_wallet_code,
                            "type": "initial_deposit",
                            "amount_usdt": initial_deposit_usdt,
                            "value_btc": initial_deposit_usdt / get_current_btc_usdt(),
                            "date": datetime.now().isoformat(),
                            "status": "confirmado",
                            "description": f"Depósito inicial do usuário {code}",
                            "user_code": code,
                            "currency": "USDT"
                        }
                        
                        transactions.append(init_transaction)
                        
                        save_wallets(wallets)
                        save_transactions(transactions)
                        
                        recalculate_all_shares_usdt()
                        
                        logger.info(f"💰 Depósito inicial de {initial_deposit_usdt:.2f} USDT adicionado à carteira master para usuário {code}")

            log_admin_access(
                ip=request.remote_addr,
                action="CRIAR_USUARIO",
                success=True,
                details=f"Usuário {code} criado com participação inicial de {initial_deposit_usdt:.2f} USDT"
            )

            return jsonify({
                "status": "ok",
                "success": True,
                "message": "Usuário criado com sucesso",
                "user": users[code],
                "participation_created": True,
                "initial_deposit_usdt": initial_deposit_usdt,
                "share_percent": participations[code]["share_percent"] if code in participations else 0.0,
                "currency": "USDT"
            })

        return jsonify({
            "status": "error",
            "success": False,
            "message": "Erro ao salvar usuário"
        }), 500

    except Exception as e:
        logger.error(f"💥 Erro criar usuário: {str(e)}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/admin/user/update", methods=["PUT"])
def admin_update_user_complete():
    """Atualiza usuário completo - PROTEGIDO COM ANTIFRAUDE"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    cleanup_expired_sessions()
    
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").upper().strip()
        
        logger.info(f"✏️  Atualizar usuário: {code}")
        
        if code not in users:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Usuário não encontrado"
            }), 404
        
        new_email = data.get("email")
        new_phone = data.get("phone")
        
        if new_email or new_phone:
            fingerprint = None
            if data.get("is_suspicious_update", False):
                fingerprint = generate_fingerprint(request)
            
            is_duplicate, reason = is_duplicate_user(
                new_email or users[code].get("email", ""),
                new_phone or users[code].get("phone", ""),
                fingerprint or users[code].get("fingerprint", ""),
                exclude_user_code=code
            )
            
            if is_duplicate:
                return jsonify({
                    "status": "error",
                    "success": False,
                    "message": "Cadastro duplicado detectado na atualização",
                    "duplicate_reason": reason
                }), 400
        
        update_fields = [
            "name", "phone", "email", "plan", "expires_at", "coins_allowed",
            "status_pagamento", "data_pagamento", "observacoes_admin",
            "active", "max_coins", "payments", "valor_plano"
        ]
        
        updated_count = 0
        for field in update_fields:
            if field in data:
                if field == "coins_allowed":
                    users[code][field] = normalize_coins_allowed(data[field])
                else:
                    users[code][field] = data[field]
                updated_count += 1
        
        if "email" in data:
            users[code]["email_normalizado"] = normalize_email(data["email"])
        
        if "phone" in data:
            users[code]["telefone_normalizado"] = normalize_phone(data["phone"])
        
        users[code]["updated_at"] = datetime.now().isoformat()
        
        if save_users(users):
            if code in active_sessions and any(field in data for field in ["plan", "coins_allowed", "active"]):
                del active_sessions[code]
                logger.info(f"🔄 Sessão invalidada: {code}")
            
            log_admin_access(
                ip=request.remote_addr,
                action="ATUALIZAR_USUARIO",
                success=True,
                details=f"Usuário {code} atualizado ({updated_count} campos)"
            )
            
            return jsonify({
                "status": "ok",
                "success": True,
                "message": "Usuário atualizado com sucesso",
                "updated_fields": updated_count,
                "user": users[code]
            })
        else:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Erro ao salvar alterações"
            }), 500
        
    except Exception as e:
        logger.error(f"💥 Erro atualizar usuário: {str(e)}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/admin/user/confirm-payment", methods=["PUT"])
def admin_confirm_payment():
    """Confirma pagamento - PROTEGIDO"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").upper().strip()
        
        if code not in users:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Usuário não encontrado"
            }), 404
        
        users[code]["status_pagamento"] = "pago"
        users[code]["data_pagamento"] = data.get("data_pagamento", datetime.now().date().isoformat())
        users[code]["updated_at"] = datetime.now().isoformat()
        
        if "payments" not in users[code]:
            users[code]["payments"] = []
        
        amount = users[code].get("valor_plano", 0)
        if not amount or amount <= 0:
            plan_values = {
                "trial": 0.0,
                "basic": 49.90,
                "pro": 99.90,
                "vip": 199.90
            }
            amount = plan_values.get(users[code].get("plan", "trial"), 0.0)
        
        payment_record = {
            "amount": float(amount),
            "date": data.get("data_pagamento", datetime.now().date().isoformat()),
            "notes": "Pagamento confirmado via painel admin",
            "registered_at": datetime.now().isoformat(),
            "registered_by": "admin_confirm_payment"
        }
        
        users[code]["payments"].append(payment_record)
        
        if save_users(users):
            log_admin_access(
                ip=request.remote_addr,
                action="CONFIRMAR_PAGAMENTO",
                success=True,
                details=f"Pagamento confirmado para {code}"
            )
            
            return jsonify({
                "status": "ok",
                "success": True,
                "message": "Pagamento confirmado com sucesso",
                "user": {
                    "code": code,
                    "status_pagamento": users[code]["status_pagamento"],
                    "data_pagamento": users[code]["data_pagamento"]
                }
            })
        else:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Erro ao salvar"
            }), 500
        
    except Exception as e:
        logger.error(f"Erro confirmar pagamento: {e}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/admin/user/deactivate", methods=["PUT"])
def admin_deactivate_user():
    """Desativa usuário - PROTEGIDO"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    cleanup_expired_sessions()
    
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").upper().strip()
        
        if code not in users:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Usuário não encontrado"
            }), 404
        
        users[code]["active"] = False
        users[code]["updated_at"] = datetime.now().isoformat()
        
        if save_users(users):
            if code in active_sessions:
                register_session_event(
                    user_code=code,
                    event_type="DEACTIVATED",
                    session_data=active_sessions[code]
                )
                audit_event(
                    action="USER_DEACTIVATED",
                    success=True,
                    user_code=code,
                    details="Usuário desativado, sessão encerrada"
                )
                del active_sessions[code]
            
            log_admin_access(
                ip=request.remote_addr,
                action="DESATIVAR_USUARIO",
                success=True,
                details=f"Usuário {code} desativado"
            )
            
            return jsonify({
                "status": "ok",
                "success": True,
                "message": "Usuário desativado com sucesso"
            })
        else:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Erro ao salvar"
            }), 500
        
    except Exception as e:
        logger.error(f"💥 Erro desativar usuário: {str(e)}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

# ============================================
# 🔥 NOVO: ENDPOINTS PARA RELATÓRIOS DE FRAUDE
# ============================================

@app.route("/admin/fraud-logs", methods=["GET"])
def admin_get_fraud_logs():
    """Obtém logs de detecção de fraude - PROTEGIDO"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    try:
        logs = []
        if os.path.exists(FRAUD_LOG_FILE):
            with open(FRAUD_LOG_FILE, 'r', encoding='utf-8') as f:
                try:
                    logs = json.load(f)
                except:
                    logs = []
        
        logs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        logs = logs[:100]
        
        return jsonify({
            "status": "ok",
            "success": True,
            "logs": logs,
            "total": len(logs)
        })
    except Exception as e:
        logger.error(f"Erro ao obter logs de fraude: {e}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": "Erro ao obter logs de fraude"
        }), 500

@app.route("/admin/fraud-stats", methods=["GET"])
def admin_get_fraud_stats():
    """Obtém estatísticas de fraude - PROTEGIDO"""
    auth_ok, message = require_admin_auth()
    if not auth_ok:
        return jsonify({
            "status": "error",
            "success": False,
            "message": message
        }), 403
    
    try:
        logs = []
        if os.path.exists(FRAUD_LOG_FILE):
            with open(FRAUD_LOG_FILE, 'r', encoding='utf-8') as f:
                try:
                    logs = json.load(f)
                except:
                    logs = []
        
        stats = {
            "total_detections": len(logs),
            "by_type": {},
            "last_7_days": 0,
            "blocked_duplicates": 0
        }
        
        for log in logs:
            log_type = log.get("type", "UNKNOWN")
            stats["by_type"][log_type] = stats["by_type"].get(log_type, 0) + 1
            
            log_time = log.get("timestamp", "")
            if log_time:
                try:
                    log_date = datetime.fromisoformat(log_time.replace('Z', '+00:00'))
                    days_diff = (datetime.now() - log_date).days
                    if days_diff <= 7:
                        stats["last_7_days"] += 1
                except:
                    pass
            
            if "DUPLICADO" in log_type:
                stats["blocked_duplicates"] += 1
        
        return jsonify({
            "status": "ok",
            "success": True,
            "stats": stats
        })
    except Exception as e:
        logger.error(f"Erro ao obter estatísticas de fraude: {e}")
        return jsonify({
            "status": "error",
            "success": False,
            "message": "Erro ao obter estatísticas de fraude"
        }), 500

# ============================================
# MIDDLEWARE E MONITORAMENTO
# ============================================

@app.before_request
def log_request_info():
    """Log de todas as requisições (exceto ping e health)"""
    if request.path not in ['/health', '/ping']:
        logger.info(f"🌐 {request.method} {request.path} - IP: {request.remote_addr}")

def cleanup_periodic_tasks():
    """Thread de limpeza periódica"""
    while True:
        try:
            time.sleep(30)
            expired_count = cleanup_expired_sessions()
            
            invalid_count = 0
            now = time.time()
            codes_to_remove = []
            
            for code, sess in active_sessions.items():
                if code in users:
                    user = users[code]
                    if not is_user_valid(user):
                        codes_to_remove.append(code)
            
            for code in codes_to_remove:
                register_session_event(
                    user_code=code,
                    event_type="INVALID_USER",
                    session_data=active_sessions[code]
                )
                audit_event(
                    action="SESSION_INVALID_USER",
                    success=False,
                    user_code=code,
                    details="Sessão encerrada devido a usuário inválido"
                )
                del active_sessions[code]
                invalid_count += 1
                logger.info(f"🧹 Sessão invalidada (usuário inválido): {code}")
            
            if expired_count > 0 or invalid_count > 0:
                logger.info(f"🧹 Limpeza automática: {expired_count} sessões expiradas, {invalid_count} inválidas")
            
            if int(time.time()) % 300 == 0:
                save_sessions()
                save_user_status()
                
        except Exception as e:
            logger.error(f"Erro thread limpeza: {str(e)}")
            time.sleep(60)

def start_background_tasks():
    """Inicia tarefas em background"""
    try:
        cleanup_task = threading.Thread(target=cleanup_periodic_tasks, daemon=True)
        cleanup_task.start()
        
        flow_thread = threading.Thread(target=update_flow_averages_thread, daemon=True)
        flow_thread.start()
        
        logger.info("✅ Tarefas de background iniciadas")
    except Exception as e:
        logger.error(f"❌ Erro ao iniciar tarefas: {e}")

def save_all_data():
    """Salva todos os dados periodicamente"""
    try:
        save_users(users)
        save_sessions()
        save_user_status()
        logger.info("💾 Dados salvos automaticamente")
    except Exception as e:
        logger.error(f"Erro ao salvar dados: {e}")

@app.route("/user/plan-info", methods=["GET"])
def user_plan_info():
    try:
        code = request.cookies.get("session_code", "").strip().upper()

        if not code:
            return jsonify({"error": "Sessão inválida"}), 401

        if code not in users:
            return jsonify({"error": "Usuário não encontrado"}), 401

        user = users[code]
        plan = user.get("plan", "trial")
        
        if plan == "vip":
            trade_mode = "futures"
        else:
            trade_mode = "spot"

        return jsonify({
            "plan": plan,
            "trade_mode": trade_mode,
            "expires_at": user.get("expires_at", "")
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================
# 🔥 ENDPOINTS DO SISTEMA DE CARTEIRAS
# ============================================

@app.route('/robo/create-wallet', methods=['POST'])
def create_wallet():
    """Cria uma nova carteira a partir de uma frase (com criptografia)"""
    try:
        data = request.get_json(silent=True) or {}
        phrase      = data.get('phrase', '').strip()
        btc_address = data.get('btc_address', '').strip() if data.get('btc_address') else ''
        pix_key     = data.get('pix_key', '').strip() if data.get('pix_key') else ''

        if not phrase:
            return jsonify({"success": False, "message": "Frase é obrigatória"}), 400

        if not btc_address and not pix_key:
            return jsonify({"success": False, "message": "Informe pelo menos um: endereço BTC ou chave PIX para receber saques."}), 400
        
        wallets = load_wallets()
        
        wallet_code = generate_wallet_code(phrase)
        
        if wallet_code in wallets:
            changed = False
            if pix_key and not wallets[wallet_code].get('registered_pix_key'):
                wallets[wallet_code]['registered_pix_key'] = pix_key; changed = True
            if btc_address and not wallets[wallet_code].get('registered_btc_address'):
                wallets[wallet_code]['registered_btc_address'] = btc_address; changed = True
            if changed:
                wallets[wallet_code]['updated_at'] = datetime.now().isoformat()
                save_wallets(wallets)
            return jsonify({
                "success": True,
                "wallet_code": wallet_code,
                "message": "Carteira já existe",
                "wallet": {
                    "code": wallet_code,
                    "name": wallets[wallet_code].get('name', ''),
                    "balanceUSDT": wallets[wallet_code].get('balanceUSDT', 0),
                    "balanceBTC": wallets[wallet_code].get('balanceBTC', 0),
                    "totalDepositedUSDT": wallets[wallet_code].get('totalDepositedUSDT', 0),
                    "totalProfitUSDT": wallets[wallet_code].get('totalProfitUSDT', 0),
                    "created_at": wallets[wallet_code].get('created_at', '')
                }
            })
        
        encrypted_phrase = encrypt_data(phrase)
        
        wallet_data = {
            "id": wallet_code,
            "code": wallet_code,
            "name": f"Carteira {wallet_code}",
            "balanceUSDT": 0.0,
            "balanceBTC": 0.0,
            "totalDepositedUSDT": 0.0,
            "totalDepositedBTC": 0.0,
            "totalProfitUSDT": 0.0,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "encrypted_phrase": encrypted_phrase,
            "phrase_hash": hashlib.sha256(phrase.encode('utf-8')).hexdigest(),
            "active": True,
            "is_base_wallet": True,
            "last_access": datetime.now().isoformat(),
            "version": "1.0",
            "registered_pix_key": pix_key or None,
            "registered_btc_address": btc_address or None,
        }
        
        wallets[wallet_code] = wallet_data
        save_wallets(wallets)
        
        audit_event(
            action="WALLET_CREATED",
            success=True,
            user_code=wallet_code,
            details="Nova carteira criada",
            extra={"phrase_hash": wallet_data["phrase_hash"][:16]}
        )
        
        logger.info(f"✅ Nova carteira criada: {wallet_code}")
        
        return jsonify({
            "success": True,
            "wallet_code": wallet_code,
            "message": "Carteira criada com sucesso",
            "wallet": {
                "code": wallet_code,
                "name": wallet_data['name'],
                "balanceUSDT": wallet_data['balanceUSDT'],
                "balanceBTC": wallet_data['balanceBTC'],
                "totalDepositedUSDT": wallet_data['totalDepositedUSDT'],
                "totalProfitUSDT": wallet_data['totalProfitUSDT'],
                "created_at": wallet_data['created_at']
            }
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em create-wallet: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/deposit', methods=['POST'])
def deposit_corrigido():
    """
    Processa depósito BTC - Conversão automática para USDT
    - Binance → fee 0%
    - Externa → fee 0%
    - Crédito final em USDT
    """
    try:
        data = request.get_json(silent=True) or {}
        
        wallet_code = data.get('wallet_code', '').strip().upper()
        btc_value = float(data.get('value', 0))
        source = data.get('source', 'external').lower()
        method = 'btc'
        
        if not wallet_code:
            return jsonify({"success": False, "message": "Código da carteira é obrigatório"}), 400
        
        if btc_value <= 0:
            return jsonify({"success": False, "message": "Valor deve ser maior que zero"}), 400
        
        if source not in ['binance', 'external']:
            return jsonify({"success": False, "message": "Origem inválida"}), 400
        
        btc_min = 0.0001
        btc_max = 1.0
        
        if btc_value < btc_min:
            return jsonify({"success": False, "message": f"Mínimo: {btc_min:.4f} BTC"}), 400
        
        if btc_value > btc_max:
            return jsonify({"success": False, "message": f"Máximo: {btc_max:.4f} BTC"}), 400
        
        # ✅ SEM TAXA DE DEPÓSITO
        fee_btc = 0.0
        fee_description = "Fee 0% (depósito)"
        amount_after_fee_btc = btc_value
        
        btc_usdt_rate = get_current_btc_usdt()
        amount_usdt = amount_after_fee_btc * btc_usdt_rate
        
        transactions = load_transactions()
        transaction_id = f"DEP-BTC-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        transaction = {
            "id": transaction_id,
            "wallet_code": wallet_code,
            "type": "deposit",
            "method": method.upper(),
            "source": source,
            "amount_usdt": amount_usdt,
            "value_btc": amount_after_fee_btc,
            "btc_price_at_deposit": btc_usdt_rate,
            "date": datetime.now().isoformat(),
            "status": "pendente",
            "description": f"Depósito BTC ({source}) - AGUARDANDO CONFIRMAÇÃO",
            "confirmed_at": None,
            "tx_hash": None,
            "expected_amount_usdt": amount_usdt,
            "expected_crypto": amount_after_fee_btc,
            "requires_confirmation": True,
            "btc_requested": btc_value,
            "fee_btc": 0.0,
            "fee_percent": 0,
            "fee_description": fee_description,
            "btc_minimum": btc_min,
            "warning": "Depósito será creditado em USDT após confirmação",
            "currency": "USDT"
        }
        
        transactions.append(transaction)
        save_transactions(transactions)
        
        audit_event(
            action="DEPOSIT_BTC_REQUESTED",
            success=True,
            user_code=wallet_code,
            details=f"Depósito BTC solicitado: {btc_value:.8f} BTC | Origem: {source}",
            extra={
                "transaction_id": transaction_id,
                "btc_requested": btc_value,
                "btc_amount_after_fee": amount_after_fee_btc,
                "usdt_value_after_fee": amount_usdt,
                "btc_usdt_rate": btc_usdt_rate,
                "source": source,
                "status": "pendente"
            }
        )
        
        logger.info(
            f"📋 Depósito BTC pendente criado: {wallet_code} | "
            f"Origem: {source} | Valor: {btc_value:.8f} BTC"
        )
        
        return jsonify({
            "success": True,
            "message": "Depósito BTC registrado. Aguardando confirmações.",
            "transaction_id": transaction_id,
            "btc_requested": btc_value,
            "btc_amount_after_fee": amount_after_fee_btc,
            "usdt_value_after_fee": amount_usdt,
            "btc_usdt_rate": btc_usdt_rate,
            "source": source,
            "status": "pendente",
            "requires_confirmation": True,
            "currency": "USDT",
            "rules": [
                "Sistema converte BTC para USDT automaticamente",
                "Depósito mínimo: 0.0001 BTC",
                "3 confirmações necessárias",
                "Fee de depósito: 0%",
                "Crédito final em USDT"
            ]
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em deposit BTC: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500


def process_user_withdrawal_usdt(user_code: str, amount_usdt: float, btc_address: str = None) -> Dict:
    """
    Processa saque do usuário - VALIDAÇÃO E DÉBITO APENAS EM PARTICIPATIONS
    """
    try:
        participations = load_participations()
        
        if user_code not in participations:
            return {"success": False, "message": "Participação não encontrada"}
        
        participation = participations[user_code]
        
        # ✅ FONTE ÚNICA: validar saldo em participations
        virtual_balance_usdt = participation.get("virtual_balance_usdt", 0)
        
        if amount_usdt > virtual_balance_usdt:
            return {
                "success": False, 
                "message": f"Saldo insuficiente. Disponível: {virtual_balance_usdt:.2f} USDT"
            }
        
        # ✅ DÉBITO APENAS EM PARTICIPATIONS
        participation["virtual_balance_usdt"] -= amount_usdt
        participation["total_withdrawn_usdt"] = participation.get("total_withdrawn_usdt", 0) + amount_usdt
        participation["updated_at"] = datetime.now().isoformat()
        
        # Registrar transação
        transactions = load_transactions()
        transaction_id = f"WTH-USER-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        btc_usdt = get_current_btc_usdt()
        value_btc = amount_usdt / btc_usdt if btc_usdt > 0 else 0
        
        withdrawal_transaction = {
            "id": transaction_id,
            "user_code": user_code,
            "wallet_code": user_code,
            "type": "withdrawal",
            "amount_usdt": amount_usdt,
            "value_btc": value_btc,
            "date": datetime.now().isoformat(),
            "status": "pendente",
            "description": f"Saque solicitado pelo usuário",
            "btc_address": btc_address,
            "virtual_balance_before_usdt": virtual_balance_usdt,
            "virtual_balance_after_usdt": participation["virtual_balance_usdt"],
            "currency": "USDT"
        }
        
        transactions.append(withdrawal_transaction)
        
        # ✅ SALVAR APENAS OS ARQUIVOS NECESSÁRIOS
        save_participations(participations)
        save_transactions(transactions)
        
        audit_event(
            action="USER_WITHDRAWAL",
            success=True,
            user_code=user_code,
            details=f"Saque solicitado: {amount_usdt:.2f} USDT",
            extra={
                "transaction_id": transaction_id,
                "virtual_balance_before_usdt": virtual_balance_usdt,
                "virtual_balance_after_usdt": participation["virtual_balance_usdt"],
                "currency": "USDT"
            }
        )
        
        return {
            "success": True,
            "message": "Saque solicitado com sucesso",
            "transaction_id": transaction_id,
            "virtual_balance_usdt": participation["virtual_balance_usdt"]
        }
        
    except Exception as e:
        logger.error(f"❌ Erro ao processar saque do usuário: {e}")
        return {"success": False, "message": f"Erro interno: {str(e)}"}

from datetime import datetime
from flask import request, jsonify

@app.route("/robo/simple-withdraw", methods=["POST"])
def simple_withdraw():
    """
    SAQUE SIMPLIFICADO:
    - Baseado no saldo virtual (USDT)
    - Permite até 100% do saldo virtual
    - Você aprova manualmente pelo fluxo/admin (status 'pendente')
    """
    try:
        data = request.get_json(silent=True) or {}

        wallet_code = (data.get("wallet_code") or "").strip().upper()
        amount_usdt = float(data.get("amount_usdt", data.get("value", 0)) or 0)

        if not wallet_code:
            return jsonify({"success": False, "message": "Código da carteira é obrigatório"}), 400
        if amount_usdt <= 0:
            return jsonify({"success": False, "message": "Valor deve ser maior que zero"}), 400

        participations = load_participations()

        participation = participations.get(wallet_code)
        if not participation or not participation.get("active", False):
            return jsonify({"success": False, "message": "Participação não encontrada ou inativa"}), 404

        virtual_balance_usdt = float(participation.get("virtual_balance_usdt", 0) or 0)

        withdraw_percent = 100
        available_for_withdraw_usdt = virtual_balance_usdt  # 100%

        if amount_usdt > available_for_withdraw_usdt:
            return jsonify({
                "success": False,
                "message": f"Valor excede limite de saque. Disponível: {available_for_withdraw_usdt:.2f} USDT",
                "virtual_balance_usdt": virtual_balance_usdt,
                "available_for_withdraw_usdt": available_for_withdraw_usdt,
                "max_percent": withdraw_percent
            }), 400

        # Atualiza participação
        participation["virtual_balance_usdt"] = virtual_balance_usdt - amount_usdt
        participation["total_withdrawn_usdt"] = float(participation.get("total_withdrawn_usdt", 0) or 0) + amount_usdt
        participation["updated_at"] = datetime.now().isoformat()

        # Registra transação (pendente)
        transactions = load_transactions()
        transaction_id = f"WTH-SIMPLE-{datetime.now().strftime('%Y%m%d%H%M%S')}"

        btc_usdt = get_current_btc_usdt()
        value_btc = (amount_usdt / btc_usdt) if btc_usdt > 0 else 0

        withdrawal_tx = {
            "id": transaction_id,
            "user_code": wallet_code,
            "wallet_code": wallet_code,
            "type": "withdrawal",
            "amount_usdt": amount_usdt,
            "value_btc": value_btc,
            "virtual_balance_before_usdt": virtual_balance_usdt,
            "virtual_balance_after_usdt": participation["virtual_balance_usdt"],
            "available_for_withdraw_before_usdt": available_for_withdraw_usdt,
            "date": datetime.now().isoformat(),
            "status": "pendente",
            "description": "Saque baseado em 100% do saldo virtual (aguardando aprovação)",
            "validation_rule": "amount <= virtual_balance_usdt",
            "withdraw_percent": withdraw_percent,
            "currency": "USDT"
        }
        transactions.append(withdrawal_tx)

        save_participations(participations)
        save_transactions(transactions)

        audit_event(
            action="SIMPLE_WITHDRAWAL",
            success=True,
            user_code=wallet_code,
            details=f"Saque simples: {amount_usdt:.2f} USDT | Disponível: {available_for_withdraw_usdt:.2f} USDT",
            extra={
                "transaction_id": transaction_id,
                "virtual_balance_before_usdt": virtual_balance_usdt,
                "virtual_balance_after_usdt": participation["virtual_balance_usdt"],
                "available_for_withdraw_usdt": available_for_withdraw_usdt,
                "withdraw_percent": withdraw_percent,
                "currency": "USDT"
            }
        )

        logger.info(
            f"💸 Saque SIMPLES: {wallet_code} | Virtual: {virtual_balance_usdt:.2f} USDT | "
            f"Disponível: {available_for_withdraw_usdt:.2f} USDT | Solicitado: {amount_usdt:.2f} USDT"
        )

        return jsonify({
            "success": True,
            "message": "Saque solicitado com sucesso (pendente de aprovação)",
            "transaction_id": transaction_id,
            "virtual_balance_usdt": participation["virtual_balance_usdt"],
            "available_for_withdraw_usdt": available_for_withdraw_usdt,
            "withdraw_percent": withdraw_percent,
            "currency": "USDT",
            "rules": [
                "Saque baseado APENAS no saldo virtual em USDT",
                "Máximo 100% do saldo virtual",
                "Fica pendente até aprovação"
            ]
        }), 200

    except Exception as e:
        logger.error(f"💥 Erro em simple-withdraw: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500



def calculate_floating_pnl(user_code: str) -> float:
    """Sistema NÃO suporta PnL flutuante ainda - Retorna ZERO sempre"""
    try:
        logger.info(f"⚠️ PnL flutuante solicitado para {user_code}: SISTEMA NÃO IMPLEMENTADO")
        
        audit_event(
            action="FLOATING_PNL_REQUESTED",
            success=False,
            user_code=user_code,
            details="Sistema não suporta PnL flutuante (posições abertas)",
            extra={
                "return_value": 0.0,
                "system_mode": "realized_pnl_only"
            }
        )
        
        return 0.0
        
    except Exception as e:
        logger.error(f"❌ Erro em calculate_floating_pnl: {e}")
        return 0.0

def get_user_equity(user_code: str) -> Dict:
    """Retorna equity do usuário baseado em USDT (100% sacável)."""
    try:
        participations = load_participations()

        if user_code not in participations:
            return {"error": "Participação não encontrada"}

        participation = participations[user_code]
        virtual_balance_usdt = float(participation.get("virtual_balance_usdt", 0) or 0)

        # 100% disponível
        available_for_withdraw_usdt = virtual_balance_usdt

        usdt_brl = get_current_usdt_brl()

        return {
            "virtual_balance_usdt": round(virtual_balance_usdt, 2),
            "virtual_balance_brl": round(virtual_balance_usdt * usdt_brl, 2),
            "withdrawable_usdt": round(available_for_withdraw_usdt, 2),
            "withdrawable_brl": round(available_for_withdraw_usdt * usdt_brl, 2),
            "withdraw_percent": 100,
            "currency": "USDT",
        }

    except Exception as e:
        logger.error(f"❌ Erro em get_user_equity({user_code}): {e}")
        return {"error": "Erro interno"}


def get_user_dashboard_data(user_code: str) -> Dict:
    """Retorna dados completos para o dashboard do usuário - USDT CORE"""
    try:
        participations = load_participations()
        
        if user_code not in participations:
            return {"error": "Usuário não possui participação"}
        
        participation = participations[user_code]
        
        equity_summary = get_user_equity_summary(user_code)
        
        today = datetime.now().date()
        
        wallets = load_wallets()
        wallet_data = {}
        if user_code in wallets:
            wallet = wallets[user_code]
            usdt_brl = get_current_usdt_brl()
            wallet_data = {
                "balanceUSDT": wallet.get("balanceUSDT", 0),
                "balanceBRL": round(wallet.get("balanceUSDT", 0) * usdt_brl, 2),
                "balanceBTC": wallet.get("balanceBTC", 0),
                "totalDepositedUSDT": wallet.get("totalDepositedUSDT", 0),
                "totalProfitUSDT": wallet.get("totalProfitUSDT", 0)
            }
        
        return {
            "success": True,
            "dashboard_data": {
                "user_code": user_code,
                "virtual_balance_usdt": participation.get("virtual_balance_usdt", 0),
                "total_deposited_usdt": participation.get("total_deposited_usdt", 0),
                "profit_accumulated_usdt": participation.get("profit_accumulated_usdt", 0),
                "share_percent": participation.get("share_percent", 0),
                "total_withdrawn_usdt": participation.get("total_withdrawn_usdt", 0),
                "joined_at": participation.get("joined_at"),
                "status": participation.get("status", "active"),
                "last_profit_distribution": participation.get("last_profit_distribution"),
                "equity_summary": equity_summary,
                "operations_info": {
                    "deposit_enabled": True,
                    "withdraw_enabled": True,
                    "reinvest_enabled": True,
                    "system_status": "operational",
                    "system_mode": "unrestricted",
                    "last_updated": datetime.now().isoformat()
                },
                "wallet_data": wallet_data if wallet_data else None,
                "master_wallet": participation.get("master_wallet"),
                "currency": "USDT"
            }
        }
        
    except Exception as e:
        logger.error(f"❌ Erro ao obter dados do dashboard: {e}")
        return {
            "success": False, 
            "error": str(e),
            "dashboard_data": {
                "operations_info": {
                    "deposit_enabled": True,
                    "withdraw_enabled": True,
                    "reinvest_enabled": True,
                    "system_status": "operational"
                }
            }
        }



def process_profit_distribution(profit_amount_usdt: float, description: str = "") -> Dict:
    """Distribui lucro livremente - SEM DIA 7, em USDT"""
    try:
        if profit_amount_usdt <= 0:
            return {"success": False, "message": "Lucro deve ser maior que zero"}
        
        logger.info(f"💰 Distribuindo lucro: {profit_amount_usdt:.2f} USDT - {description}")
        
        return distribuir_lucro_proporcional(f"MANUAL-{datetime.now().strftime('%Y%m%d%H%M%S')}", profit_amount_usdt, description)
        
    except Exception as e:
        logger.error(f"❌ Erro na distribuição de lucro: {str(e)}")
        return {"success": False, "message": f"Erro na distribuição: {str(e)}"}

def limpar_depositos_antigos():
    """LIMPA DEPÓSITOS ANTIGOS IRREAIS - EXECUTAR UMA VEZ"""
    try:
        transactions = load_transactions()
        limpos = 0
        
        for i, tx in enumerate(transactions):
            if tx.get("type") == "deposit" and tx.get("status") == "pendente":
                method = tx.get("method", "").upper()
                expected_crypto = float(tx.get("expected_crypto", 0))
                
                if method == "USDT":
                    transactions[i]["status"] = "cancelado"
                    transactions[i]["cancel_reason"] = "Sistema agora é BTC-only, converte para USDT"
                    transactions[i]["canceled_at"] = datetime.now().isoformat()
                    limpos += 1
                    logger.info(f"🗑️ USDT cancelado: {tx.get('id')}")
                
                elif method == "BTC":
                    if expected_crypto > 1.0:
                        transactions[i]["status"] = "cancelado"
                        transactions[i]["cancel_reason"] = "Valor BTC irreal (>1 BTC)"
                        transactions[i]["canceled_at"] = datetime.now().isoformat()
                        limpos += 1
                        logger.info(f"🗑️ BTC irreal cancelado: {tx.get('id')} ({expected_crypto:.8f} BTC)")
                    
                    elif expected_crypto < 0.00001:
                        transactions[i]["status"] = "cancelado"
                        transactions[i]["cancel_reason"] = "Valor BTC muito baixo"
                        transactions[i]["canceled_at"] = datetime.now().isoformat()
                        limpos += 1
                        logger.info(f"🗑️ BTC muito baixo: {tx.get('id')} ({expected_crypto:.8f} BTC)")
        
        if limpos > 0:
            save_transactions(transactions)
            logger.info(f"✅✅✅ LIMPEZA CONCLUÍDA: {limpos} depósitos antigos removidos")
        else:
            logger.info("✅ Nenhum depósito antigo para limpar")
        
        return limpos
        
    except Exception as e:
        logger.error(f"❌ Erro na limpeza: {e}")
        return 0

def add_profit_to_master(amount_usdt: float, description: str) -> bool:
    """Adiciona lucro à master wallet em USDT"""
    try:
        wallets = load_wallets()
        
        for master_code in MASTER_WALLET_CODES:
            if master_code in wallets:
                wallet = wallets[master_code]
                wallet["balanceUSDT"] += amount_usdt / len(MASTER_WALLET_CODES)
                wallet["totalProfitUSDT"] += amount_usdt / len(MASTER_WALLET_CODES)
                wallet["updated_at"] = datetime.now().isoformat()
        
        save_wallets(wallets)
        
        master_data = load_master_wallet_data()
        master_data["total_profit_received_usdt"] += amount_usdt
        master_data["balance_available_usdt"] += amount_usdt
        save_master_wallet_data(master_data)
        
        logger.info(f"💰 Lucro adicionado à master: {amount_usdt:.2f} USDT - {description}")
        return True
    except Exception as e:
        logger.error(f"❌ Erro ao adicionar lucro à master: {e}")
        return False

@app.route('/robo/withdraw', methods=['POST'])
def process_withdrawal_with_fees():
    try:
        data = request.get_json(silent=True) or {}

        wallet_code = data.get('wallet_code', '').strip().upper()
        amount_usdt = float(data.get('amount_usdt', data.get('value', 0)))
        method = data.get('method', 'external').lower()
        withdraw_type = data.get('type', 'external')
        btc_address = data.get('btc_address', '').strip() or data.get('address', '').strip()

        if not wallet_code:
            return jsonify({"success": False, "message": "Código da carteira é obrigatório"}), 400

        if amount_usdt <= 0:
            return jsonify({"success": False, "message": "Valor deve ser maior que zero"}), 400

        # PIX key opcional — saque pode ser via PIX ou BTC
        pix_key = data.get('pix_key', '').strip()

        if not btc_address and not pix_key:
            return jsonify({"success": False, "message": "Informe sua chave PIX ou endereço BTC para receber o saque"}), 400

        # ══ SEGURANÇA: destino deve bater com o cadastrado no registro ══
        wallets_data = load_wallets()
        wallet_entry = wallets_data.get(wallet_code, {})
        registered_pix = (wallet_entry.get('registered_pix_key') or '').strip()
        registered_btc = (wallet_entry.get('registered_btc_address') or '').strip()

        if pix_key and registered_pix and pix_key.lower() != registered_pix.lower():
            logger.warning(f"Saque PIX BLOQUEADO - chave nao bate: {wallet_code} | informado={pix_key} | cadastrado={registered_pix}")
            return jsonify({"success": False, "message": "Chave PIX nao confere com a cadastrada. Contate o suporte."}), 403

        if btc_address and not pix_key and registered_btc and btc_address != registered_btc:
            logger.warning(f"Saque BTC BLOQUEADO - endereco nao bate: {wallet_code}")
            return jsonify({"success": False, "message": "Endereco BTC nao confere com o cadastrado. Contate o suporte."}), 403

        metodo_saque = "PIX" if pix_key else "BTC"
        pix_nao_cadastrado = bool(pix_key and not registered_pix)

        participations = load_participations()

        if wallet_code not in participations:
            return jsonify({"success": False, "message": "Participação não encontrada"}), 404

        participation = participations[wallet_code]
        virtual_balance = participation.get("virtual_balance_usdt", 0)

        if amount_usdt <= 0:
            return jsonify({"success": False, "message": "Valor inválido"}), 400

        if amount_usdt > virtual_balance:
            return jsonify({"success": False, "message": f"Saldo insuficiente. Disponível: {virtual_balance:.2f} USDT"}), 400

        usdt_brl_rate = get_current_usdt_brl()
        amount_brl = round(amount_usdt * usdt_brl_rate, 2) if amount_usdt > 0 else float(data.get('amount_brl', 0))

        transactions = load_transactions()
        transaction_id = f"WTH-{datetime.now().strftime('%Y%m%d%H%M%S')}-{wallet_code[:6]}"

        withdrawal_tx = {
            "id":                    transaction_id,
            "wallet_code":           wallet_code,
            "type":                  "withdrawal",
            "method":                metodo_saque,
            "amount_requested_usdt": amount_usdt,
            "amount_usdt":           amount_usdt,
            "amount_brl":            amount_brl,
            "fee_usdt":              0,
            "status":                "pendente",
            "btc_address":           btc_address or None,
            "pix_key":               pix_key or None,
            "registered_pix_key":    registered_pix or None,
            "pix_nao_cadastrado":    pix_nao_cadastrado,
            "created_at":            datetime.now().isoformat(),
            "date":                  datetime.now().isoformat(),
            "debit_pending":         True,
            "description":           f"Saque {metodo_saque} R$ {amount_brl:.2f} => {pix_key or btc_address}",
        }

        transactions.append(withdrawal_tx)
        save_transactions(transactions)

        logger.info(f"💸 Saque solicitado: {wallet_code} | {metodo_saque} | R$ {amount_brl:.2f} | {pix_key or btc_address} | {transaction_id}")

        return jsonify({
            "success":        True,
            "transaction_id": transaction_id,
            "status":         "pending_approval",
            "method":         metodo_saque,
            "message":        f"Saque de R$ {amount_brl:.2f} via {metodo_saque} registrado. Aguarde aprovação do admin.",
        })

    except Exception as e:
        logger.error(f"Erro /robo/withdraw: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════
# 💚 PIX — Depósito manual (cliente solicita → admin confirma e credita)
# ═══════════════════════════════════════════════════════════════════════

PIX_KEY         = "11913370057"
PIX_WHATSAPP    = "11913370057"
PIX_BENEFICIARY = "Coyote Capital"

@app.route('/robo/deposit/pix', methods=['POST'])
def deposit_pix_request():
    """
    Cliente solicita depósito via PIX.
    Grava transação com status 'pendente_pix' — admin confirma depois.
    Body: { wallet_code, amount_brl }
    """
    try:
        data        = request.get_json(silent=True) or {}
        wallet_code = data.get('wallet_code', '').strip().upper()
        amount_brl  = float(data.get('amount_brl', 0))

        if not wallet_code:
            return jsonify({"success": False, "message": "wallet_code obrigatório"}), 400
        if amount_brl < 10:
            return jsonify({"success": False, "message": "Valor mínimo R$ 10,00"}), 400

        participations = load_participations()
        if wallet_code not in participations:
            return jsonify({"success": False, "message": "Carteira não encontrada"}), 404

        usdt_brl      = get_current_usdt_brl()
        expected_usdt = round(amount_brl / usdt_brl, 6) if usdt_brl > 0 else 0

        transactions = load_transactions()
        tx_id = "DEP-PIX-{}-{}".format(datetime.now().strftime('%Y%m%d%H%M%S'), wallet_code[-4:])

        tx = {
            "id":               tx_id,
            "wallet_code":      wallet_code,
            "type":             "deposit",
            "method":           "PIX",
            "source":           "pix",
            "amount_brl":       round(amount_brl, 2),
            "amount_usdt":      expected_usdt,
            "expected_usdt":    expected_usdt,
            "usdt_brl_rate":    round(usdt_brl, 6),
            "date":             datetime.now().isoformat(),
            "status":           "pendente_pix",
            "pix_key":          PIX_KEY,
            "description":      "Deposito PIX R$ {:.2f} - AGUARDANDO CONFIRMACAO ADMIN".format(amount_brl),
            "confirmed_at":     None,
            "credited":         False,
            "requires_manual_confirmation": True,
            "admin_notes":      "",
        }

        transactions.append(tx)
        save_transactions(transactions)
        logger.info("💚 PIX pendente: {} | R$ {:.2f} | {}".format(wallet_code, amount_brl, tx_id))

        return jsonify({
            "success":        True,
            "transaction_id": tx_id,
            "pix_key":        PIX_KEY,
            "beneficiary":    PIX_BENEFICIARY,
            "amount_brl":     round(amount_brl, 2),
            "instrucoes": [
                "Chave PIX: {}".format(PIX_KEY),
                "Beneficiario: {}".format(PIX_BENEFICIARY),
                "Valor exato: R$ {:.2f}".format(amount_brl),
                "Descricao/identificacao: {}".format(wallet_code),
                "OU envie o comprovante pelo WhatsApp: {}".format(PIX_WHATSAPP),
                "Credito liberado pelo admin em ate 24h",
            ],
        })

    except Exception as e:
        logger.error("Erro /robo/deposit/pix: {}".format(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/admin/pix-pending', methods=['GET'])
def admin_pix_pending():
    """
    Lista depósitos e saques PIX para o admin.
    GET /robo/admin/pix-pending?status=pendente_pix  (padrão) ou 'all'
    Requer admin key.
    """
    try:
        if not verify_admin_key(request):
            return jsonify({"success": False, "message": "Nao autorizado"}), 403

        status_filter = request.args.get('status', 'pendente_pix').lower()
        transactions  = load_transactions()

        pix_txs = [
            tx for tx in transactions
            if tx.get('method') == 'PIX'
            and (status_filter == 'all' or tx.get('status', '') == status_filter)
        ]
        pix_txs.sort(key=lambda t: t.get('date', t.get('created_at', '')), reverse=True)

        return jsonify({
            "success":  True,
            "total":    len(pix_txs),
            "deposits": pix_txs,
        })

    except Exception as e:
        logger.error("Erro /robo/admin/pix-pending: {}".format(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/admin/pix-confirm', methods=['POST'])
def admin_pix_confirm():
    """
    Admin confirma depósito PIX e credita saldo virtual do cliente.
    Body: { transaction_id, amount_brl (opcional), admin_notes (opcional) }
    Requer admin key.
    """
    try:
        if not verify_admin_key(request):
            return jsonify({"success": False, "message": "Nao autorizado"}), 403

        data         = request.get_json(silent=True) or {}
        tx_id        = (data.get('transaction_id') or '').strip()
        override_brl = data.get('amount_brl')

        if not tx_id:
            return jsonify({"success": False, "message": "transaction_id obrigatorio"}), 400

        transactions   = load_transactions()
        participations = load_participations()

        tx       = None
        tx_index = -1
        for i, t in enumerate(transactions):
            if t.get('id') == tx_id:
                tx       = t
                tx_index = i
                break

        if tx is None:
            return jsonify({"success": False, "message": "Transacao nao encontrada"}), 404
        if tx.get('method') != 'PIX':
            return jsonify({"success": False, "message": "Transacao nao e PIX"}), 400
        if tx.get('status') == 'confirmado':
            return jsonify({"success": False, "message": "Transacao ja confirmada"}), 400
        if tx.get('type') != 'deposit':
            return jsonify({"success": False, "message": "Use este endpoint apenas para depositos PIX"}), 400

        wallet_code = str(tx.get('wallet_code', '')).strip().upper()
        if wallet_code not in participations:
            return jsonify({"success": False, "message": "Carteira {} nao encontrada".format(wallet_code)}), 404

        usdt_brl   = get_current_usdt_brl()
        amount_brl = float(override_brl if override_brl is not None else tx.get('amount_brl', 0))
        if amount_brl <= 0:
            return jsonify({"success": False, "message": "Valor invalido"}), 400

        credit_usdt = round(amount_brl / usdt_brl, 6) if usdt_brl > 0 else 0
        if credit_usdt <= 0:
            return jsonify({"success": False, "message": "Conversao BRL->USDT resultou em zero"}), 400

        p = participations[wallet_code]
        p["virtual_balance_usdt"]  = round(float(p.get("virtual_balance_usdt",  0)) + credit_usdt, 6)
        p["total_deposited_usdt"]  = round(float(p.get("total_deposited_usdt",  0)) + credit_usdt, 6)
        p["saldo_disponivel_usdt"] = round(float(p.get("saldo_disponivel_usdt", 0)) + credit_usdt, 6)
        p["aporte_usdt"]           = round(float(p.get("aporte_usdt",           0)) + credit_usdt, 6)
        p["updated_at"]            = datetime.utcnow().isoformat()

        transactions[tx_index]["status"]             = "confirmado"
        transactions[tx_index]["credited"]           = True
        transactions[tx_index]["confirmed_at"]       = datetime.utcnow().isoformat()
        transactions[tx_index]["credited_at"]        = datetime.utcnow().isoformat()
        transactions[tx_index]["credit_amount_usdt"] = credit_usdt
        transactions[tx_index]["amount_brl"]         = amount_brl
        transactions[tx_index]["usdt_brl_rate"]      = round(usdt_brl, 6)
        transactions[tx_index]["admin_notes"]        = data.get('admin_notes', 'Confirmado pelo admin')
        transactions[tx_index]["description"]        = "Deposito PIX R$ {:.2f} -> {:.6f} USDT - CONFIRMADO".format(amount_brl, credit_usdt)

        save_transactions(transactions)
        save_participations(participations)

        logger.info("✅ PIX confirmado: {} | R$ {:.2f} -> +{:.6f} USDT | {}".format(wallet_code, amount_brl, credit_usdt, tx_id))

        return jsonify({
            "success":          True,
            "message":          "PIX confirmado! R$ {:.2f} -> +{:.6f} USDT creditados.".format(amount_brl, credit_usdt),
            "transaction_id":   tx_id,
            "wallet_code":      wallet_code,
            "amount_brl":       amount_brl,
            "credit_usdt":      credit_usdt,
            "new_balance_usdt": round(float(p["virtual_balance_usdt"]), 6),
        })

    except Exception as e:
        logger.error("Erro /robo/admin/pix-confirm: {}".format(e))
        return jsonify({"success": False, "message": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════
# 💸 PIX — Saque manual (admin paga via PIX)
# ═══════════════════════════════════════════════════════════════════════

@app.route('/robo/withdraw/pix', methods=['POST'])
def process_withdrawal_pix():
    """
    Saque via PIX — cliente informa chave PIX, admin processa manualmente.
    Body: { wallet_code, amount_brl, pix_key }
    Débita saldo imediatamente (fica com status 'pendente_pix').
    """
    try:
        data        = request.get_json(silent=True) or {}
        wallet_code = data.get('wallet_code', '').strip().upper()
        amount_brl  = float(data.get('amount_brl', 0))
        pix_key     = data.get('pix_key', '').strip()

        if not wallet_code:
            return jsonify({"success": False, "message": "wallet_code obrigatorio"}), 400
        if amount_brl < 10:
            return jsonify({"success": False, "message": "Valor minimo de saque e R$ 10,00"}), 400
        if not pix_key:
            return jsonify({"success": False, "message": "Informe sua chave PIX para receber"}), 400

        participations = load_participations()
        if wallet_code not in participations:
            return jsonify({"success": False, "message": "Carteira nao encontrada"}), 404

        p              = participations[wallet_code]
        usdt_brl       = get_current_usdt_brl()
        fee_brl        = 3.50
        amount_net_brl = round(amount_brl - fee_brl, 2)
        if amount_net_brl <= 0:
            return jsonify({"success": False, "message": "Valor insuficiente apos taxa de R$ 3,50"}), 400

        amount_usdt = round(amount_brl     / usdt_brl, 6) if usdt_brl > 0 else 0
        fee_usdt    = round(fee_brl        / usdt_brl, 6) if usdt_brl > 0 else 0
        net_usdt    = round(amount_net_brl / usdt_brl, 6) if usdt_brl > 0 else 0

        saldo = float(p.get("virtual_balance_usdt", 0))
        if amount_usdt > saldo:
            return jsonify({
                "success": False,
                "message": "Saldo insuficiente. Disponivel: R$ {:.2f}".format(round(saldo * usdt_brl, 2))
            }), 400

        p["virtual_balance_usdt"] = round(saldo - amount_usdt, 6)
        p["total_withdrawn_usdt"] = round(float(p.get("total_withdrawn_usdt", 0)) + amount_usdt, 6)
        p["updated_at"]           = datetime.utcnow().isoformat()

        transactions = load_transactions()
        tx_id = "WTH-PIX-{}-{}".format(datetime.now().strftime('%Y%m%d%H%M%S'), wallet_code[-4:])

        tx = {
            "id":             tx_id,
            "wallet_code":    wallet_code,
            "type":           "withdrawal",
            "method":         "PIX",
            "pix_key":        pix_key,
            "amount_brl":     round(amount_brl, 2),
            "amount_net_brl": amount_net_brl,
            "fee_brl":        fee_brl,
            "amount_usdt":    amount_usdt,
            "fee_usdt":       fee_usdt,
            "net_usdt":       net_usdt,
            "usdt_brl_rate":  round(usdt_brl, 6),
            "status":         "pendente_pix",
            "date":           datetime.now().isoformat(),
            "created_at":     datetime.now().isoformat(),
            "description":    "Saque PIX R$ {:.2f} (liq. R$ {:.2f}) -> {}".format(amount_brl, amount_net_brl, pix_key),
            "paid_at":        None,
            "admin_notes":    "",
        }

        transactions.append(tx)
        save_transactions(transactions)
        save_participations(participations)

        logger.info("💸 Saque PIX: {} | R$ {:.2f} -> {} | {}".format(wallet_code, amount_brl, pix_key, tx_id))

        return jsonify({
            "success":        True,
            "transaction_id": tx_id,
            "amount_brl":     round(amount_brl, 2),
            "fee_brl":        fee_brl,
            "amount_net_brl": amount_net_brl,
            "pix_key":        pix_key,
            "message":        "Saque de R$ {:.2f} registrado (taxa R$ {:.2f}). Processamento em ate 24h.".format(amount_net_brl, fee_brl),
        })

    except Exception as e:
        logger.error("Erro /robo/withdraw/pix: {}".format(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/admin/reset-system', methods=['POST'])
def admin_reset_system():
    """Endpoint administrativo para resetar o sistema"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        data = request.get_json(silent=True) or {}
        total_balance_usdt = float(data.get('total_balance_usdt', 0))
        
        if total_balance_usdt <= 0:
            return jsonify({
                "success": False,
                "message": "Informe um saldo total válido em USDT"
            }), 400
        
        success, message = reset_and_distribute_balance(total_balance_usdt)
        
        if success:
            return jsonify({
                "success": True,
                "message": message
            })
        else:
            return jsonify({
                "success": False,
                "message": message
            }), 500
        
    except Exception as e:
        logger.error(f"💥 Erro em admin-reset-system: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/user/transactions', methods=['GET'])
def get_user_transactions():
    """Retorna movimentações do usuário sem expor dados do pool"""
    try:
        wallet_code = request.args.get('code', '').strip().upper()

        if not wallet_code:
            return jsonify({
                "success": False,
                "message": "Código da carteira é obrigatório"
            }), 400

        transactions = load_transactions()
        user_tx = []

        TRADE_DIST_TYPES = {
            "profit_distribution", "loss_distribution",
            "trade_closed_profit", "trade_closed_loss",
            "trade_closed_breakeven",
        }

        for tx in transactions:
            tx_wallet = (tx.get("wallet_code") or tx.get("user_code") or "").upper()
            if tx_wallet != wallet_code:
                continue

            tx_type = tx.get("type", "")

            # ✔ Depósitos
            if tx_type == "deposit":
                user_tx.append({
                    "id":          tx.get("id"),
                    "type":        "deposit",
                    "date":        tx.get("date"),
                    "amount_usdt": float(tx.get("amount_usdt", 0) or 0),
                    "status":      tx.get("status"),
                })

            # ✔ Saques
            elif tx_type in ("withdraw", "withdrawal"):
                user_tx.append({
                    "id":          tx.get("id"),
                    "type":        "withdraw",
                    "date":        tx.get("date"),
                    "amount_usdt": float(tx.get("amount_usdt", 0) or 0),
                    "status":      tx.get("status"),
                })

            # ✔ Trades fechados (profit_distribution / loss_distribution / trade_closed_*)
            elif tx_type in TRADE_DIST_TYPES:
                pnl_liq = float(
                    tx.get("pnl_liquido_usdt") or
                    tx.get("amount_usdt") or
                    tx.get("pnl_usdt") or 0
                )
                user_tx.append({
                    "id":               tx.get("id"),
                    "type":             tx_type,
                    "date":             tx.get("date"),
                    "amount_usdt":      round(pnl_liq, 8),
                    "pnl_liquido_usdt": round(pnl_liq, 8),
                    "symbol":           tx.get("symbol", ""),
                    "trade_id":         tx.get("trade_id", ""),
                    "exit_reason":      tx.get("exit_reason", tx.get("motivo_saida", "")),
                    "status":           tx.get("status", "completed"),
                    "cycle_id":         tx.get("cycle_id", ""),
                })

            # ✔ Formato legado: TRADE_CLOSED_POOL com sub-distribuição
            elif tx_type == "TRADE_CLOSED_POOL":
                for d in tx.get("distribution", []):
                    if (d.get("wallet_code") or "").upper() == wallet_code:
                        user_tx.append({
                            "id":               tx.get("id"),
                            "type":             "profit_distribution",
                            "date":             tx.get("date"),
                            "amount_usdt":      float(d.get("pnl_applied", 0)),
                            "pnl_liquido_usdt": float(d.get("pnl_applied", 0)),
                            "symbol":           tx.get("symbol", ""),
                            "trade_id":         tx.get("trade_id", tx.get("id", "")),
                            "status":           "completed",
                        })

        # Ordenar do mais recente para o mais antigo
        user_tx.sort(key=lambda x: x.get("date", ""), reverse=True)

        return jsonify({
            "success":      True,
            "transactions": user_tx[:200],  # últimas 200
            "count":        len(user_tx),
        })

    except Exception as e:
        logger.error(f"💥 Erro em user-transactions: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500


@app.route('/robo/admin/update-transaction', methods=['POST'])
def admin_update_transaction():
    """Atualiza o status de uma transação (apenas admin)"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        data = request.get_json(silent=True) or {}
        transaction_id = data.get('transaction_id', '').strip()
        new_status = data.get('status', '').strip().lower()
        
        if not transaction_id:
            return jsonify({
                "success": False,
                "message": "ID da transação é obrigatório"
            }), 400
        
        if new_status not in ['pendente', 'confirmado', 'rejeitado', 'completed']:
            return jsonify({
                "success": False,
                "message": "Status inválido"
            }), 400
        
        transactions = load_transactions()
        
        transaction_found = False
        for i, t in enumerate(transactions):
            if t.get('id') == transaction_id:
                old_status = t.get('status', '')
                t['status'] = new_status
                
                if new_status == 'confirmado' and not t.get('confirmed_at'):
                    t['confirmed_at'] = datetime.now().isoformat()
                
                transaction_found = True
                
                audit_event(
                    action="TRANSACTION_UPDATED",
                    success=True,
                    details=f"Transação {transaction_id} atualizada: {old_status} -> {new_status}",
                    extra={
                        "transaction_id": transaction_id,
                        "old_status": old_status,
                        "new_status": new_status
                    }
                )
                
                logger.info(f"📝 Transação atualizada: {transaction_id} | {old_status} -> {new_status}")
                break
        
        if not transaction_found:
            return jsonify({
                "success": False,
                "message": "Transação não encontrada"
            }), 404
        
        save_transactions(transactions)
        
        return jsonify({
            "success": True,
            "message": f"Transação {transaction_id} atualizada para '{new_status}'"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em admin-update-transaction: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

# ============================================
# 🔥 ROTAS PARA SERVER OS ARQUIVOS HTML
# ============================================

@app.route('/')
def index_home():
    """Página inicial"""
    try:
        return send_file(os.path.join(BASE_DIR, "index.html"))
    except:
        return jsonify({"error": "Página não encontrada"}), 404

@app.route('/painel')
def serve_painel():
    """Painel de ranking"""
    try:
        return send_file(os.path.join(BASE_DIR, "painel.html"))
    except:
        return jsonify({"error": "Página não encontrada"}), 404

@app.route('/tutorial_scanner')
def serve_tutorial_scanner():
    """Tutorial do scanner"""
    try:
        return send_file(os.path.join(BASE_DIR, "tutorial_scanner.html"))
    except Exception as e:
        logger.error(f"❌ Erro ao servir tutorial_scanner.html: {str(e)}")
        return jsonify({"error": "Página não encontrada"}), 404

@app.route('/adminrobo')
def serve_adminrobo():
    """Painel administrativo do robô"""
    try:
        return send_file(os.path.join(BASE_DIR, "adminrobo.html"))
    except Exception as e:
        logger.error(f"❌ Erro ao servir adminrobo.html: {str(e)}")
        return jsonify({"error": "Página não encontrada"}), 404

# =============================================
# 📊 ADMIN ROBO - ENDPOINTS DE VISÃO FINANCEIRA (USDT)
# =============================================

@app.route('/adminrobo/finance/overview', methods=['GET'])
def adminrobo_finance_overview():
    """Visão Geral Financeira (somente leitura) em USDT"""
    try:
        auth_ok, message = require_admin_auth()
        if not auth_ok:
            return jsonify({"success": False, "message": message}), 403
        
        logger.info("📊 [AdminRobo] Gerando visão financeira geral")
        
        transactions = load_transactions()
        participations = load_participations()
        wallets = load_wallets()
        
        lucro_bruto_usdt = 0.0
        prejuizo_total_usdt = 0.0
        fee_sistema_usdt = 0.0
        
        for tx in transactions:
            if tx.get("type") == "trade_closed_profit":
                valor = tx.get("pnl_usdt", tx.get("amount_usdt", 0))
                if valor > 0:
                    lucro_bruto_usdt += valor
                else:
                    prejuizo_total_usdt += valor
            
            if tx.get("type") in ["company_fee_income", "fee_income"]:
                fee_sistema_usdt += tx.get("amount_usdt", 0)
        
        resultado_liquido_usdt = lucro_bruto_usdt + prejuizo_total_usdt
        
        total_depositado_usuarios_usdt = 0.0
        saldo_virtual_total_usdt = 0.0
        
        for user_code, part in participations.items():
            if part.get("status") == "active":
                saldo_virtual_total_usdt += part.get("virtual_balance_usdt", 0)
                total_depositado_usuarios_usdt += part.get("total_deposited_usdt", 0)
        
        resultado_usuarios_usdt = 0.0
        for user_code, part in participations.items():
            if part.get("status") == "active":
                resultado_usuarios_usdt += part.get("profit_accumulated_usdt", 0)
        
        saldo_total_usdt = 0.0
        for wallet_code, wallet in wallets.items():
            if wallet_code in MASTER_WALLET_CODES:
                saldo_total_usdt += wallet.get("balanceUSDT", 0)
        
        capital_livre_usdt = saldo_total_usdt
        capital_em_posicoes_usdt = 0.0
        
        response = {
            "binance": {
                "saldo_total_usdt": round(saldo_total_usdt, 2),
                "capital_em_posicoes_usdt": round(capital_em_posicoes_usdt, 2),
                "capital_livre_usdt": round(capital_livre_usdt, 2)
            },
            "virtual": {
                "total_depositado_usuarios_usdt": round(total_depositado_usuarios_usdt, 2),
                "saldo_virtual_total_usdt": round(saldo_virtual_total_usdt, 2)
            },
            "resultado": {
                "lucro_bruto_usdt": round(lucro_bruto_usdt, 2),
                "prejuizo_usdt": round(prejuizo_total_usdt, 2),
                "resultado_liquido_usdt": round(resultado_liquido_usdt, 2),
                "fee_sistema_usdt": round(fee_sistema_usdt, 2),
                "resultado_usuarios_usdt": round(resultado_usuarios_usdt, 2)
            },
            "consolidation": {
                "total_transactions": len(transactions),
                "active_users": len([p for p in participations.values() if p.get("status") == "active"]),
                "master_wallets": len(MASTER_WALLET_CODES),
                "calculation_timestamp": datetime.now().isoformat(),
                "currency": "USDT"
            }
        }
        
        audit_event(
            action="ADMINROBO_FINANCE_OVERVIEW",
            success=True,
            details="Visão financeira geral gerada",
            extra={"response_keys": list(response.keys())}
        )
        
        return jsonify({
            "success": True,
            "data": response,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em adminrobo_finance_overview: {str(e)}", exc_info=True)
        return jsonify({
            "success": False,
            "message": f"Erro ao gerar visão financeira: {str(e)}"
        }), 500

@app.route('/adminrobo/finance/dre', methods=['GET'])
def adminrobo_finance_dre():
    """DRE do Robô (Demonstrativo de Resultados) em USDT"""
    try:
        auth_ok, message = require_admin_auth()
        if not auth_ok:
            return jsonify({"success": False, "message": message}), 403
        
        logger.info("📊 [AdminRobo] Gerando DRE do robô")
        
        transactions = load_transactions()
        
        trades_lucro = 0
        trades_prejuizo = 0
        lucro_bruto_usdt = 0.0
        prejuizo_total_usdt = 0.0
        fee_sistema_usdt = 0.0
        
        trade_ids_processados = set()
        
        for tx in transactions:
            trade_id = tx.get("trade_id")
            
            if tx.get("type") == "trade_closed_profit" and trade_id:
                if trade_id not in trade_ids_processados:
                    trade_ids_processados.add(trade_id)
                    
                    pnl_usdt = tx.get("pnl_usdt", tx.get("amount_usdt", 0))
                    if pnl_usdt > 0:
                        trades_lucro += 1
                        lucro_bruto_usdt += pnl_usdt
                    elif pnl_usdt < 0:
                        trades_prejuizo += 1
                        prejuizo_total_usdt += pnl_usdt
            
            if tx.get("type") in ["company_fee_income", "fee_income"]:
                fee_sistema_usdt += tx.get("amount_usdt", 0)
        
        total_trades = trades_lucro + trades_prejuizo
        resultado_liquido_usdt = lucro_bruto_usdt + prejuizo_total_usdt
        resultado_clientes_usdt = resultado_liquido_usdt - fee_sistema_usdt
        
        response = {
            "total_trades": total_trades,
            "trades_lucro": trades_lucro,
            "trades_prejuizo": trades_prejuizo,
            "lucro_bruto_usdt": round(lucro_bruto_usdt, 2),
            "prejuizo_total_usdt": round(prejuizo_total_usdt, 2),
            "resultado_liquido_usdt": round(resultado_liquido_usdt, 2),
            "fee_sistema_usdt": round(fee_sistema_usdt, 2),
            "resultado_clientes_usdt": round(resultado_clientes_usdt, 2),
            "metrics": {
                "win_rate": round((trades_lucro / total_trades * 100) if total_trades > 0 else 0, 1),
                "average_win_usdt": round((lucro_bruto_usdt / trades_lucro) if trades_lucro > 0 else 0, 2),
                "average_loss_usdt": round((prejuizo_total_usdt / trades_prejuizo) if trades_prejuizo > 0 else 0, 2),
                "profit_factor": round((lucro_bruto_usdt / abs(prejuizo_total_usdt)) if prejuizo_total_usdt < 0 else 0, 2)
            },
            "period": {
                "start_date": None,
                "end_date": datetime.now().isoformat(),
                "calculation_timestamp": datetime.now().isoformat()
            },
            "currency": "USDT"
        }
        
        return jsonify({
            "success": True,
            "data": response,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em adminrobo_finance_dre: {str(e)}", exc_info=True)
        return jsonify({
            "success": False,
            "message": f"Erro ao gerar DRE: {str(e)}"
        }), 500

@app.route('/adminrobo/users/summary', methods=['GET'])
def adminrobo_users_summary():
    """Resumo de Usuários (para admin) em USDT"""
    try:
        auth_ok, message = require_admin_auth()
        if not auth_ok:
            return jsonify({"success": False, "message": message}), 403
        
        logger.info("📊 [AdminRobo] Gerando resumo de usuários")
        
        participations = load_participations()
        
        saldo_virtual_total_usdt = sum(
            p.get("virtual_balance_usdt", 0) 
            for p in participations.values() 
            if p.get("status") == "active"
        )
        
        users_summary = []
        
        for user_code, part in participations.items():
            if part.get("status") != "active":
                continue
            
            virtual_balance_usdt = part.get("virtual_balance_usdt", 0)
            profit_accumulated_usdt = part.get("profit_accumulated_usdt", 0)
            
            depositado_usdt = virtual_balance_usdt - profit_accumulated_usdt
            if depositado_usdt < 0:
                depositado_usdt = 0
            
            participation_percent = part.get("share_percent", 0)
            if participation_percent == 0 and saldo_virtual_total_usdt > 0:
                participation_percent = (virtual_balance_usdt / saldo_virtual_total_usdt) * 100
            
            users_summary.append({
                "user_code": user_code,
                "depositado_usdt": round(depositado_usdt, 2),
                "participation_percent": round(participation_percent, 2),
                "lucro_usdt": round(profit_accumulated_usdt, 2),
                "saldo_virtual_usdt": round(virtual_balance_usdt, 2),
                "status": part.get("status", "unknown"),
                "last_update": part.get("updated_at", part.get("created_at", "")),
                "metrics": {
                    "roi": round((profit_accumulated_usdt / depositado_usdt * 100) if depositado_usdt > 0 else 0, 1),
                    "share_of_total": round((virtual_balance_usdt / saldo_virtual_total_usdt * 100) if saldo_virtual_total_usdt > 0 else 0, 1)
                }
            })
        
        users_summary.sort(key=lambda x: x["saldo_virtual_usdt"], reverse=True)
        
        totais = {
            "total_users": len(users_summary),
            "total_depositado_usdt": round(sum(u["depositado_usdt"] for u in users_summary), 2),
            "total_saldo_virtual_usdt": round(sum(u["saldo_virtual_usdt"] for u in users_summary), 2),
            "total_lucro_usdt": round(sum(u["lucro_usdt"] for u in users_summary), 2),
            "avg_participation_percent": round(
                sum(u["participation_percent"] for u in users_summary) / len(users_summary) if users_summary else 0, 
                2
            )
        }
        
        response = {
            "users": users_summary,
            "totals": totais,
            "summary": {
                "active_users": len([u for u in users_summary if u["status"] == "active"]),
                "inactive_users": len([u for u in users_summary if u["status"] != "active"]),
                "timestamp": datetime.now().isoformat(),
                "currency": "USDT"
            }
        }
        
        return jsonify({
            "success": True,
            "data": response,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em adminrobo_users_summary: {str(e)}", exc_info=True)
        return jsonify({
            "success": False,
            "message": f"Erro ao gerar resumo de usuários: {str(e)}"
        }), 500

@app.route('/adminrobo/finance/transactions-summary', methods=['GET'])
def adminrobo_transactions_summary():
    """Resumo das Transações (para dashboard) em USDT"""
    try:
        auth_ok, message = require_admin_auth()
        if not auth_ok:
            return jsonify({"success": False, "message": message}), 403
        
        logger.info("📊 [AdminRobo] Gerando resumo de transações")
        
        transactions = load_transactions()
        
        tipos = {}
        for tx in transactions:
            tipo = tx.get("type", "unknown")
            valor = tx.get("amount_usdt", tx.get("pnl_usdt", 0))
            
            if tipo not in tipos:
                tipos[tipo] = {
                    "count": 0,
                    "total_value_usdt": 0.0,
                    "last_date": tx.get("date", "")
                }
            
            tipos[tipo]["count"] += 1
            tipos[tipo]["total_value_usdt"] += valor
            tipos[tipo]["last_date"] = max(tipos[tipo]["last_date"], tx.get("date", ""))
        
        for tipo, dados in tipos.items():
            dados["total_value_usdt"] = round(dados["total_value_usdt"], 2)
        
        response = {
            "total_transactions": len(transactions),
            "by_type": tipos,
            "recent_activity": {
                "last_7_days": 0,
                "last_30_days": 0,
                "last_trade_date": next(
                    (tx.get("date") for tx in reversed(transactions) if tx.get("type") in ["trade_closed_profit", "trade_closed_loss"]), 
                    None
                )
            },
            "timestamp": datetime.now().isoformat(),
            "currency": "USDT"
        }
        
        return jsonify({
            "success": True,
            "data": response,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em adminrobo_transactions_summary: {str(e)}", exc_info=True)
        return jsonify({
            "success": False,
            "message": f"Erro ao gerar resumo de transações: {str(e)}"
        }), 500

@app.route('/adminrobo/health', methods=['GET'])
def adminrobo_health():
    """Health Check do AdminRobo (status do sistema)"""
    try:
        auth_ok, message = require_admin_auth()
        if not auth_ok:
            return jsonify({"success": False, "message": message}), 403
        
        files_status = {
            "transactions": os.path.exists(TRANSACTIONS_FILE),
            "participations": os.path.exists(PARTICIPATIONS_FILE),
            "wallets": os.path.exists(WALLETS_FILE)
        }
        
        try:
            transactions = load_transactions()
            participations = load_participations()
            wallets = load_wallets()
            
            data_status = {
                "transactions_count": len(transactions),
                "participations_count": len(participations),
                "wallets_count": len(wallets),
                "master_wallets": len([w for w in wallets.keys() if w in MASTER_WALLET_CODES]),
                "active_users": len([p for p in participations.values() if p.get("status") == "active"])
            }
        except Exception as e:
            data_status = {"error": str(e)}
        
        response = {
            "status": "operational",
            "timestamp": datetime.now().isoformat(),
            "files": files_status,
            "data": data_status,
            "endpoints": {
                "finance_overview": "/adminrobo/finance/overview",
                "finance_dre": "/adminrobo/finance/dre",
                "users_summary": "/adminrobo/users/summary",
                "transactions_summary": "/adminrobo/finance/transactions-summary"
            },
            "currency": "USDT"
        }
        
        return jsonify({
            "success": True,
            "data": response
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em adminrobo_health: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro no health check: {str(e)}",
            "status": "degraded"
        }), 500

@app.route('/robo/get-wallet', methods=['GET'])
def api_get_wallet():
    """Retorna dados da carteira - CORRIGIDO: Usa participation.virtual_balance_usdt direto"""
    try:
        wallet_code = request.args.get('code', '').strip().upper()
        
        if not wallet_code:
            return jsonify({
                "success": False,
                "message": "Código da carteira é obrigatório"
            }), 400
        
        participations = load_participations()
        
        if wallet_code in participations:
            participation = participations[wallet_code]
            usdt_brl = get_current_usdt_brl()
            
            return jsonify({
                "success": True,
                "wallet_code": wallet_code,
                "virtual_balance_usdt": float(participation.get("virtual_balance_usdt", 0)),
                "virtual_balance_brl": round(float(participation.get("virtual_balance_usdt", 0)) * usdt_brl, 2),
                "total_deposited_usdt": float(participation.get("total_deposited_usdt", 0)),
                "profit_accumulated_usdt": float(participation.get("profit_accumulated_usdt", 0)),
                "share_percent": float(participation.get("share_percent", 0)),
                "total_withdrawn_usdt": float(participation.get("total_withdrawn_usdt", 0)),
                "status": participation.get("status", "active"),
                "master_wallet": participation.get("master_wallet"),
                "joined_at": participation.get("joined_at"),
                "updated_at": participation.get("updated_at"),
                "currency": "USDT",
                "note": "🔥 SALDO VIRTUAL DIRETO DA PARTICIPAÇÃO EM USDT"
            })
        
        wallets = load_wallets()
        
        if wallet_code not in wallets:
            return jsonify({
                "success": False,
                "message": "Carteira não encontrada"
            }), 404
        
        wallet = wallets[wallet_code].copy()
        
        if 'phrase_hash' in wallet:
            del wallet['phrase_hash']
        if 'encrypted_phrase' in wallet:
            del wallet['encrypted_phrase']
        
        transactions = load_transactions()
        wallet_transactions = [
            t for t in transactions 
            if t.get('wallet_code') == wallet_code
        ]
        
        usdt_brl = get_current_usdt_brl()
        
        return jsonify({
            "success": True,
            "wallet_code": wallet_code,
            "balanceUSDT": wallet.get('balanceUSDT', 0),
            "balanceBRL": round(wallet.get('balanceUSDT', 0) * usdt_brl, 2),
            "balanceBTC": wallet.get('balanceBTC', 0),
            "totalDepositedUSDT": wallet.get('totalDepositedUSDT', 0),
            "totalProfitUSDT": wallet.get('totalProfitUSDT', 0),
            "wallet": wallet,
            "transactions": wallet_transactions,
            "has_participation": False,
            "currency": "USDT",
            "note": "Carteira sem participação ativa"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em get-wallet: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

# ═══════════════════════════════════════════════════════════════════
# 🆕 GESTÃO DE RISCO INDIVIDUAL POR USUÁRIO
# ═══════════════════════════════════════════════════════════════════

def _ensure_user_bot_fields(participation: dict) -> dict:
    """Garante que todos os campos de gestão de risco existam na participação."""
    participation.setdefault("bot_active", False)        # Start/Pause por usuário
    participation.setdefault("aporte_usdt", 0.0)         # Capital alocado pelo usuário
    participation.setdefault("risco_por_trade", 0.05)    # 5% padrão
    participation.setdefault("max_posicoes_abertas", 3)  # máx simultâneas
    participation.setdefault("saldo_disponivel_usdt", float(participation.get("virtual_balance_usdt", 0)))
    participation.setdefault("saldo_em_posicoes_usdt", 0.0)
    participation.setdefault("valor_minimo_entrada_usdt", 10.0)
    participation.setdefault("user_positions", [])        # posições individuais isoladas
    participation.setdefault("user_closed_trades", [])    # histórico individual
    participation.setdefault("perda_diaria_limite_usdt", 0.0)   # 0 = sem limite
    participation.setdefault("perda_diaria_acumulada_usdt", 0.0)
    participation.setdefault("perda_diaria_data", "")
    participation.setdefault("scanner_config", {          # config isolada por usuário
        "max_posicoes": 3,
        "valor_entrada": 10.0,
        "score_minimo": 7
    })
    return participation


@app.route('/robo/user/bot-status', methods=['GET'])
def user_get_bot_status():
    """Retorna o status atual do bot e configurações de risco do usuário."""
    try:
        code = request.args.get('code', '').strip().upper()
        if not code:
            return jsonify({"success": False, "message": "code obrigatório"}), 400

        participations = load_participations()
        if code not in participations:
            return jsonify({"success": False, "message": "Usuário não encontrado"}), 404

        p = _ensure_user_bot_fields(participations[code])
        usdt_brl = get_current_usdt_brl()

        return jsonify({
            "success": True,
            "bot_active": p.get("bot_active", False),
            "aporte_usdt": round(float(p.get("aporte_usdt", 0)), 2),
            "risco_por_trade": float(p.get("risco_por_trade", 0.05)),
            "max_posicoes_abertas": int(p.get("max_posicoes_abertas", 3)),
            "saldo_disponivel_usdt": round(float(p.get("saldo_disponivel_usdt", 0)), 4),
            "saldo_em_posicoes_usdt": round(float(p.get("saldo_em_posicoes_usdt", 0)), 4),
            "valor_minimo_entrada_usdt": 10.0,
            "perda_diaria_limite_usdt": round(float(p.get("perda_diaria_limite_usdt", 0)), 2),
            "scanner_config": p.get("scanner_config", {"max_posicoes": 3, "valor_entrada": 10.0, "score_minimo": 7}),
            "user_positions": p.get("user_positions", []),
            "usdt_brl": round(usdt_brl, 2),
        })
    except Exception as e:
        logger.error(f"Erro /robo/user/bot-status: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/user/bot-toggle', methods=['POST'])
def user_bot_toggle():
    """Liga (start) ou pausa o bot para um usuário específico.
    Se pausado, quando a última posição fechar, permanece pausado.
    NÃO afeta outros usuários nem a carteira master."""
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip().upper()
        action = (data.get("action") or "").strip().lower()  # "start" ou "pause"

        if not code or action not in ("start", "pause"):
            return jsonify({"success": False, "message": "code e action (start|pause) são obrigatórios"}), 400

        participations = load_participations()
        if code not in participations:
            return jsonify({"success": False, "message": "Usuário não encontrado"}), 404

        p = _ensure_user_bot_fields(participations[code])
        p["bot_active"] = (action == "start")
        p["bot_status_updated_at"] = datetime.now().isoformat()
        p["updated_at"] = datetime.now().isoformat()

        save_participations(participations)

        logger.info(f"🤖 Bot {'ATIVADO' if p['bot_active'] else 'PAUSADO'} para {code}")
        return jsonify({
            "success": True,
            "bot_active": p["bot_active"],
            "message": f"Bot {'iniciado' if p['bot_active'] else 'pausado'} com sucesso"
        })
    except Exception as e:
        logger.error(f"Erro /robo/user/bot-toggle: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/user/save-config', methods=['POST'])
def user_save_individual_config():
    """Salva configurações individuais do scanner/risco para UM usuário.
    NÃO altera configurações globais do robô nem de outros usuários."""
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip().upper()
        if not code:
            return jsonify({"success": False, "message": "code obrigatório"}), 400

        participations = load_participations()
        if code not in participations:
            return jsonify({"success": False, "message": "Usuário não encontrado"}), 404

        p = _ensure_user_bot_fields(participations[code])

        # Aporte do usuário
        if "aporte_usdt" in data:
            val = float(data["aporte_usdt"])
            if val < 0:
                return jsonify({"success": False, "message": "Aporte não pode ser negativo"}), 400
            p["aporte_usdt"] = val
            # Sempre sincronizar saldo disponível descontando o que já está em posições abertas
            em_posicao = float(p.get("saldo_em_posicoes_usdt", 0))
            p["saldo_disponivel_usdt"] = max(0.0, round(val - em_posicao, 4))

        # Risco por trade (%)
        if "risco_por_trade" in data:
            val = float(data["risco_por_trade"])
            if not (0.001 <= val <= 1.0):
                return jsonify({"success": False, "message": "Risco deve ser entre 0.1% e 100%"}), 400
            p["risco_por_trade"] = val

        # Máximo de posições abertas
        if "max_posicoes_abertas" in data:
            val = int(data["max_posicoes_abertas"])
            if not (1 <= val <= 20):
                return jsonify({"success": False, "message": "Max posições deve ser entre 1 e 20"}), 400
            p["max_posicoes_abertas"] = val

        # Limite de perda diária
        if "perda_diaria_limite_usdt" in data:
            p["perda_diaria_limite_usdt"] = max(0.0, float(data["perda_diaria_limite_usdt"]))

        # Config do scanner individual
        if "scanner_config" in data:
            cfg = data["scanner_config"]
            sc = p.get("scanner_config", {})
            if "max_posicoes" in cfg:
                sc["max_posicoes"] = max(1, min(20, int(cfg["max_posicoes"])))
            if "valor_entrada" in cfg:
                sc["valor_entrada"] = max(10.0, float(cfg["valor_entrada"]))
            if "score_minimo" in cfg:
                sc["score_minimo"] = max(1, min(17, int(cfg["score_minimo"])))
            p["scanner_config"] = sc

        p["updated_at"] = datetime.now().isoformat()
        save_participations(participations)

        logger.info(f"⚙️ Config individual salva para {code}")
        return jsonify({
            "success": True,
            "message": "Configurações salvas com sucesso",
            "config": {
                "aporte_usdt": p.get("aporte_usdt"),
                "risco_por_trade": p.get("risco_por_trade"),
                "max_posicoes_abertas": p.get("max_posicoes_abertas"),
                "saldo_disponivel_usdt": p.get("saldo_disponivel_usdt"),
                "scanner_config": p.get("scanner_config"),
                "bot_active": p.get("bot_active"),
            }
        })
    except Exception as e:
        logger.error(f"Erro /robo/user/save-config: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/user/positions', methods=['GET'])
def user_get_positions():
    """Retorna posições abertas individuais do usuário com nome e quantidade da moeda."""
    try:
        code = request.args.get('code', '').strip().upper()
        if not code:
            return jsonify({"success": False, "message": "code obrigatório"}), 400

        participations = load_participations()
        if code not in participations:
            return jsonify({"success": False, "message": "Usuário não encontrado"}), 404

        p = _ensure_user_bot_fields(participations[code])
        usdt_brl = get_current_usdt_brl()

        positions = p.get("user_positions", [])
        closed = p.get("user_closed_trades", [])

        # ── FALLBACK: se user_closed_trades vazio, reconstrói a partir das transações ──
        if not closed:
            all_txs = load_transactions()
            tx_fechados = {}
            for tx in all_txs:
                if tx.get("wallet_code", tx.get("user_code", "")) != code:
                    continue
                tx_type     = tx.get("type", "")
                trade_id_tx = tx.get("trade_id", tx.get("id", ""))
                date_str    = tx.get("date", "")
                if tx_type in ("loss_distribution", "profit_distribution"):
                    amount = float(tx.get("amount_usdt", 0))
                    symbol = ""
                    parts  = trade_id_tx.replace("LOSS-","").replace("PROFIT-","").replace("PROF-","").split("_") if trade_id_tx else []
                    if parts and parts[0].endswith("USDT"):
                        symbol = parts[0]
                    tx_fechados[trade_id_tx] = {
                        "symbol":         symbol or "TRADE",
                        "trade_id":       trade_id_tx,
                        "pnl_liquido_usdt": amount,
                        "pnl_usdt":       amount,
                        "exit_time":      date_str,
                        "timestamp_saida": date_str,
                        "exit_reason":    "LOSS" if tx_type == "loss_distribution" else "PROFIT",
                        "status":         "fechada",
                        "description":    tx.get("description", trade_id_tx),
                    }
            closed = sorted(tx_fechados.values(), key=lambda x: x.get("exit_time",""), reverse=True)

        # Enriquecer posições abertas com PnL atual usando _live_state (leitura thread-safe)
        with _live_state_lock:
            _positions_snapshot = list(_live_state.get("positions_open", []))
        live_prices = {pos.get("symbol"): pos for pos in _positions_snapshot}

        enriched_positions = []
        for pos in positions:
            sym = pos.get("symbol", "")
            entry_price = float(pos.get("preco_entrada", 0))
            quantidade = float(pos.get("quantidade", 0))
            valor_entrada = float(pos.get("valor_entrada_usdt", 0))

            current_price = entry_price
            live_data = live_prices.get(sym)
            if live_data:
                current_price = float(live_data.get("current_price", entry_price))

            pnl_usdt = (current_price - entry_price) * quantidade if entry_price > 0 else 0
            pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
            valor_atual = quantidade * current_price

            enriched_positions.append({
                "symbol": sym,
                "nome_moeda": sym.replace("USDT", ""),
                "quantidade": round(quantidade, 8),
                "preco_entrada": round(entry_price, 8),
                "preco_atual": round(current_price, 8),
                "valor_entrada_usdt": round(valor_entrada, 2),
                "valor_atual_usdt": round(valor_atual, 2),
                "pnl_usdt": round(pnl_usdt, 4),
                "pnl_brl": round(pnl_usdt * usdt_brl, 2),
                "pnl_percent": round(pnl_pct, 2),
                "take_profit": pos.get("take_profit", 0),
                "stop_loss": pos.get("stop_loss", 0),
                "timestamp": pos.get("timestamp", ""),
                "status": "aberta",
            })

        # Lucro realizado individual — usa pnl_liquido (já com fee aplicada no fechamento)
        # pnl_liquido_usdt = lucro*50% ou prejuízo*(1+10%) — valor já líquido
        lucro_realizado_usdt = sum(
            float(t.get("pnl_liquido_usdt", t.get("pnl_usdt", 0))) for t in closed
        )

        return jsonify({
            "success": True,
            "positions_open": enriched_positions,
            "positions_count": len(enriched_positions),
            "closed_trades": closed[-20:],
            "lucro_realizado_usdt": round(lucro_realizado_usdt, 4),
            "lucro_realizado_brl": round(lucro_realizado_usdt * usdt_brl, 2),
            "saldo_disponivel_usdt": round(max(0.0, float(p.get("virtual_balance_usdt", 0)) - float(p.get("saldo_em_posicoes_usdt", 0))), 4),
            "saldo_em_posicoes_usdt": round(float(p.get("saldo_em_posicoes_usdt", 0)), 4),
            "virtual_balance_usdt": round(float(p.get("virtual_balance_usdt", 0)), 4),
            "bot_active": p.get("bot_active", False),
            "usdt_brl": round(usdt_brl, 2),
        })
    except Exception as e:
        logger.error(f"Erro /robo/user/positions: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════

@app.route('/robo/user/wallet', methods=['GET'])
def api_user_wallet():
    """Endpoint usado pelo adminrobo.html para carregar dados"""
    try:
        code = request.args.get('code', '').strip().upper()
        
        if not code:
            return jsonify({
                "success": False,
                "message": "Código não fornecido"
            }), 400
        
        usdt_brl = get_current_usdt_brl()
        
        return jsonify({
            "success": True,
            "wallet": {
                "code": code,
                "balanceUSDT": 1500.00,
                "balanceBRL": round(1500.00 * usdt_brl, 2),
                "balanceBTC": 1500.00 / get_current_btc_usdt(),
                "totalDepositedUSDT": 2000.00,
                "totalDepositedBTC": 2000.00 / get_current_btc_usdt(),
                "totalProfitUSDT": 300.00
            }
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

@app.route('/robo/user/deposits', methods=['GET'])
def api_user_deposits():
    """Retorna histórico de depósitos do usuário"""
    try:
        code = request.args.get('code', '').strip().upper()
        
        if not code:
            return jsonify({
                "success": False,
                "message": "Código não fornecido"
            }), 400
        
        deposits = [
            {
                "id": "DEP001",
                "date": "2024-01-07T10:30:00",
                "amount_usdt": 192.31,
                "value_btc": 0.00296,
                "type": "btc",
                "status": "confirmado"
            },
            {
                "id": "DEP002",
                "date": "2024-02-07T14:45:00",
                "amount_usdt": 96.15,
                "value_btc": 0.00148,
                "type": "btc",
                "status": "confirmado"
            },
            {
                "id": "DEP003",
                "date": "2024-03-07T09:15:00",
                "amount_usdt": 96.15,
                "value_btc": 0.00148,
                "type": "btc",
                "status": "pendente"
            }
        ]
        
        return jsonify({
            "success": True,
            "deposits": deposits
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

@app.route('/robo/admin/all-wallets', methods=['GET'])
def get_all_wallets_admin_robo():
    """Retorna todas as carteiras (apenas admin) - Robô"""
    try:
        if not admin_auth_ok(request):
            logger.warning(f"❌ Acesso negado a all-wallets")
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        wallets = load_wallets()
        usdt_brl = get_current_usdt_brl()
        
        wallet_list = []
        for code, wallet in wallets.items():
            wallet_info = {
                "code": wallet.get('code', code),
                "name": wallet.get('name', ''),
                "balanceUSDT": wallet.get('balanceUSDT', 0),
                "balanceBRL": round(wallet.get('balanceUSDT', 0) * usdt_brl, 2),
                "balanceBTC": wallet.get('balanceBTC', 0),
                "totalDepositedUSDT": wallet.get('totalDepositedUSDT', 0),
                "totalDepositedBTC": wallet.get('totalDepositedBTC', 0),
                "totalProfitUSDT": wallet.get('totalProfitUSDT', 0),
                "created_at": wallet.get('created_at', ''),
                "updated_at": wallet.get('updated_at', ''),
                "last_access": wallet.get('last_access', ''),
                "active": wallet.get('active', True),
                "is_base_wallet": wallet.get('is_base_wallet', True),
                "version": wallet.get('version', '1.0')
            }
            wallet_list.append(wallet_info)
        
        wallet_list.sort(key=lambda x: x.get('updated_at', ''), reverse=True)
        
        return jsonify({
            "success": True,
            "wallets": wallet_list,
            "count": len(wallet_list),
            "base_wallets": BASE_WALLETS,
            "currency": "USDT"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em admin/all-wallets: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/admin/all-transactions', methods=['GET'])
def get_all_transactions_admin_robo():
    """Retorna todas as transações (apenas admin)"""
    try:
        if not admin_auth_ok(request):
            logger.warning(f"❌ Acesso negado a all-transactions")
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        transactions = load_transactions()
        
        transactions.sort(key=lambda x: x.get('date', ''), reverse=True)
        
        return jsonify({
            "success": True,
            "transactions": transactions,
            "count": len(transactions)
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em admin/all-transactions: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/admin/stats', methods=['GET'])
def get_system_stats():
    """Estatísticas do sistema - APENAS valores CONFIRMADOS em USDT"""
    try:
        if not admin_auth_ok(request):
            logger.warning(f"❌ Acesso negado a stats")
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        transactions = load_transactions()
        
        confirmed_deposits = [
            tx for tx in transactions 
            if tx.get("type") == "deposit" 
            and tx.get("status") == "confirmado"
        ]
        
        confirmed_withdrawals = [
            tx for tx in transactions 
            if tx.get("type") == "withdrawal" 
            and tx.get("status") == "confirmado"
        ]
        
        total_invested_usdt = sum(tx.get("amount_usdt", tx.get("value_usdt", 0)) for tx in confirmed_deposits)
        total_withdrawn_usdt = sum(tx.get("amount_usdt", tx.get("value_usdt", 0)) for tx in confirmed_withdrawals)
        
        master_balance_usdt = total_invested_usdt - total_withdrawn_usdt
        
        active_users = 0
        users_with_confirmed_deposits = set()
        
        for tx in confirmed_deposits:
            wallet_code = tx.get("wallet_code")
            if wallet_code and wallet_code not in users_with_confirmed_deposits:
                users_with_confirmed_deposits.add(wallet_code)
                active_users += 1
        
        pending_deposits = [
            tx for tx in transactions 
            if tx.get("type") == "deposit" 
            and tx.get("status") == "pendente"
        ]
        
        if total_invested_usdt > 0 and len(confirmed_deposits) == 0:
            logger.error("🚨 INCONSISTÊNCIA: total_invested_usdt > 0 mas confirmed_deposits = 0")
            total_invested_usdt = 0
            master_balance_usdt = 0
        
        cashbox_data = load_cashbox_data()
        cashbox_balance_usdt = cashbox_data.get("balance", 0.0)
        
        btc_usdt = get_current_btc_usdt()
        usdt_brl = get_current_usdt_brl()
        
        return jsonify({
            "success": True,
            "stats": {
                "totalInvestedUSDT": round(total_invested_usdt, 2),
                "totalInvestedBRL": round(total_invested_usdt * usdt_brl, 2),
                "masterBalanceUSDT": round(master_balance_usdt, 2),
                "masterBalanceBRL": round(master_balance_usdt * usdt_brl, 2),
                "totalProfitUSDT": 0.0,
                "confirmedDeposits": len(confirmed_deposits),
                "pendingDeposits": len(pending_deposits),
                "activeUsers": active_users,
                "cashboxBalanceUSDT": round(cashbox_balance_usdt, 2),
                "cashboxBalanceBRL": round(cashbox_balance_usdt * usdt_brl, 2),
                "totalInvestedBTC": round(total_invested_usdt / btc_usdt, 8) if btc_usdt > 0 else 0.0,
                "masterBalanceBTC": round(master_balance_usdt / btc_usdt, 8) if btc_usdt > 0 else 0.0,
                "totalProfitBTC": 0.0,
                "serverTime": datetime.now().isoformat(),
                "audit": {
                    "source": "transactions.json_only",
                    "confirmed_deposits_count": len(confirmed_deposits),
                    "pending_deposits_count": len(pending_deposits),
                    "calculation_method": "confirmed_transactions_only",
                    "cashbox_start": 0.0,
                    "timestamp": datetime.now().isoformat(),
                    "currency": "USDT"
                }
            }
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em admin/stats: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/admin/binance-balance', methods=['GET'])
def get_binance_balance_info():
    """Retorna saldo real da Binance (apenas informativo - NUNCA altera ledger)"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            return jsonify({
                "success": False,
                "connected": False,
                "message": "API Binance não configurada"
            }), 400
        
        try:
            client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
            
            balances = {}
            account_info = client.get_account()
            
            for b in account_info["balances"]:
                free = float(b["free"])
                locked = float(b["locked"])
                total = free + locked
                
                if total > 0:
                    balances[b["asset"]] = {
                        "free": free,
                        "locked": locked,
                        "total": total
                    }
            
            return jsonify({
                "success": True,
                "connected": True,
                "balances": balances,
                "timestamp": datetime.now().isoformat(),
                "warning": "⚠️ Valores apenas informativos - NUNCA afetam ledger"
            })
            
        except Exception as e:
            logger.error(f"❌ Erro Binance: {e}")
            return jsonify({
                "success": False,
                "connected": False,
                "message": f"Erro Binance: {str(e)}"
            }), 500
            
    except Exception as e:
        logger.error(f"💥 Erro em binance-balance: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/api/init_wallet", methods=["POST"])
def init_wallet():
    data = request.get_json()
    frase = data.get("frase")
    if not frase:
        return jsonify({"success": False, "message": "Frase inválida"}), 400

    wallet_code = generate_wallet_code(frase)

    wallets = load_wallets()
    if wallet_code not in wallets:
        wallets[wallet_code] = {
            "code": wallet_code,
            "balanceUSDT": 0.0,
            "balanceBTC": 0.0,
            "history": [],
            "created_at": str(datetime.utcnow())
        }
        save_wallets(wallets)

    return jsonify({"success": True, "wallet_code": wallet_code})

ADMIN_WALLETS = ["3NezWZcHmTS3oECk9dx5LdzXptVPQr7P1q"]

@app.route('/login', methods=["POST"])
def login():
    data = request.get_json()
    frase = data.get("frase")
    if not frase:
        return jsonify({"success": False, "message": "Frase inválida"}), 400

    wallet_code = generate_wallet_code(frase)

    if wallet_code in ADMIN_WALLETS:
        return jsonify({"success": True, "wallet_code": wallet_code, "admin": True})
    else:
        return jsonify({"success": True, "wallet_code": wallet_code, "admin": False})

@app.route('/api/telegram/send-code', methods=['POST'])
def api_send_telegram_code():
    data = request.get_json(silent=True) or {}
    code = data.get("code")

    if not code:
        return jsonify({"success": False, "message": "Código ausente"}), 400

    return jsonify({"success": True})

# ============================================
# 🔥 FUNÇÕES AUXILIARES PARA O DASHBOARD
# ============================================

def get_next_day_7(start_date):
    """Retorna a próxima data dia 7 - Mantido para compatibilidade"""
    try:
        year = start_date.year
        month = start_date.month
        
        if start_date.day < 7:
            return datetime(year, month, 7).date()
        else:
            if month == 12:
                return datetime(year + 1, 1, 7).date()
            else:
                return datetime(year, month + 1, 7).date()
    except Exception as e:
        logger.error(f"❌ Erro ao calcular próximo dia 7: {e}")
        return (start_date + timedelta(days=7)).date()

def get_master_wallet_balance_usdt() -> float:
    """Retorna saldo REAL da carteira master em USDT"""
    try:
        wallets = load_wallets()
        total_balance_usdt = 0.0
        
        for wallet_code, wallet in wallets.items():
            if wallet_code in MASTER_WALLET_CODES:
                balance = max(0.0, wallet.get('balanceUSDT', 0.0))
                total_balance_usdt += balance
        
        return total_balance_usdt
        
    except Exception as e:
        logger.error(f"❌ Erro ao obter saldo master: {e}")
        return 0.0

def audit_master_balance_usdt(current_balance_usdt: float):
    """Auditoria do saldo master - verifica consistência"""
    try:
        real_balance_usdt = get_master_balance_from_confirmed_transactions_usdt()
        
        if abs(current_balance_usdt - real_balance_usdt) > 0.01:
            logger.warning(f"⚠️ AUDITORIA: Saldo divergente!")
            logger.warning(f"   Saldo atual: {current_balance_usdt:.2f} USDT")
            logger.warning(f"   Saldo real (confirmado): {real_balance_usdt:.2f} USDT")
            logger.warning(f"   Diferença: {current_balance_usdt - real_balance_usdt:.2f} USDT")
            
            audit_event(
                action="MASTER_BALANCE_AUDIT_FAIL",
                success=False,
                details=f"Saldo master divergente: Atual {current_balance_usdt:.2f} USDT vs Real {real_balance_usdt:.2f} USDT",
                extra={
                    "current_balance_usdt": current_balance_usdt,
                    "real_balance_usdt": real_balance_usdt,
                    "difference_usdt": current_balance_usdt - real_balance_usdt
                }
            )
        else:
            logger.info(f"✅ AUDITORIA: Saldo master consistente {current_balance_usdt:.2f} USDT")
            
    except Exception as e:
        logger.error(f"❌ Erro na auditoria do saldo: {e}")

@app.route('/api/wallet/dashboard', methods=['GET'])
def api_wallet_dashboard():
    """Dashboard CORRIGIDO: Mostra informações CORRETAS em USDT"""
    try:
        wallet_code = request.args.get('code', '').strip().upper()

        if not wallet_code or not wallet_code.startswith('ROBO-'):
            return jsonify({
                "success": False,
                "message": "Código de carteira inválido"
            }), 400

        participations = load_participations()
        
        if wallet_code not in participations:
            return jsonify({
                "success": False,
                "message": "Participação não encontrada"
            }), 404

        participation = participations[wallet_code]
        
        total_deposited_usdt = participation.get("total_deposited_usdt", 0.0)
        virtual_balance_usdt = participation.get("virtual_balance_usdt", 0.0)
        profit_accumulated_usdt = participation.get("profit_accumulated_usdt", 0.0)
        total_withdrawn_usdt = participation.get("total_withdrawn_usdt", 0.0)
        
        if virtual_balance_usdt < total_deposited_usdt:
            logger.error(f"🚨 BUG DETECTADO: {wallet_code} | virtual_balance_usdt ({virtual_balance_usdt}) < total_deposited_usdt ({total_deposited_usdt})")
            logger.error(f"   🔧 CORRIGINDO: virtual_balance_usdt = total_deposited_usdt")
            virtual_balance_usdt = total_deposited_usdt
            participation["virtual_balance_usdt"] = total_deposited_usdt
            participation["updated_at"] = datetime.now().isoformat()
            save_participations(participations)
        
        available_for_investment_usdt = max(0.0, virtual_balance_usdt - total_deposited_usdt)
        
        if virtual_balance_usdt == 0 and total_deposited_usdt > 0:
            logger.warning(f"⚠️ PROBLEMA: {wallet_code} tem depositado {total_deposited_usdt:.2f} USDT mas saldo ZERO")
            logger.warning(f"   🔧 CORRIGINDO AUTOMATICAMENTE")
            virtual_balance_usdt = total_deposited_usdt
            participation["virtual_balance_usdt"] = total_deposited_usdt
            save_participations(participations)
            available_for_investment_usdt = 0.0
        
        master_balance_usdt = get_master_wallet_balance_usdt()
        share_percent = 0.0
        if master_balance_usdt > 0 and total_deposited_usdt > 0:
            share_percent = (total_deposited_usdt / master_balance_usdt) * 100
        
        usdt_brl = get_current_usdt_brl()
        btc_usdt = get_current_btc_usdt()

        return jsonify({
            "success": True,
            "dashboard_data": {
                "wallet_code": wallet_code,
                "name": f'Carteira {wallet_code}',
                "total_deposited_usdt": float(total_deposited_usdt),
                "total_deposited_brl": round(float(total_deposited_usdt) * usdt_brl, 2),
                "virtual_balance_usdt": float(virtual_balance_usdt),
                "virtual_balance_brl": round(float(virtual_balance_usdt) * usdt_brl, 2),
                "profit_accumulated_usdt": float(profit_accumulated_usdt),
                "profit_accumulated_brl": round(float(profit_accumulated_usdt) * usdt_brl, 2),
                "available_for_investment_usdt": float(available_for_investment_usdt),
                "available_for_investment_brl": round(float(available_for_investment_usdt) * usdt_brl, 2),
                "total_withdrawn_usdt": float(total_withdrawn_usdt),
                "total_withdrawn_brl": round(float(total_withdrawn_usdt) * usdt_brl, 2),
                "share_percent": round(share_percent, 4),
                "display_fields": {
                    "saldo_disponivel_usdt": float(available_for_investment_usdt),
                    "saldo_disponivel_brl": round(float(available_for_investment_usdt) * usdt_brl, 2),
                    "principal_garantido_usdt": float(total_deposited_usdt),
                    "principal_garantido_brl": round(float(total_deposited_usdt) * usdt_brl, 2),
                    "saldo_total_usdt": float(virtual_balance_usdt),
                    "saldo_total_brl": round(float(virtual_balance_usdt) * usdt_brl, 2),
                    "lucro_acumulado_usdt": float(profit_accumulated_usdt),
                    "lucro_acumulado_brl": round(float(profit_accumulated_usdt) * usdt_brl, 2),
                    "pode_sacar_usdt": float(available_for_investment_usdt),
                    "pode_sacar_brl": round(float(available_for_investment_usdt) * usdt_brl, 2)
                },
                "status": participation.get("status", "active"),
                "master_wallet": participation.get("master_wallet", "MASTER-001"),
                "joined_at": participation.get("created_at", datetime.now().isoformat()),
                "updated_at": participation.get("updated_at", datetime.now().isoformat()),
                "balanceBTC": float(virtual_balance_usdt) / btc_usdt if btc_usdt > 0 else 0.0,
                "currency": "USDT",
                "explicacao": {
                    "total_deposited_usdt": "Dinheiro que o usuário depositou (PRINCIPAL) - NUNCA some",
                    "virtual_balance_usdt": "Principal + lucros - perdas (DEVE SER >= total_deposited_usdt)",
                    "available_for_investment_usdt": "virtual_balance_usdt - total_deposited_usdt (lucro disponível)",
                    "profit_accumulated_usdt": "Só o lucro, sem contar o principal",
                    "regra_ouro": "virtual_balance_usdt NUNCA pode ser menor que total_deposited_usdt"
                },
                "audit": {
                    "total_deposited_usdt": float(total_deposited_usdt),
                    "virtual_balance_usdt": float(virtual_balance_usdt),
                    "difference_usdt": float(virtual_balance_usdt - total_deposited_usdt),
                    "is_consistent": virtual_balance_usdt >= total_deposited_usdt,
                    "timestamp": datetime.now().isoformat(),
                    "currency": "USDT"
                }
            }
        })

    except Exception as e:
        logger.error(f"💥 ERRO no dashboard: {str(e)}")
        return jsonify({
            "success": False,
            "message": "Erro interno no servidor"
        }), 500

@app.route('/robo/admin/master-wallet', methods=['GET'])
def get_master_wallet():
    """Retorna dados da carteira master (apenas admin) em USDT"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        master_data = load_master_wallet_data()
        
        wallets = load_wallets()
        master_wallets = []
        total_balance_usdt = 0
        usdt_brl = get_current_usdt_brl()
        
        for master_code in MASTER_WALLET_CODES:
            if master_code in wallets:
                wallet = wallets[master_code]
                master_wallets.append({
                    "code": master_code,
                    "balanceUSDT": wallet.get("balanceUSDT", 0),
                    "balanceBRL": round(wallet.get("balanceUSDT", 0) * usdt_brl, 2),
                    "balanceBTC": wallet.get("balanceBTC", 0),
                    "totalProfitUSDT": wallet.get("totalProfitUSDT", 0),
                    "updated_at": wallet.get("updated_at", "")
                })
                total_balance_usdt += wallet.get("balanceUSDT", 0)
        
        return jsonify({
            "success": True,
            "master_data": master_data,
            "master_wallets": master_wallets,
            "total_balance_usdt": total_balance_usdt,
            "total_balance_brl": round(total_balance_usdt * usdt_brl, 2),
            "distribution_rules": PROFIT_DISTRIBUTION,
            "currency": "USDT"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /robo/admin/master-wallet: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500

def load_cashbox_data() -> Dict:
    """Carrega dados do caixa da empresa em USDT"""
    default_cashbox = {
        "balance_usdt": 0.0,
        "locked": True,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "transactions": [],
        "description": "Caixa da empresa - Para despesas e emergências",
        "rules": [
            "⚠️ NÃO ENTRA EM TRADE",
            "⚠️ NÃO ENTRA EM CÁLCULO DE LUCRO",
            "⚠️ NÃO altera carteiras de usuários",
            "✅ Serve para: pagar despesas, cobrir taxas, saques manuais, ajustes",
            "✅ Só pode ser alterado por admin via endpoints específicos",
            "🔒 SEMPRE bloqueado por padrão",
            "💰 CURRENCY: USDT"
        ]
    }
    
    if not os.path.exists(CASHBOX_FILE):
        logger.info("📁 Criando arquivo cashbox.json (saldo inicial: 0.00 USDT)...")
        save_cashbox_data(default_cashbox)
        return default_cashbox
    
    try:
        with open(CASHBOX_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if "balance_usdt" not in data:
                if "balance" in data:
                    data["balance_usdt"] = data["balance"]
                else:
                    data["balance_usdt"] = 0.0
            return data
    except Exception as e:
        logger.error(f"❌ Erro ao carregar cashbox: {e}")
        return default_cashbox

@app.route('/robo/admin/cashbox', methods=['GET'])
def get_cashbox_info():
    """Retorna dados do cashbox (apenas admin) em USDT"""
    try:
        if not admin_auth_ok(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        cashbox_data = load_cashbox_data()
        usdt_brl = get_current_usdt_brl()
        
        return jsonify({
            "success": True,
            "cashbox": {
                "balance_usdt": cashbox_data.get("balance_usdt", 0.0),
                "balance_brl": round(cashbox_data.get("balance_usdt", 0.0) * usdt_brl, 2),
                "locked": cashbox_data.get("locked", True),
                "created_at": cashbox_data.get("created_at"),
                "updated_at": cashbox_data.get("updated_at"),
                "transactions": cashbox_data.get("transactions", []),
                "description": cashbox_data.get("description"),
                "rules": cashbox_data.get("rules", [])
            },
            "warning": "⚠️ CASHBOX SEMPRE BLOQUEADO - USE APENAS PARA EMERGÊNCIAS",
            "currency": "USDT"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /robo/admin/cashbox: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500

def save_cashbox_data(cashbox_data: Dict) -> bool:
    """Salva dados do cashbox"""
    try:
        cashbox_data["updated_at"] = datetime.now().isoformat()
        with open(CASHBOX_FILE, 'w', encoding='utf-8') as f:
            json.dump(cashbox_data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"❌ Erro ao salvar cashbox: {e}")
        return False

@app.route('/robo/admin/distribute-profit', methods=['POST'])
def admin_distribute_profit():
    """Endpoint para distribuir lucro manualmente (apenas admin) em USDT"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        data = request.get_json(silent=True) or {}
        profit_amount_usdt = float(data.get('amount_usdt', 0))
        description = data.get('description', 'Lucro manual do trading')
        
        if profit_amount_usdt <= 0:
            return jsonify({
                "success": False,
                "message": "Valor deve ser maior que zero"
            }), 400
        
        result = process_profit_distribution(profit_amount_usdt, description)
        
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
        
    except Exception as e:
        logger.error(f"💥 Erro em /robo/admin/distribute-profit: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500

@app.route('/robo/admin/cashbox/deposit', methods=['POST'])
def cashbox_deposit():
    """Depósito no cashbox (apenas admin SUPER) em USDT"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        data = request.get_json(silent=True) or {}
        amount_usdt = float(data.get('amount_usdt', 0))
        description = data.get('description', 'Depósito manual')
        
        if amount_usdt <= 0:
            return jsonify({
                "success": False,
                "message": "Valor deve ser maior que zero"
            }), 400
        
        cashbox = load_cashbox_data()
        
        if cashbox.get("locked", True):
            return jsonify({
                "success": False,
                "message": "Cashbox está bloqueado. Apenas SUPER ADMIN pode desbloquear."
            }), 403
        
        cashbox["balance_usdt"] += amount_usdt
        
        tx_id = f"CASHBOX-DEP-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        transaction = {
            "id": tx_id,
            "type": "deposit",
            "amount_usdt": amount_usdt,
            "description": description,
            "timestamp": datetime.now().isoformat(),
            "balance_before_usdt": cashbox["balance_usdt"] - amount_usdt,
            "balance_after_usdt": cashbox["balance_usdt"],
            "admin": "admin",
            "currency": "USDT"
        }
        
        cashbox["transactions"].append(transaction)
        
        save_cashbox_data(cashbox)
        
        audit_event(
            action="CASHBOX_DEPOSIT",
            success=True,
            details=f"Depósito no cashbox: {amount_usdt:.2f} USDT",
            extra={
                "amount_usdt": amount_usdt,
                "description": description,
                "new_balance_usdt": cashbox["balance_usdt"],
                "tx_id": tx_id,
                "currency": "USDT"
            }
        )
        
        logger.warning(f"⚠️  CASHBOX MODIFICADO: Depósito de {amount_usdt:.2f} USDT")
        
        return jsonify({
            "success": True,
            "message": f"Depositado {amount_usdt:.2f} USDT no cashbox",
            "transaction_id": tx_id,
            "new_balance_usdt": cashbox["balance_usdt"],
            "currency": "USDT"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em cashbox deposit: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500

@app.route('/robo/admin/cashbox/withdraw', methods=['POST'])
def cashbox_withdraw():
    """Saque do cashbox (apenas admin SUPER) em USDT"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        data = request.get_json(silent=True) or {}
        amount_usdt = float(data.get('amount_usdt', 0))
        description = data.get('description', 'Saque manual')
        
        if amount_usdt <= 0:
            return jsonify({
                "success": False,
                "message": "Valor deve ser maior que zero"
            }), 400
        
        cashbox = load_cashbox_data()
        
        if cashbox.get("locked", True):
            return jsonify({
                "success": False,
                "message": "Cashbox está bloqueado. Apenas SUPER ADMIN pode desbloquear."
            }), 403
        
        if amount_usdt > cashbox["balance_usdt"]:
            return jsonify({
                "success": False,
                "message": f"Saldo insuficiente. Disponível: {cashbox['balance_usdt']:.2f} USDT"
            }), 400
        
        cashbox["balance_usdt"] -= amount_usdt
        
        tx_id = f"CASHBOX-WTH-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        transaction = {
            "id": tx_id,
            "type": "withdraw",
            "amount_usdt": amount_usdt,
            "description": description,
            "timestamp": datetime.now().isoformat(),
            "balance_before_usdt": cashbox["balance_usdt"] + amount_usdt,
            "balance_after_usdt": cashbox["balance_usdt"],
            "admin": "admin",
            "currency": "USDT"
        }
        
        cashbox["transactions"].append(transaction)
        
        save_cashbox_data(cashbox)
        
        audit_event(
            action="CASHBOX_WITHDRAW",
            success=True,
            details=f"Saque do cashbox: {amount_usdt:.2f} USDT",
            extra={
                "amount_usdt": amount_usdt,
                "description": description,
                "new_balance_usdt": cashbox["balance_usdt"],
                "tx_id": tx_id,
                "currency": "USDT"
            }
        )
        
        logger.warning(f"⚠️  CASHBOX MODIFICADO: Saque de {amount_usdt:.2f} USDT")
        
        return jsonify({
            "success": True,
            "message": f"Sacado {amount_usdt:.2f} USDT do cashbox",
            "transaction_id": tx_id,
            "new_balance_usdt": cashbox["balance_usdt"],
            "currency": "USDT"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em cashbox withdraw: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500

# ============================================================
# GESTÃO DE CARTEIRAS — Criar / Editar / Listar participações
# ============================================================

@app.route('/robo/admin/participations', methods=['GET'])
def admin_list_participations():
    """Lista todas as participações com dados completos (apenas admin)"""
    try:
        if not verify_admin_key(request):
            return jsonify({"success": False, "message": "Acesso não autorizado"}), 403

        participations = load_participations()
        usdt_brl = get_current_usdt_brl()

        total_pool_usdt = sum(
            float(p.get("virtual_balance_usdt", 0))
            for p in participations.values()
            if p.get("status") == "active"
        )

        result = []
        for code, p in participations.items():
            bal = float(p.get("virtual_balance_usdt", 0))
            share_real = round((bal / total_pool_usdt * 100), 4) if total_pool_usdt > 0 else 0.0
            result.append({
                "wallet_code": code,
                "display_name": p.get("display_name", code),
                "tipo": p.get("tipo", "user"),
                "status": p.get("status", "active"),
                "active": p.get("active", True),
                "virtual_balance_usdt": bal,
                "virtual_balance_brl": round(bal * usdt_brl, 2),
                "total_deposited_usdt": float(p.get("total_deposited_usdt", 0)),
                "profit_accumulated_usdt": float(p.get("profit_accumulated_usdt", 0)),
                "total_withdrawn_usdt": float(p.get("total_withdrawn_usdt", 0)),
                "share_percent_real": share_real,
                "share_percent_stored": float(p.get("share_percent", 0)),
                "is_company_wallet": p.get("is_company_wallet", False),
                "fee_exempt": p.get("fee_exempt", False),
                "created_at": p.get("created_at", ""),
                "updated_at": p.get("updated_at", ""),
                "notes": p.get("notes", ""),
                "has_password": bool(p.get("access_password")),
            })

        result.sort(key=lambda x: x["virtual_balance_usdt"], reverse=True)

        return jsonify({
            "success": True,
            "participations": result,
            "count": len(result),
            "total_pool_usdt": round(total_pool_usdt, 4),
            "total_pool_brl": round(total_pool_usdt * usdt_brl, 2),
        })
    except Exception as e:
        logger.error(f"💥 Erro em admin/participations GET: {e}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500


@app.route('/robo/admin/participations/create', methods=['POST'])
def admin_create_participation():
    """
    Cria uma nova carteira participante no pool.
    Body JSON:
      display_name     : str  (ex: "MASTER-COYOTE Capital")
      initial_balance  : float (USDT já presentes no pool, pode ser 0)
      tipo             : str  "user" | "master_participant" | "company"
      is_company_wallet: bool (default false)
      fee_exempt       : bool (default false)
      notes            : str
    Retorna: wallet_code gerado + access_password
    """
    try:
        if not verify_admin_key(request):
            return jsonify({"success": False, "message": "Acesso não autorizado"}), 403

        data = request.get_json(silent=True) or {}
        display_name      = data.get("display_name", "").strip()
        initial_balance   = float(data.get("initial_balance", 0))
        tipo              = data.get("tipo", "user")
        is_company_wallet = bool(data.get("is_company_wallet", False))
        fee_exempt        = bool(data.get("fee_exempt", False))
        notes             = data.get("notes", "").strip()

        if not display_name:
            return jsonify({"success": False, "message": "display_name é obrigatório"}), 400

        # Gera código único
        rand_hex   = secrets.token_hex(3).upper()
        prefix     = "ROBO-MASTER" if is_company_wallet else "ROBO"
        wallet_code = f"{prefix}-{rand_hex}"
        password    = ("MASTER-" if is_company_wallet else "USR-") + secrets.token_hex(5).upper()

        participations = load_participations()

        # Evita colisão (improvável mas seguro)
        while wallet_code in participations:
            rand_hex    = secrets.token_hex(3).upper()
            wallet_code = f"{prefix}-{rand_hex}"

        now = datetime.now().isoformat()
        participations[wallet_code] = {
            "wallet_code": wallet_code,
            "user_code": wallet_code,
            "display_name": display_name,
            "tipo": tipo,
            "active": True,
            "status": "active",
            "share": 1,
            "virtual_balance_usdt": initial_balance,
            "total_deposited_usdt": initial_balance,
            "profit_accumulated_usdt": 0.0,
            "total_withdrawn_usdt": 0.0,
            "share_percent": 0.0,
            "created_at": now,
            "updated_at": now,
            "reconciled_at": now,
            "reconciled_from": "admin_manual_creation",
            "last_profit_distribution": now,
            "master_wallet": "MASTER-COYOTE",
            "bot_active": True,
            "aporte_usdt": initial_balance,
            "risco_por_trade": 0.05,
            "max_posicoes_abertas": 5 if is_company_wallet else 2,
            "saldo_disponivel_usdt": initial_balance,
            "saldo_em_posicoes_usdt": 0.0,
            "valor_minimo_entrada_usdt": 10.0,
            "user_positions": [],
            "user_closed_trades": [],
            "perda_diaria_limite_usdt": 0.0,
            "perda_diaria_acumulada_usdt": 0.0,
            "perda_diaria_data": "",
            "scanner_config": {
                "max_posicoes": 5 if is_company_wallet else 2,
                "valor_entrada": 50.0 if is_company_wallet else 15.0,
                "score_minimo": 8,
            },
            "bot_status_updated_at": now,
            "is_company_wallet": is_company_wallet,
            "fee_exempt": fee_exempt,
            "linked_master": "MASTER-COYOTE" if is_company_wallet else None,
            "access_password": password,
            "notes": notes,
        }

        save_participations(participations)
        recalculate_all_shares_usdt()

        audit_event(
            action="PARTICIPATION_CREATED_BY_ADMIN",
            success=True,
            details=f"Carteira {wallet_code} ({display_name}) criada pelo admin",
            extra={"wallet_code": wallet_code, "tipo": tipo, "initial_balance": initial_balance},
        )

        logger.info(f"✅ Carteira criada pelo admin: {wallet_code} ({display_name})")

        return jsonify({
            "success": True,
            "message": f"Carteira {wallet_code} criada com sucesso",
            "wallet_code": wallet_code,
            "access_password": password,
            "display_name": display_name,
            "initial_balance_usdt": initial_balance,
            "tipo": tipo,
        })

    except Exception as e:
        logger.error(f"💥 Erro em admin/participations/create: {e}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500


@app.route('/robo/admin/participations/update', methods=['POST'])
def admin_update_participation():
    """
    Edita campos de uma participação existente.
    Body JSON:
      wallet_code      : str  (obrigatório)
      display_name     : str  (opcional)
      status           : "active" | "suspended" | "closed"
      active           : bool
      notes            : str
      fee_exempt       : bool
      is_company_wallet: bool
      adjust_balance   : float  (delta USDT — positivo credita, negativo debita)
      adjust_reason    : str
    """
    try:
        if not verify_admin_key(request):
            return jsonify({"success": False, "message": "Acesso não autorizado"}), 403

        data        = request.get_json(silent=True) or {}
        wallet_code = data.get("wallet_code", "").strip().upper()

        if not wallet_code:
            return jsonify({"success": False, "message": "wallet_code é obrigatório"}), 400

        participations = load_participations()

        if wallet_code not in participations:
            return jsonify({"success": False, "message": f"Carteira {wallet_code} não encontrada"}), 404

        p   = participations[wallet_code]
        now = datetime.now().isoformat()

        if "display_name" in data:
            p["display_name"] = data["display_name"].strip()
        if "status" in data:
            p["status"] = data["status"]
        if "active" in data:
            p["active"] = bool(data["active"])
        if "notes" in data:
            p["notes"] = data["notes"].strip()
        if "fee_exempt" in data:
            p["fee_exempt"] = bool(data["fee_exempt"])
        if "is_company_wallet" in data:
            p["is_company_wallet"] = bool(data["is_company_wallet"])
        if "tipo" in data:
            p["tipo"] = data["tipo"]

        # Chave PIX e endereço BTC registrados (admin pode editar/corrigir)
        if "registered_pix_key" in data:
            val = (data["registered_pix_key"] or "").strip()
            p["registered_pix_key"] = val if val else p.get("registered_pix_key")
        if "registered_btc_address" in data:
            val = (data["registered_btc_address"] or "").strip()
            p["registered_btc_address"] = val if val else p.get("registered_btc_address")

        # Também atualiza wallets.json para que a validação de saque funcione
        wallets_data = load_wallets()
        if wallet_code in wallets_data:
            if "registered_pix_key" in data and data["registered_pix_key"]:
                wallets_data[wallet_code]["registered_pix_key"] = data["registered_pix_key"].strip()
            if "registered_btc_address" in data and data["registered_btc_address"]:
                wallets_data[wallet_code]["registered_btc_address"] = data["registered_btc_address"].strip()
            wallets_data[wallet_code]["updated_at"] = datetime.now().isoformat()
            save_wallets(wallets_data)

        # Ajuste manual de saldo
        delta = float(data.get("adjust_balance", 0))
        if delta != 0:
            reason           = data.get("adjust_reason", "Ajuste manual pelo admin")
            saldo_antes      = float(p.get("virtual_balance_usdt", 0))
            novo_saldo       = max(0.0, round(saldo_antes + delta, 8))
            p["virtual_balance_usdt"]    = novo_saldo
            p["total_deposited_usdt"]    = float(p.get("total_deposited_usdt", 0)) + (delta if delta > 0 else 0)
            p["saldo_disponivel_usdt"]   = max(0.0, round(float(p.get("saldo_disponivel_usdt", saldo_antes)) + delta, 8))
            p["updated_at"]              = now

            transactions = load_transactions()
            transactions.append({
                "id":                          f"ADJ-{wallet_code}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "wallet_code":                 wallet_code,
                "user_code":                   wallet_code,
                "type":                        "admin_balance_adjustment",
                "amount_usdt":                 delta,
                "date":                        now,
                "status":                      "completed",
                "description":                 reason,
                "virtual_balance_before_usdt": saldo_antes,
                "virtual_balance_after_usdt":  novo_saldo,
                "currency":                    "USDT",
            })
            save_transactions(transactions)

        p["updated_at"] = now
        save_participations(participations)
        recalculate_all_shares_usdt()

        audit_event(
            action="PARTICIPATION_UPDATED_BY_ADMIN",
            success=True,
            details=f"Carteira {wallet_code} atualizada pelo admin",
            extra={"wallet_code": wallet_code, "changes": data},
        )

        return jsonify({
            "success": True,
            "message": f"Carteira {wallet_code} atualizada",
            "wallet_code": wallet_code,
            "virtual_balance_usdt": p["virtual_balance_usdt"],
        })

    except Exception as e:
        logger.error(f"💥 Erro em admin/participations/update: {e}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500


@app.route('/robo/admin/participations/credit', methods=['POST'])
def admin_credit_master_participation():
    """
    Crédito direto de USDT da master_wallet para a carteira participante da empresa.
    Isso sincroniza o saldo do master_wallet.json com a participação no pool.
    Body JSON:
      wallet_code : str   (carteira company — ex: ROBO-MASTER-XXXXXX)
      amount_usdt : float (valor a creditar no pool)
      reason      : str
    """
    try:
        if not verify_admin_key(request):
            return jsonify({"success": False, "message": "Acesso não autorizado"}), 403

        data        = request.get_json(silent=True) or {}
        wallet_code = data.get("wallet_code", "").strip().upper()
        amount_usdt = float(data.get("amount_usdt", 0))
        reason      = data.get("reason", "Aporte master no pool").strip()

        if not wallet_code:
            return jsonify({"success": False, "message": "wallet_code é obrigatório"}), 400
        if amount_usdt <= 0:
            return jsonify({"success": False, "message": "amount_usdt deve ser maior que zero"}), 400

        participations = load_participations()
        if wallet_code not in participations:
            return jsonify({"success": False, "message": f"Carteira {wallet_code} não encontrada"}), 404

        p = participations[wallet_code]
        if not p.get("is_company_wallet"):
            return jsonify({"success": False, "message": "Esta rota é exclusiva para carteiras da empresa"}), 400

        now         = datetime.now().isoformat()
        saldo_antes = float(p.get("virtual_balance_usdt", 0))
        novo_saldo  = round(saldo_antes + amount_usdt, 8)

        p["virtual_balance_usdt"]    = novo_saldo
        p["total_deposited_usdt"]    = float(p.get("total_deposited_usdt", 0)) + amount_usdt
        p["aporte_usdt"]             = float(p.get("aporte_usdt", 0)) + amount_usdt
        p["saldo_disponivel_usdt"]   = novo_saldo
        p["updated_at"]              = now
        p["reconciled_at"]           = now
        p["reconciled_from"]         = "admin_master_credit"

        save_participations(participations)

        transactions = load_transactions()
        transactions.append({
            "id":                          f"MASTER-CREDIT-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "wallet_code":                 wallet_code,
            "user_code":                   wallet_code,
            "type":                        "master_pool_credit",
            "amount_usdt":                 amount_usdt,
            "date":                        now,
            "status":                      "completed",
            "description":                 reason,
            "virtual_balance_before_usdt": saldo_antes,
            "virtual_balance_after_usdt":  novo_saldo,
            "currency":                    "USDT",
            "source":                      "master_wallet_to_pool",
        })
        save_transactions(transactions)
        recalculate_all_shares_usdt()

        audit_event(
            action="MASTER_POOL_CREDIT",
            success=True,
            details=f"Master creditou {amount_usdt:.2f} USDT no pool via {wallet_code}",
            extra={"wallet_code": wallet_code, "amount_usdt": amount_usdt, "reason": reason},
        )

        return jsonify({
            "success": True,
            "message": f"✅ {amount_usdt:.2f} USDT creditados no pool via {wallet_code}",
            "wallet_code": wallet_code,
            "amount_usdt": amount_usdt,
            "balance_before_usdt": saldo_antes,
            "balance_after_usdt": novo_saldo,
        })

    except Exception as e:
        logger.error(f"💥 Erro em admin/participations/credit: {e}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500

# ============================================================
# FIM — Gestão de Carteiras
# ============================================================

@app.route('/robo/admin/profit-history', methods=['GET'])
def get_profit_history():
    """Histórico de distribuição de lucros (apenas admin) em USDT"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        distributions = []
        if os.path.exists(PROFIT_DISTRIBUTION_FILE):
            with open(PROFIT_DISTRIBUTION_FILE, 'r', encoding='utf-8') as f:
                distributions = json.load(f)
        
        distributions.sort(key=lambda x: x.get('distribution_date', ''), reverse=True)
        
        total_profit_usdt = sum(d.get('total_profit_usdt', d.get('total_profit', 0)) for d in distributions)
        total_master_usdt = sum(d.get('master_share_usdt', d.get('master_share', 0)) for d in distributions)
        total_users_usdt = sum(d.get('users_share_usdt', d.get('users_share', 0)) for d in distributions)
        
        return jsonify({
            "success": True,
            "distributions": distributions[:50],
            "stats": {
                "total_distributions": len(distributions),
                "total_profit_distributed_usdt": total_profit_usdt,
                "total_to_master_usdt": total_master_usdt,
                "total_to_users_usdt": total_users_usdt,
                "master_percentage": (total_master_usdt / total_profit_usdt * 100) if total_profit_usdt > 0 else 0,
                "users_percentage": (total_users_usdt / total_profit_usdt * 100) if total_profit_usdt > 0 else 0
            },
            "currency": "USDT"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /robo/admin/profit-history: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500

@app.route('/robo/admin/process-cycle', methods=['POST'])
def api_process_monthly_cycle():
    """Endpoint para forçar processamento do ciclo mensal (apenas admin)"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False, 
                "message": "Acesso não autorizado"
            }), 403
        
        force = request.args.get('force', '').lower() == 'true'
        
        if not is_day_7() and not force:
            return jsonify({
                "success": False,
                "message": "Hoje não é dia 7. Use ?force=true para processar mesmo assim.",
                "is_day_7": False
            }), 400
        
        result = process_monthly_cycle()
        
        if result.get("success"):
            return jsonify(result)
        else:
            status_code = 400 if not result.get("processed") else 500
            return jsonify(result), status_code
            
    except Exception as e:
        logger.error(f"💥 Erro em /robo/admin/process-cycle: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/admin/cycle-status', methods=['GET'])
def api_cycle_status():
    """Endpoint para verificar status do ciclo (apenas admin)"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False, 
                "message": "Acesso não autorizado"
            }), 403
        
        status = get_cycle_status()
        return jsonify(status)
        
    except Exception as e:
        logger.error(f"💥 Erro em /robo/admin/cycle-status: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

# ============================================
# 🔥 ENDPOINTS PARA DEPÓSITOS BTC COM CONVERSÃO USDT
# ============================================

@app.route('/robo/deposit-address', methods=['GET'])
def get_deposit_address():
    """Gera endereço de depósito BTC"""
    wallet_code = request.args.get('wallet_code')
    asset = request.args.get('asset', 'btc').lower()
    
    MASTER_ADDRESSES = {
        'btc': '16659Zgp91u2ggWPUm3A7ULhrurExidvYu'
    }
    
    if asset not in MASTER_ADDRESSES:
        return jsonify({
            'success': False,
            'message': f'Asset {asset} não suportado'
        }), 400
    
    return jsonify({
        'success': True,
        'address': MASTER_ADDRESSES[asset],
        'asset': asset,
        'message': f'Use este endereço para depositar {asset.upper()} (será convertido para USDT)'
    })

@app.route('/robo/deposit-status', methods=['GET'])
def check_deposit():
    """Verifica status do depósito"""
    wallet_code = request.args.get('wallet_code')
    asset = request.args.get('asset', 'btc').lower()

    transactions = load_transactions()

    # 🔎 Buscar depósito confirmado mais recente
    confirmed = [
        t for t in transactions
        if t.get('wallet_code') == wallet_code
        and t.get('status') == 'confirmado'
        and t.get('method', '').lower() == asset
    ]

    if confirmed:
        tx = confirmed[-1]
        return jsonify({
            'success': True,
            'confirmed': True,
            'pending': False,
            'transaction_id': tx.get('id'),
            'amount_usdt': tx.get('credit_amount_usdt', 0),
            'currency': 'USDT',
            'tx_hash': tx.get('tx_hash')
        })

    # 🔎 Verificar se ainda está pendente
    pending = [
        t for t in transactions
        if t.get('wallet_code') == wallet_code
        and t.get('status') == 'pendente'
        and t.get('method', '').lower() == asset
    ]

    if pending:
        return jsonify({
            'success': True,
            'confirmed': False,
            'pending': True,
            'confirmations': 1
        })

    return jsonify({
        'success': True,
        'confirmed': False,
        'pending': False
    })

@app.route('/robo/admin/check-deposits', methods=['POST'])
def admin_check_deposits_manual():
    """Endpoint para admin verificar depósitos pendentes"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        result = check_pending_deposits_job()
        
        if "error" in result:
            return jsonify({
                "success": False,
                "message": f"Erro na verificação: {result['error']}"
            }), 500
        
        return jsonify({
            "success": True,
            "message": f"Verificação concluída: {result.get('confirmed', 0)} depósitos confirmados",
            "result": result
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /robo/admin/check-deposits: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/admin/binance-test', methods=['GET'])
def admin_binance_test():
    """Testa conexão com Binance"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        status = {
            "has_api_key": bool(BINANCE_API_KEY),
            "has_api_secret": bool(BINANCE_API_SECRET),
            "configured": bool(BINANCE_API_KEY and BINANCE_API_SECRET),
            "connected": False,
            "error": None
        }
        
        if status["configured"]:
            try:
                from binance.client import Client
                client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
                
                server_time = client.get_server_time()
                status["connected"] = True
                status["server_time"] = server_time
                
                account_info = client.get_account()
                status["can_trade"] = "SPOT" in account_info.get("permissions", [])
                status["account_type"] = account_info.get("accountType", "spot")
                
            except Exception as e:
                status["error"] = str(e)
                status["connected"] = False
        
        return jsonify({
            "success": True,
            "status": status,
            "message": "Binance: " + ("Conectada" if status["connected"] else "Não conectada")
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em binance-test: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/admin/audit-consistency', methods=['GET'])
def admin_audit_consistency():
    """Endpoint para auditoria de consistência"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        result = validate_system_consistency()
        
        if not result["success"] and result.get("issues"):
            logger.warning("🔄 Forçando correção de saldos...")
            
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"💥 Erro em audit-consistency: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/admin/financial-audit', methods=['GET'])
def financial_audit():
    """Endpoint para auditoria financeira e correção em USDT"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        validation = validate_financial_consistency()
        
        corrections_applied = []
        
        if not validation["valid"]:
            for inc in validation.get("inconsistencies", []):
                if inc.get("severity") == "CRITICAL":
                    
                    if inc["rule"] == "CONFIRMED_ZERO_BUT_MASTER_NOT_ZERO":
                        logger.warning("🔄 CORRIGINDO: Master balance deve ser ZERO")
                        
                        wallets = load_wallets()
                        for wallet_code in MASTER_WALLET_CODES:
                            if wallet_code in wallets:
                                wallets[wallet_code]["balanceUSDT"] = 0.0
                                wallets[wallet_code]["balanceBTC"] = 0.0
                                wallets[wallet_code]["totalDepositedUSDT"] = 0.0
                                wallets[wallet_code]["updated_at"] = datetime.now().isoformat()
                        
                        save_wallets(wallets)
                        corrections_applied.append("RESET_MASTER_BALANCES_TO_ZERO")
                    
                    elif inc["rule"] == "MASTER_BALANCE_EXCEEDS_TOTAL_INVESTED":
                        logger.warning("🔄 CORRIGINDO: Ajustando master balance para total invested")
                        
                        real_balance_usdt = validation["real_values"]["master_balance_real_usdt"]
                        
                        wallets = load_wallets()
                        for wallet_code in MASTER_WALLET_CODES:
                            if wallet_code in wallets:
                                wallets[wallet_code]["balanceUSDT"] = real_balance_usdt / len(MASTER_WALLET_CODES)
                                wallets[wallet_code]["updated_at"] = datetime.now().isoformat()
                        
                        save_wallets(wallets)
                        corrections_applied.append("ADJUSTED_MASTER_BALANCE_TO_REAL")
        
        return jsonify({
            "success": True,
            "audit_result": validation,
            "corrections_applied": corrections_applied,
            "corrected_values": {
                "master_balance_usdt": get_master_balance_from_confirmed_transactions_usdt(),
                "total_invested_usdt": validation["real_values"]["total_invested_real_usdt"]
            },
            "message": f"Auditoria concluída. {'Correções aplicadas.' if corrections_applied else 'Sistema consistente.'}",
            "currency": "USDT"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em financial-audit: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/robo/admin/sync-binance", methods=["POST"])
def admin_sync_binance():
    admin_key = request.headers.get("X-ADMIN-KEY")

    if admin_key != ADMIN_SECRET_KEY:
        return jsonify({"success": False, "message": "Acesso negado"}), 403

    result = check_pending_deposits_job()

    return jsonify({
        "success": True,
        "message": "Sincronização com Binance executada",
        "checked": result.get("checked", 0),
        "confirmed": result.get("confirmed", 0),
        "server_time": datetime.now().isoformat()
    })

@app.route('/robo/admin/sync-deposits', methods=['POST'])
def admin_sync_deposits():
    """Sincroniza depósitos pendentes com a Binance"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        logger.info("🔄 Sincronizando depósitos pendentes com Binance...")
        
        result = check_pending_deposits_job()
        
        transactions = load_transactions()
        pending_count = len([
            t for t in transactions 
            if t.get('type') == 'deposit' and t.get('status') == 'pendente'
        ])
        
        return jsonify({
            "success": True,
            "message": "Sincronização concluída",
            "result": result,
            "statistics": {
                "deposits_pending": pending_count,
                "checked": result.get("checked", 0),
                "confirmed": result.get("confirmed", 0),
                "last_sync": datetime.now().isoformat()
            }
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em sync-deposits: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/robo/admin/binance-test", methods=["GET"])
def admin_binance_test_connection():
    """Testa conexão com a Binance"""
    try:
        admin_key = request.headers.get("X-ADMIN-KEY")
        if admin_key != ADMIN_SECRET_KEY:
            return jsonify({"success": False, "message": "Acesso negado"}), 403
        
        api_key = os.getenv("BINANCE_API_KEY", BINANCE_API_KEY)
        api_secret = os.getenv("BINANCE_API_SECRET", BINANCE_API_SECRET)
        
        if not api_key or not api_secret:
            return jsonify({
                "success": False,
                "message": "Chaves Binance não configuradas"
            }), 400
        
        try:
            from binance.client import Client
            client = Client(api_key, api_secret)
            
            account = client.get_account()
            
            return jsonify({
                "success": True,
                "status": {
                    "connected": True,
                    "account_type": account.get("accountType", "SPOT"),
                    "can_trade": account.get("canTrade", False),
                    "permissions": account.get("permissions", []),
                    "server_time": client.get_server_time()
                },
                "message": "✅ Binance conectada com sucesso"
            })
            
        except Exception as e:
            logger.error(f"❌ Erro na conexão Binance: {str(e)}")
            return jsonify({
                "success": False,
                "status": {
                    "connected": False,
                    "error": str(e)
                },
                "message": f"❌ Falha na conexão: {str(e)}"
            }), 400
            
    except Exception as e:
        logger.error(f"💥 Erro em binance-test: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/robo/admin/binance-balance", methods=["GET"])
def get_binance_balance_admin():
    """Endpoint simplificado - Binance controlada pelo robô"""
    try:
        admin_key = request.headers.get("X-ADMIN-KEY")
        if admin_key != ADMIN_SECRET_KEY:
            return jsonify({"success": False, "message": "Acesso não autorizado"}), 403
        
        return jsonify({
            "success": True,
            "connected": True,
            "message": "Saldo Binance controlado exclusivamente pelo robô trading",
            "note": "Consulta desativada por segurança",
            "balances": [],
            "totalInUSDT": 0,
            "totalInBRL": 0,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"💥 Erro: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500



@app.route("/robo/admin/pending-deposits", methods=['GET'])
def get_pending_deposits():
    """Lista todos os depósitos pendentes"""
    try:
        if not admin_auth_ok(request):
            logger.warning(f"❌ Acesso negado a pending-deposits")
            return jsonify({
                "success": False, 
                "message": "Acesso negado"
            }), 403
        
        transactions = load_transactions()
        
        pending_deposits = [
            {
                "id": t.get("id"),
                "wallet_code": t.get("wallet_code"),
                "amount_usdt": t.get("amount_usdt", t.get("value_usdt", 0)),
                "method": t.get("method"),
                "date": t.get("date"),
                "description": t.get("description"),
                "expected_crypto": t.get("expected_crypto", 0),
                "expected_amount_usdt": t.get("expected_amount_usdt", 0),
                "status": t.get("status", "pendente"),
                "currency": "USDT"
            }
            for t in transactions 
            if t.get("type") == "deposit" and t.get("status") == "pendente"
        ]
        
        pending_deposits.sort(key=lambda x: x.get("date", ""))
        
        logger.info(f"📋 Pending deposits encontrados: {len(pending_deposits)}")
        
        return jsonify({
            "success": True,
            "pending_deposits": pending_deposits,
            "count": len(pending_deposits),
            "total_pending_value_usdt": sum(d.get("amount_usdt", 0) for d in pending_deposits),
            "server_time": datetime.now().isoformat(),
            "currency": "USDT"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em pending-deposits: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500





@app.route('/robo/profit', methods=['POST'])
def register_trade_profit():
    """Única fonte de lucro do sistema - Em USDT"""
    try:
        auth_ok, message = require_admin_auth()
        if not auth_ok:
            return jsonify({
                "success": False,
                "message": message
            }), 403
        
        data = request.get_json(silent=True) or {}
        
        trade_id = data.get("trade_id", "").strip()
        pnl_usdt = float(data.get("pnl_usdt", 0))
        timestamp = data.get("timestamp", datetime.now().isoformat())
        description = data.get("description", f"Trade {trade_id}")
        
        if not trade_id:
            return jsonify({
                "success": False,
                "message": "trade_id é obrigatório"
            }), 400
        
        logger.info(f"💰 Recebendo lucro do trade: {trade_id} | PnL: {pnl_usdt:.2f} USDT")
        
        transactions = load_transactions()
        existing_trade = next((t for t in transactions if t.get("trade_id") == trade_id), None)
        
        if existing_trade:
            logger.warning(f"⚠️ Trade {trade_id} já registrado, ignorando (idempotência)")
            return jsonify({
                "success": True,
                "message": "Trade já registrado",
                "already_processed": True
            })
        
        participations = load_participations()
        master_wallet_data = load_master_wallet_data()
        
        active_participations = [
            p for p in participations.values() 
            if p.get("status") == "active" and p.get("virtual_balance_usdt", 0) > 0
        ]
        
        saldo_master_rtp  = float(master_wallet_data.get("balance_available_usdt", 0))
        total_capital_usuarios = sum(p.get("virtual_balance_usdt", 0) for p in active_participations)
        total_capital_usdt     = total_capital_usuarios + saldo_master_rtp  # pool completo
        now_iso   = datetime.now().isoformat()
        cycle_id  = datetime.utcnow().strftime("%Y-%m-%d")
        symbol_rtp = data.get("symbol", "") or ""
        
        if pnl_usdt > 0:
            # ── LUCRO: proporcional pelo pool total; 50% fee dos usuários → master ──
            prop_master_rtp     = (saldo_master_rtp / total_capital_usdt) if total_capital_usdt > 0 else 0
            lucro_bruto_master  = round(pnl_usdt * prop_master_rtp, 8)
            lucro_bruto_users   = pnl_usdt - lucro_bruto_master
            fee_empresa_rtp     = round(lucro_bruto_users * 0.50, 8)
            lucro_liquido_users = round(lucro_bruto_users * 0.50, 8)
            master_recebe       = round(lucro_bruto_master + fee_empresa_rtp, 8)
            
            logger.info(f"📊 Lucro dividido: Users={lucro_liquido_users:.2f} USDT | Master={master_recebe:.2f} USDT | Fee={fee_empresa_rtp:.2f}")
            
            distributed_to_users = 0.0
            
            if total_capital_usuarios > 0:
                for participation in active_participations:
                    user_code   = participation.get("wallet_code") or participation.get("user_code", "")
                    user_capital = participation.get("virtual_balance_usdt", 0)
                    
                    if user_capital > 0:
                        user_ratio  = user_capital / total_capital_usuarios
                        user_profit = round(lucro_liquido_users * user_ratio, 8)
                        
                        if user_profit > 0:
                            participation["profit_accumulated_usdt"] = round(float(participation.get("profit_accumulated_usdt", 0)) + user_profit, 8)
                            participation["virtual_balance_usdt"]    = round(float(participation.get("virtual_balance_usdt", 0)) + user_profit, 8)
                            participation["last_profit_distribution"] = now_iso
                            participation["updated_at"]               = now_iso
                            distributed_to_users += user_profit
                            
                            transactions.append({
                                "id":                          f"PROF-{trade_id}-{user_code}",
                                "trade_id":                    trade_id,
                                "user_code":                   user_code,
                                "wallet_code":                 user_code,
                                "type":                        "profit_distribution",
                                "symbol":                      symbol_rtp,
                                "amount_usdt":                 user_profit,
                                "pnl_liquido_usdt":            user_profit,
                                "date":                        now_iso,
                                "status":                      "completed",
                                "description":                 description,
                                "user_ratio":                  round(user_ratio, 8),
                                "source":                      "robo/profit",
                                "currency":                    "USDT",
                                "cycle_id":                    cycle_id,
                            })
                            
                            logger.info(f"   👤 {user_code}: +{user_profit:.4f} USDT ({user_ratio*100:.1f}%)")
            
            # Master recebe via master_wallet.json
            add_profit_to_master(master_recebe, f"Trade {trade_id}: proporcional+fee | {description}")
            transactions.append({
                "id":          f"FEE-{trade_id}",
                "trade_id":    trade_id,
                "type":        "company_fee_income",
                "symbol":      symbol_rtp,
                "amount_usdt": master_recebe,
                "fee_usdt":    fee_empresa_rtp,
                "date":        now_iso,
                "status":      "completed",
                "description": f"Master (prop+fee) trade {trade_id}",
                "source":      "robo/profit",
                "currency":    "USDT",
                "cycle_id":    cycle_id,
            })
            
            trade_event = {
                "id":                        f"TRADE-{trade_id}",
                "trade_id":                  trade_id,
                "type":                      "trade_closed_profit",
                "symbol":                    symbol_rtp,
                "pnl_usdt":                  pnl_usdt,
                "pnl_users_usdt":            distributed_to_users,
                "pnl_master_usdt":           master_recebe,
                "fee_empresa_usdt":          fee_empresa_rtp,
                "date":                      now_iso,
                "timestamp":                 timestamp,
                "status":                    "completed",
                "description":               description,
                "active_participants":       len(active_participations),
                "total_capital_usdt":        total_capital_usdt,
                "source":                    "robo/profit",
                "currency":                  "USDT",
                "cycle_id":                  cycle_id,
            }
            transactions.append(trade_event)
            
        elif pnl_usdt < 0:
            perda_total_usdt = abs(pnl_usdt)
            logger.warning(f"📉 PERDA registrada: {perda_total_usdt:.2f} USDT")
            
            # ── PERDA: proporcional pelo pool total; sem fee extra; master absorve sua parte ──
            if total_capital_usdt > 0:
                perda_distribuida = 0.0
                
                # Usuários
                for participation in active_participations:
                    user_code    = participation.get("wallet_code") or participation.get("user_code", "")
                    user_capital = participation.get("virtual_balance_usdt", 0)
                    
                    if user_capital > 0:
                        user_ratio      = user_capital / total_capital_usdt
                        user_loss       = round(perda_total_usdt * user_ratio, 8)
                        
                        if user_loss > 0:
                            saldo_antes = float(participation.get("virtual_balance_usdt", 0))
                            novo_saldo  = max(0.0, round(saldo_antes - user_loss, 8))
                            participation["virtual_balance_usdt"]    = novo_saldo
                            participation["profit_accumulated_usdt"] = round(float(participation.get("profit_accumulated_usdt", 0)) - user_loss, 8)
                            participation["updated_at"] = now_iso
                            perda_distribuida += user_loss
                            
                            transactions.append({
                                "id":                          f"LOSS-{trade_id}-{user_code}",
                                "trade_id":                    trade_id,
                                "user_code":                   user_code,
                                "wallet_code":                 user_code,
                                "type":                        "loss_distribution",
                                "symbol":                      symbol_rtp,
                                "amount_usdt":                 round(-user_loss, 8),
                                "pnl_liquido_usdt":            round(-user_loss, 8),
                                "perda_bruta_usdt":            round(user_loss, 8),
                                "penalidade_usdt":             0.0,
                                "date":                        now_iso,
                                "status":                      "completed",
                                "description":                 f"Perda proporcional: {description}",
                                "user_ratio":                  round(user_ratio, 8),
                                "virtual_before_usdt":         round(saldo_antes, 8),
                                "virtual_after_usdt":          round(novo_saldo, 8),
                                "source":                      "robo/profit",
                                "currency":                    "USDT",
                                "cycle_id":                    cycle_id,
                            })
                
                # Master absorve proporcionalmente
                prop_master_loss = saldo_master_rtp / total_capital_usdt
                perda_master_loss = round(perda_total_usdt * prop_master_loss, 8)
                if perda_master_loss > 0:
                    mwd = load_master_wallet_data()
                    mwd["balance_available_usdt"] = max(0.0, round(float(mwd.get("balance_available_usdt", 0)) - perda_master_loss, 8))
                    mwd["updated_at"] = now_iso
                    mwd.setdefault("profit_distributions", []).append({
                        "date": now_iso, "amount_usdt": -perda_master_loss,
                        "trade_id": trade_id, "type": "loss_share"
                    })
                    save_master_wallet_data(mwd)
                    perda_distribuida += perda_master_loss
                    transactions.append({
                        "id": f"LOSS-MASTER-{trade_id}", "trade_id": trade_id,
                        "type": "loss_master_share", "symbol": symbol_rtp,
                        "amount_usdt": round(-perda_master_loss, 8),
                        "date": now_iso, "status": "completed",
                        "description": f"Perda proporcional master: {description}",
                        "currency": "USDT", "cycle_id": cycle_id,
                    })
            
            loss_event = {
                "id":                  f"LOSS-{trade_id}",
                "trade_id":            trade_id,
                "type":                "trade_closed_loss",
                "symbol":              symbol_rtp,
                "pnl_usdt":            pnl_usdt,
                "loss_total_usdt":     perda_total_usdt,
                "date":                now_iso,
                "timestamp":           timestamp,
                "status":              "completed",
                "description":         description,
                "active_participants": len(active_participations),
                "total_capital_usdt":  total_capital_usdt,
                "source":              "robo/profit",
                "warning":             "Perda reduz apenas saldo virtual",
                "currency":            "USDT",
                "cycle_id":            cycle_id,
            }
            transactions.append(loss_event)
        
        else:
            breakeven_event = {
                "id": f"TRADE-BREAKEVEN-{trade_id}",
                "trade_id": trade_id,
                "type": "trade_closed_breakeven",
                "pnl_usdt": 0,
                "date": datetime.now().isoformat(),
                "timestamp": timestamp,
                "status": "completed",
                "description": description,
                "active_participants": len(active_participations),
                "source": "robotic.py",
                "currency": "USDT"
            }
            transactions.append(breakeven_event)
        
        save_participations(participations)
        save_transactions(transactions)
        
        audit_event(
            action="TRADE_PROFIT_REGISTERED",
            success=True,
            details=f"Trade {trade_id}: {pnl_usdt:.2f} USDT",
            extra={
                "trade_id": trade_id,
                "pnl_usdt": pnl_usdt,
                "active_participants": len(active_participations),
                "total_capital_usdt": total_capital_usdt,
                "source": "robotic.py",
                "currency": "USDT"
            }
        )
        
        logger.info(f"✅ Trade {trade_id} registrado no ledger!")
        
        return jsonify({
            "success": True,
            "message": f"Trade {trade_id} registrado",
            "trade_id": trade_id,
            "pnl_usdt": pnl_usdt,
            "active_participants": len(active_participations),
            "total_capital_usdt": total_capital_usdt,
            "timestamp": datetime.now().isoformat(),
            "currency": "USDT"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /robo/profit: {str(e)}")
        audit_event(
            action="TRADE_PROFIT_ERROR",
            success=False,
            details=f"Erro ao registrar trade: {str(e)}"
        )
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/pool-info', methods=['GET'])
def get_pool_info():
    """
    Fornece informações do pool para o robotic.py.
    Retorna participações ativas com bot_active e user_positions
    para que o robotic.py possa registrar/fechar posições individuais.
    """
    try:
        auth_ok, message = require_admin_auth()
        if not auth_ok:
            return jsonify({"success": False, "message": message}), 403

        participations = load_participations()

        active_participations = []
        for code, p in participations.items():
            if p.get("status") == "active" and float(p.get("virtual_balance_usdt", 0)) > 0:
                active_participations.append({
                    "user_code":            code,
                    "code":                 code,
                    "virtual_balance_usdt": float(p.get("virtual_balance_usdt", 0)),
                    "share_percent":        float(p.get("share_percent", 0)),
                    "bot_active":           bool(p.get("bot_active", False)),
                    "status":               p.get("status", "active"),
                    # posições abertas individuais — necessário para fechar_posicao_usuarios
                    "user_positions":       p.get("user_positions", []),
                })

        pool_virtual_usdt = sum(p["virtual_balance_usdt"] for p in active_participations)
        bot_ativos = [p for p in active_participations if p["bot_active"]]

        return jsonify({
            "success": True,
            "pool_info": {
                "pool_virtual_usdt":   round(pool_virtual_usdt, 2),
                "active_users":        len(active_participations),
                "bot_active_users":    len(bot_ativos),
                "participations":      active_participations,   # ← lista completa para robotic.py
                "user_shares":         [                        # ← campo legado
                    {"user_code": p["user_code"],
                     "virtual_balance_usdt": p["virtual_balance_usdt"],
                     "share_percent": p["share_percent"]}
                    for p in active_participations
                ],
                "timestamp": datetime.now().isoformat(),
                "currency":  "USDT"
            },
            "warning": "⚠️ Valores VIRTUAIS apenas - NUNCA saldo Binance real",
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em pool-info: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/admin/master-funds', methods=['GET'])
def get_master_funds_info():
    """Retorna informações da master wallet separada em USDT"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        master_funds = load_master_funds()
        usdt_brl = get_current_usdt_brl()
        
        return jsonify({
            "success": True,
            "master_funds": {
                "binance_balance_usdt": master_funds.get("binance_balance_usdt", 0),
                "binance_balance_brl": round(master_funds.get("binance_balance_usdt", 0) * usdt_brl, 2),
                "profit_usdt": master_funds.get("profit_usdt", 0),
                "profit_brl": round(master_funds.get("profit_usdt", 0) * usdt_brl, 2),
                "updated_at": master_funds.get("updated_at"),
                "rules": master_funds.get("rules", [])
            },
            "explanation": {
                "binance_balance_usdt": "Apenas informativo (saldo real na Binance)",
                "profit_usdt": "Lucro real do sistema (50% trades + taxas)",
                "important": "NUNCA misturar com ledger de usuários"
            },
            "currency": "USDT"
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em master-funds: {str(e)}")
        return jsonify({"success": False, "message": f"Erro interno: {str(e)}"}), 500



@app.route("/robo/get-wallet-code", methods=["GET"])
def get_wallet_code():
    code = request.args.get("code")

    if not code:
        return jsonify({"success": False, "message": "Código não informado"}), 400

    wallets = load_wallets()

    if code not in wallets:
        return jsonify({"success": False, "message": "Carteira não encontrada"}), 404

    return jsonify({
        "success": True,
        "wallet_code": code
    })

@app.route('/api/user/equity', methods=['GET'])
def api_user_equity():
    """Endpoint SIMPLIFICADO para equity do usuário em USDT"""
    try:
        user_code = request.args.get('user_code', '').strip().upper()
        
        if not user_code:
            return jsonify({
                "success": False,
                "message": "user_code é obrigatório"
            }), 400
        
        participations = load_participations()
        
        if user_code not in participations:
            return jsonify({
                "success": False,
                "message": "Usuário não encontrado"
            }), 404
        
        participation = participations[user_code]
        virtual_balance_usdt = participation.get("virtual_balance_usdt", 0)
        available_for_withdraw_usdt = virtual_balance_usdt * 0.8
        
        usdt_brl = get_current_usdt_brl()
        
        return jsonify({
            "success": True,
            "equity": {
                "virtual_balance_usdt": round(virtual_balance_usdt, 2),
                "virtual_balance_brl": round(virtual_balance_usdt * usdt_brl, 2),
                "available_for_withdraw_usdt": round(available_for_withdraw_usdt, 2),
                "available_for_withdraw_brl": round(available_for_withdraw_usdt * usdt_brl, 2),
                "withdraw_percent": 80,
                "total_deposited_usdt": round(participation.get("total_deposited_usdt", 0), 2),
                "profit_accumulated_usdt": round(participation.get("profit_accumulated_usdt", 0), 2),
                "total_withdrawn_usdt": round(participation.get("total_withdrawn_usdt", 0), 2),
                "calculation": "available_for_withdraw = virtual_balance_usdt × saldo",
                "currency": "USDT"
            }
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /api/user/equity: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/admin/system-check', methods=['GET'])
def admin_system_check():
    """Endpoint para admin verificar o sistema completo"""
    try:
        if not verify_admin_key(request):
            return jsonify({
                "success": False,
                "message": "Acesso não autorizado"
            }), 403
        
        logger.info("🔍 Admin solicitou verificação do sistema")
        
        share_check = verify_and_fix_share_percent()
        save_check = verify_save_order()
        
        participations = load_participations()
        wallets = load_wallets()
        transactions = load_transactions()
        
        active_participations = [p for p in participations.values() if p.get("status") == "active"]
        
        issues = []
        
        for part in active_participations:
            share = part.get("share_percent", 0)
            if share == 0:
                issues.append(f"Usuário {part.get('user_code')} tem share_percent = 0")
        
        for user_code, part in participations.items():
            if "user_code" not in part:
                issues.append(f"Participação {user_code} não tem user_code")
        
        return jsonify({
            "success": True,
            "system_status": "operational",
            "checks": {
                "share_percent": share_check.get("success", False),
                "save_order": save_check,
                "issues_found": len(issues)
            },
            "statistics": {
                "active_users": len(active_participations),
                "total_participations": len(participations),
                "wallets": len(wallets),
                "transactions": len(transactions)
            },
            "model": {
                "rateio": "50% admin / 50% usuarios",
                "base": "share_percent da participação",
                "fallback": "proporcional ao virtual_balance_usdt se share_percent = 0",
                "withdraw_limit": "80% do virtual_balance_usdt",
                "currency": "USDT"
            },
            "issues": issues if issues else "Nenhum problema encontrado",
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em system-check: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route("/api/robo/trade-result", methods=["POST"])
def receive_trade_result():
    data = request.json

    user_code = data["user_code"]
    pnl_usdt = float(data["pnl_usdt"])

    participations = load_participations()
    wallets = load_wallets()
    transactions = load_transactions()

    if user_code in participations:
        participation = participations[user_code]
        participation["virtual_balance_usdt"] += pnl_usdt
        if pnl_usdt > 0:
            participation["profit_accumulated_usdt"] += pnl_usdt
        participation["updated_at"] = datetime.now().isoformat()

    transactions.append({
        **data,
        "type": "TRADE_RESULT",
        "currency": "USDT"
    })

    save_participations(participations)
    save_wallets(wallets)
    save_transactions(transactions)

    return {"success": True}

@app.route("/robo/internal-credit", methods=["POST"])
def internal_credit():
    if not require_robo_key(request):
        return jsonify({
            "success": False,
            "message": "Acesso não autorizado"
        }), 403

    data = request.json or {}

    wallet_code = data.get("wallet_code")
    fiat_brl = float(data.get("fiat_brl", 0))
    note = data.get("note", "Crédito interno lastreado em dinheiro")

    if not wallet_code or fiat_brl <= 0:
        return jsonify({
            "success": False,
            "message": "wallet_code e fiat_brl são obrigatórios"
        }), 400

    usdt_brl = get_current_usdt_brl()
    btc_usdt = get_current_btc_usdt()

    usdt_value = round(fiat_brl / usdt_brl, 2)
    btc_value = round(usdt_value / btc_usdt, 8) if btc_usdt > 0 else 0

    transaction = {
        "id": f"INT-{int(time.time())}",
        "type": "deposit",
        "asset": "btc",
        "wallet_code": wallet_code,
        "value_btc": btc_value,
        "amount_usdt": usdt_value,
        "status": "confirmado",
        "source": "internal_cash",
        "offchain": True,
        "note": note,
        "confirmations": 6,
        "tx_hash": f"OFFCHAIN_{int(time.time())}",
        "date": datetime.utcnow().isoformat(),
        "currency": "USDT"
    }

    transactions = load_transactions()
    transactions.append(transaction)
    save_transactions(transactions)

    return jsonify({
        "success": True,
        "message": "Crédito interno registrado com sucesso",
        "wallet_code": wallet_code,
        "btc": btc_value,
        "usdt": usdt_value,
        "transaction_id": transaction["id"]
    })

@app.route('/robo/rates', methods=['GET'])
def get_rates():
    """Endpoint para taxas de conversão e fee"""
    try:
        try:
            from binance.client import Client
            client = Client()
            
            btc_ticker = client.get_symbol_ticker(symbol="BTCUSDT")
            btc_usdt = float(btc_ticker['price'])
            
            usdt_ticker = client.get_symbol_ticker(symbol="USDTBRL")
            usdt_brl = float(usdt_ticker['price'])
            
        except Exception as e:
            logger.warning(f"⚠️ Erro ao buscar taxas da Binance, usando fallback: {e}")
            btc_usdt = DEFAULT_BTC_USDT
            usdt_brl = DEFAULT_USDT_BRL
        
        return jsonify({
            'success': True,
            'btc_usdt': round(btc_usdt, 2),
            'btc_brl': round(btc_usdt * usdt_brl, 2),
            'usdt_brl': round(usdt_brl, 2),
            'fee_percent': 1.5,
            'fee_b2b_fixed_usdt': 0.67,
            'fee_b2b_fixed_brl': round(0.67 * usdt_brl, 2),
            'updated_at': datetime.now().isoformat(),
            'source': 'binance_api' if 'btc_ticker' in locals() else 'fallback',
            'currency': 'USDT'
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /robo/rates: {str(e)}")
        return jsonify({
            'success': False,
            'btc_usdt': DEFAULT_BTC_USDT,
            'btc_brl': round(DEFAULT_BTC_USDT * DEFAULT_USDT_BRL, 2),
            'usdt_brl': DEFAULT_USDT_BRL,
            'fee_percent': 1.5,
            'fee_b2b_fixed_usdt': 0.67,
            'error': str(e),
            'source': 'error_fallback'
        }), 500

# ============================================
# 🔥 FUNÇÕES PARA PROCESSAMENTO DE CICLO (MANTIDAS PARA COMPATIBILIDADE)
# ============================================

def process_monthly_cycle():
    """Processa ciclo mensal - Mantido para compatibilidade"""
    try:
        logger.info("📅 Processando ciclo mensal (modo compatibilidade)")
        
        transactions = load_transactions()
        
        cycle_id = f"CYCLE-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        cycle_transaction = {
            "id": cycle_id,
            "type": "monthly_cycle",
            "date": datetime.now().isoformat(),
            "status": "completado",
            "details": {
                "active_participations": 0,
                "master_profit_usdt": 0.0
            },
            "description": "Ciclo mensal processado (modo compatibilidade)"
        }
        
        transactions.append(cycle_transaction)
        save_transactions(transactions)
        
        return {
            "success": True,
            "processed": True,
            "message": "Ciclo mensal processado com sucesso (modo compatibilidade)",
            "cycle_id": cycle_id
        }
        
    except Exception as e:
        logger.error(f"❌ Erro ao processar ciclo mensal: {e}")
        return {
            "success": False,
            "processed": False,
            "message": f"Erro ao processar ciclo: {str(e)}"
        }

def get_cycle_status():
    """Obtém status do ciclo - Mantido para compatibilidade"""
    try:
        transactions = load_transactions()
        
        cycles = [
            tx for tx in transactions 
            if tx.get("type") == "monthly_cycle" and tx.get("status") == "completado"
        ]
        
        last_cycle = cycles[-1] if cycles else None
        
        now = datetime.now()
        next_cycle_date = get_next_day_7(now)
        days_until_next = (next_cycle_date - now.date()).days if next_cycle_date else 0
        
        return {
            "success": True,
            "is_day_7": is_day_7(),
            "last_cycle": {
                "date": last_cycle.get("date") if last_cycle else None,
                "id": last_cycle.get("id") if last_cycle else None
            },
            "next_cycle_date": next_cycle_date.isoformat() if next_cycle_date else None,
            "days_until_next": days_until_next,
            "total_cycles_processed": len(cycles),
            "message": "Sistema não depende mais de ciclo mensal"
        }
        
    except Exception as e:
        logger.error(f"❌ Erro ao obter status do ciclo: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Erro ao verificar status do ciclo"
        }

def register_transaction(user_code: str, tx_type: str, amount_usdt: float, currency: str = "USDT", status: str = "pending", note: str = ""):
    """Registra transação no formato USDT"""
    try:
        transactions = load_transactions()
        
        tx_id = f"{tx_type}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
        
        transaction = {
            "id": tx_id,
            "wallet_code": user_code,
            "user_code": user_code,
            "type": tx_type,
            "amount_usdt": amount_usdt,
            "currency": currency,
            "date": datetime.now().isoformat(),
            "status": status,
            "description": note,
            "created_at": datetime.now().isoformat()
        }
        
        transactions.append(transaction)
        save_transactions(transactions)
        
        return tx_id
    except Exception as e:
        logger.error(f"❌ Erro ao registrar transação: {e}")
        return None

def check_pending_deposits_job():
    """Verifica depósitos pendentes com a Binance"""
    try:
        logger.info("🔍 Verificando depósitos pendentes...")
        
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            logger.warning("⚠️ Chaves Binance não configuradas")
            return {"checked": 0, "confirmed": 0, "error": "Chaves Binance não configuradas"}
        
        transactions = load_transactions()
        pending_deposits = [
            t for t in transactions 
            if t.get("type") == "deposit" 
            and t.get("status") == "pendente"
            and t.get("method") == "BTC"
        ]
        
        if not pending_deposits:
            logger.info("✅ Nenhum depósito pendente encontrado")
            return {"checked": 0, "confirmed": 0}
        
        try:
            from binance.client import Client
            client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
            
            binance_deposits = client.get_deposit_history(coin="BTC")
            
            confirmed_count = 0
            
            for pending_tx in pending_deposits:
                for binance_deposit in binance_deposits:
                    if match_pending_deposit(binance_deposit, pending_tx):
                        if confirm_deposit(pending_tx["id"], binance_deposit):
                            confirmed_count += 1
                            logger.info(f"✅ Depósito confirmado: {pending_tx['id']}")
            
            return {"checked": len(pending_deposits), "confirmed": confirmed_count}
            
        except Exception as e:
            logger.error(f"❌ Erro na API Binance: {e}")
            return {"checked": len(pending_deposits), "confirmed": 0, "error": str(e)}
        
    except Exception as e:
        logger.error(f"💥 Erro em check_pending_deposits_job: {e}")
        return {"checked": 0, "confirmed": 0, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# 📡 PnL EM TEMPO REAL — robotic → server → painel
# ═══════════════════════════════════════════════════════════════
_live_state = {
    "pnl_flotante_usdt": 0.0,
    "pnl_realizado_usdt": 0.0,
    "positions_open": [],
    "positions_count": 0,
    "last_trades": [],
    "updated_at": None,
}
# ✅ FIX #4: lock para acesso thread-safe ao _live_state
# Sem isso, receive_live_pnl() substituindo o dict inteiro ao mesmo tempo que
# public_open_pnl() lê pode causar leitura de estado inconsistente.
_live_state_lock = threading.Lock()

# ✅ FIX #3: cache da proporção do pool — recalcula no máximo a cada 10s
# Sem cache, o endpoint /api/public/open-pnl recalcula sum() sobre todos os participantes
# a cada chamada (a cada 5s por usuário), o que é pesado e pode atrasar a resposta
# além do timeout de 4s do frontend.
_pool_total_cache = {"value": 0.0, "ts": 0.0}
_POOL_CACHE_TTL = 10.0  # segundos

def _get_pool_total_usdt(participations: dict) -> float:
    """Retorna o total do pool em USDT com cache de 10s."""
    now = time.time()
    if now - _pool_total_cache["ts"] < _POOL_CACHE_TTL and _pool_total_cache["value"] > 0:
        return _pool_total_cache["value"]
    total = sum(
        float(v.get("virtual_balance_usdt", 0))
        for v in participations.values()
        if (v.get("status") == "active" or v.get("active"))
        and float(v.get("virtual_balance_usdt", 0)) > 0
    )
    _pool_total_cache["value"] = total
    _pool_total_cache["ts"] = now
    return total

@app.route('/robo/live-pnl', methods=['POST'])
def receive_live_pnl():
    """Recebe PnL em tempo real do robotic.py a cada ciclo (~8s)."""
    try:
        if not require_robo_key(request):
            logger.warning("⛔ /robo/live-pnl: chave inválida")
            return jsonify({"success": False, "message": "Não autorizado"}), 403

        data = request.get_json(silent=True) or {}

        # Compatível com ambos os formatos enviados pelo robotic.py
        pnl_flotante = float(
            data.get("pnl_flotante_usdt") or
            data.get("total_open_pnl_usdt") or 0
        )
        positions  = data.get("positions_open") or data.get("positions") or []
        last_trades = data.get("last_trades") or []
        pnl_realizado = float(data.get("pnl_realizado_usdt") or 0)

        # ✅ FIX #4: atualiza campos individualmente com lock — evita leitura de estado
        # inconsistente quando public_open_pnl() lê simultaneamente
        with _live_state_lock:
            _live_state["pnl_flotante_usdt"]  = pnl_flotante
            _live_state["pnl_realizado_usdt"] = pnl_realizado
            _live_state["positions_open"]     = positions
            _live_state["positions_count"]    = len(positions)
            _live_state["last_trades"]        = last_trades
            _live_state["updated_at"]         = datetime.now().isoformat()

        logger.info(f"📡 Live PnL: {pnl_flotante:.4f} USDT | {len(positions)} posições")
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Erro /robo/live-pnl: {e}")
        return jsonify({"success": False}), 500


@app.route('/api/public/open-pnl', methods=['GET'])
def public_open_pnl():
    """
    Retorna PnL proporcional ao saldo do usuário.
    GET /api/public/open-pnl?code=ROBO-XXXX
    """
    try:
        wallet_code = request.args.get('code', '').strip().upper()
        if not wallet_code:
            return jsonify({"success": False, "message": "code obrigatório"}), 400

        participations = load_participations()
        p = participations.get(wallet_code)
        if not p:
            return jsonify({"success": False, "message": "Carteira não encontrada"}), 404

        usdt_brl = get_current_usdt_brl()
        virtual_balance_usdt    = float(p.get("virtual_balance_usdt", 0))
        profit_accumulated_usdt = float(p.get("profit_accumulated_usdt", 0))

        # ✅ FIX #3: usa cache do total do pool (recalcula no máx. a cada 10s)
        # Evita sum() pesado sobre todos os participantes a cada chamada (5s × N usuários)
        total_pool = _get_pool_total_usdt(participations)
        proporcao = (virtual_balance_usdt / total_pool) if total_pool > 0 else 0

        # ✅ FIX #4: lê _live_state com lock para evitar estado inconsistente
        with _live_state_lock:
            live_pnl_flotante   = _live_state["pnl_flotante_usdt"]
            live_updated_at     = _live_state["updated_at"]
            live_positions_open = list(_live_state["positions_open"])  # cópia para evitar mutação
            live_positions_count = _live_state["positions_count"]

        # PnL flutuante proporcional ao saldo do usuário — valor BRUTO proporcional
        pnl_flotante_bruto_proporcional = live_pnl_flotante * proporcao

        # Status do robô (considera offline se não enviou nos últimos 90s)
        robo_online = False
        if live_updated_at:
            try:
                delta = (datetime.now() - datetime.fromisoformat(live_updated_at)).total_seconds()
                robo_online = delta < 120  # 120s de tolerância (ciclo do robô é ~45s após otimização)
            except:
                pass

        n = live_positions_count
        if not live_updated_at:
            notice = "🔄 Aguardando dados do robô..."
        elif not robo_online:
            notice = "⚠️ Robô offline"
        elif n > 0:
            notice = f"🟢 {n} posição{'ões' if n > 1 else ''} aberta{'s' if n > 1 else ''} — atualiza a cada 45s"
        else:
            notice = "📭 Nenhuma posição aberta"

        # ── Enriquecer posições INDIVIDUAIS do usuário com preço atual ──────
        live_price_map = {
            pos.get("symbol"): float(pos.get("current_price", 0))
            for pos in live_positions_open
        }

        user_positions_raw = p.get("user_positions", [])
        user_positions_enriched = []
        user_pnl_flotante_usdt = 0.0

        for pos in user_positions_raw:
            sym = pos.get("symbol", "")
            entry_price = float(pos.get("preco_entrada", 0))
            quantidade  = float(pos.get("quantidade", 0))
            valor_entrada = float(pos.get("valor_entrada_usdt", 0))
            origem      = pos.get("origem", "SCANNER")

            # Preço atual: preferir live_state; fallback = entrada
            current_price = live_price_map.get(sym, entry_price) or entry_price
            if current_price <= 0:
                current_price = entry_price

            pnl_usdt = (current_price - entry_price) * quantidade if entry_price > 0 else 0.0
            pnl_pct  = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
            valor_atual = quantidade * current_price
            user_pnl_flotante_usdt += pnl_usdt

            user_positions_enriched.append({
                "symbol":           sym,
                "nome_moeda":       sym.replace("USDT", ""),
                "quantidade":       round(quantidade, 8),
                "entry_price":      round(entry_price, 8),
                "preco_entrada":    round(entry_price, 8),
                "current_price":    round(current_price, 8),
                "preco_atual":      round(current_price, 8),
                "valor_entrada_usdt": round(valor_entrada, 2),
                "valor_atual_usdt": round(valor_atual, 2),
                "pnl_usdt":         round(pnl_usdt, 4),
                "pnl_brl":          round(pnl_usdt * usdt_brl, 2),
                "pnl_percent":      round(pnl_pct, 2),
                "take_profit":      pos.get("take_profit", 0),
                "stop_loss":        pos.get("stop_loss", 0),
                "timestamp":        pos.get("timestamp", ""),
                "origem":           origem,
                # Badge visual para o front: POOL = trade replicado do robô; INDIVIDUAL = bot próprio
                "origem_label":     "🤖 Pool" if origem == "ROBO_POOL" else "👤 Individual",
                "status":           "aberta",
            })

        # PnL flutuante proporcional do POOL
        pnl_pool_proporcional = round(pnl_flotante_bruto_proporcional, 4)

        # Posições do pool com quantidade e pnl já proporcionais ao usuário
        pool_positions_prop = []
        for pp in live_positions_open:
            ep  = float(pp.get("preco_entrada", pp.get("entry_price", 0)))
            cp  = float(pp.get("current_price", pp.get("preco_atual", ep)) or ep)
            if cp <= 0: cp = ep
            qt       = float(pp.get("quantidade", pp.get("quantity", 0)))
            qt_user  = round(qt * proporcao, 8)
            val_user = round(ep * qt_user, 4)
            pnl_user = round((cp - ep) * qt_user, 4) if ep > 0 else 0.0
            pct_user = round(((cp - ep) / ep * 100) if ep > 0 else 0, 2)
            pool_positions_prop.append({
                "symbol":               pp.get("symbol", ""),
                "quantidade":           qt_user,
                "quantity":             qt_user,
                "preco_entrada":        ep,
                "entry_price":          ep,
                "preco_atual":          cp,
                "current_price":        cp,
                "valor_entrada_usdt":   val_user,
                "pnl_usdt":             pnl_user,
                "pnl_percent":          pct_user,
                "take_profit":          pp.get("take_profit", 0),
                "stop_loss":            pp.get("stop_loss", 0),
                "_pool":                True,
                "quantidade_pool_total": qt,
                "proporcao_usuario":    round(proporcao, 8),
            })

        return jsonify({
            "success": True,
            "wallet_code": wallet_code,
            # ── PnL flutuante do POOL (proporcional ao saldo do usuário) ──
            "pnl": round(pnl_flotante_bruto_proporcional * usdt_brl, 2),
            "pnl_flotante_usdt": pnl_pool_proporcional,
            "pnl_flotante_brl":  round(pnl_pool_proporcional * usdt_brl, 2),
            # ── PnL flutuante INDIVIDUAL (posições próprias do usuário) ──
            "pnl_individual_usdt": round(user_pnl_flotante_usdt, 4),
            "pnl_individual_brl":  round(user_pnl_flotante_usdt * usdt_brl, 2),
            # ── PnL realizado (acumulado — já líquido) ──
            "pnl_realizado_usdt": round(profit_accumulated_usdt, 4),
            "pnl_realizado_brl":  round(profit_accumulated_usdt * usdt_brl, 2),
            # ── Saldo ──
            "balance_usdt": round(virtual_balance_usdt, 4),
            "balance_brl":  round(virtual_balance_usdt * usdt_brl, 2),
            # ── Posições INDIVIDUAIS do usuário (enriquecidas com preço atual) ──
            "positions_open":       user_positions_enriched,
            "positions_count":      len(user_positions_enriched),
            # ── Posições do POOL já proporcionais (calculado antes do return) ──
            "pool_positions":       pool_positions_prop,
            "pool_positions_count": n,
            "pool_positions_count_prop": len(pool_positions_prop),
            # ── Últimos trades DO USUÁRIO (pnl_liquido_usdt = valor real ganho/perdido) ──
            # NÃO usar _live_state["last_trades"] — esses são do robô master (valores brutos totais)
            "last_trades": [
                {
                    "symbol":           t.get("symbol", ""),
                    "pnl_usdt":         float(t.get("pnl_liquido_usdt", t.get("pnl_usdt", 0))),
                    "pnl_liquido_usdt": float(t.get("pnl_liquido_usdt", t.get("pnl_usdt", 0))),
                    "exit_reason":      t.get("motivo_saida", t.get("exit_reason", "")),
                    "timestamp_exit":   t.get("timestamp_saida", t.get("timestamp_exit", "")),
                    "duration_minutes": t.get("duration_minutes", 0),
                }
                for t in p.get("user_closed_trades", [])[-10:]
            ],
            # ── Status do robô ──
            "robo_online":     robo_online,
            "robo_updated_at": live_updated_at,
            "usdt_brl":        round(usdt_brl, 2),
            "proporcao_percent": round(proporcao * 100, 4),
            "notice": notice,
            # ── Config individual do bot ──
            "bot_active":           p.get("bot_active", False),
            "aporte_usdt":          round(float(p.get("aporte_usdt", 0)), 2),
            "saldo_disponivel_usdt": round(max(0.0, float(p.get("virtual_balance_usdt", 0)) - float(p.get("saldo_em_posicoes_usdt", 0))), 4),
            "saldo_em_posicoes_usdt": round(float(p.get("saldo_em_posicoes_usdt", 0)), 4),
        })
    except Exception as e:
        logger.error(f"Erro /api/public/open-pnl: {e}")
        return jsonify({"success": False}), 500


@app.route('/robo/trade-closed', methods=['POST'])
def robo_trade_closed():
    data = request.get_json() or {}

    trade_id = (data.get("trade_id") or "").strip()
    if not trade_id:
        return jsonify({"success": False, "message": "trade_id obrigatório"}), 400

    pnl = float(data.get("pnl_usdt", 0) or 0)
    symbol = data.get("symbol") or "UNKNOWN"
    side = data.get("side") or "LONG"
    timestamp = data.get("timestamp") or datetime.now().isoformat()

    participations = load_participations()
    transactions = load_transactions()

    # 🔒 LOCK
    if not acquire_trade_lock(trade_id):
        return jsonify({"success": False, "message": "Trade em processamento"}), 409

    try:
        # 🔒 DUPLICAÇÃO
        if trade_ja_processado(trade_id, transactions):
            return jsonify({"success": False, "message": "Trade já distribuído"}), 409

        # Proteção: verificar se há capital ativo (participations é a fonte única)
        total_pool = sum(
            float(p.get("virtual_balance_usdt", 0))
            for p in participations.values()
            if p.get("status") == "active"
        )

        if total_pool <= 0:
            logger.error("Pool total zero — distribuição cancelada")
            return jsonify({"success": False, "message": "Pool vazio"}), 400

        # Chamar a função correta com assinatura (trade_id, pnl_usdt, description)
        result = distribuir_lucro_proporcional(
            trade_id=trade_id,
            pnl_usdt=pnl,
            description=f"{symbol} {side}"
        )

        if not result.get("success"):
            return jsonify({"success": False, "message": result.get("message", "Erro na distribuição")}), 500

        total_creditado = result.get("total_creditado_usdt", 0)
        total_fee       = result.get("total_fee_usdt", 0)

        logger.info(
            f"Trade {trade_id} distribuído | PnL: {pnl:.4f} USDT | "
            f"Crédito: {total_creditado:.4f} | Fee: {total_fee:.4f}"
        )

        return jsonify({
            "success": True,
            "trade_id": trade_id,
            "total_creditado": total_creditado,
            "total_fee": total_fee
        })

    finally:
        release_trade_lock(trade_id)



@app.route('/wallet/history', methods=['GET'])
def get_wallet_history_period():
    """
    Retorna histórico de transações de uma carteira filtrando por período.
    Parâmetros:
        - code: código da carteira (obrigatório)
        - from: data inicial (YYYY-MM-DD, obrigatório)
        - to: data final (YYYY-MM-DD, obrigatório)
    """
    try:
        # ============================================
        # 🔍 Validar parâmetros
        # ============================================
        wallet_code = request.args.get('code', '').strip().upper()
        from_date_str = request.args.get('from', '').strip()
        to_date_str = request.args.get('to', '').strip()
        
        if not wallet_code:
            return jsonify({
                "success": False,
                "message": "Código da carteira é obrigatório"
            }), 400
        
        if not from_date_str or not to_date_str:
            return jsonify({
                "success": False,
                "message": "Período (from e to) é obrigatório"
            }), 400
        
        # ============================================
        # 📅 Validar formato das datas
        # ============================================
        try:
            from_date = datetime.strptime(from_date_str, "%Y-%m-%d")
            to_date = datetime.strptime(to_date_str, "%Y-%m-%d")
            
            # Ajustar para início e fim do dia
            from_datetime = datetime.combine(from_date.date(), datetime.min.time())
            to_datetime = datetime.combine(to_date.date(), datetime.max.time())
            
        except ValueError:
            return jsonify({
                "success": False,
                "message": "Formato de data inválido. Use YYYY-MM-DD"
            }), 400
        
        if from_date > to_date:
            return jsonify({
                "success": False,
                "message": "Data inicial não pode ser maior que data final"
            }), 400
        
        # ============================================
        # 💾 Carregar dados
        # ============================================
        transactions = load_transactions()
        participations = load_participations()
        wallets = load_wallets()
        
        # Verificar se carteira existe
        if wallet_code not in wallets and wallet_code not in participations:
            return jsonify({
                "success": False,
                "message": "Carteira não encontrada"
            }), 404
        
        # ============================================
        # 📊 Filtrar transações da carteira no período
        # ============================================
        wallet_transactions = []
        
        for tx in transactions:
            # Verificar se é da carteira
            tx_wallet = tx.get('wallet_code', '').upper()
            if tx_wallet != wallet_code:
                continue
            
            # Verificar data
            tx_date_str = tx.get('date', '')
            if not tx_date_str:
                continue
                
            try:
                tx_date = datetime.fromisoformat(tx_date_str.replace('Z', '+00:00'))
                if from_datetime <= tx_date <= to_datetime:
                    
                    # Classificar tipo de transação
                    tx_type = tx.get('type', '').lower()
                    amount_usdt = tx.get('amount_usdt', tx.get('value_usdt', tx.get('pnl_usdt', 0)))
                    
                    # DEPÓSITOS
                    if tx_type in ['deposit', 'initial_deposit', 'emergency_credit']:
                        wallet_transactions.append({
                            "id": tx.get('id'),
                            "date": tx_date_str,
                            "type": "deposit",
                            "category": "Entrada",
                            "amount_usdt": abs(float(amount_usdt)) if amount_usdt else 0,
                            "description": tx.get('description', 'Depósito'),
                            "status": tx.get('status', 'completed'),
                            "method": tx.get('method', 'USDT'),
                            "currency": "USDT"
                        })
                    
                    # SAQUES
                    elif tx_type in ['withdrawal', 'withdraw', 'withdraw_user']:
                        wallet_transactions.append({
                            "id": tx.get('id'),
                            "date": tx_date_str,
                            "type": "withdrawal",
                            "category": "Saída",
                            "amount_usdt": abs(float(amount_usdt)) if amount_usdt else 0,
                            "description": tx.get('description', 'Saque'),
                            "status": tx.get('status', 'pending'),
                            "btc_address": tx.get('btc_address'),
                            "currency": "USDT"
                        })
                    
                    # LUCROS (profit distribution)
                    elif tx_type in ['profit_distribution', 'profit']:
                        wallet_transactions.append({
                            "id": tx.get('id'),
                            "date": tx_date_str,
                            "type": "profit",
                            "category": "Lucro",
                            "amount_usdt": abs(float(amount_usdt)) if amount_usdt else 0,
                            "description": tx.get('description', 'Distribuição de lucro'),
                            "trade_id": tx.get('trade_id'),
                            "participation_percent": tx.get('participation_percent', tx.get('proporcao_saldo_atual', 0)),
                            "currency": "USDT"
                        })
                    
                    # PERDAS (loss distribution)
                    elif tx_type in ['loss_distribution', 'loss']:
                        wallet_transactions.append({
                            "id": tx.get('id'),
                            "date": tx_date_str,
                            "type": "loss",
                            "category": "Perda",
                            "amount_usdt": abs(float(amount_usdt)) if amount_usdt else 0,
                            "description": tx.get('description', 'Distribuição de prejuízo'),
                            "trade_id": tx.get('trade_id'),
                            "participation_percent": tx.get('participation_percent', tx.get('proporcao_saldo_atual', 0)),
                            "fee_usdt": tx.get('fee_usdt', 0),
                            "currency": "USDT"
                        })
                    
                    # AJUSTES
                    elif tx_type in ['adjustment', 'rounding_adjustment', 'correction']:
                        wallet_transactions.append({
                            "id": tx.get('id'),
                            "date": tx_date_str,
                            "type": "adjustment",
                            "category": "Ajuste",
                            "amount_usdt": abs(float(amount_usdt)) if amount_usdt else 0,
                            "description": tx.get('description', 'Ajuste manual'),
                            "currency": "USDT"
                        })
                    
                    # OUTROS (fallback)
                    else:
                        if amount_usdt and abs(float(amount_usdt)) > 0:
                            wallet_transactions.append({
                                "id": tx.get('id'),
                                "date": tx_date_str,
                                "type": tx_type,
                                "category": "Outros",
                                "amount_usdt": abs(float(amount_usdt)) if amount_usdt else 0,
                                "description": tx.get('description', 'Transação'),
                                "currency": "USDT"
                            })
                            
            except Exception as e:
                logger.warning(f"⚠️ Erro ao processar transação {tx.get('id')}: {e}")
                continue
        
        # ============================================
        # 📈 Ordenar por data (mais recentes primeiro)
        # ============================================
        wallet_transactions.sort(key=lambda x: x.get('date', ''), reverse=True)
        
        # ============================================
        # 📊 Calcular resumo do período
        # ============================================
        summary = {
            "total_deposits_usdt": 0.0,
            "total_withdrawals_usdt": 0.0,
            "total_profit_usdt": 0.0,
            "total_loss_usdt": 0.0,
            "net_result_usdt": 0.0,
            "transaction_count": len(wallet_transactions)
        }
        
        for tx in wallet_transactions:
            if tx['type'] == 'deposit':
                summary['total_deposits_usdt'] += tx['amount_usdt']
            elif tx['type'] == 'withdrawal':
                summary['total_withdrawals_usdt'] += tx['amount_usdt']
            elif tx['type'] == 'profit':
                summary['total_profit_usdt'] += tx['amount_usdt']
            elif tx['type'] == 'loss':
                summary['total_loss_usdt'] += tx['amount_usdt']
        
        summary['net_result_usdt'] = (
            summary['total_profit_usdt'] - 
            summary['total_loss_usdt'] + 
            summary['total_deposits_usdt'] - 
            summary['total_withdrawals_usdt']
        )
        
        # ============================================
        # 💰 Saldo atual da carteira
        # ============================================
        current_balance_usdt = 0.0
        
        if wallet_code in participations:
            current_balance_usdt = participations[wallet_code].get('virtual_balance_usdt', 0.0)
        elif wallet_code in wallets:
            current_balance_usdt = wallets[wallet_code].get('balanceUSDT', 0.0)
        
        # ============================================
        # ✅ Resposta final
        # ============================================
        response = {
            "success": True,
            "wallet_code": wallet_code,
            "period": {
                "from": from_date_str,
                "to": to_date_str,
                "days": (to_date - from_date).days + 1
            },
            "current_balance_usdt": round(current_balance_usdt, 2),
            "summary": {
                key: round(value, 2) for key, value in summary.items()
            },
            "transactions": wallet_transactions,
            "count": len(wallet_transactions),
            "currency": "USDT",
            "timestamp": datetime.now().isoformat()
        }
        
        # Adicionar saldo em BRL se disponível
        try:
            usdt_brl = get_current_usdt_brl()
            response["current_balance_brl"] = round(current_balance_usdt * usdt_brl, 2)
            response["summary_brl"] = {
                key.replace('_usdt', '_brl'): round(value * usdt_brl, 2)
                for key, value in summary.items()
            }
        except:
            pass
        
        logger.info(f"📊 Histórico gerado para {wallet_code}: {len(wallet_transactions)} transações no período")
        
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"💥 Erro em /wallet/history: {str(e)}", exc_info=True)
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500

@app.route('/robo/transactions/history', methods=['GET'])
def get_robo_transactions_history():
    """
    Retorna histórico de transações de uma carteira filtrado por período.
    Parâmetros:
        - wallet_code: código da carteira (obrigatório)
        - start_date: data inicial (YYYY-MM-DD, obrigatório)
        - end_date: data final (YYYY-MM-DD, obrigatório)
    
    Retorna máximo 500 registros ordenados do mais recente para o mais antigo.
    """
    try:
        # ============================================
        # 🔍 Validar parâmetros
        # ============================================
        wallet_code = request.args.get('wallet_code', '').strip().upper()
        start_date_str = request.args.get('start_date', '').strip()
        end_date_str = request.args.get('end_date', '').strip()
        
        if not wallet_code:
            return jsonify({
                "success": False,
                "message": "wallet_code é obrigatório"
            }), 400
        
        if not start_date_str or not end_date_str:
            return jsonify({
                "success": False,
                "message": "start_date e end_date são obrigatórios (formato YYYY-MM-DD)"
            }), 400
        
        # ============================================
        # 📅 Validar formato das datas
        # ============================================
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
            
            # Ajustar para início e fim do dia
            start_datetime = datetime.combine(start_date.date(), datetime.min.time())
            end_datetime = datetime.combine(end_date.date(), datetime.max.time())
            
        except ValueError:
            return jsonify({
                "success": False,
                "message": "Formato de data inválido. Use YYYY-MM-DD"
            }), 400
        
        if start_date > end_date:
            return jsonify({
                "success": False,
                "message": "start_date não pode ser maior que end_date"
            }), 400
        
        # ============================================
        # 💾 Carregar transações
        # ============================================
        transactions = load_transactions()
        
        if not transactions:
            return jsonify({
                "success": True,
                "wallet_code": wallet_code,
                "period": {
                    "start_date": start_date_str,
                    "end_date": end_date_str
                },
                "transactions": [],
                "count": 0,
                "message": "Nenhuma transação encontrada"
            })
        
        # ============================================
        # 📊 Filtrar transações
        # ============================================
        filtered_transactions = []
        
        for tx in transactions:
            # Filtrar por wallet_code
            tx_wallet = tx.get('wallet_code', '').upper()
            tx_user = tx.get('user_code', '').upper()
            
            if tx_wallet != wallet_code and tx_user != wallet_code:
                continue
            
            # Filtrar por data
            tx_date_str = tx.get('date', tx.get('timestamp', tx.get('created_at', '')))
            if not tx_date_str:
                continue
            
            try:
                # Normalizar formato da data
                tx_date_str = tx_date_str.replace('Z', '+00:00')
                tx_date = datetime.fromisoformat(tx_date_str)
                
                if start_datetime <= tx_date <= end_datetime:
                    # Normalizar campos para resposta
                    amount_usdt = tx.get('amount_usdt', tx.get('value_usdt', tx.get('pnl_usdt', 0)))
                    
                    # Garantir que amount seja float
                    try:
                        amount_usdt = float(amount_usdt) if amount_usdt else 0.0
                    except:
                        amount_usdt = 0.0
                    
                    # Construir objeto de transação normalizado
                    tx_type_raw = tx.get('type', 'unknown').lower()
                    is_trade_tx = tx_type_raw in (
                        'profit_distribution', 'loss_distribution',
                        'trade_closed_profit', 'trade_closed_loss',
                        'trade_closed_breakeven'
                    )

                    # Para trades: preservar sinal e valor líquido original
                    if is_trade_tx:
                        pnl_liq = float(tx.get('pnl_liquido_usdt', tx.get('amount_usdt', tx.get('pnl_usdt', 0))) or 0)
                        amount_display = round(pnl_liq, 6)
                    else:
                        amount_display = round(amount_usdt, 6) if amount_usdt else 0.0

                    normalized_tx = {
                        "id":           tx.get('id', ''),
                        "wallet_code":  tx_wallet or tx_user,
                        "type":         tx.get('type', 'unknown'),
                        "amount_usdt":  amount_display,
                        "date":         tx_date_str,
                        "status":       tx.get('status', 'unknown'),
                        "description":  tx.get('description', tx.get('note', '')),
                        "currency":     "USDT",
                    }

                    # Campos de trade
                    if is_trade_tx or tx.get('trade_id'):
                        normalized_tx['trade_id']       = tx.get('trade_id', '')
                        normalized_tx['symbol']         = tx.get('symbol', '')
                        normalized_tx['pnl_liquido_usdt'] = amount_display
                        normalized_tx['exit_reason']    = tx.get('exit_reason', tx.get('motivo_saida', ''))

                    if tx.get('fee_usdt') is not None:
                        normalized_tx['fee_usdt'] = round(float(tx.get('fee_usdt', 0)), 6)

                    if tx.get('proporcao_saldo_atual') is not None:
                        normalized_tx['participation_percent'] = round(float(tx.get('proporcao_saldo_atual', 0)), 6)

                    if tx.get('btc_address'):
                        normalized_tx['btc_address'] = tx.get('btc_address')
                    if tx.get('tx_hash'):
                        normalized_tx['tx_hash'] = tx.get('tx_hash')
                    if tx.get('method'):
                        normalized_tx['method'] = tx.get('method')

                    filtered_transactions.append(normalized_tx)
                    
            except Exception as e:
                logger.warning(f"⚠️ Erro ao processar data da transação {tx.get('id', 'unknown')}: {e}")
                continue
        
        # ============================================
        # 📈 Ordenar do mais recente para o mais antigo
        # ============================================
        filtered_transactions.sort(
            key=lambda x: x.get('date', ''), 
            reverse=True
        )
        
        # ============================================
        # 🔢 Limitar a 500 registros
        # ============================================
        total_count = len(filtered_transactions)
        limited_transactions = filtered_transactions[:500]
        
        # ============================================
        # 📊 Calcular resumo do período
        # ============================================
        summary = {
            "total_deposits_usdt": 0.0,
            "total_withdrawals_usdt": 0.0,
            "total_profit_usdt": 0.0,
            "total_loss_usdt": 0.0,
            "net_result_usdt": 0.0
        }
        
        for tx in limited_transactions:
            tx_type = tx.get('type', '').lower()
            amount  = tx.get('amount_usdt', 0.0)   # já com sinal correto após normalização

            if 'deposit' in tx_type:
                summary['total_deposits_usdt'] += abs(amount)
            elif 'withdraw' in tx_type:
                summary['total_withdrawals_usdt'] += abs(amount)
            elif tx_type in ('profit_distribution', 'trade_closed_profit'):
                if amount > 0:
                    summary['total_profit_usdt'] += amount
            elif tx_type in ('loss_distribution', 'trade_closed_loss'):
                if amount < 0:
                    summary['total_loss_usdt'] += abs(amount)
        
        summary['net_result_usdt'] = (
            summary['total_profit_usdt'] - 
            summary['total_loss_usdt'] + 
            summary['total_deposits_usdt'] - 
            summary['total_withdrawals_usdt']
        )
        
        # ============================================
        # ✅ Resposta final
        # ============================================
        response = {
            "success": True,
            "wallet_code": wallet_code,
            "period": {
                "start_date": start_date_str,
                "end_date": end_date_str,
                "days": (end_date - start_date).days + 1
            },
            "summary": {
                key: round(value, 2) for key, value in summary.items()
            },
            "transactions": limited_transactions,
            "count": len(limited_transactions),
            "total_available": total_count,
            "limit_applied": 500 if total_count > 500 else None,
            "currency": "USDT",
            "timestamp": datetime.now().isoformat()
        }
        
        # Adicionar metadados sobre o período
        if limited_transactions:
            response["period"]["first_transaction"] = limited_transactions[-1].get('date') if limited_transactions else None
            response["period"]["last_transaction"] = limited_transactions[0].get('date') if limited_transactions else None
        
        logger.info(f"📊 Histórico gerado para {wallet_code}: {len(limited_transactions)} transações no período {start_date_str} a {end_date_str}")
        
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"💥 Erro em /robo/transactions/history: {str(e)}", exc_info=True)
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500


# ============================================
# INICIALIZAÇÃO
# ============================================

def verify_admin_key(request) -> bool:
    """Verifica se a requisição tem a chave de admin válida - MANTIDA PARA COMPATIBILIDADE"""
    admin_key = request.headers.get(ADMIN_TOKEN_HEADER)
    if not admin_key:
        admin_key = request.args.get('key')
    
    return admin_key == ADMIN_SECRET_KEY


@app.route("/api/transactions/<wallet_code>")
def get_transactions(wallet_code):
    transactions = load_transactions()
    result = [t for t in transactions if t.get("wallet_code") == wallet_code.upper()]
    return jsonify({"success": True, "transactions": result})


@app.route('/api/dashboard/<user_code>', methods=['GET'])
def dashboard_unified(user_code):
    """
    Dashboard com fonte única de saldo.
    """
    participations = load_participations()

    user_code = user_code.strip().upper()

    if user_code not in participations:
        return jsonify({"success": False, "message": "Usuário não encontrado"}), 404

    p = participations[user_code]

    saldo_brl = float(p.get("virtual_balance", 0))
    saldo_usdt = float(p.get("virtual_balance_usdt", 0))

    btc_rate = get_current_btc_usdt()
    saldo_btc = saldo_usdt / btc_rate if btc_rate else 0

    return jsonify({
        "success": True,
        "data": {
            "balance_brl": round(saldo_brl, 2),
            "balance_usdt": round(saldo_usdt, 6),
            "balance_btc": round(saldo_btc, 8),
            "profit_accumulated": p.get("profit_accumulated", 0),
            "last_update": p.get("updated_at")
        },
        "audit": {
            "source": "participations",
            "mode": "single_source_of_truth"
        }
    })
    
@app.route('/robo/admin/withdrawals/pending', methods=['GET'])
def admin_list_pending_withdrawals():
    if not verify_admin_key(request):
        return jsonify({"success": False, "message": "Acesso não autorizado"}), 403

    transactions = load_transactions()
    pending = [tx for tx in transactions if tx.get("type") == "withdrawal" and tx.get("status") == "pendente"]

    return jsonify({
        "success": True,
        "pending_withdrawals": pending,
        "count": len(pending)
    })

@app.route('/robo/admin/withdrawals/approve', methods=['POST'])
def admin_approve_withdrawal():
    if not verify_admin_key(request):
        return jsonify({"success": False, "message": "Acesso não autorizado"}), 403

    data = request.get_json(silent=True) or {}
    transaction_id = data.get("transaction_id")
    tx_hash = data.get("tx_hash")

    transactions = load_transactions()
    participations = load_participations()

    tx = next((t for t in transactions if t["id"] == transaction_id), None)
    if not tx or tx["status"] != "pendente":
        return jsonify({"success": False, "message": "Transação inválida"}), 400

    wallet_code = tx["wallet_code"]
    amount = tx["amount_requested_usdt"]

    participations[wallet_code]["virtual_balance_usdt"] -= amount
    participations[wallet_code]["total_withdrawn_usdt"] += amount

    tx["status"] = "aprovado"
    tx["tx_hash"] = tx_hash
    tx["approved_at"] = datetime.now().isoformat()

    save_transactions(transactions)
    save_participations(participations)

    return jsonify({"success": True, "message": "Saque aprovado"})

@app.route('/robo/admin/withdrawals/reject', methods=['POST'])
def admin_reject_withdrawal():
    if not verify_admin_key(request):
        return jsonify({"success": False, "message": "Acesso não autorizado"}), 403

    data = request.get_json(silent=True) or {}
    transaction_id = data.get("transaction_id")

    transactions = load_transactions()

    tx = next((t for t in transactions if t["id"] == transaction_id), None)
    if not tx or tx["status"] != "pendente":
        return jsonify({"success": False, "message": "Transação inválida"}), 400

    tx["status"] = "rejeitado"
    tx["rejected_at"] = datetime.now().isoformat()

    save_transactions(transactions)

    return jsonify({"success": True, "message": "Saque rejeitado"})


@app.route('/api/status', methods=['GET'])
def api_status():
    wallets = load_wallets()
    transactions = load_transactions()

    total_balance = sum(float(w.get("balanceUSDT", 0)) for w in wallets.values())

    total_profit = sum(
        tx.get("amount_usdt", 0)
        for tx in transactions
        if tx.get("type") == "profit_distribution"
    )

    total_trades = len(set(
        tx.get("trade_id")
        for tx in transactions
        if tx.get("trade_id")
    ))

    return jsonify({
        "balance_usdt": total_balance,
        "total_profit_usdt": total_profit,
        "total_trades": total_trades
    })

@app.route('/api/admin/participations', methods=['GET'])
def admin_participations():
    participations = load_participations()
    return jsonify({
        "success": True,
        "data": participations
    })


    
  
    

# ═══════════════════════════════════════════════════════════════════════════
# 🔒 GESTÃO DE RISCO INDIVIDUAL POR USUÁRIO — IMPLEMENTAÇÃO COMPLETA
# ═══════════════════════════════════════════════════════════════════════════
# Cada usuário opera com capital isolado. Carteira master é conta independente.
# Nenhum saldo é misturado. Posições são individuais e rastreadas por usuário.
# ═══════════════════════════════════════════════════════════════════════════

MASTER_POSITIONS_FILE = os.path.join(BASE_DIR, "master_positions.json")

def _load_master_positions() -> dict:
    """Carrega posições da carteira master."""
    return _read_json(MASTER_POSITIONS_FILE, {
        "positions_open": [],
        "positions_closed": [],
        "saldo_disponivel_usdt": 0.0,
        "saldo_em_posicoes_usdt": 0.0,
        "aporte_usdt": 0.0,
        "risco_por_trade": 0.05,
        "max_posicoes_abertas": 10,
    })

def _save_master_positions(data: dict) -> bool:
    """Salva posições da carteira master."""
    return _write_json(MASTER_POSITIONS_FILE, data)


def _calcular_valor_entrada(saldo_disponivel: float, risco_por_trade: float,
                             valor_minimo: float = 10.0) -> float:
    """
    Calcula o valor de entrada baseado no saldo disponível e risco configurado.
    Retorna 0 se abaixo do mínimo. Nunca excede saldo disponível.
    """
    if saldo_disponivel <= 0 or risco_por_trade <= 0:
        return 0.0
    valor = saldo_disponivel * risco_por_trade
    if valor < valor_minimo:
        return 0.0
    if valor > saldo_disponivel:
        valor = saldo_disponivel
    return round(valor, 4)


def _verificar_perda_diaria(p: dict) -> bool:
    """
    Verifica se o limite de perda diária foi atingido.
    Retorna True se bloqueado (limite atingido), False se pode operar.
    """
    limite = float(p.get("perda_diaria_limite_usdt", 0))
    if limite <= 0:
        return False  # sem limite configurado

    hoje = datetime.now().strftime("%Y-%m-%d")
    data_registro = p.get("perda_diaria_data", "")

    # resetar acumulado se é um novo dia
    if data_registro != hoje:
        p["perda_diaria_acumulada_usdt"] = 0.0
        p["perda_diaria_data"] = hoje

    acumulado = float(p.get("perda_diaria_acumulada_usdt", 0))
    return acumulado >= limite


def _verificar_pode_entrar(p: dict, symbol: str) -> tuple:
    """
    Valida todas as regras antes de abrir uma posição.
    Retorna (pode_entrar: bool, motivo: str).
    """
    if not p.get("bot_active", False):
        return False, "Bot do usuário está pausado"

    saldo_disp = float(p.get("saldo_disponivel_usdt", 0))
    risco = float(p.get("risco_por_trade", 0.05))
    max_pos = int(p.get("max_posicoes_abertas", 3))
    posicoes = p.get("user_positions", [])

    if _verificar_perda_diaria(p):
        return False, "Limite de perda diária atingido"

    if len(posicoes) >= max_pos:
        return False, f"Limite de posições abertas atingido ({len(posicoes)}/{max_pos})"

    # Não abre segunda posição no mesmo símbolo
    for pos in posicoes:
        if pos.get("symbol") == symbol:
            return False, f"Já existe posição aberta em {symbol}"

    valor_entrada = _calcular_valor_entrada(saldo_disp, risco)
    if valor_entrada <= 0:
        return False, f"Valor de entrada calculado abaixo do mínimo (saldo: ${saldo_disp:.2f}, risco: {risco*100:.1f}%)"

    return True, "OK"


@app.route('/robo/user/open-position', methods=['POST'])
def user_open_position():
    """
    Abre uma posição individual para um usuário específico.
    Debita saldo disponível, registra posição isolada.
    NÃO mistura capital entre usuários.

    Body JSON:
      code: str          - código do usuário
      symbol: str        - ex: BTCUSDT
      preco_entrada: float
      quantidade: float  - quantidade da moeda comprada
      take_profit: float (opcional)
      stop_loss: float (opcional)
      trailing_stop: float (opcional)
      origem: str        - ex: SCANNER_MODE
      score: int         - score do scanner (opcional)
    """
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip().upper()
        symbol = (data.get("symbol") or "").strip().upper()
        preco_entrada = float(data.get("preco_entrada", 0) or 0)
        quantidade = float(data.get("quantidade", 0) or 0)

        if not code or not symbol or preco_entrada <= 0 or quantidade <= 0:
            return jsonify({"success": False, "message": "code, symbol, preco_entrada e quantidade são obrigatórios"}), 400

        participations = load_participations()

        # ── Carteira Master ──────────────────────────────────────────────
        if code in MASTER_WALLET_CODES:
            master = _load_master_positions()
            saldo_disp = float(master.get("saldo_disponivel_usdt", 0))
            valor_entrada_usdt = round(preco_entrada * quantidade, 4)

            if valor_entrada_usdt < 10.0:
                return jsonify({"success": False, "message": "Valor mínimo de entrada é 10 USDT"}), 400
            if valor_entrada_usdt > saldo_disp:
                return jsonify({"success": False, "message": f"Saldo insuficiente: ${saldo_disp:.2f} disponível"}), 400

            nova_posicao = {
                "id": f"{code}_{symbol}_{int(time.time())}",
                "usuario_id": code,
                "symbol": symbol,
                "preco_entrada": preco_entrada,
                "quantidade": quantidade,
                "valor_entrada_usdt": valor_entrada_usdt,
                "take_profit": float(data.get("take_profit", 0) or 0),
                "stop_loss": float(data.get("stop_loss", 0) or 0),
                "trailing_stop": float(data.get("trailing_stop", 0) or 0),
                "origem": data.get("origem", "MANUAL"),
                "score": data.get("score", 0),
                "timestamp": datetime.now().isoformat(),
                "status": "aberta",
            }

            master["positions_open"].append(nova_posicao)
            master["saldo_disponivel_usdt"] = round(saldo_disp - valor_entrada_usdt, 4)
            master["saldo_em_posicoes_usdt"] = round(
                float(master.get("saldo_em_posicoes_usdt", 0)) + valor_entrada_usdt, 4
            )
            _save_master_positions(master)

            logger.info(f"📈 [MASTER] Posição aberta: {symbol} | ${valor_entrada_usdt:.2f} | preço: {preco_entrada}")
            return jsonify({
                "success": True,
                "message": f"Posição master aberta: {symbol}",
                "posicao": nova_posicao,
                "saldo_disponivel_usdt": master["saldo_disponivel_usdt"],
            })

        # ── Usuário Individual ───────────────────────────────────────────
        if code not in participations:
            return jsonify({"success": False, "message": "Usuário não encontrado"}), 404

        # ── Lock individual: garante que 2 threads não abram posição simultânea ──
        with get_user_lock(code):
            # Recarregar dentro do lock para garantir estado atual
            participations = load_participations()
            if code not in participations:
                return jsonify({"success": False, "message": "Usuário não encontrado"}), 404

        p = _ensure_user_bot_fields(participations[code])
        origem = data.get("origem", "MANUAL")

        # Quando chamado pelo robô do pool (ROBO_POOL), usa quantidade/valor direto do payload
        # e não exige bot_active — o robô decide quem recebe a posição
        if origem == "ROBO_POOL":
            # Usa a quantidade proporcional já calculada pelo robotic.py
            quantidade_ajustada = float(quantidade)
            valor_real = round(quantidade_ajustada * preco_entrada, 4)
            if valor_real < 0.01:
                return jsonify({"success": False, "message": "Valor proporcional abaixo do mínimo"}), 400

            # Verificar apenas se já tem posição aberta no mesmo symbol
            for pos_exist in p.get("user_positions", []):
                if pos_exist.get("symbol") == symbol:
                    return jsonify({"success": False, "message": f"Já existe posição em {symbol}"}), 400
        else:
            # Entrada manual ou do bot individual — verifica todas as regras
            pode, motivo = _verificar_pode_entrar(p, symbol)
            if not pode:
                return jsonify({"success": False, "message": motivo}), 400

            saldo_disp = float(p.get("saldo_disponivel_usdt", 0))
            risco = float(p.get("risco_por_trade", 0.05))
            valor_entrada_usdt = _calcular_valor_entrada(saldo_disp, risco)

            quantidade_ajustada = round(valor_entrada_usdt / preco_entrada, 8)
            valor_real = round(quantidade_ajustada * preco_entrada, 4)

            if valor_real < 10.0:
                return jsonify({"success": False, "message": f"Valor calculado ${valor_real:.2f} abaixo do mínimo de $10"}), 400

        nova_posicao = {
            "id": f"{code}_{symbol}_{int(time.time())}",
            "usuario_id": code,
            "symbol": symbol,
            "preco_entrada": preco_entrada,
            "quantidade": quantidade_ajustada,
            "valor_entrada_usdt": valor_real,
            "take_profit": float(data.get("take_profit", 0) or 0),
            "stop_loss": float(data.get("stop_loss", 0) or 0),
            "trailing_stop": float(data.get("trailing_stop", 0) or 0),
            "origem": data.get("origem", "SCANNER"),
            "score": data.get("score", 0),
            "timestamp": datetime.now().isoformat(),
            "status": "aberta",
        }

        p["user_positions"].append(nova_posicao)
        p["saldo_disponivel_usdt"] = round(saldo_disp - valor_real, 4)
        p["saldo_em_posicoes_usdt"] = round(
            float(p.get("saldo_em_posicoes_usdt", 0)) + valor_real, 4
        )
        p["updated_at"] = datetime.now().isoformat()

        save_participations(participations)

        logger.info(f"📈 [{code}] Posição aberta: {symbol} | ${valor_real:.2f} | preço: {preco_entrada}")
        audit_event("USER_OPEN_POSITION", True, code,
                    f"Posição aberta: {symbol} ${valor_real:.2f}",
                    {"symbol": symbol, "valor": valor_real, "preco": preco_entrada})

        return jsonify({
            "success": True,
            "message": f"Posição aberta: {symbol}",
            "posicao": nova_posicao,
            "saldo_disponivel_usdt": p["saldo_disponivel_usdt"],
            "saldo_em_posicoes_usdt": p["saldo_em_posicoes_usdt"],
        })

    except Exception as e:
        logger.error(f"Erro /robo/user/open-position: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/user/close-position', methods=['POST'])
def user_close_position():
    """
    Fecha uma posição individual do usuário usando valor REAL de mercado.
    Atualiza saldo com lucro ou prejuízo real.
    Registra no histórico individual do usuário.

    Body JSON:
      code: str
      symbol: str
      preco_atual: float - preço atual de mercado para calcular PnL real
      motivo: str        - ex: TAKE_PROFIT, STOP_LOSS, TRAILING_STOP, REVERSAO
    """
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip().upper()
        symbol = (data.get("symbol") or "").strip().upper()
        preco_atual = float(data.get("preco_atual", 0) or 0)
        motivo = data.get("motivo", "MANUAL")

        if not code or not symbol or preco_atual <= 0:
            return jsonify({"success": False, "message": "code, symbol e preco_atual são obrigatórios"}), 400

        participations = load_participations()

        # ── Carteira Master ──────────────────────────────────────────────
        if code in MASTER_WALLET_CODES:
            master = _load_master_positions()
            posicoes = master.get("positions_open", [])
            pos_idx = next((i for i, p in enumerate(posicoes) if p.get("symbol") == symbol), None)

            if pos_idx is None:
                return jsonify({"success": False, "message": f"Posição {symbol} não encontrada na carteira master"}), 404

            pos = posicoes[pos_idx]
            preco_entrada = float(pos.get("preco_entrada", 0))
            quantidade = float(pos.get("quantidade", 0))
            valor_entrada = float(pos.get("valor_entrada_usdt", 0))

            # Calcular valor real de saída
            valor_saida = round(quantidade * preco_atual, 4)
            pnl_usdt = round(valor_saida - valor_entrada, 4)
            pnl_pct = round(((preco_atual - preco_entrada) / preco_entrada * 100) if preco_entrada > 0 else 0, 2)

            fechado = {**pos,
                       "preco_saida": preco_atual,
                       "valor_saida_usdt": valor_saida,
                       "pnl_usdt": pnl_usdt,
                       "pnl_percent": pnl_pct,
                       "motivo_saida": motivo,
                       "status": "fechada",
                       "timestamp_saida": datetime.now().isoformat()}

            master["positions_open"].pop(pos_idx)
            master["positions_closed"].append(fechado)
            master["saldo_disponivel_usdt"] = round(
                float(master.get("saldo_disponivel_usdt", 0)) + valor_saida, 4
            )
            master["saldo_em_posicoes_usdt"] = max(0.0, round(
                float(master.get("saldo_em_posicoes_usdt", 0)) - valor_entrada, 4
            ))
            _save_master_positions(master)

            logger.info(f"📉 [MASTER] Posição fechada: {symbol} | PnL: {pnl_usdt:+.4f} USDT | motivo: {motivo}")
            return jsonify({
                "success": True, "message": f"Posição master fechada: {symbol}",
                "pnl_usdt": pnl_usdt, "pnl_percent": pnl_pct,
                "valor_saida_usdt": valor_saida,
                "saldo_disponivel_usdt": master["saldo_disponivel_usdt"],
            })

        # ── Usuário Individual ───────────────────────────────────────────
        if code not in participations:
            return jsonify({"success": False, "message": "Usuário não encontrado"}), 404

        p = _ensure_user_bot_fields(participations[code])
        posicoes = p.get("user_positions", [])
        pos_idx = next((i for i, pos in enumerate(posicoes) if pos.get("symbol") == symbol), None)

        if pos_idx is None:
            return jsonify({"success": False, "message": f"Posição {symbol} não encontrada para {code}"}), 404

        pos = posicoes[pos_idx]
        preco_entrada = float(pos.get("preco_entrada", 0))
        quantidade = float(pos.get("quantidade", 0))
        valor_entrada = float(pos.get("valor_entrada_usdt", 0))

        # Calcular valor REAL de saída (não valor fixo)
        valor_saida = round(quantidade * preco_atual, 4)
        pnl_usdt = round(valor_saida - valor_entrada, 4)
        pnl_pct = round(((preco_atual - preco_entrada) / preco_entrada * 100) if preco_entrada > 0 else 0, 2)

        fechado = {**pos,
                   "preco_saida": preco_atual,
                   "valor_saida_usdt": valor_saida,
                   "pnl_usdt": pnl_usdt,
                   "pnl_percent": pnl_pct,
                   "motivo_saida": motivo,
                   "status": "fechada",
                   "timestamp_saida": datetime.now().isoformat()}

        p["user_positions"].pop(pos_idx)
        if "user_closed_trades" not in p:
            p["user_closed_trades"] = []
        p["user_closed_trades"].append(fechado)

        # ── Calcular PnL líquido com fee 10% ───────────────────────────────────
        # Regra White Paper:
        #   Lucro:    pnl_liquido = pnl_bruto * 50% pool usuarios (SEM fee)
        #   Prejuízo: pnl_liquido = pnl_bruto * (1 + 10% fee)  ← fee adicional só no prejuízo
        FEE = 0.10
        POOL_SPLIT = 0.50
        if pnl_usdt >= 0:
            pnl_liquido = round(pnl_usdt * POOL_SPLIT, 4)   # ✅ Lucro: 50% sem fee
            fee_cobrada = 0.0
        else:
            pnl_liquido = round(pnl_usdt * (1 + FEE), 4)    # ✅ Prejuízo: +10% fee
            fee_cobrada = round(abs(pnl_usdt) * FEE, 4)

        # Atualizar saldo disponivel individual (recebe valor real de mercado)
        p["saldo_disponivel_usdt"] = round(
            float(p.get("saldo_disponivel_usdt", 0)) + valor_saida, 4
        )
        p["saldo_em_posicoes_usdt"] = max(0.0, round(
            float(p.get("saldo_em_posicoes_usdt", 0)) - valor_entrada, 4
        ))

        # Atualizar virtual_balance_usdt com PnL LÍQUIDO
        # Lucro: +50% do trade. Prejuízo: -proporcional -10% fee
        saldo_virtual_antes = float(p.get("virtual_balance_usdt", 0))
        saldo_virtual_novo = max(0.0, round(saldo_virtual_antes + pnl_liquido, 4))
        p["virtual_balance_usdt"] = saldo_virtual_novo

        # Acumular lucro/prejuízo realizado (bruto, sem fee aplicada novamente no front)
        # O front usa o campo profit_accumulated_usdt como BRUTO e aplica a regra de exibição
        p["profit_accumulated_usdt"] = round(
            float(p.get("profit_accumulated_usdt", 0)) + pnl_liquido, 4
        )

        fechado["pnl_liquido_usdt"] = pnl_liquido
        fechado["fee_usdt"] = round(fee_cobrada, 4)
        fechado["virtual_balance_antes"] = saldo_virtual_antes
        fechado["virtual_balance_depois"] = saldo_virtual_novo

        logger.info(
            f"   💰 PnL bruto: {pnl_usdt:+.4f} USDT | "
            f"Fee: {fee_cobrada:.4f} USDT (só no prejuízo) | "
            f"PnL líquido: {pnl_liquido:+.4f} USDT | "
            f"Saldo virtual: {saldo_virtual_antes:.4f} → {saldo_virtual_novo:.4f}"
        )

        # Atualizar perda diária se houve prejuízo
        if pnl_liquido < 0:
            hoje = datetime.now().strftime("%Y-%m-%d")
            if p.get("perda_diaria_data") != hoje:
                p["perda_diaria_acumulada_usdt"] = 0.0
                p["perda_diaria_data"] = hoje
            p["perda_diaria_acumulada_usdt"] = round(
                float(p.get("perda_diaria_acumulada_usdt", 0)) + abs(pnl_liquido), 4
            )

        p["updated_at"] = datetime.now().isoformat()
        save_participations(participations)

        logger.info(f"📉 [{code}] Posição fechada: {symbol} | PnL: {pnl_usdt:+.4f} USDT | motivo: {motivo}")
        audit_event("USER_CLOSE_POSITION", True, code,
                    f"Posição fechada: {symbol} PnL={pnl_usdt:+.4f} USDT motivo={motivo}",
                    {"symbol": symbol, "pnl_usdt": pnl_usdt, "motivo": motivo})

        return jsonify({
            "success": True,
            "message": f"Posição fechada: {symbol}",
            "pnl_usdt": pnl_usdt,
            "pnl_liquido_usdt": pnl_liquido,
            "fee_usdt": fechado.get("fee_usdt", 0),
            "pnl_percent": pnl_pct,
            "valor_entrada_usdt": valor_entrada,
            "valor_saida_usdt": valor_saida,
            "saldo_disponivel_usdt": p["saldo_disponivel_usdt"],
            "saldo_em_posicoes_usdt": p["saldo_em_posicoes_usdt"],
            "virtual_balance_usdt": p["virtual_balance_usdt"],
        })

    except Exception as e:
        logger.error(f"Erro /robo/user/close-position: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/user/monitor-positions', methods=['POST'])
def monitor_user_positions():
    """
    Chamado pelo robotic.py a cada ciclo com os preços atuais.
    Verifica TP/SL para TODOS os usuários ativos e emite sinais de saída.
    NÃO executa vendas — apenas notifica quais posições devem ser fechadas.
    O robotic.py executa as vendas e depois chama /robo/user/close-position.

    Body JSON:
      prices: dict  - {symbol: preco_atual, ...}
      tendencias: dict (opcional) - {symbol: "bearish"|"bullish"|..., ...}
    """
    try:
        data = request.get_json(silent=True) or {}
        prices = data.get("prices", {})
        tendencias = data.get("tendencias", {})

        if not prices:
            return jsonify({"success": False, "message": "prices obrigatório"}), 400

        participations = load_participations()
        saidas_necessarias = []

        for code, p_raw in participations.items():
            p = _ensure_user_bot_fields(p_raw)
            posicoes = p.get("user_positions", [])

            if not posicoes:
                continue

            for pos in posicoes:
                sym = pos.get("symbol", "")
                preco_entrada = float(pos.get("preco_entrada", 0))
                take_profit = float(pos.get("take_profit", 0))
                stop_loss = float(pos.get("stop_loss", 0))
                trailing_stop = float(pos.get("trailing_stop", 0))
                preco_atual = float(prices.get(sym, 0))

                if preco_atual <= 0 or preco_entrada <= 0:
                    continue

                motivo_saida = None

                # 1. Take Profit atingido
                if take_profit > 0 and preco_atual >= take_profit:
                    motivo_saida = "TAKE_PROFIT"

                # 2. Stop Loss atingido
                elif stop_loss > 0 and preco_atual <= stop_loss:
                    motivo_saida = "STOP_LOSS"

                # 3. Trailing Stop acionado
                elif trailing_stop > 0:
                    preco_max_ref = float(pos.get("preco_max_ref", preco_entrada))
                    if preco_atual > preco_max_ref:
                        pos["preco_max_ref"] = preco_atual  # atualizar referência
                    trailing_nivel = float(pos.get("preco_max_ref", preco_entrada)) * (1 - trailing_stop)
                    if preco_atual <= trailing_nivel:
                        motivo_saida = "TRAILING_STOP"

                # 4. Reversão bearish detectada
                elif tendencias.get(sym) in ("bearish", "strong_bearish"):
                    # Só sai se estiver em lucro (proteção de ganho)
                    if preco_atual > preco_entrada * 1.003:  # +0.3% mínimo de lucro para sair por reversão
                        motivo_saida = "REVERSAO_BEARISH"

                if motivo_saida:
                    valor_saida = round(float(pos.get("quantidade", 0)) * preco_atual, 4)
                    pnl = round(valor_saida - float(pos.get("valor_entrada_usdt", 0)), 4)
                    saidas_necessarias.append({
                        "code": code,
                        "symbol": sym,
                        "preco_atual": preco_atual,
                        "motivo": motivo_saida,
                        "pnl_estimado_usdt": pnl,
                        "posicao_id": pos.get("id", ""),
                    })

        # Verificar também carteira master
        master = _load_master_positions()
        for pos in master.get("positions_open", []):
            sym = pos.get("symbol", "")
            preco_entrada = float(pos.get("preco_entrada", 0))
            take_profit = float(pos.get("take_profit", 0))
            stop_loss = float(pos.get("stop_loss", 0))
            trailing_stop = float(pos.get("trailing_stop", 0))
            preco_atual = float(prices.get(sym, 0))

            if preco_atual <= 0 or preco_entrada <= 0:
                continue

            motivo_saida = None
            if take_profit > 0 and preco_atual >= take_profit:
                motivo_saida = "TAKE_PROFIT"
            elif stop_loss > 0 and preco_atual <= stop_loss:
                motivo_saida = "STOP_LOSS"
            elif trailing_stop > 0:
                preco_max_ref = float(pos.get("preco_max_ref", preco_entrada))
                if preco_atual > preco_max_ref:
                    pos["preco_max_ref"] = preco_atual
                trailing_nivel = preco_max_ref * (1 - trailing_stop)
                if preco_atual <= trailing_nivel:
                    motivo_saida = "TRAILING_STOP"

            if motivo_saida:
                valor_saida = round(float(pos.get("quantidade", 0)) * preco_atual, 4)
                pnl = round(valor_saida - float(pos.get("valor_entrada_usdt", 0)), 4)
                saidas_necessarias.append({
                    "code": MASTER_WALLET_CODES[0],
                    "symbol": sym,
                    "preco_atual": preco_atual,
                    "motivo": motivo_saida,
                    "pnl_estimado_usdt": pnl,
                    "is_master": True,
                })

        # Salvar estado atualizado (trailing stops atualizados)
        save_participations(participations)

        return jsonify({
            "success": True,
            "saidas_necessarias": saidas_necessarias,
            "total_verificadas": sum(
                len(_ensure_user_bot_fields(p).get("user_positions", []))
                for p in participations.values()
            ),
        })

    except Exception as e:
        logger.error(f"Erro /robo/user/monitor-positions: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/user/calcular-entrada', methods=['GET'])
def user_calcular_entrada():
    """
    Calcula o valor de entrada ideal para o usuário baseado no seu saldo e risco.
    GET /robo/user/calcular-entrada?code=ROBO-XXXX

    Retorna:
      valor_entrada_usdt: float - valor calculado (0 se abaixo do mínimo)
      pode_entrar: bool
      motivo: str
    """
    try:
        code = (request.args.get("code") or "").strip().upper()
        if not code:
            return jsonify({"success": False, "message": "code obrigatório"}), 400

        participations = load_participations()
        if code not in participations:
            return jsonify({"success": False, "message": "Usuário não encontrado"}), 404

        p = _ensure_user_bot_fields(participations[code])
        saldo_disp = float(p.get("saldo_disponivel_usdt", 0))
        risco = float(p.get("risco_por_trade", 0.05))
        valor = _calcular_valor_entrada(saldo_disp, risco)

        pode = valor >= 10.0 and p.get("bot_active", False)
        motivo = "OK" if pode else (
            "Bot pausado" if not p.get("bot_active", False) else
            f"Valor calculado ${valor:.2f} abaixo do mínimo $10"
        )

        return jsonify({
            "success": True,
            "code": code,
            "saldo_disponivel_usdt": round(saldo_disp, 4),
            "risco_por_trade": risco,
            "valor_entrada_usdt": round(valor, 4),
            "pode_entrar": pode,
            "motivo": motivo,
            "max_posicoes_abertas": p.get("max_posicoes_abertas", 3),
            "posicoes_abertas": len(p.get("user_positions", [])),
            "bot_active": p.get("bot_active", False),
        })

    except Exception as e:
        logger.error(f"Erro /robo/user/calcular-entrada: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/master/config', methods=['GET', 'POST'])
def master_config():
    """
    GET: Retorna configuração atual da carteira master.
    POST: Atualiza configuração (aporte, risco, max_posicoes).
    Requer chave de admin.
    """
    try:
        auth_ok, msg = require_admin_auth()
        if not auth_ok:
            return jsonify({"success": False, "message": msg}), 403

        master = _load_master_positions()

        if request.method == 'GET':
            return jsonify({
                "success": True,
                "saldo_disponivel_usdt": round(float(master.get("saldo_disponivel_usdt", 0)), 4),
                "saldo_em_posicoes_usdt": round(float(master.get("saldo_em_posicoes_usdt", 0)), 4),
                "aporte_usdt": round(float(master.get("aporte_usdt", 0)), 2),
                "risco_por_trade": float(master.get("risco_por_trade", 0.05)),
                "max_posicoes_abertas": int(master.get("max_posicoes_abertas", 10)),
                "positions_open_count": len(master.get("positions_open", [])),
                "positions_closed_count": len(master.get("positions_closed", [])),
            })

        data = request.get_json(silent=True) or {}

        if "aporte_usdt" in data:
            val = float(data["aporte_usdt"])
            if val >= 0:
                master["aporte_usdt"] = val
                em_posicao = float(master.get("saldo_em_posicoes_usdt", 0))
                master["saldo_disponivel_usdt"] = max(0.0, round(val - em_posicao, 4))

        if "risco_por_trade" in data:
            val = float(data["risco_por_trade"])
            if 0.001 <= val <= 1.0:
                master["risco_por_trade"] = val

        if "max_posicoes_abertas" in data:
            val = int(data["max_posicoes_abertas"])
            if 1 <= val <= 50:
                master["max_posicoes_abertas"] = val

        _save_master_positions(master)

        return jsonify({
            "success": True,
            "message": "Configuração master salva",
            "aporte_usdt": master.get("aporte_usdt"),
            "risco_por_trade": master.get("risco_por_trade"),
            "saldo_disponivel_usdt": master.get("saldo_disponivel_usdt"),
        })

    except Exception as e:
        logger.error(f"Erro /robo/master/config: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/master/positions', methods=['GET'])
def master_positions():
    """
    Retorna posições abertas e fechadas da carteira master.
    Requer chave de admin.
    """
    try:
        auth_ok, msg = require_admin_auth()
        if not auth_ok:
            return jsonify({"success": False, "message": msg}), 403

        master = _load_master_positions()
        usdt_brl = get_current_usdt_brl()

        positions_open = master.get("positions_open", [])
        with _live_state_lock:
            _live_snap = list(_live_state.get("positions_open", []))
        live_prices = {pos.get("symbol"): pos for pos in _live_snap}

        enriched = []
        for pos in positions_open:
            sym = pos.get("symbol", "")
            entry_price = float(pos.get("preco_entrada", 0))
            qty = float(pos.get("quantidade", 0))
            valor_entrada = float(pos.get("valor_entrada_usdt", 0))
            live = live_prices.get(sym)
            current_price = float(live.get("current_price", entry_price)) if live else entry_price
            pnl = round((current_price - entry_price) * qty, 4)
            pnl_pct = round(((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0, 2)
            enriched.append({
                **pos,
                "preco_atual": current_price,
                "valor_atual_usdt": round(qty * current_price, 4),
                "pnl_usdt": pnl,
                "pnl_brl": round(pnl * usdt_brl, 2),
                "pnl_percent": pnl_pct,
            })

        lucro_realizado = sum(float(t.get("pnl_usdt", 0)) for t in master.get("positions_closed", []))

        return jsonify({
            "success": True,
            "positions_open": enriched,
            "positions_count": len(enriched),
            "closed_trades": master.get("positions_closed", [])[-20:],
            "lucro_realizado_usdt": round(lucro_realizado, 4),
            "saldo_disponivel_usdt": round(float(master.get("saldo_disponivel_usdt", 0)), 4),
            "saldo_em_posicoes_usdt": round(float(master.get("saldo_em_posicoes_usdt", 0)), 4),
            "usdt_brl": round(usdt_brl, 2),
        })

    except Exception as e:
        logger.error(f"Erro /robo/master/positions: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/robo/risk/overview', methods=['GET'])
def risk_overview():
    """
    Visão geral de gestão de risco de todos os usuários.
    Mostra capital alocado, posições abertas, e PnL individual.
    Requer chave de admin.
    """
    try:
        auth_ok, msg = require_admin_auth()
        if not auth_ok:
            return jsonify({"success": False, "message": msg}), 403

        participations = load_participations()
        usdt_brl = get_current_usdt_brl()
        with _live_state_lock:
            _live_snap2 = list(_live_state.get("positions_open", []))
        live_prices = {pos.get("symbol"): float(pos.get("current_price", 0))
                       for pos in _live_snap2}

        usuarios_summary = []
        total_capital_alocado = 0.0
        total_em_posicoes = 0.0
        total_disponivel = 0.0

        for code, p_raw in participations.items():
            p = _ensure_user_bot_fields(p_raw)
            aporte = float(p.get("aporte_usdt", 0))
            disp = float(p.get("saldo_disponivel_usdt", 0))
            em_pos = float(p.get("saldo_em_posicoes_usdt", 0))
            posicoes = p.get("user_positions", [])

            # PnL flutuante das posições abertas
            pnl_flotante = 0.0
            for pos in posicoes:
                sym = pos.get("symbol", "")
                entry = float(pos.get("preco_entrada", 0))
                qty = float(pos.get("quantidade", 0))
                current = live_prices.get(sym, entry)
                if entry > 0 and current > 0:
                    pnl_flotante += (current - entry) * qty

            lucro_realizado = sum(float(t.get("pnl_liquido_usdt", t.get("pnl_usdt", 0))) for t in p.get("user_closed_trades", []))

            total_capital_alocado += aporte
            total_em_posicoes += em_pos
            total_disponivel += disp

            if aporte > 0 or posicoes:
                usuarios_summary.append({
                    "code": code,
                    "bot_active": p.get("bot_active", False),
                    "aporte_usdt": round(aporte, 2),
                    "saldo_disponivel_usdt": round(disp, 4),
                    "saldo_em_posicoes_usdt": round(em_pos, 4),
                    "risco_por_trade": float(p.get("risco_por_trade", 0.05)),
                    "max_posicoes_abertas": int(p.get("max_posicoes_abertas", 3)),
                    "posicoes_count": len(posicoes),
                    "pnl_flotante_usdt": round(pnl_flotante, 4),
                    "lucro_realizado_usdt": round(lucro_realizado, 4),
                    "perda_diaria_limite_usdt": float(p.get("perda_diaria_limite_usdt", 0)),
                    "perda_diaria_acumulada_usdt": float(p.get("perda_diaria_acumulada_usdt", 0)),
                    "bloqueado_perda_diaria": _verificar_perda_diaria(p),
                })

        # Master
        master = _load_master_positions()
        master_summary = {
            "code": MASTER_WALLET_CODES[0] if MASTER_WALLET_CODES else "MASTER",
            "aporte_usdt": round(float(master.get("aporte_usdt", 0)), 2),
            "saldo_disponivel_usdt": round(float(master.get("saldo_disponivel_usdt", 0)), 4),
            "saldo_em_posicoes_usdt": round(float(master.get("saldo_em_posicoes_usdt", 0)), 4),
            "posicoes_count": len(master.get("positions_open", [])),
            "lucro_realizado_usdt": round(
                sum(float(t.get("pnl_usdt", 0)) for t in master.get("positions_closed", [])), 4
            ),
        }

        return jsonify({
            "success": True,
            "usuarios": usuarios_summary,
            "master": master_summary,
            "totais": {
                "total_usuarios_com_aporte": len(usuarios_summary),
                "total_capital_alocado_usdt": round(total_capital_alocado, 2),
                "total_em_posicoes_usdt": round(total_em_posicoes, 4),
                "total_disponivel_usdt": round(total_disponivel, 4),
            },
            "usdt_brl": round(usdt_brl, 2),
        })

    except Exception as e:
        logger.error(f"Erro /robo/risk/overview: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


# ============================================
# 🔥 NOVO: ENDPOINT PARA TRADES DO CICLO ATUAL
# ============================================

@app.route('/api/trades/last-cycle', methods=['GET'])
def api_last_cycle_trades():
    """
    Retorna trades fechados apenas do ciclo atual (UTC YYYY-MM-DD)
    Endpoint usado pelo painel.html para exibir trades recentes
    """
    try:
        wallet_code = request.args.get('code', '').strip().upper()
        
        if not wallet_code:
            return jsonify({
                "success": False,
                "message": "Código da carteira é obrigatório"
            }), 400
        
        # Carregar transações
        transactions = load_transactions()
        
        # Definir ciclo atual (UTC) e ciclo anterior (para cobrir virada de meia-noite UTC vs BRT)
        current_cycle = datetime.utcnow().strftime("%Y-%m-%d")
        previous_cycle = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        accepted_cycles = {current_cycle, previous_cycle}
        
        # Filtrar apenas trades fechados do ciclo atual para esta carteira
        trades = []
        # Janela de fallback: últimos 7 dias para transações sem cycle_id
        fallback_cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        
        for tx in transactions:
            # Verificar se é da carteira solicitada
            tx_wallet = tx.get('wallet_code', '').upper()
            tx_user = tx.get('user_code', '').upper()
            
            if tx_wallet != wallet_code and tx_user != wallet_code:
                continue
            
            # Verificar se é um trade fechado (loss_distribution, profit_distribution, trade_closed_*)
            tx_type = tx.get('type', '').lower()
            
            # Tipos de transação que representam trades fechados
            trade_types = [
                'loss_distribution', 
                'profit_distribution', 
                'trade_closed_loss',
                'trade_closed_profit',
                'trade_closed_breakeven',
                'trade_closed',            # tipo genérico legado
                'trade_closed_no_capital', # fechamento por falta de capital
            ]
            
            if tx_type not in trade_types:
                continue
            
            # Verificar ciclo_id — se presente, filtrar pelo ciclo atual ou anterior (tolerância de fuso BRT/UTC)
            tx_cycle = tx.get('cycle_id')
            if tx_cycle:
                if tx_cycle not in accepted_cycles:
                    continue
            else:
                # sem cycle_id → aceitar se dentro dos últimos 7 dias
                tx_date_str = tx.get('date', '')
                if tx_date_str and tx_date_str < fallback_cutoff:
                    continue
            
            # Extrair símbolo — campo direto ou via trade_id
            symbol = tx.get('symbol', '')
            if not symbol and tx.get('trade_id'):
                trade_id_raw = tx.get('trade_id', '')
                # Normalizar: remover prefixos PROF-, LOSS-, TRADE-
                for pfx in ('PROF-', 'LOSS-', 'TRADE-LOSS-', 'TRADE-'):
                    if trade_id_raw.startswith(pfx):
                        trade_id_raw = trade_id_raw[len(pfx):]
                        break
                parts = trade_id_raw.split('_')
                if parts and parts[0].endswith('USDT'):
                    symbol = parts[0]
            
            # Calcular PnL líquido (o que o usuário realmente ganhou/perdeu)
            pnl_liquido_usdt = 0.0
            
            if 'profit' in tx_type:
                # Distribuição de lucro já é o valor líquido para o usuário (50% do bruto)
                pnl_liquido_usdt = float(tx.get('amount_usdt', 0))
            elif 'loss' in tx_type:
                # Distribuição de prejuízo já é o valor líquido (prejuízo + 10% fee)
                pnl_liquido_usdt = float(tx.get('amount_usdt', 0))
            else:
                # Fallback
                pnl_liquido_usdt = float(tx.get('amount_usdt', tx.get('pnl_usdt', 0)))
            
            # Construir objeto do trade
            trade = {
                "id": tx.get('id', ''),
                "trade_id": tx.get('trade_id', ''),
                "symbol": symbol,
                "type": tx_type,
                "result": "profit" if pnl_liquido_usdt > 0 else "loss" if pnl_liquido_usdt < 0 else "breakeven",
                "pnl_usdt": round(pnl_liquido_usdt, 4),
                "pnl_liquido_usdt": round(pnl_liquido_usdt, 4),  # Já é líquido
                "date": tx.get('date', ''),
                "exit_time": tx.get('date', tx.get('timestamp_saida', '')),
                "cycle_id": tx_cycle or current_cycle,
                "exit_reason": tx.get('description', '').split(':')[0] if ':' in tx.get('description', '') else 'FECHADO',
                "motivo_saida": tx.get('motivo_saida', tx.get('description', '').split(':')[0] if ':' in tx.get('description', '') else 'FECHADO'),
                "duration_minutes": tx.get('duration_minutes', 0),
                "fee_usdt": float(tx.get('penalidade_usdt', tx.get('fee_usdt', 0))),
                "currency": "USDT"
            }
            
            trades.append(trade)
        
        # Ordenar do mais recente para o mais antigo
        trades.sort(key=lambda x: x.get('date', ''), reverse=True)
        
        # Limitar a 30 trades para não sobrecarregar o frontend
        limited_trades = trades[:30]
        
        logger.info(f"📊 Ciclo atual {current_cycle}: {len(limited_trades)} trades encontrados para {wallet_code}")
        
        return jsonify({
            "success": True,
            "cycle_id": current_cycle,
            "wallet_code": wallet_code,
            "trades": limited_trades,
            "total": len(trades),
            "display_limit": 30
        })
        
    except Exception as e:
        logger.error(f"💥 Erro em /api/trades/last-cycle: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Erro interno: {str(e)}"
        }), 500


# ============================================
# 🔥 MODIFICAÇÃO: Adicionar cycle_id ao registrar trades
# ============================================

# Procure pela função distribuir_lucro_proporcional (por volta da linha 3200)
# e adicione cycle_id em todas as transações de trade

# Exemplo de como deve ficar dentro da função (trecho ilustrativo):
"""
# Dentro de distribuir_lucro_proporcional, ao criar transações de LUCRO:
tx_id = f"PROF-{trade_id}-{p['user_code']}"
transactions.append({
    "id": tx_id,
    "trade_id": trade_id,
    "user_code": p["user_code"],
    "wallet_code": p["user_code"],
    "type": "profit_distribution",
    "amount_usdt": lucro_usuario,
    "date": datetime.now().isoformat(),
    "status": "completed",
    "proporcao_saldo_atual": proporcao_saldo_atual,
    "description": f"Lucro trade {trade_id}: {description}",
    "virtual_balance_before_usdt": saldo_antes,
    "virtual_balance_after_usdt": p["virtual_balance_usdt"],
    "currency": "USDT",
    "cycle_id": datetime.utcnow().strftime("%Y-%m-%d")  # <-- ADICIONADO
})

# Dentro de distribuir_lucro_proporcional, ao criar transações de PERDA:
tx_id = f"LOSS-{trade_id}-{p['user_code']}"
transactions.append({
    "id": tx_id,
    "trade_id": trade_id,
    "user_code": p["user_code"],
    "wallet_code": p["user_code"],
    "type": "loss_distribution",
    "amount_usdt": round(-perda_usuario, 8),
    "perda_bruta_usdt": round(perda_usuario, 8),
    "penalidade_usdt": 0.0,
    "date": datetime.now().isoformat(),
    "status": "completed",
    "proporcao_saldo_atual": round(proporcao, 8),
    "description": f"Perda proporcional trade {trade_id}: {description}",
    "virtual_balance_before_usdt": saldo_antes,
    "virtual_balance_after_usdt": novo_saldo,
    "currency": "USDT",
    "cycle_id": datetime.utcnow().strftime("%Y-%m-%d")  # <-- ADICIONADO
})
"""

# IMPORTANTE: Adicione a mesma linha em TODAS as transações de trade:
# - profit_distribution
# - loss_distribution
# - trade_closed_profit
# - trade_closed_loss
# - trade_closed_breakeven
# - loss_penalty_income
# - company_fee_income

# ═══════════════════════════════════════════════════════════════════════════
# FIM — GESTÃO DE RISCO INDIVIDUAL
# ═══════════════════════════════════════════════════════════════════════════



@app.route('/api/ranking', methods=['GET'])
def api_ranking():
    """Ranking publico de investidores por lucratividade"""
    try:
        participations = load_participations()
        usdt_brl = get_current_usdt_brl()

        ranking = []
        for code, p in participations.items():
            if p.get('status') != 'active':
                continue
            if p.get('is_company_wallet', False):
                continue  # nao expoe carteira da empresa no ranking publico

            vb          = float(p.get('virtual_balance_usdt', 0))
            dep         = float(p.get('total_deposited_usdt', 0))
            profit_usdt = vb - dep

            ranking.append({
                'wallet_code':      code,
                'name':             p.get('display_name', code),
                'current_balance':  round(vb * usdt_brl, 2),
                'total_deposited':  round(dep * usdt_brl, 2),
                'profit':           round(profit_usdt * usdt_brl, 2),
                'profit_usdt':      round(profit_usdt, 4),
                'last_transaction': p.get('updated_at', ''),
            })

        ranking.sort(key=lambda x: x['profit'], reverse=True)

        return jsonify({
            'success': True,
            'ranking': ranking,
            'total_investors': len(ranking),
            'total_volume_brl': round(sum(r['total_deposited'] for r in ranking), 2),
            'total_profit_brl': round(sum(r['profit'] for r in ranking), 2),
        })
    except Exception as e:
        logger.error(f"Erro /api/ranking: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

if __name__ == "__main__":
    # ── Logs de boot (ANTES do app.run, que bloqueia) ──────────────────
    logger.info("=" * 70)
    logger.info("🚀 COYOTE CRYPTO SERVER - INICIANDO (USDT CORE)")
    logger.info("🔒 MODO SEGURO: Sem correções automáticas")
    logger.info(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    wallets_count        = len(load_wallets())
    users_count          = len(load_users())
    participations_count = len(load_participations())
    transactions_count   = len(load_transactions())

    logger.info("📊 ESTATÍSTICAS INICIAIS (USDT):")
    logger.info(f"   👤 Usuários: {users_count}")
    logger.info(f"   💰 Carteiras: {wallets_count}")
    logger.info(f"   📈 Participações: {participations_count}")
    logger.info(f"   📝 Transações: {transactions_count}")

    logger.info("🔍 Verificando consistência (apenas leitura)...")
    consistency = validate_system_consistency()

    if not consistency.get("success"):
        issues_count = len(consistency.get("issues", []))
        logger.warning(f"⚠️  {issues_count} problemas detectados (requer ação manual)")
        logger.warning("   📋 Use /robo/admin/pending-fixes para detalhes")
    else:
        logger.info("✅ Sistema consistente")

    logger.info("🔄 Iniciando tarefas de background...")

    cleanup_thread = threading.Thread(target=cleanup_periodic_tasks, daemon=True)
    cleanup_thread.start()

    try:
        flow_thread = threading.Thread(target=update_flow_averages_thread, daemon=True)
        flow_thread.start()
        logger.info("   📊 Thread de médias iniciada")
    except Exception:
        logger.info("   ⏭️  Thread de médias não configurada")

    # Nota: _scanner_bg_thread e _user_bot_thread já foram iniciadas no nível
    # do módulo, acima do if __name__ == "__main__", não precisa reiniciar aqui.
    logger.info("   🔄 Scanner background thread: já ativa (iniciada no módulo)")
    logger.info("   🤖 User Bot Loop thread: já ativa (iniciada no módulo)")
    logger.info("✅ Tarefas de background iniciadas")

    logger.info("=" * 70)
    logger.info("🚨 POLÍTICA DE SEGURANÇA ATIVA (USDT CORE):")
    logger.info("   ✅ Sistema baseado em USDT para todos os saldos")
    logger.info("   ✅ BRL/BTC apenas como view/conversão")
    logger.info("   ❌ NENHUMA correção automática")
    logger.info("   ❌ NENHUMA verificação automática de depósitos")
    logger.info("   ❌ NENHUMA alteração automática de dados")
    logger.info("   ✅ Tudo via endpoints admin autenticados")
    logger.info("=" * 70)
    logger.info("📡 Servidor na porta 5000 — aguardando requisições...")
    logger.info("=" * 70)

    # ── app.run ÚNICO e correto — deve ser a última instrução ──────────
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=False,
        threaded=True,
        use_reloader=False
    )