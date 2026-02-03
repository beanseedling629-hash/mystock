import logging
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
import akshare as ak
import pandas as pd
import pandas_ta as ta

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
CORS(app)

def analyze_stock(symbol):
    try:
        # 1. 获取实时快照 (Spot)
        spot_df = ak.stock_hk_spot_em()
        target_row = spot_df[spot_df['代码'] == symbol]

        if target_row.empty:
            return None, "未找到该股票或代码错误 (请使用5位代码如 02556)"

        # 提取核心实时数据
        latest_price = float(target_row.iloc[0]['最新价'])
        latest_amount = float(target_row.iloc[0]['成交额'])
        latest_volume = float(target_row.iloc[0]['成交量'])
        latest_chg_pct = float(target_row.iloc[0]['涨跌幅'])
        # 换手率反映活跃度
        turnover_rate = float(target_row.iloc[0]['换手率']) if '换手率' in target_row.columns else 0
        
        # 2. 计算日内 VWAP (均价)
        if latest_volume > 0:
            vwap_price = latest_amount / latest_volume
        else:
            vwap_price = latest_price
            
        vwap_bias = ((latest_price - vwap_price) / vwap_price) * 100

        # 3. 获取历史数据 (用于计算趋势)
        # 必须获取足够长的数据来计算 MA60 和 MACD
        df_hist = ak.stock_hk_hist(symbol=symbol, start_date="20240101", adjust="qfq")
        df_hist['日期'] = pd.to_datetime(df_hist['日期']).dt.date
        
        # 剔除可能的今日重复数据，并拼接今日实时数据
        today = datetime.now().date()
        if not df_hist.empty and df_hist.iloc[-1]['日期'] == today:
            df_hist = df_hist.iloc[:-1]

        new_row = pd.DataFrame([{
            '日期': today,
            '收盘': latest_price,
            '开盘': latest_price, 
            '最高': latest_price, 
            '最低': latest_price,
            '成交量': latest_volume
        }])
        df_final = pd.concat([df_hist, new_row], ignore_index=True)

        # 4. 计算复杂指标
        # --- RSI ---
        df_final['RSI_6'] = ta.rsi(df_final['收盘'], length=6)
        
        # --- 均线趋势 (MA) ---
        df_final['MA_5'] = ta.sma(df_final['收盘'], length=5)
        df_final['MA_10'] = ta.sma(df_final['收盘'], length=10)
        df_final['MA_20'] = ta.sma(df_final['收盘'], length=20)
        df_final['MA_60'] = ta.sma(df_final['收盘'], length=60)

        # --- MACD (动量) ---
        macd = ta.macd(df_final['收盘'])
        df_final['MACD'] = macd['MACD_12_26_9']
        df_final['MACD_SIGNAL'] = macd['MACDs_12_26_9']
        df_final['MACD_HIST'] = macd['MACDh_12_26_9']

        # 获取最新一帧数据
        latest = df_final.iloc[-1]
        
        # 5. 深度逻辑分析 (AI Analyst)
        trend_status = ""
        momentum_status = ""
        advice = ""
        risk_level = "中"
        
        # A. 趋势判断
        if latest['MA_5'] < latest['MA_10'] < latest['MA_20']:
            trend_status = "📉 空头排列 (主跌浪)"
            downward_pressure = "极高"
        elif latest['MA_5'] > latest['MA_10'] > latest['MA_20']:
            trend_status = "📈 多头排列 (上升趋势)"
            downward_pressure = "低"
        else:
            trend_status = "〰️ 震荡整理"
            downward_pressure = "中"

        # B. 动量/利空判断
        if latest['MACD_HIST'] < 0 and latest['MACD'] < latest['MACD_SIGNAL']:
            momentum_status = "🟢 空头动能增强 (加速下跌)"
        elif latest['MACD_HIST'] > 0 and latest['MACD_HIST'] < df_final.iloc[-2]['MACD_HIST']:
            momentum_status = "⚠️ 多头动能衰减 (上涨乏力)"
        elif latest['MACD_HIST'] > 0:
            momentum_status = "🔴 多头占优"
        else:
            momentum_status = "⚪ 动能不明"

        # C. 综合买入建议
        score = 0
        reasons = []

        # 狙击逻辑
        if vwap_bias < -2.5:
            score += 3
            reasons.append("分时极度超跌(黄金坑)")
        if latest['RSI_6'] < 20:
            score += 2
            reasons.append("RSI严重超卖")
        if trend_status.startswith("📉"):
            score -= 2 # 逆势接飞刀风险大
            risk_level = "高 (逆势)"
        
        if score >= 3:
            advice = "⚡️ 激进买入 (博反弹)"
        elif score >= 1:
            advice = "👀 密切观察"
        else:
            advice = "🛑 观望/规避"

        # D. 估算抛压 (利用换手率和跌幅)
        # 既然拿不到沽空数据，我们用“量价背离”来描述抛压
        selling_pressure = "正常"
        if latest_chg_pct < -3 and turnover_rate > 1:
            selling_pressure = "🔥 恐慌性抛售 (放量大跌)"
        elif latest_chg_pct < 0 and latest_volume < df_final.iloc[-2]['成交量']:
            selling_pressure = "阴跌 (无量下跌)"

        result = {
            "symbol": symbol,
            "price": latest_price,
            "change_pct": round(latest_chg_pct, 2),
            "vwap_bias": round(vwap_bias, 2),
            "indicators": {
                "rsi": round(latest['RSI_6'], 2),
                "ma20": round(latest['MA_20'], 3),
                "macd_bar": round(latest['MACD_HIST'], 4)
            },
            "analysis": {
                "trend": trend_status,
                "momentum": momentum_status,
                "pressure": selling_pressure,
                "downside_risk": downward_pressure
            },
            "strategy": {
                "advice": advice,
                "risk": risk_level,
                "reasons": " + ".join(reasons) if reasons else "无特殊信号"
            }
        }
        return result, None

    except Exception as e:
        logging.error(f"Error: {e}")
        return None, str(e)

@app.route('/api/analyze')
def api_analyze():
    # 从 URL 参数获取 code，默认迈富时
    code = request.args.get('code', '02556')
    data, error = analyze_stock(code)
    
    if error:
        return jsonify({"status": "error", "message": error}), 500
    
    return jsonify({"status": "success", "data": data})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
