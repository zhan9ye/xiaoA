from typing import Dict


def get_rpc_browser_headers() -> Dict[str, str]:
    """与浏览器抓包一致的表单 RPC 请求头（Login / My_Subaccount 等）。"""
    return {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Origin": "https://ak2018.vip",
        "Referer": "https://ak2018.vip/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,en;q=0.7",
    }
