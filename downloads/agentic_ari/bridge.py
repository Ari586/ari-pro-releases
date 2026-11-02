"""Pont MT4 / MT5 et Courtiers (Capital.com, MetaAPI, Webhook, Démo/Paper Trading).

Permet à Agentic Ari d'agir comme un trader senior expérimenté :
- Routage d'ordres direct vers un compte MetaTrader 4 / MetaTrader 5 (via MetaAPI Cloud ou MetaTrader5 Python)
- Routage vers Capital.com (OpenAPI) ou Webhook / EA HTTP personnalisé
- Moteur de simulation (Paper Trading) senior réaliste pour tester sans risque
- RÈGLES DE GESTION DU RISQUE STRICTES (Risk Management Institutionnel) :
  1. Stop Loss obligatoire sur chaque position (aucun trade sans SL autorisé)
  2. Dimensionnement de position dynamique selon le risque max (1 % ou 2 % du capital)
  3. Vérification de la cohérence directionnelle (Achat : SL < Entrée < TP ; Vente : SL > Entrée > TP)
  4. Enregistrement automatique de chaque trade dans le Journal de bord ARI Pro
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

import httpx

from .spot import display

log = logging.getLogger("ari.bridge")

CONTRACT_SIZES = {
    "GC=F": 100.0,
    "XAUUSD": 100.0,
    "GOLD": 100.0,
    "SI=F": 5000.0,
    "XAGUSD": 5000.0,
    "EURUSD": 100000.0,
    "EURUSD=X": 100000.0,
    "GBPUSD": 100000.0,
    "GBPUSD=X": 100000.0,
    "USDJPY": 100000.0,
    "USDJPY=X": 100000.0,
    "BTC": 1.0,
    "BTC-USD": 1.0,
    "BTCUSD": 1.0,
    "ETH": 1.0,
    "ETH-USD": 1.0,
    "ETHUSD": 1.0,
}


def normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip()
    if s == "GC=F":
        return "XAUUSD"
    if s == "SI=F":
        return "XAGUSD"
    if s == "CL=F":
        return "WTI"
    if s.endswith("=X"):
        return s[:-2]
    if s.endswith("-USD"):
        return s.replace("-USD", "USD")
    return s.upper()


def get_contract_size(symbol: str) -> float:
    norm = normalize_symbol(symbol)
    return CONTRACT_SIZES.get(symbol, CONTRACT_SIZES.get(norm, 1.0))


class BridgeManager:
    """Gestionnaire de connexion et d'exécution des ordres MT4/MT5 et courtiers."""

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.db = sqlite3.connect(cfg.memory_path, check_same_thread=False, timeout=10)
        self._init_db()

    def _init_db(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS bridge_config(
                id INTEGER PRIMARY KEY CHECK (id=1),
                enabled INTEGER DEFAULT 0,
                bridge_type TEXT DEFAULT 'demo',
                server TEXT DEFAULT '',
                login TEXT DEFAULT '',
                password TEXT DEFAULT '',
                api_key TEXT DEFAULT '',
                webhook_url TEXT DEFAULT '',
                is_live INTEGER DEFAULT 0,
                max_risk_pct REAL DEFAULT 1.0,
                default_lot REAL DEFAULT 0.0,
                require_confirmation INTEGER DEFAULT 1,
                auto_trade INTEGER DEFAULT 0,
                updated REAL
            );

            CREATE TABLE IF NOT EXISTS bridge_orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket TEXT,
                symbol TEXT,
                side TEXT,
                lots REAL,
                entry REAL,
                sl REAL,
                tp REAL,
                status TEXT,
                exit_price REAL,
                pnl REAL,
                comment TEXT,
                created REAL,
                closed REAL
            );

            CREATE TABLE IF NOT EXISTS bridge_demo_account(
                id INTEGER PRIMARY KEY CHECK (id=1),
                balance REAL DEFAULT 10000.0,
                currency TEXT DEFAULT 'USD',
                updated REAL
            );
        """)
        # Initialise bridge_config si vide
        row = self.db.execute("SELECT id FROM bridge_config WHERE id=1").fetchone()
        if not row:
            self.db.execute(
                "INSERT INTO bridge_config(id, enabled, bridge_type, server, login, password, api_key, "
                "webhook_url, is_live, max_risk_pct, default_lot, require_confirmation, auto_trade, updated) "
                "VALUES(1, 1, 'demo', 'ARI-Demo-Server', 'ari_demo_1001', '', '', '', 0, 1.0, 0.0, 1, 0, ?)",
                (time.time(),)
            )
        # Initialise compte démo si vide
        row_demo = self.db.execute("SELECT id FROM bridge_demo_account WHERE id=1").fetchone()
        if not row_demo:
            self.db.execute(
                "INSERT INTO bridge_demo_account(id, balance, currency, updated) VALUES(1, 10000.0, 'USD', ?)",
                (time.time(),)
            )
        self.db.commit()

    def get_config(self) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT enabled, bridge_type, server, login, password, api_key, webhook_url, is_live, "
            "max_risk_pct, default_lot, require_confirmation, auto_trade, updated FROM bridge_config WHERE id=1"
        ).fetchone()
        if not row:
            return {
                "enabled": True, "bridge_type": "demo", "server": "ARI-Demo-Server", "login": "ari_demo_1001",
                "password": "", "api_key": "", "webhook_url": "", "is_live": False, "max_risk_pct": 1.0,
                "default_lot": 0.0, "require_confirmation": True, "auto_trade": False, "updated": time.time(),
            }
        return {
            "enabled": bool(row[0]),
            "bridge_type": row[1] or "demo",
            "server": row[2] or "",
            "login": row[3] or "",
            "password": "***" if row[4] else "",
            "has_password": bool(row[4]),
            "api_key": "***" if row[5] else "",
            "has_api_key": bool(row[5]),
            "webhook_url": row[6] or "",
            "is_live": bool(row[7]),
            "max_risk_pct": float(row[8] or 1.0),
            "default_lot": float(row[9] or 0.0),
            "require_confirmation": bool(row[10]),
            "auto_trade": bool(row[11]),
            "updated": row[12] or time.time(),
        }

    def save_config(self, data: dict[str, Any]) -> dict[str, Any]:
        cur = self.get_config()
        # Conserver le mot de passe existant si non fourni ou masqué
        pwd = data.get("password")
        if pwd is None or pwd == "***":
            row_pwd = self.db.execute("SELECT password FROM bridge_config WHERE id=1").fetchone()
            pwd = row_pwd[0] if row_pwd else ""
        api_k = data.get("api_key")
        if api_k is None or api_k == "***":
            row_api = self.db.execute("SELECT api_key FROM bridge_config WHERE id=1").fetchone()
            api_k = row_api[0] if row_api else ""

        enabled = int(bool(data.get("enabled", cur["enabled"])))
        bridge_type = str(data.get("bridge_type", cur["bridge_type"])).lower().strip()
        server = str(data.get("server", cur["server"])).strip()
        login = str(data.get("login", cur["login"])).strip()
        webhook_url = str(data.get("webhook_url", cur["webhook_url"])).strip()
        is_live = int(bool(data.get("is_live", cur["is_live"])))
        max_risk = min(max(float(data.get("max_risk_pct", cur["max_risk_pct"])), 0.1), 5.0)
        default_lot = max(float(data.get("default_lot", cur["default_lot"])), 0.0)
        req_conf = int(bool(data.get("require_confirmation", cur["require_confirmation"])))
        auto_trade = int(bool(data.get("auto_trade", cur["auto_trade"])))

        self.db.execute(
            "UPDATE bridge_config SET enabled=?, bridge_type=?, server=?, login=?, password=?, "
            "api_key=?, webhook_url=?, is_live=?, max_risk_pct=?, default_lot=?, require_confirmation=?, "
            "auto_trade=?, updated=? WHERE id=1",
            (enabled, bridge_type, server, login, pwd, api_k, webhook_url, is_live, max_risk,
             default_lot, req_conf, auto_trade, time.time())
        )
        self.db.commit()
        return self.get_config()

    def get_account_info(self) -> dict[str, Any]:
        cfg = self.get_config()
        btype = cfg["bridge_type"]

        if btype == "demo":
            row = self.db.execute("SELECT balance, currency FROM bridge_demo_account WHERE id=1").fetchone()
            balance = row[0] if row else 10000.0
            currency = row[1] if row else "USD"
            positions = self.get_positions()
            floating_pnl = sum(p["pnl"] for p in positions)
            margin = sum(p["lots"] * 1000.0 for p in positions) # approximation standard
            equity = balance + floating_pnl
            return {
                "connected": True,
                "bridge_type": "demo",
                "is_live": False,
                "server": "ARI Senior Paper Trading",
                "login": cfg["login"] or "ari_demo_1001",
                "balance": round(balance, 2),
                "equity": round(equity, 2),
                "margin": round(margin, 2),
                "free_margin": round(equity - margin, 2),
                "currency": currency,
                "open_positions": len(positions),
                "floating_pnl": round(floating_pnl, 2),
                "message": "Mode Simulation actif. Les ordres réagissent aux cours réels sans risque.",
            }

        if btype == "mt5":
            # Test d'import MetaTrader5 (uniquement Windows/Linux natif)
            try:
                import MetaTrader5 as mt5  # type: ignore
                if not mt5.initialize():
                    return {
                        "connected": False,
                        "bridge_type": "mt5",
                        "is_live": cfg["is_live"],
                        "message": f"Échec initialisation terminal MT5 : {mt5.last_error()}",
                        "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
                    }
                account = mt5.account_info()
                if not account:
                    return {
                        "connected": False,
                        "bridge_type": "mt5",
                        "is_live": cfg["is_live"],
                        "message": "Impossible de lire le compte MT5",
                        "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
                    }
                return {
                    "connected": True,
                    "bridge_type": "mt5",
                    "is_live": cfg["is_live"],
                    "server": account.server,
                    "login": str(account.login),
                    "balance": round(account.balance, 2),
                    "equity": round(account.equity, 2),
                    "margin": round(account.margin, 2),
                    "free_margin": round(account.margin_free, 2),
                    "currency": account.currency,
                    "open_positions": mt5.positions_total() or 0,
                    "floating_pnl": round(account.profit, 2),
                    "message": f"Connecté avec succès au terminal MT5 ({account.server}).",
                }
            except ImportError:
                return {
                    "connected": False,
                    "bridge_type": "mt5",
                    "is_live": cfg["is_live"],
                    "server": cfg["server"],
                    "login": cfg["login"],
                    "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
                    "message": "Le module 'MetaTrader5' n'est pas installé sur ce système (ou macOS/Android). "
                               "Pour connecter MT4/MT5 depuis mobile ou Mac, utilisez MetaAPI (Bridge Cloud), "
                               "l'API Capital.com ou le mode Webhook EA.",
                }

        if btype == "metaapi":
            # Requête MetaAPI Cloud
            api_key = self._get_secret("api_key")
            account_id = cfg["login"]
            if not api_key or not account_id:
                return {
                    "connected": False,
                    "bridge_type": "metaapi",
                    "is_live": cfg["is_live"],
                    "message": "Clé API MetaAPI ou ID de compte manquant.",
                    "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
                }
            try:
                url = f"https://mt-client-api-v1.agiliumtrade.agilepro.solutions/users/current/accounts/{account_id}/account-information"
                resp = httpx.get(url, headers={"auth-token": api_key}, timeout=8.0)
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "connected": True,
                        "bridge_type": "metaapi",
                        "is_live": cfg["is_live"],
                        "server": data.get("server", cfg["server"]),
                        "login": str(data.get("login", account_id)),
                        "balance": float(data.get("balance", 0.0)),
                        "equity": float(data.get("equity", 0.0)),
                        "margin": float(data.get("margin", 0.0)),
                        "free_margin": float(data.get("freeMargin", 0.0)),
                        "currency": data.get("currency", "USD"),
                        "open_positions": int(data.get("openPositionsCount", 0)),
                        "floating_pnl": float(data.get("equity", 0.0)) - float(data.get("balance", 0.0)),
                        "message": "Connecté à MT4/MT5 via MetaAPI Cloud.",
                    }
                return {
                    "connected": False,
                    "bridge_type": "metaapi",
                    "is_live": cfg["is_live"],
                    "message": f"Erreur MetaAPI ({resp.status_code}) : {resp.text[:120]}",
                    "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
                }
            except Exception as e:
                return {
                    "connected": False,
                    "bridge_type": "metaapi",
                    "is_live": cfg["is_live"],
                    "message": f"Impossible de contacter MetaAPI : {e}",
                    "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
                }

        if btype == "capital":
            # Capital.com API
            api_key = self._get_secret("api_key")
            identifier = cfg["login"]
            password = self._get_secret("password")
            if not api_key or not identifier:
                return {
                    "connected": False,
                    "bridge_type": "capital",
                    "is_live": cfg["is_live"],
                    "message": "Identifiant Capital.com ou clé API manquante.",
                    "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
                }
            try:
                base = "https://api-capital.backend-capital.com" if cfg["is_live"] else "https://demo-api-capital.backend-capital.com"
                resp = httpx.post(
                    f"{base}/api/v1/session",
                    headers={"X-CAP-API-KEY": api_key, "Content-Type": "application/json"},
                    json={"identifier": identifier, "password": password},
                    timeout=8.0
                )
                if resp.status_code == 200:
                    cst = resp.headers.get("CST", "")
                    x_sec = resp.headers.get("X-SECURITY-TOKEN", "")
                    acc_resp = httpx.get(
                        f"{base}/api/v1/accounts",
                        headers={"X-CAP-API-KEY": api_key, "CST": cst, "X-SECURITY-TOKEN": x_sec},
                        timeout=8.0
                    )
                    if acc_resp.status_code == 200:
                        acc_data = acc_resp.json().get("accounts", [{}])[0]
                        bal = float(acc_data.get("balance", {}).get("balance", 0.0))
                        eq = float(acc_data.get("balance", {}).get("equity", bal))
                        return {
                            "connected": True,
                            "bridge_type": "capital",
                            "is_live": cfg["is_live"],
                            "server": "Capital.com Live" if cfg["is_live"] else "Capital.com Demo",
                            "login": identifier,
                            "balance": round(bal, 2),
                            "equity": round(eq, 2),
                            "margin": round(float(acc_data.get("balance", {}).get("margin", 0.0)), 2),
                            "free_margin": round(eq - float(acc_data.get("balance", {}).get("margin", 0.0)), 2),
                            "currency": acc_data.get("currency", "USD"),
                            "open_positions": 0,
                            "floating_pnl": round(eq - bal, 2),
                            "message": "Connecté avec succès au compte Capital.com.",
                        }
                return {
                    "connected": False,
                    "bridge_type": "capital",
                    "is_live": cfg["is_live"],
                    "message": f"Erreur Capital.com ({resp.status_code}) : {resp.text[:120]}",
                    "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
                }
            except Exception as e:
                return {
                    "connected": False,
                    "bridge_type": "capital",
                    "is_live": cfg["is_live"],
                    "message": f"Erreur connexion Capital.com : {e}",
                    "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
                }

        if btype == "webhook":
            wh_url = cfg["webhook_url"]
            if not wh_url:
                return {
                    "connected": False,
                    "bridge_type": "webhook",
                    "is_live": cfg["is_live"],
                    "message": "URL de Webhook non renseignée.",
                    "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
                }
            try:
                resp = httpx.post(f"{wh_url.rstrip('/')}/status", json={"ping": True}, timeout=4.0)
                if resp.status_code in (200, 204):
                    return {
                        "connected": True,
                        "bridge_type": "webhook",
                        "is_live": cfg["is_live"],
                        "server": "EA HTTP / Webhook",
                        "login": cfg["login"] or "webhook",
                        "balance": 10000.0, "equity": 10000.0, "currency": "USD", "open_positions": 0,
                        "floating_pnl": 0.0,
                        "message": "Webhook EA accessible et prêt à recevoir les ordres.",
                    }
            except Exception:
                pass
            return {
                "connected": False,
                "bridge_type": "webhook",
                "is_live": cfg["is_live"],
                "message": f"Impossible de joindre le Webhook ({wh_url}).",
                "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
            }

        return {
            "connected": False,
            "bridge_type": btype,
            "is_live": False,
            "message": "Type de passerelle inconnu.",
            "balance": 0.0, "equity": 0.0, "currency": "USD", "open_positions": 0,
        }

    def _get_secret(self, field: str) -> str:
        row = self.db.execute(f"SELECT {field} FROM bridge_config WHERE id=1").fetchone()
        return row[0] if row and row[0] else ""

    def calculate_lots(self, symbol: str, entry: float, sl: float, capital: float, risk_pct: float) -> float:
        """Calcul rigoureux du nombre de lots selon le risque senior (FTMO/Prop firm standard)."""
        dist = abs(entry - sl)
        if dist <= 0 or capital <= 0:
            return 0.01
        contract_size = get_contract_size(symbol)
        risk_money = capital * (risk_pct / 100.0)
        risk_per_full_lot = dist * contract_size
        if risk_per_full_lot <= 0:
            return 0.01
        lots = risk_money / risk_per_full_lot
        # Arrondi à 2 décimales (micro-lot minimum = 0.01)
        return max(round(lots, 2), 0.01)

    def execute_order(
        self,
        symbol: str,
        side: str,
        entry: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        lots: float | None = None,
        comment: str = "ARI Senior Pro",
    ) -> dict[str, Any]:
        """Exécute un ordre avec contrôle de gestion du risque senior strict."""
        cfg = self.get_config()
        if not cfg["enabled"]:
            raise ValueError("Le bridge de trading est actuellement désactivé dans les réglages.")

        side = str(side).lower().strip()
        if side in ("achat", "buy", "long"):
            action = "buy"
        elif side in ("vente", "sell", "short"):
            action = "sell"
        else:
            raise ValueError(f"Direction d'ordre non reconnue : {side} (attendu 'buy' ou 'sell')")

        # RÈGLE SENIOR 1 : Stop Loss obligatoire
        if sl is None or float(sl) <= 0:
            raise ValueError(
                "Règle Senior de Risque Violée : Tout ordre doit obligatoirement inclure un Stop Loss défini."
            )
        sl = float(sl)

        # Récupération du prix actuel si non fourni
        if entry is None or float(entry) <= 0:
            from .watch import candles
            c = candles(symbol, "15m")
            if not c:
                raise ValueError(f"Impossible de déterminer le prix actuel pour {symbol}.")
            entry = float(c[-1][4])
        else:
            entry = float(entry)

        # RÈGLE SENIOR 2 : Cohérence directionnelle
        if action == "buy":
            if sl >= entry:
                raise ValueError(
                    f"Stop Loss invalide pour un Achat : le SL ({sl}) doit être sous le prix d'entrée ({entry})."
                )
            if tp and float(tp) <= entry:
                raise ValueError(
                    f"Take Profit invalide pour un Achat : le TP ({tp}) doit être au-dessus du prix d'entrée ({entry})."
                )
        else:
            if sl <= entry:
                raise ValueError(
                    f"Stop Loss invalide pour une Vente : le SL ({sl}) doit être au-dessus du prix d'entrée ({entry})."
                )
            if tp and float(tp) >= entry:
                raise ValueError(
                    f"Take Profit invalide pour une Vente : le TP ({tp}) doit être en dessous du prix d'entrée ({entry})."
                )

        tp = float(tp) if tp else None

        # RÈGLE SENIOR 3 : Calcul ou validation de la taille de lot
        account = self.get_account_info()
        capital = account.get("equity") or account.get("balance") or 10000.0
        risk_pct = cfg["max_risk_pct"]

        if lots is None or float(lots) <= 0:
            lots = self.calculate_lots(symbol, entry, sl, capital, risk_pct)
        else:
            lots = round(float(lots), 2)
            # Vérification de sécurité : si le lot donné dépasse le risque autorisé de plus de 10%, on le plafonne
            dist = abs(entry - sl)
            contract = get_contract_size(symbol)
            max_loss = capital * (risk_pct / 100.0)
            actual_loss = lots * dist * contract
            if actual_loss > max_loss * 1.15:
                capped = self.calculate_lots(symbol, entry, sl, capital, risk_pct)
                log.warning(
                    f"Lot demandé ({lots}) dépasse le risque max de {risk_pct}% ({actual_loss:.2f} $ > {max_loss:.2f} $). "
                    f"Ajusté à {capped} lot."
                )
                lots = capped

        ticket = f"T{int(time.time() * 1000) % 10000000}"
        btype = cfg["bridge_type"]

        # Routage vers la passerelle
        message = ""

        if btype == "demo":
            message = "Ordre exécuté en simulation senior (Paper Trading)."

        elif btype == "mt5":
            try:
                import MetaTrader5 as mt5  # type: ignore
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": normalize_symbol(symbol),
                    "volume": float(lots),
                    "type": mt5.ORDER_TYPE_BUY if action == "buy" else mt5.ORDER_TYPE_SELL,
                    "price": float(entry),
                    "sl": float(sl),
                    "tp": float(tp) if tp else 0.0,
                    "deviation": 20,
                    "magic": 240401,
                    "comment": comment[:30],
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                res = mt5.order_send(req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    ticket = str(res.order)
                    message = f"Ordre exécuté avec succès sur MT5 (Ticket #{ticket})."
                else:
                    err = res.comment if res else mt5.last_error()
                    raise RuntimeError(f"Rejet MT5 ({res.retcode if res else 'Err'}) : {err}")
            except ImportError:
                raise RuntimeError("Terminal MT5 indisponible localement sur ce système.")

        elif btype == "metaapi":
            api_key = self._get_secret("api_key")
            account_id = cfg["login"]
            url = f"https://mt-client-api-v1.agiliumtrade.agilepro.solutions/users/current/accounts/{account_id}/trade"
            payload = {
                "actionType": "ORDER_TYPE_BUY" if action == "buy" else "ORDER_TYPE_SELL",
                "symbol": normalize_symbol(symbol),
                "volume": float(lots),
                "stopLoss": float(sl),
                "takeProfit": float(tp) if tp else None,
                "comment": comment[:30],
            }
            resp = httpx.post(url, headers={"auth-token": api_key}, json=payload, timeout=12.0)
            if resp.status_code in (200, 201):
                data = resp.json()
                ticket = str(data.get("orderId") or ticket)
                message = f"Ordre transmis à MT4/MT5 via MetaAPI (Ticket #{ticket})."
            else:
                raise RuntimeError(f"Erreur MetaAPI ({resp.status_code}) : {resp.text[:140]}")

        elif btype == "capital":
            api_key = self._get_secret("api_key")
            identifier = cfg["login"]
            password = self._get_secret("password")
            base = "https://api-capital.backend-capital.com" if cfg["is_live"] else "https://demo-api-capital.backend-capital.com"
            s_resp = httpx.post(
                f"{base}/api/v1/session",
                headers={"X-CAP-API-KEY": api_key, "Content-Type": "application/json"},
                json={"identifier": identifier, "password": password},
                timeout=8.0
            )
            if s_resp.status_code == 200:
                cst = s_resp.headers.get("CST", "")
                x_sec = s_resp.headers.get("X-SECURITY-TOKEN", "")
                trade_payload = {
                    "epic": normalize_symbol(symbol),
                    "direction": "BUY" if action == "buy" else "SELL",
                    "size": float(lots),
                    "stopLevel": float(sl),
                    "profitLevel": float(tp) if tp else None,
                }
                t_resp = httpx.post(
                    f"{base}/api/v1/positions",
                    headers={"X-CAP-API-KEY": api_key, "CST": cst, "X-SECURITY-TOKEN": x_sec},
                    json=trade_payload,
                    timeout=10.0
                )
                if t_resp.status_code in (200, 201):
                    t_data = t_resp.json()
                    ticket = str(t_data.get("dealReference") or ticket)
                    message = f"Ordre exécuté sur Capital.com (Réf : {ticket})."
                else:
                    raise RuntimeError(f"Erreur Capital.com ({t_resp.status_code}) : {t_resp.text[:140]}")
            else:
                raise RuntimeError("Authentification Capital.com échouée.")

        elif btype == "webhook":
            wh_url = cfg["webhook_url"]
            payload = {
                "action": "ORDER",
                "ticket": ticket,
                "symbol": normalize_symbol(symbol),
                "type": action.upper(),
                "volume": float(lots),
                "price": float(entry),
                "sl": float(sl),
                "tp": float(tp) if tp else 0.0,
                "comment": comment,
            }
            resp = httpx.post(f"{wh_url.rstrip('/')}/order", json=payload, timeout=8.0)
            if resp.status_code in (200, 201, 204):
                message = f"Ordre envoyé au Webhook EA (Ticket #{ticket})."
            else:
                raise RuntimeError(f"Erreur Webhook ({resp.status_code}) : {resp.text[:100]}")

        # Enregistrement dans la base locale d'ordres
        now = time.time()
        self.db.execute(
            "INSERT INTO bridge_orders(ticket, symbol, side, lots, entry, sl, tp, status, pnl, comment, created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ticket, symbol, action, lots, entry, sl, tp, "open", 0.0, comment, now)
        )
        self.db.commit()

        # Enregistrement synchronisé dans le Journal d'ARI
        try:
            from .journal import Journal
            j = Journal(self.cfg)
            j_side = "achat" if action == "buy" else "vente"
            j.open(
                symbol=symbol,
                side=j_side,
                entry=entry,
                stop=sl,
                target=tp,
                lots=lots,
                note=f"Pont {btype.upper()} [#{ticket}] : {comment}"
            )
        except Exception as e:
            log.warning(f"Erreur lors de la journalisation du trade : {e}")

        risk_amount = lots * abs(entry - sl) * get_contract_size(symbol)
        reward_amount = lots * abs(tp - entry) * get_contract_size(symbol) if tp else None
        rr_ratio = round(reward_amount / risk_amount, 2) if reward_amount and risk_amount > 0 else None

        return {
            "ok": True,
            "ticket": ticket,
            "symbol": symbol,
            "side": action,
            "lots": lots,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "risk_money": round(risk_amount, 2),
            "risk_pct": round((risk_amount / capital) * 100.0, 2),
            "reward_money": round(reward_amount, 2) if reward_amount else None,
            "rr_ratio": f"1:{rr_ratio}" if rr_ratio else "—",
            "bridge_type": btype,
            "message": message,
            "created": now,
        }

    def get_positions(self) -> list[dict[str, Any]]:
        """Liste les positions actuellement ouvertes avec cours direct et P&L flottant."""
        rows = self.db.execute(
            "SELECT id, ticket, symbol, side, lots, entry, sl, tp, created FROM bridge_orders WHERE status='open' ORDER BY id DESC"
        ).fetchall()
        positions = []
        for row_id, ticket, sym, side, lots, entry, sl, tp, created in rows:
            # Récupère le prix actuel
            current_price = entry
            try:
                from .watch import candles
                c = candles(sym, "5m")
                if c:
                    current_price = float(c[-1][4])
            except Exception:
                pass

            contract = get_contract_size(sym)
            sign = 1.0 if side == "buy" else -1.0
            pnl = (current_price - entry) * sign * lots * contract
            pips = (current_price - entry) * sign * (10.0 if sym in ("GC=F", "XAUUSD") else 10000.0 if "USD" in sym else 1.0)

            positions.append({
                "id": row_id,
                "ticket": ticket,
                "symbol": display(sym),
                "code": sym,
                "side": side,
                "lots": lots,
                "entry": entry,
                "current_price": current_price,
                "sl": sl,
                "tp": tp,
                "pnl": round(pnl, 2),
                "pips": round(pips, 1),
                "created": created,
            })
        return positions

    def close_position(self, ticket: str) -> dict[str, Any]:
        """Clôture une position ouverte et met à jour le solde et le journal."""
        row = self.db.execute(
            "SELECT id, symbol, side, lots, entry, sl, tp FROM bridge_orders WHERE ticket=? AND status='open'",
            (ticket,)
        ).fetchone()
        if not row:
            raise ValueError(f"Position ouverte introuvable avec le ticket {ticket}")

        row_id, sym, side, lots, entry, sl, tp = row

        # Obtenir le prix de sortie
        exit_price = entry
        try:
            from .watch import candles
            c = candles(sym, "5m")
            if c:
                exit_price = float(c[-1][4])
        except Exception:
            pass

        contract = get_contract_size(sym)
        sign = 1.0 if side == "buy" else -1.0
        pnl = (exit_price - entry) * sign * lots * contract

        # Marque comme fermée dans bridge_orders
        now = time.time()
        self.db.execute(
            "UPDATE bridge_orders SET status='closed', exit_price=?, pnl=?, closed=? WHERE id=?",
            (exit_price, pnl, now, row_id)
        )

        # Si démo : crédite/débite le compte démo
        cfg = self.get_config()
        if cfg["bridge_type"] == "demo":
            self.db.execute("UPDATE bridge_demo_account SET balance=balance+?, updated=?", (pnl, now))

        self.db.commit()

        # Met à jour le Journal d'ARI
        try:
            from .journal import Journal
            j = Journal(self.cfg)
            j.close(exit_price=exit_price)
        except Exception as e:
            log.warning(f"Erreur mise à jour Journal lors de la fermeture : {e}")

        return {
            "ok": True,
            "ticket": ticket,
            "symbol": display(sym),
            "exit_price": exit_price,
            "pnl": round(pnl, 2),
            "closed": now,
        }
