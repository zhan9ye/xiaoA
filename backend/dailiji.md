1. 登录代理机
在 阿里云控制台 里：

看实例是否已设 登录密码 或绑定 密钥对；
用 Workbench 远程连接 或你本机：
ssh root@47.236.126.165
# 若只用密钥：ssh -i /path/to/key.pem root@47.236.126.165
（公网 IP 以控制台为准。）

2. 安装 Squid（Ubuntu）
sudo apt update
sudo apt install -y squid apache2-utils
3. 写最小配置（只信任 xiaoA 私网 IP，先不配密码）
先备份：

sudo cp /etc/squid/squid.conf /etc/squid/squid.conf.bak
编辑（nano / vim 均可）：

rm -rf /etc/squid/squid.conf
touch /etc/squid/squid.conf
vim /etc/squid/squid.conf

删掉或注释掉原文件里大段默认规则（避免冲突），换成类似下面这一段（把 172.19.205.90 换成你主服务器私网 IP，若不一致）：

# 监听（默认 3128）
http_port 3128
visible_hostname proxy-internal
# 只允许 xiaoA 这台机访问代理
acl allowed_src src 172.19.205.90/32
# HTTPS 走 CONNECT（访问 akapi1 需要）
acl SSL_ports port 443
acl CONNECT method CONNECT
http_access allow CONNECT SSL_ports allowed_src
http_access allow allowed_src
http_access deny all
# 关闭 Via 头（可选）
via off

保存后：

sudo squid -k parse
sudo systemctl restart squid
sudo systemctl status squid
4. 本机防火墙（若开了 ufw）
sudo ufw allow from 172.19.205.90 to any port 3128 proto tcp
sudo ufw reload
（云安全组你已放行 172.19.205.90 → 3128，和这里一致即可。）

5. 在 xiaoA 主服务器上测试
无认证时代理池里填：

http://172.19.205.89:3128
（172.19.205.89 换成代理机私网 IP。）

在主服务器执行：

curl -x http://172.19.205.89:3128 -k -I --connect-timeout 10 https://www.akapi1.com/
能出现 HTTP 响应头（例如 403）即表示 代理 + CONNECT 正常。

sudo tail -f /var/log/squid/access.log 
查看日志看是否走代理了

6.（可选）再加「用户名密码」
在代理机上：

sudo htpasswd -c /etc/squid/passwd 你的用户名
# 按提示设密码
在 squid.conf 里 在 http_access 之前 增加认证相关配置（不同 Ubuntu/Squid 版本路径可能略有差异，若报错把报错贴出来）：

auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic realm Squid Proxy
acl authenticated proxy_auth REQUIRED
http_access allow CONNECT SSL_ports authenticated allowed_src
http_access allow authenticated allowed_src
http_access deny all
然后 sudo systemctl restart squid。池子里改为：

http://你的用户名:密码@172.19.205.89:3128
7. 在 xiaoA 管理后台
出站代理池 新增一条，填上面的 URL；再给用户绑定即可。

顺序小结：SSH 登录 → apt install squid → 改 squid.conf → restart squid → 主服务器 curl -x 测通 → 再考虑密码 → 最后写入 xiaoA 代理池。

若某一步报错（例如 squid -k parse 或 basic_ncsa_auth 路径不对），把完整报错发我，按你 Ubuntu 版本改一版配置即可。