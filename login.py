import requests

url = "https://www.akapi1.com/RPC/Login"

# 1. 构造请求头 (Headers)
headers = {
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "Origin": "https://ak2018.vip",
    "Referer": "https://ak2018.vip/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,en;q=0.7"
}

# 2. 构造表单数据 (Payload)
# 将你抓包拿到的参数按键值对填入字典
payload = {
    "account": "cy19720",
    "password": "Zyf1968", # 注意保护你的真实密码
    "client": "WEB",
    "key": "123",
    "UserID": "123",
    "v": "2053",
    "lang": "cn"
}

# 3. 建议使用 Session 来保持会话状态
# 这样登录成功后，服务器返回的 Cookie 会自动保存在 session 中
# 后续用 session 发送其他请求时，会自动带上登录凭证
session = requests.Session()

try:
    # 4. 发送 POST 请求
    # 关键：一定要用 data=payload，千万不能用 json=payload
    response = session.post(url, headers=headers, data=payload)
    
    # 5. 打印结果
    print(f"状态码: {response.status_code}")
    
    # 尝试将返回的文本解析为 JSON（大部分现代接口会返回 JSON 格式结果）
    try:
        print("返回结果:", response.json())
    except ValueError:
        print("返回结果(非JSON):", response.text)
        
    # 查看服务器下发的 Cookie
    print("当前会话 Cookie:", session.cookies.get_dict())

except requests.exceptions.RequestException as e:
    print(f"请求发生异常: {e}")
