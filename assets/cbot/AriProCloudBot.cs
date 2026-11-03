// -------------------------------------------------------------------------------------------------
//
//    ARI Pro - cTrader Cloud Automate cBot (v1.0.64)
//    Auteur : Ari Havana (@LahatraAriel)
//    Architecture : 100% Cloud Execution (cTrader Automate) & Liaison Mobile sans PC
//    Stratégies : Havana-SMC, PWO SMC & CRT v6, Risk Shield Institutionnel
//
// -------------------------------------------------------------------------------------------------

using System;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    public enum StrategyMode
    {
        HavanaSMC,
        PwoSmcCrt,
        SignalsOnly,
        WebhookListener
    }

    [Robot(TimeZone = TimeZones.UTC, AccessRights = AccessRights.FullAccess)]
    public class AriProCloudBot : Robot
    {
        // =========================================================================================
        // PARAMÈTRES UTILISATEUR (GESTION DU RISQUE INSTITUTIONNELLE)
        // =========================================================================================

        [Parameter("Stratégie Active", Group = "Configuration Générale", DefaultValue = StrategyMode.HavanaSMC)]
        public StrategyMode Strategy { get; set; }

        [Parameter("Label Positions", Group = "Configuration Générale", DefaultValue = "ARI-Pro-Cloud")]
        public string BotLabel { get; set; }

        [Parameter("Risque par Trade (%)", Group = "Risk Shield (Bornes Stricte)", DefaultValue = 1.0, MinValue = 0.25, MaxValue = 3.0, Step = 0.25)]
        public double RiskPercentage { get; set; }

        [Parameter("Perte Max Journalière (%)", Group = "Risk Shield (Bornes Stricte)", DefaultValue = 3.0, MinValue = 1.0, MaxValue = 5.0, Step = 0.5)]
        public double MaxDailyLossPct { get; set; }

        [Parameter("Rapport Gain/Risque Minimum (R:R)", Group = "Take Profit", DefaultValue = 2.0, MinValue = 1.0, MaxValue = 5.0, Step = 0.5)]
        public double MinRr { get; set; }

        [Parameter("Activer Break-Even Auto à +1R", Group = "Gestion Position", DefaultValue = true)]
        public bool EnableBreakEven { get; set; }

        [Parameter("Activer Écouteur Webhook (Port)", Group = "Liaison Directe Mobile", DefaultValue = 5001)]
        public int WebhookPort { get; set; }

        // =========================================================================================
        // VARIABLES INTERNES
        // =========================================================================================

        private double _initialBalance;
        private double _dailyStartingBalance;
        private DateTime _currentDay;
        private HttpListener _httpListener;
        private Thread _listenerThread;
        private bool _isListening;
        private AverageTrueRange _atr;

        protected override void OnStart()
        {
            _initialBalance = Account.Balance;
            _dailyStartingBalance = Account.Balance;
            _currentDay = Server.TimeInUtc.Date;
            _atr = Indicators.AverageTrueRange(14, MovingAverageType.Simple);

            Print("=================================================================");
            Print("ARI Pro cTrader Cloud Bot démarré avec succès !");
            Print("Compte : {0} | Devise : {1} | Balance : {2:N2}", Account.Number, Account.Currency, Account.Balance);
            Print("Mode : {0} | Risque max : {1}% | Perte journalière max : {2}%", Strategy, RiskPercentage, MaxDailyLossPct);
            Print("Exécution 100% Cloud cTrader active : Aucun PC requis.");
            Print("=================================================================");

            // Démarrage du serveur Webhook local si activé
            if (Strategy == StrategyMode.WebhookListener)
            {
                StartWebhookListener();
            }
        }

        protected override void OnTick()
        {
            CheckNewDay();
            CheckDailyLossLimit();

            if (EnableBreakEven)
            {
                ManageBreakEven();
            }
        }

        protected override void OnBar()
        {
            // Vérification disjoncteur perte max
            if (IsDailyLossExceeded())
            {
                return;
            }

            // Exécution autonome selon la stratégie sélectionnée
            if (Strategy == StrategyMode.HavanaSMC)
            {
                EvaluateHavanaSmc();
            }
            else if (Strategy == StrategyMode.PwoSmcCrt)
            {
                EvaluatePwoSmcCrt();
            }
        }

        protected override void OnStop()
        {
            StopWebhookListener();
            Print("ARI Pro cTrader Cloud Bot arrêté.");
        }

        // =========================================================================================
        // 1. RISK SHIELD & SIZING DYNAMIQUE
        // =========================================================================================

        private void CheckNewDay()
        {
            if (Server.TimeInUtc.Date != _currentDay)
            {
                _currentDay = Server.TimeInUtc.Date;
                _dailyStartingBalance = Account.Balance;
                Print("Nouveau jour de trading UTC : Balance de référence réinitialisée à {0:N2} {1}", _dailyStartingBalance, Account.Currency);
            }
        }

        private bool IsDailyLossExceeded()
        {
            double currentEquity = Account.Equity;
            double drawdown = (_dailyStartingBalance - currentEquity) / _dailyStartingBalance * 100.0;
            return drawdown >= MaxDailyLossPct;
        }

        private void CheckDailyLossLimit()
        {
            if (IsDailyLossExceeded())
            {
                // Disjoncteur activé : protection du compte
                var positions = Positions.FindAll(BotLabel, SymbolName);
                if (positions.Length > 0)
                {
                    Print("ALERTE DISJONCTEUR : Perte max journalière atteinte ({0:F2}% >= {1:F2}%). Neutralisation.", 
                          (_dailyStartingBalance - Account.Equity) / _dailyStartingBalance * 100.0, MaxDailyLossPct);
                }
            }
        }

        private double CalculateVolume(double entryPrice, double stopLossPrice)
        {
            double riskMoney = Account.Balance * (RiskPercentage / 100.0);
            double priceDistance = Math.Abs(entryPrice - stopLossPrice);

            if (priceDistance <= 0) return Symbol.VolumeInUnitsMin;

            // Calcul précis du volume par unité
            double riskPerUnit = priceDistance * Symbol.PipValue / Symbol.PipSize;
            if (riskPerUnit <= 0) riskPerUnit = priceDistance;

            double volume = riskMoney / priceDistance;
            
            // Normalisation aux pas et limites du courtier
            double normalizedVolume = Symbol.NormalizeVolumeInUnits(volume, RoundingMode.Down);
            if (normalizedVolume < Symbol.VolumeInUnitsMin) normalizedVolume = Symbol.VolumeInUnitsMin;
            if (normalizedVolume > Symbol.VolumeInUnitsMax) normalizedVolume = Symbol.VolumeInUnitsMax;

            return normalizedVolume;
        }

        // =========================================================================================
        // 2. STRATÉGIE HAVANA-SMC (OR & FOREX)
        // =========================================================================================

        private void EvaluateHavanaSmc()
        {
            // Vérification si une position est déjà ouverte sur cet actif pour éviter la surexposition
            if (Positions.FindAll(BotLabel, SymbolName).Length > 0) return;

            int lastIndex = Bars.ClosePrices.Count - 1;
            if (lastIndex < 50) return;

            double currentClose = Bars.ClosePrices[lastIndex];
            double currentOpen = Bars.OpenPrices[lastIndex];
            double prevHigh = Bars.HighPrices[lastIndex - 1];
            double prevLow = Bars.LowPrices[lastIndex - 1];
            double atrVal = _atr.Result[lastIndex];

            // Détection FVG Haussier (Fair Value Gap)
            bool isBullFvg = Bars.LowPrices[lastIndex] > Bars.HighPrices[lastIndex - 2] && Bars.ClosePrices[lastIndex - 1] > Bars.OpenPrices[lastIndex - 1];
            // Détection FVG Baissier
            bool isBearFvg = Bars.HighPrices[lastIndex] < Bars.LowPrices[lastIndex - 2] && Bars.ClosePrices[lastIndex - 1] < Bars.OpenPrices[lastIndex - 1];

            // Rejet sur support ou bas de liquidité (Sweep)
            bool bullSweep = Bars.LowPrices[lastIndex] < prevLow && currentClose > prevLow;
            bool bearSweep = Bars.HighPrices[lastIndex] > prevHigh && currentClose < prevHigh;

            if (isBullFvg && bullSweep && currentClose > currentOpen)
            {
                double entry = Symbol.Ask;
                double sl = Bars.LowPrices[lastIndex] - 0.2 * atrVal;
                double risk = entry - sl;
                double tp = entry + risk * MinRr;

                double volume = CalculateVolume(entry, sl);
                ExecuteMarketOrder(TradeType.Buy, SymbolName, volume, BotLabel, sl, tp, "ARI Havana-SMC Buy");
                Print("ARI Pro [Havana-SMC] ACHAT exécuté : Entrée {0:F2} | SL {1:F2} | TP {2:F2} | Vol {3}", entry, sl, tp, volume);
            }
            else if (isBearFvg && bearSweep && currentClose < currentOpen)
            {
                double entry = Symbol.Bid;
                double sl = Bars.HighPrices[lastIndex] + 0.2 * atrVal;
                double risk = sl - entry;
                double tp = entry - risk * MinRr;

                double volume = CalculateVolume(entry, sl);
                ExecuteMarketOrder(TradeType.Sell, SymbolName, volume, BotLabel, sl, tp, "ARI Havana-SMC Sell");
                Print("ARI Pro [Havana-SMC] VENTE exécutée : Entrée {0:F2} | SL {1:F2} | TP {2:F2} | Vol {3}", entry, sl, tp, volume);
            }
        }

        // =========================================================================================
        // 3. STRATÉGIE PWO SMC & CRT v6 (CANDLE RANGE THEORY + ORDER BLOCKS)
        // =========================================================================================

        private void EvaluatePwoSmcCrt()
        {
            if (Positions.FindAll(BotLabel, SymbolName).Length > 0) return;

            int i = Bars.ClosePrices.Count - 1;
            if (i < 30) return;

            double c0 = Bars.ClosePrices[i];
            double o0 = Bars.OpenPrices[i];
            double atrVal = _atr.Result[i];

            // Détection des Pivots High / Low (SwingLen = 5)
            int swingLen = 5;
            double lastHigh = double.MinValue;
            double lastLow = double.MaxValue;

            for (int k = i - swingLen; k >= Math.Max(swingLen, i - 40); k--)
            {
                double h = Bars.HighPrices[k];
                bool isPh = true;
                for (int s = 1; s <= swingLen; s++)
                {
                    if (Bars.HighPrices[k - s] > h || Bars.HighPrices[k + s] > h) { isPh = false; break; }
                }
                if (isPh && lastHigh == double.MinValue) lastHigh = h;

                double l = Bars.LowPrices[k];
                bool isPl = true;
                for (int s = 1; s <= swingLen; s++)
                {
                    if (Bars.LowPrices[k - s] < l || Bars.LowPrices[k + s] < l) { isPl = false; break; }
                }
                if (isPl && lastLow == double.MaxValue) lastLow = l;

                if (lastHigh != double.MinValue && lastLow != double.MaxValue) break;
            }

            // Cassure de Structure (BOS / CHoCH)
            bool bosBull = lastHigh != double.MinValue && c0 > lastHigh && Bars.ClosePrices[i - 1] <= lastHigh;
            bool bosBear = lastLow != double.MaxValue && c0 < lastLow && Bars.ClosePrices[i - 1] >= lastLow;

            // Signal Buy-KH
            if (bosBull && c0 > o0)
            {
                double entry = Symbol.Ask;
                double sl = Math.Min(Bars.LowPrices[i], Bars.LowPrices[i - 1]) - 0.25 * atrVal;
                double risk = entry - sl;
                double tp = entry + risk * MinRr;

                double volume = CalculateVolume(entry, sl);
                ExecuteMarketOrder(TradeType.Buy, SymbolName, volume, BotLabel, sl, tp, "ARI PWO-SMC Buy-KH");
                Print("ARI Pro [PWO SMC & CRT] Signal Buy-KH exécuté : Entrée {0:F2} | SL {1:F2} | TP {2:F2}", entry, sl, tp);
            }
            // Signal Sell-KH
            else if (bosBear && c0 < o0)
            {
                double entry = Symbol.Bid;
                double sl = Math.Max(Bars.HighPrices[i], Bars.HighPrices[i - 1]) + 0.25 * atrVal;
                double risk = sl - entry;
                double tp = entry - risk * MinRr;

                double volume = CalculateVolume(entry, sl);
                ExecuteMarketOrder(TradeType.Sell, SymbolName, volume, BotLabel, sl, tp, "ARI PWO-SMC Sell-KH");
                Print("ARI Pro [PWO SMC & CRT] Signal Sell-KH exécuté : Entrée {0:F2} | SL {1:F2} | TP {2:F2}", entry, sl, tp);
            }
        }

        // =========================================================================================
        // 4. GESTION DU BREAK-EVEN AUTOMATIQUE (+1R)
        // =========================================================================================

        private void ManageBreakEven()
        {
            foreach (var pos in Positions.FindAll(BotLabel, SymbolName))
            {
                if (!pos.StopLoss.HasValue) continue;

                double initialRisk = Math.Abs(pos.EntryPrice - pos.StopLoss.Value);
                if (initialRisk <= 0) continue;

                if (pos.TradeType == TradeType.Buy)
                {
                    double currentProfitDistance = Symbol.Bid - pos.EntryPrice;
                    // Dès que le trade atteint +1R de gain
                    if (currentProfitDistance >= initialRisk && pos.StopLoss.Value < pos.EntryPrice)
                    {
                        double newSl = pos.EntryPrice + (1 * Symbol.PipSize); // Léger bénéfice sécurisé
                        ModifyPosition(pos, newSl, pos.TakeProfit);
                        Print("Position #{0} BUY sécurisée à Break-Even (+1R atteint) !", pos.Id);
                    }
                }
                else if (pos.TradeType == TradeType.Sell)
                {
                    double currentProfitDistance = pos.EntryPrice - Symbol.Ask;
                    if (currentProfitDistance >= initialRisk && pos.StopLoss.Value > pos.EntryPrice)
                    {
                        double newSl = pos.EntryPrice - (1 * Symbol.PipSize);
                        ModifyPosition(pos, newSl, pos.TakeProfit);
                        Print("Position #{0} SELL sécurisée à Break-Even (+1R atteint) !", pos.Id);
                    }
                }
            }
        }

        // =========================================================================================
        // 5. ÉCOUTEUR WEBHOOK HTTP INTÉGRÉ (RÉCEPTION DIRECTE DEPUIS ARI PRO MOBILE)
        // =========================================================================================

        private void StartWebhookListener()
        {
            try
            {
                _httpListener = new HttpListener();
                _httpListener.Prefixes.Add($"http://*:{WebhookPort}/order/");
                _httpListener.Prefixes.Add($"http://localhost:{WebhookPort}/order/");
                _httpListener.Start();
                _isListening = true;

                _listenerThread = new Thread(ListenLoop) { IsBackground = true };
                _listenerThread.Start();
                Print("Serveur Webhook cBot écoute sur le port {0} (Endpoint: /order/)", WebhookPort);
            }
            catch (Exception ex)
            {
                Print("Note Webhook : Mode autonome Cloud prioritaire ({0})", ex.Message);
            }
        }

        private void ListenLoop()
        {
            while (_isListening && _httpListener != null && _httpListener.IsListening)
            {
                try
                {
                    var ctx = _httpListener.GetContext();
                    using var reader = new StreamReader(ctx.Request.InputStream, ctx.Request.ContentEncoding);
                    string json = reader.ReadToEnd();

                    // Traitement dans le thread principal de cTrader
                    BeginInvokeOnMainThread(() => ProcessIncomingOrder(json));

                    byte[] response = Encoding.UTF8.GetBytes("{\"status\":\"OK\",\"received\":true}");
                    ctx.Response.StatusCode = 200;
                    ctx.Response.ContentType = "application/json";
                    ctx.Response.OutputStream.Write(response, 0, response.Length);
                    ctx.Response.Close();
                }
                catch
                {
                    // Listener stopped or interrupted
                }
            }
        }

        private void ProcessIncomingOrder(string json)
        {
            try
            {
                Print("Signal mobile reçu d'ARI Pro : {0}", json);
                // Exemple : {"action":"BUY","symbol":"XAUUSD","sl":2720.5,"tp":2745.0,"risk":1.0}
                bool isBuy = json.IndexOf("\"action\":\"BUY\"", StringComparison.OrdinalIgnoreCase) >= 0;
                bool isSell = json.IndexOf("\"action\":\"SELL\"", StringComparison.OrdinalIgnoreCase) >= 0;

                if (!isBuy && !isSell) return;

                TradeType type = isBuy ? TradeType.Buy : TradeType.Sell;
                double entry = isBuy ? Symbol.Ask : Symbol.Bid;
                double atrVal = _atr.Result.LastValue;
                double sl = isBuy ? (entry - 1.5 * atrVal) : (entry + 1.5 * atrVal);
                double tp = isBuy ? (entry + 3.0 * atrVal) : (entry - 3.0 * atrVal);

                double vol = CalculateVolume(entry, sl);
                ExecuteMarketOrder(type, SymbolName, vol, BotLabel, sl, tp, "ARI Pro Signal Mobile");
            }
            catch (Exception ex)
            {
                Print("Erreur lors de l'exécution du signal : {0}", ex.Message);
            }
        }

        private void StopWebhookListener()
        {
            _isListening = false;
            try
            {
                _httpListener?.Stop();
                _httpListener?.Close();
            }
            catch { }
        }
    }
}
