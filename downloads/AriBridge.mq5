//+------------------------------------------------------------------+
//| AriBridge.mq5 - ARI Pro <-> MetaTrader 5 bridge (common files)    |
//| Receives ARI Pro orders and places them on this account.          |
//+------------------------------------------------------------------+
#property copyright "ARI Pro"
#property version   "1.21"
#property description "ARI Pro bridge: orders from ARI Pro are placed on this account."
#property description "Keep MetaTrader 5 open, this EA on one chart, Algo Trading on."

#include <Trade/Trade.mqh>

input long InpMagic          = 240401;   // Magic number of ARI Pro orders
input int  InpDeviationPoints = 30;      // Max slippage (points)

CTrade trade;
const string ACCOUNT_FILE = "ari_account.txt";
const string PROTECT_FILE = "ari_protect.txt";   // protection settings sent by ARI Pro (PROTECT)
const string EA_VERSION = "1.21";   // read by ARI Pro (ari_account.txt, QUOTE)

//--- position protection (Break-Even then trailing stop), like RiskShieldBot.
//--- Only positions opened by ARI Pro (magic InpMagic). Pips: point x 10 at 3 or 5 digits, else 1 point.
bool   P_Enabled = false;
bool   P_BeOn    = true;
double P_BeAct   = 150;   // Break-Even from this profit (pips); XAUUSD: ~1.50$
double P_BePlus  = 20;    // stop moved to open +/- these pips
bool   P_TsOn    = true;
double P_TsStart = 250;   // trailing stop from this profit (pips)
double P_TsDist  = 120;   // stop distance from price (pips)
double P_TsStep  = 30;    // only move when the stop improves by at least this (pips)

int OnInit()
  {
   trade.SetExpertMagicNumber((ulong)InpMagic);
   trade.SetDeviationInPoints((ulong)InpDeviationPoints);
   EventSetMillisecondTimer(500);
   LoadProtect();
   WriteAccount();
   return(INIT_SUCCEEDED);
  }

void OnTick()
  {
   Protect();
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   FileDelete(ACCOUNT_FILE, FILE_COMMON);   // ARI sees at once that the bridge stopped
  }

void OnTimer()
  {
   ProcessCommands();
   Protect();   // every symbol of ARI's positions, not only this chart's (OnTick)
   WriteAccount();
  }

//--- files -----------------------------------------------------------
bool WriteText(const string name, const string text)
  {
   string tmp = name + ".tmp";
   int h = FileOpen(tmp, FILE_WRITE | FILE_BIN | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
      return(false);
   FileWriteString(h, text);
   FileClose(h);
   return(FileMove(tmp, FILE_COMMON, name, FILE_COMMON | FILE_REWRITE));
  }

bool ReadKeyValues(const string name, string &keys[], string &vals[])
  {
   int h = FileOpen(name, FILE_READ | FILE_BIN | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
      return(false);
   string text = FileReadString(h, (int)FileSize(h));
   FileClose(h);
   string lines[];
   int n = StringSplit(text, '\n', lines);
   ArrayResize(keys, 0);
   ArrayResize(vals, 0);
   for(int i = 0; i < n; i++)
     {
      string line = lines[i];
      StringReplace(line, "\r", "");
      int eq = StringFind(line, "=");
      if(eq <= 0)
         continue;
      int k = ArraySize(keys);
      ArrayResize(keys, k + 1);
      ArrayResize(vals, k + 1);
      keys[k] = StringSubstr(line, 0, eq);
      vals[k] = StringSubstr(line, eq + 1);
     }
   return(true);
  }

string Get(string &keys[], string &vals[], const string key)
  {
   for(int i = 0; i < ArraySize(keys); i++)
      if(keys[i] == key)
         return(vals[i]);
   return("");
  }

string Clean(string s)
  {
   StringReplace(s, "\r", " ");
   StringReplace(s, "\n", " ");
   return(s);
  }

//--- account ---------------------------------------------------------
void WriteAccount()
  {
   string mode = "demo";
   long m = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(m == ACCOUNT_TRADE_MODE_REAL)
      mode = "real";
   else
      if(m == ACCOUNT_TRADE_MODE_CONTEST)
         mode = "contest";
   string ids = "";
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0)
         continue;
      if(ids != "")
         ids += ",";
      ids += IntegerToString(PositionGetInteger(POSITION_IDENTIFIER));
     }
   bool allowed = TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) != 0 && MQLInfoInteger(MQL_TRADE_ALLOWED) != 0
                  && AccountInfoInteger(ACCOUNT_TRADE_EXPERT) != 0;
   string text = "time=" + IntegerToString((long)TimeGMT()) + "\n"
                 + "login=" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "\n"
                 + "server=" + Clean(AccountInfoString(ACCOUNT_SERVER)) + "\n"
                 + "company=" + Clean(AccountInfoString(ACCOUNT_COMPANY)) + "\n"
                 + "currency=" + AccountInfoString(ACCOUNT_CURRENCY) + "\n"
                 + "balance=" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + "\n"
                 + "equity=" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + "\n"
                 + "margin=" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2) + "\n"
                 + "free_margin=" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + "\n"
                 + "leverage=" + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) + "\n"
                 + "mode=" + mode + "\n"
                 + "connected=" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "1" : "0") + "\n"
                 + "trade_allowed=" + (allowed ? "1" : "0") + "\n"
                 + "positions=" + ids + "\n"
                 + "protect=" + ProtectSignature() + "\n"
                 + "ea_version=" + EA_VERSION + "\n";
   WriteText(ACCOUNT_FILE, text);
  }

//--- commands --------------------------------------------------------
void ProcessCommands()
  {
   string name;
   string names[];
   long h = FileFindFirst("ari_cmd_*.txt", name, FILE_COMMON);
   if(h == INVALID_HANDLE)
      return;
   do
     {
      int n = ArraySize(names);
      ArrayResize(names, n + 1);
      names[n] = name;
     }
   while(FileFindNext(h, name));
   FileFindClose(h);
   for(int i = 0; i < ArraySize(names); i++)
      HandleCommand(names[i]);
  }

void HandleCommand(const string file)
  {
   string keys[], vals[];
   if(!ReadKeyValues(file, keys, vals))
      return;
   if(!FileDelete(file, FILE_COMMON))   // never executed twice
      return;
   string id = Get(keys, vals, "id");
   string action = Get(keys, vals, "action");
   string res;
   if(action == "ORDERS")
      res = PendingOrders();
   else if(action == "POSITIONS")
      res = AriPositions();
   else if(action == "CANCEL")
      res = CancelPending(keys, vals);
   else if(action == "ORDER_STATUS")
      res = PendingStatus(keys, vals);
   else
   if(action == "ORDER")
      res = DoOrder(keys, vals);
   else
      if(action == "CLOSE")
         res = DoClose(keys, vals);
      else
         if(action == "MODIFY")
            res = DoModify(keys, vals);
         else
            if(action == "QUOTE")
               res = DoQuote(keys, vals);
            else
               if(action == "PROTECT")
                  res = DoProtect(keys, vals);
               else
                  res = "ok=0\nmessage=Unknown command " + action + "\n";
   WriteText("ari_res_" + id + ".txt", "id=" + id + "\n" + res);
  }

string Failure(const string what)
  {
   return("ok=0\nretcode=" + IntegerToString(trade.ResultRetcode()) + "\nmessage=" + what + ": "
          + Clean(trade.ResultRetcodeDescription()) + "\n");
  }

string ResolveSymbol(const string want)
  {
   if(want == "")
      return("");
   if(SymbolSelect(want, true))
      return(want);
   string up = want;
   StringToUpper(up);
   int total = SymbolsTotal(false);
   for(int i = 0; i < total; i++)
     {
      string s = SymbolName(i, false);
      string su = s;
      StringToUpper(su);
      // Broker suffixes: EURUSDm, EURUSD.sd, XAUUSD.ecn...
      if(StringFind(su, up) == 0 && SymbolInfoInteger(s, SYMBOL_TRADE_MODE) == SYMBOL_TRADE_MODE_FULL)
        {
         SymbolSelect(s, true);
         return(s);
        }
     }
   return("");
  }

int VolumeDigits(const double step)
  {
   int d = 0;
   double s = step;
   while(d < 8 && MathAbs(s - MathRound(s)) > 1e-9)
     {
      s *= 10.0;
      d++;
     }
   return(d);
  }

string DoOrder(string &keys[], string &vals[])
  {
   string want = Get(keys, vals, "symbol");
   string sym = ResolveSymbol(want);
   if(sym == "")
      return("ok=0\nmessage=Symbol not found at the broker: " + want + "\n");
   string side = Get(keys, vals, "side");
   if(side != "BUY" && side != "SELL")
      return("ok=0\nmessage=Unknown side " + side + "\n");
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   string kind = Get(keys, vals, "order_type");
   if(kind == "") kind = "MARKET";
   if(kind != "MARKET" && kind != "LIMIT" && kind != "STOP")
      return("ok=0\nmessage=Unknown order type\n");
   bool pending = kind != "MARKET";
   double requested = StringToDouble(Get(keys, vals, "price"));
   double planned = NormalizeDouble(requested, digits);
   MqlTick current;
   if(!SymbolInfoTick(sym, current))
      return("ok=0\nmessage=No broker price\n");
   if(pending)
     {
      if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
         return("ok=0\nmessage=Pending orders require a hedging account to keep ARI separate from manual trades and other EAs\n");
      double ticksize = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
      if(ticksize <= 0 || planned <= 0 || MathAbs(planned / ticksize - MathRound(planned / ticksize)) > 1e-6)
         return("ok=0\nmessage=Entry is not on the broker tick grid\n");
      double distance = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL) * SymbolInfoDouble(sym, SYMBOL_POINT);
      double delta = side == "BUY" ? planned - current.ask : current.bid - planned;
      if((kind == "LIMIT" && delta >= -distance) || (kind == "STOP" && delta <= distance))
         return("ok=0\nmessage=Pending entry is on the wrong side or too close to market\n");
     }
   double sl = NormalizeDouble(StringToDouble(Get(keys, vals, "sl")), digits);
   double tp = StringToDouble(Get(keys, vals, "tp"));
   tp = tp > 0 ? NormalizeDouble(tp, digits) : 0.0;
   if(sl <= 0)
      return("ok=0\nmessage=No stop loss\n");
   double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   if(step <= 0)
      step = 0.01;
   double volume = StringToDouble(Get(keys, vals, "volume"));
   double risk = StringToDouble(Get(keys, vals, "risk"));
   if(risk > 0)
     {
      // Size from the broker's own contract: loss at the stop for 1 lot, in account currency.
      MqlTick tick;
      if(!SymbolInfoTick(sym, tick))
         return("ok=0\nmessage=No price for " + sym + "\n");
      double price = pending ? planned : (side == "BUY" ? tick.ask : tick.bid);
      double loss = 0.0;
      ENUM_ORDER_TYPE type = side == "BUY" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      if(!OrderCalcProfit(type, sym, 1.0, price, sl, loss) || loss >= 0)
         return("ok=0\nmessage=Cannot compute the risk of 1 lot on " + sym + "\n");
      volume = MathFloor(risk / -loss / step + 1e-9) * step;
      if(volume < vmin)
         return("ok=0\nmin_lot=1\nmessage=Minimum lot " + DoubleToString(vmin, VolumeDigits(step))
                + " risks more than allowed with this stop\n");
     }
   volume = MathFloor(volume / step + 1e-9) * step;
   if(volume < vmin)
      return("ok=0\nmin_lot=1\nmessage=Volume below the broker minimum ("
             + DoubleToString(vmin, VolumeDigits(step)) + ")\n");
   if(volume > vmax)
      volume = vmax;
   volume = NormalizeDouble(volume, VolumeDigits(step));
   string comment = Get(keys, vals, "comment");
   trade.SetTypeFillingBySymbol(sym);
   bool ok = false;
   if(kind == "LIMIT")
      ok = side == "BUY" ? trade.BuyLimit(volume, planned, sym, sl, tp, ORDER_TIME_GTC, 0, comment)
                          : trade.SellLimit(volume, planned, sym, sl, tp, ORDER_TIME_GTC, 0, comment);
   else if(kind == "STOP")
      ok = side == "BUY" ? trade.BuyStop(volume, planned, sym, sl, tp, ORDER_TIME_GTC, 0, comment)
                          : trade.SellStop(volume, planned, sym, sl, tp, ORDER_TIME_GTC, 0, comment);
   else
      ok = side == "BUY" ? trade.Buy(volume, sym, 0.0, sl, tp, comment)
                          : trade.Sell(volume, sym, 0.0, sl, tp, comment);
   uint rc = trade.ResultRetcode();
   if(!ok || (rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED && rc != TRADE_RETCODE_DONE_PARTIAL))
      return(Failure("Order rejected"));
   ulong position = trade.ResultOrder();   // MT5: position id = ticket of the opening order
   ulong deal = trade.ResultDeal();
   if(deal > 0 && HistoryDealSelect(deal))
      position = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
   return("ok=1\nposition=" + IntegerToString((long)position)
          + "\nsymbol=" + sym
          + "\nvolume=" + DoubleToString(trade.ResultVolume() > 0 ? trade.ResultVolume() : volume, VolumeDigits(step))
          + "\nprice=" + DoubleToString(pending ? planned : trade.ResultPrice(), digits) + "\n");
  }

//--- ARI pending orders only. Never cancel a manual order or another EA's order.
string AriPositions()
  {
   string out = "ok=1\n";
   int count = 0;
   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      string prefix = "position_" + IntegerToString(count) + "_";
      out += prefix + "id=" + IntegerToString(PositionGetInteger(POSITION_IDENTIFIER)) + "\n"
             + prefix + "entry=" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), 10) + "\n"
             + prefix + "sl=" + DoubleToString(PositionGetDouble(POSITION_SL), 10) + "\n"
             + prefix + "tp=" + DoubleToString(PositionGetDouble(POSITION_TP), 10) + "\n"
             + prefix + "volume=" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 8) + "\n";
      count++;
     }
   return(out + "count=" + IntegerToString(count) + "\n");
  }

string PendingOrders()
  {
   string out = "ok=1\n";
   int count = 0;
   for(int i = OrdersTotal()-1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || OrderGetInteger(ORDER_MAGIC) != InpMagic) continue;
      string sym = OrderGetString(ORDER_SYMBOL);
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      ENUM_ORDER_TYPE type = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      string prefix = "order_" + IntegerToString(count) + "_";
      out += prefix + "ticket=" + IntegerToString((long)ticket) + "\n"
             + prefix + "symbol=" + sym + "\n"
             + prefix + "side=" + ((type == ORDER_TYPE_BUY_LIMIT || type == ORDER_TYPE_BUY_STOP) ? "BUY" : "SELL") + "\n"
             + prefix + "type=" + EnumToString(type) + "\n"
             + prefix + "price=" + DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), digits) + "\n"
             + prefix + "volume=" + DoubleToString(OrderGetDouble(ORDER_VOLUME_CURRENT), 8) + "\n"
             + prefix + "sl=" + DoubleToString(OrderGetDouble(ORDER_SL), digits) + "\n"
             + prefix + "tp=" + DoubleToString(OrderGetDouble(ORDER_TP), digits) + "\n";
      count++;
     }
   return(out + "count=" + IntegerToString(count) + "\n");
  }

string CancelPending(string &keys[], string &vals[])
  {
   ulong ticket = (ulong)StringToInteger(Get(keys, vals, "ticket"));
   if(!OrderSelect(ticket) || OrderGetInteger(ORDER_MAGIC) != InpMagic)
      return("ok=0\nmessage=ARI pending order not found\n");
   if(!trade.OrderDelete(ticket) || trade.ResultRetcode() != TRADE_RETCODE_DONE)
      return(Failure("Cancellation rejected"));
   return(PendingStatus(keys, vals));   // a partially filled then cancelled order still has a position
  }

string PendingStatus(string &keys[], string &vals[])
  {
   ulong ticket = (ulong)StringToInteger(Get(keys, vals, "ticket"));
   if(!HistoryOrderSelect(ticket) || HistoryOrderGetInteger(ticket, ORDER_MAGIC) != InpMagic)
      return("ok=0\nmessage=ARI order history not found\n");
   ENUM_ORDER_STATE state = (ENUM_ORDER_STATE)HistoryOrderGetInteger(ticket, ORDER_STATE);
   long position = HistoryOrderGetInteger(ticket, ORDER_POSITION_ID);
   double volume = 0.0, amount = 0.0;
   if(position > 0 && HistorySelectByPosition((ulong)position))
      for(int i = 0; i < HistoryDealsTotal(); i++)
        {
         ulong deal = HistoryDealGetTicket(i);
         if(deal == 0 || (ulong)HistoryDealGetInteger(deal, DEAL_ORDER) != ticket) continue;
         double v = HistoryDealGetDouble(deal, DEAL_VOLUME);
         volume += v;
         amount += v * HistoryDealGetDouble(deal, DEAL_PRICE);
        }
   if(volume > 0)
     {
      return("ok=1\nstatus=filled\nposition=" + IntegerToString(position)
             + "\nprice=" + DoubleToString(amount / volume, 10) + "\nvolume=" + DoubleToString(volume, 8) + "\n");
     }
   if(state == ORDER_STATE_FILLED) return("ok=0\nmessage=Execution history not ready\n");
   if(state == ORDER_STATE_CANCELED) return("ok=1\nstatus=cancelled\n");
   if(state == ORDER_STATE_EXPIRED) return("ok=1\nstatus=expired\n");
   if(state == ORDER_STATE_REJECTED) return("ok=1\nstatus=rejected\n");
   return("ok=1\nstatus=pending\n");
  }

//--- QUOTE: exact broker spread and lot data (read only, never trades) ---
string DoQuote(string &keys[], string &vals[])
  {
   string want = Get(keys, vals, "symbol");
   string sym = ResolveSymbol(want);
   if(sym == "")
      return("ok=0\nmessage=Symbol not found at the broker: " + want + "\n");
   MqlTick tick;
   if(!SymbolInfoTick(sym, tick) || tick.bid <= 0 || tick.ask <= 0)
      return("ok=0\nmessage=No price for " + sym + "\n");
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double spread = tick.ask - tick.bid;
   long points = point > 0 ? (long)MathRound(spread / point) : 0;
   string out = "ok=1\nsymbol=" + sym
                + "\nbid=" + DoubleToString(tick.bid, digits)
                + "\nask=" + DoubleToString(tick.ask, digits)
                + "\nspread=" + DoubleToString(spread, digits)
                + "\nspread_points=" + IntegerToString(points)
                + "\ndigits=" + IntegerToString(digits)
                + "\npoint=" + DoubleToString(point, 10)
                + "\ntick_size=" + DoubleToString(SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE), 10)
                + "\ntick_value=" + DoubleToString(SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE), 8)
                + "\ncontract_size=" + DoubleToString(SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE), 4)
                + "\nvolume_min=" + DoubleToString(SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN), 8)
                + "\nvolume_step=" + DoubleToString(step, 8)
                + "\nvolume_max=" + DoubleToString(SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX), 8)
                + "\nstops_level=" + IntegerToString(SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL))
                + "\ncurrency=" + AccountInfoString(ACCOUNT_CURRENCY)
                + "\nprofit_currency=" + SymbolInfoString(sym, SYMBOL_CURRENCY_PROFIT)
                + "\nea_version=" + EA_VERSION + "\n";
   string side = Get(keys, vals, "side");
   if(side != "BUY" && side != "SELL")
      return(out);
   ENUM_ORDER_TYPE type = side == "BUY" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double price = side == "BUY" ? tick.ask : tick.bid;
   double requested = StringToDouble(Get(keys, vals, "price"));
   if(requested > 0) price = requested;   // pending order: size at the requested entry, not today's quote
   double sl = StringToDouble(Get(keys, vals, "sl"));
   if(sl <= 0)
      return(out);
   double margin = 0.0;
   if(OrderCalcMargin(type, sym, 1.0, price, margin))
      out += "margin_per_lot=" + DoubleToString(margin, 4) + "\n";
   // Loss at the stop for 1 lot, in account currency (positive), spread included:
   // a buy opens at the ask, a sell at the bid.
   double loss = 0.0;
   if(OrderCalcProfit(type, sym, 1.0, price, NormalizeDouble(sl, digits), loss) && loss < 0)
      out += "loss_per_lot=" + DoubleToString(-loss, 6) + "\n";
   else
      out += "loss_error=Stop on the wrong side or loss not computable\n";
   return(out);
  }

//--- MODIFY: update stop loss / take profit (e.g. Break-Even) ---
string DoModify(string &keys[], string &vals[])
  {
   long want = StringToInteger(Get(keys, vals, "position"));
   if(want <= 0)
      want = StringToInteger(Get(keys, vals, "ticket"));
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || (PositionGetInteger(POSITION_IDENTIFIER) != want && (long)t != want))
         continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double sl = StringToDouble(Get(keys, vals, "sl"));
      double tp = StringToDouble(Get(keys, vals, "tp"));
      double cur_sl = PositionGetDouble(POSITION_SL);
      double cur_tp = PositionGetDouble(POSITION_TP);
      double set_sl = sl > 0 ? NormalizeDouble(sl, digits) : cur_sl;
      double set_tp = tp > 0 ? NormalizeDouble(tp, digits) : cur_tp;
      if(!trade.PositionModify(t, set_sl, set_tp))
         return(Failure("Modify rejected"));
      return("ok=1\nposition=" + IntegerToString(want)
             + "\nsl=" + DoubleToString(set_sl, digits)
             + "\ntp=" + DoubleToString(set_tp, digits) + "\n");
     }
   return("ok=0\nmessage=Position not found\n");
  }

//--- PROTECT: settings of the position protection (never opens nor closes a position) ---
string ProtectSignature()
  {
   return((P_Enabled ? "1" : "0") + "," + (P_BeOn ? "1" : "0") + "," + DoubleToString(P_BeAct, 0) + ","
          + DoubleToString(P_BePlus, 0) + "," + (P_TsOn ? "1" : "0") + "," + DoubleToString(P_TsStart, 0) + ","
          + DoubleToString(P_TsDist, 0) + "," + DoubleToString(P_TsStep, 0));
  }

void ApplyProtect(string &keys[], string &vals[])
  {
   string v;
   v = Get(keys, vals, "enabled");  if(v != "") P_Enabled = StringToInteger(v) != 0;
   v = Get(keys, vals, "be_on");    if(v != "") P_BeOn = StringToInteger(v) != 0;
   v = Get(keys, vals, "be_act");   if(v != "" && StringToDouble(v) > 0) P_BeAct = StringToDouble(v);
   v = Get(keys, vals, "be_plus");  if(v != "" && StringToDouble(v) >= 0) P_BePlus = StringToDouble(v);
   v = Get(keys, vals, "ts_on");    if(v != "") P_TsOn = StringToInteger(v) != 0;
   v = Get(keys, vals, "ts_start"); if(v != "" && StringToDouble(v) > 0) P_TsStart = StringToDouble(v);
   v = Get(keys, vals, "ts_dist");  if(v != "" && StringToDouble(v) > 0) P_TsDist = StringToDouble(v);
   v = Get(keys, vals, "ts_step");  if(v != "" && StringToDouble(v) > 0) P_TsStep = StringToDouble(v);
   if(P_BePlus >= P_BeAct)   // the Break-Even stop must stay below the activation profit
      P_BePlus = MathMax(0.0, P_BeAct - 1);
  }

void LoadProtect()
  {
   string keys[], vals[];
   if(ReadKeyValues(PROTECT_FILE, keys, vals))
      ApplyProtect(keys, vals);
  }

string DoProtect(string &keys[], string &vals[])
  {
   ApplyProtect(keys, vals);
   string text = "enabled=" + (P_Enabled ? "1" : "0") + "\nbe_on=" + (P_BeOn ? "1" : "0")
                 + "\nbe_act=" + DoubleToString(P_BeAct, 2) + "\nbe_plus=" + DoubleToString(P_BePlus, 2)
                 + "\nts_on=" + (P_TsOn ? "1" : "0") + "\nts_start=" + DoubleToString(P_TsStart, 2)
                 + "\nts_dist=" + DoubleToString(P_TsDist, 2) + "\nts_step=" + DoubleToString(P_TsStep, 2) + "\n";
   if(!WriteText(PROTECT_FILE, text))   // kept for the next start of MetaTrader
      return("ok=0\nmessage=Cannot save " + PROTECT_FILE + "\n");
   return("ok=1\nprotect=" + ProtectSignature() + "\n" + text);
  }

double PipOf(const string sym)
  {
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   return((digits == 3 || digits == 5) ? point * 10.0 : point);
  }

void Protect()
  {
   if(!P_Enabled || (!P_BeOn && !P_TsOn))
      return;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetInteger(POSITION_MAGIC) != InpMagic)
         continue;   // never the positions of other EAs or of the user
      string sym = PositionGetString(POSITION_SYMBOL);
      MqlTick tick;
      if(!SymbolInfoTick(sym, tick) || tick.bid <= 0 || tick.ask <= 0)
         continue;
      double pip = PipOf(sym);
      if(pip <= 0)
         continue;
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double point = SymbolInfoDouble(sym, SYMBOL_POINT);
      bool buy = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double cur = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);
      double profit = (buy ? tick.bid - open : open - tick.ask) / pip;
      double best = 0.0;
      bool have = false;
      if(P_BeOn && profit >= P_BeAct)
        {
         double be = buy ? open + P_BePlus * pip : open - P_BePlus * pip;
         if(cur <= 0 || (buy ? be > cur : be < cur))
           {
            best = be;
            have = true;
           }
        }
      if(P_TsOn && profit >= P_TsStart)
        {
         double ts = buy ? tick.bid - P_TsDist * pip : tick.ask + P_TsDist * pip;
         double ref = have ? best : cur;
         double moved = buy ? ts - ref : ref - ts;
         if(ref <= 0 || moved >= P_TsStep * pip - 1e-12)
            if(ref <= 0 || (buy ? ts > ref : ts < ref))
              {
               best = ts;
               have = true;
              }
        }
      if(!have)
         continue;
      best = NormalizeDouble(best, digits);
      if(cur > 0 && MathAbs(best - cur) < point / 2)
         continue;
      long lvl = MathMax(SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL), SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL));
      double min_dist = lvl * point;
      if(lvl > 0 && (buy ? tick.bid - best <= min_dist : best - tick.ask <= min_dist))
         continue;   // too close to the price for the broker (RiskShieldBot: strictly beyond the level)
      trade.PositionModify(t, best, tp);   // TP kept
     }
  }

string DoClose(string &keys[], string &vals[])
  {
   long want = StringToInteger(Get(keys, vals, "position"));
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetInteger(POSITION_IDENTIFIER) != want)
         continue;
      int digits = (int)SymbolInfoInteger(PositionGetString(POSITION_SYMBOL), SYMBOL_DIGITS);
      if(!trade.PositionClose(t))
         return(Failure("Close rejected"));
      return("ok=1\nprice=" + DoubleToString(trade.ResultPrice(), digits) + "\n");
     }
   return("ok=1\nalready_closed=1\n");
  }
//+------------------------------------------------------------------+
