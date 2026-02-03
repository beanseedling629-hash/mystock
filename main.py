import time
import logging
from datetime import datetime, timedelta
from flask import Flask, jsonify
from flask_cors import CORS
import akshare as ak
import pandas as pd
import pandas_ta as ta

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
CORS(app)  # 允许跨域，方便前端调用

# 配置参数
SYMBOL_CODE = "02556"  # 迈富时
HISTORY_START_DATE = "20240101" # 历史数据回溯起点

def get_realtime_factor():
    try:
        logging.info(f"开始获取 {SYMBOL_CODE} 数据...")

        # ---------------------------------------------------
        # 1. 获取实时快照 (Spot) - 这是最关键的一步
        # ---------------------------------------------------
        # stock_hk_spot_em 是东财接口，包含当天的【成交额】和【成交量】，这对计算 VWAP 至关重要
        spot_df = ak.stock_hk_spot_em()
        target_row = spot_df[spot_df['代码'] == SYMBOL_CODE]

        if target_row.empty:
            raise Exception("实时接口未找到该股票数据，可能是休市或接口调整。")

        # 提取核心实时数据
        latest_price = float(target_row.iloc[0]['最新价'])
        latest_amount = float(target_row.iloc[0]['成交额']) # 累计成交额
        latest_volume = float(target_row.iloc[0]['成交量']) # 累计成交量
        latest_chg_pct = float(target_row.iloc[0]['涨跌幅'])
        
        # ---------------------------------------------------
        # 2. 计算日内 VWAP (黄线均价) 及 乖离率
        # ---------------------------------------------------
        # VWAP = 总成交额 / 总成交量
        if latest_volume > 0:
            vwap_price = latest_amount / latest_volume
        else:
            vwap_price = latest_price # 开盘瞬间防除零
            
        # 乖离率 = (现价 - 均价) / 均价
        # 如果结果是 -2.5，说明现价低于均价 2.5%，属于深水区
        vwap_bias = ((latest_price - vwap_price) / vwap_price) * 100

        # ---------------------------------------------------
        # 3. 获取历史数据并拼接 (为了算 RSI, 布林带)
        # ---------------------------------------------------
        df_hist = ak.stock_hk_hist(symbol=SYMBOL_CODE, start_date=HISTORY_START_DATE, adjust="qfq")
        
        # 数据清洗：统一日期格式
        df_hist['日期'] = pd.to_datetime(df_hist['日期']).dt.date
        today = datetime.now().date()

        # 如果历史数据里包含了"今天"（收盘后可能出现），先剔除，确保我们用的是最新的 Spot 数据
        if df_hist.iloc[-1]['日期'] == today:
            df_hist = df_hist.iloc[:-1]

        # 构造今日的临时 DataFrame 行
        # 注意：pandas_ta 计算需要 Open/High/Low/Close，这里我们暂时用现价填充
        # 虽然 High/Low 不精准，但不影响 RSI 这种基于 Close 的指标计算
        new_row = pd.DataFrame([{
            '日期': today,
            '收盘': latest_price,
            '开盘': latest_price, 
            '最高': latest_price, 
            '最低': latest_price,
            '成交量': latest_volume
        }])

        # 拼接到末尾
        df_final = pd.concat([df_hist, new_row], ignore_index=True)

        # ---------------------------------------------------
        # 4. 计算技术指标 (Pandas TA)
        # ---------------------------------------------------
        # RSI
        df_final['RSI_6'] = ta.rsi(df_final['收盘'], length=6)
        
        # 布林带 (用于看是否跌破下轨)
        bbands = ta.bbands(df_final['收盘'], length=20, std=2)
        # BBP (Bollinger Band Percentage) < 0 表示跌破下轨
        df_final['BB_PctB'] = bbands['BBP_20_2.0'] 

        # ---------------------------------------------------
        # 5. 生成信号与评分
        # ---------------------------------------------------
        current_rsi = df_final.iloc[-1]['RSI_6']
        current_bb = df_final.iloc[-1]['BB_PctB']
        
        score = 0
        signals = []

        # 信号 A: 日内分时急跌 (你截图里的那个坑)
        # 阈值：现价低于均价 2%
        if vwap_bias < -2.0:
            score += 3
            signals.append(f"分时超跌{abs(vwap_bias):.1f}%")
        
        # 信号 B: RSI 超卖
        if current_rsi < 20:
            score += 2
            signals.append(f"RSI低位({current_rsi:.1f})")
            
        # 信号 C: 跌破布林下轨 (恐慌盘)
        if current_bb < 0:
            score += 1
            signals.append("破布林下轨")

        # 汇总文案
        if score >= 4:
            signal_text = "🔥 极佳买点 (共振)"
            signal_color = "red"
        elif score >= 2:
            signal_text = "⚠️ 关注反弹"
            signal_color = "#d93025"
        else:
            signal_text = "观望 / 盘整"
            signal_color = "#5f6368"

        # ---------------------------------------------------
        # 6. 返回结果
        # ---------------------------------------------------
        return jsonify({
            "status": "success",
            "update_time": datetime.now().strftime("%H:%M:%S"),
            "data": {
                "symbol": "迈富时 (02556.HK)",
                "price": latest_price,
                "change_pct": round(latest_chg_pct, 2),
                "vwap": {
                    "price": round(vwap_price, 3),
                    "bias": round(vwap_bias, 2), # 重点关注这个
                    "bias_desc": "低于均价" if vwap_bias < 0 else "高于均价"
                },
                "indicators": {
                    "rsi_6": round(current_rsi, 2),
                    "bb_pct": round(current_bb, 2)
                },
                "strategy": {
                    "score": score,
                    "text": signal_text,
                    "color": signal_color,
                    "reasons": " + ".join(signals) if signals else "无明显信号"
                }
            }
        })

    except Exception as e:
        logging.error(f"Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/')
def index():
    return "Maifushi Monitor is Running."

if __name__ == '__main__':
    # 监听 0.0.0.0 才能被外部访问
    app.run(host='0.0.0.0', port=8080)
