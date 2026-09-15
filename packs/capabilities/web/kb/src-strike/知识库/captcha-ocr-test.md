# 图形验证码 OCR 破解（ddddocr）— 解锁登录爆破链路

> 工具：ddddocr。用途：把「登录爆破 / 越权注册 / 绑定 / 找回改密」链路上的图形验证码自动解掉，不手动读图、不卡验证码。
> **图验证码本身不报洞**——只是开链路的钥匙；锁的价值在解锁之后（爆破/撞库/枚举/改密）。

## 环境（已验证 2026-09-08）

- `E:\Miniconda3\python.exe`（ddddocr OK）
- `E:\Miniconda3\envs\Ciphey\python.exe` 也装了 ddddocr（脚本可复用）
- `import ddddocr` 即可，无需额外配置

## 用法（标准流程）

1. `requests.Session` 保持 cookie（验证码通常绑会话）：先 GET 页面拿 session，再 GET 验证码接口取 PNG
2. `ddddocr.DdddOcr(show_ad=False).classification(png)` 返回 str 答案
3. 答案长度 **3~5** 视为有效，直接回填 code 提交
4. 识别失败重试 **3~6 次**（验证码接口每次刷新）

```python
import ddddocr, requests
s = requests.Session()
png = s.get('https://target/captcha').content          # 绑会话
ans = ddddocr.DdddOcr(show_ad=False).classification(png)
r = s.post('https://target/login', data={'code': ans, ...})
```

## 变种：算式验证码（RuoYi 常见 `a op b =`）

- `ddddocr.DdddOcr(show_ad=False, beta=True)` —— beta 模型读算式，常把末位等号误读成 `7/9`
- 典型输出 `'8+97'` `'2+57'` → 取数字对 + 算子 `eval` 得答案
- tesseract 对算式类识别差，别用

## 边界（强）

- **文字图形验证码破解 = 不算漏洞**，只当解锁「登录爆破 / 越权 / 注册 / 绑定 / 找回」链路的钥匙
- 验证码自带**防爆破成本**，解锁后爆破/撞库/枚举的取值收敛仍认 `edusrc / ysrc / osrc` 对应边界（越权≤5组、注入/SQLi/XSS/RCE 只证明等）
- 以破解图验证码为核心危害的洞（如图验证码绕过仅用于短信轰炸）→ 撤，不落（见 `rules/` 不收清单）

## 关联面

- 登录爆破后：无账户锁 / 无限流 → 撞库面，配合差分（用户存在枚举 diff）即「有料爆破」而非空爆
- 算术验证码本地可解 → 与登录接口无 IP 限流组合 = 独特撞库面