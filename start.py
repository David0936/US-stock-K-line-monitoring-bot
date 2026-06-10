#!/usr/bin/env python3
"""启动入口：python3 start.py（默认端口 5002，可用 PORT 环境变量改）。"""
import os

if __name__ == "__main__":
    from app import app, config

    port = int(os.environ.get("PORT", 5005))
    print("=" * 56)
    print("📈 Stock Desk — 美股 K线盯盘 + 资讯联动 + AI 多空判断")
    print(f"   关注：{config.get('DESK_INDICES')} + {config.get('DESK_TICKERS')}")
    print(f"   打开 http://localhost:{port}")
    print("   首次登录密码见上方日志 / data/default_password.txt")
    print("=" * 56)
    app.run(host="0.0.0.0", port=port, debug=False)
