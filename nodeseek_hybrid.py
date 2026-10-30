# -*- coding: utf-8 -*-
"""
NodeSeek Hybrid Auto Signin Script
融合三个脚本优势的混合签到器 - GitHub Actions 优化版

Features:
- 渐进式 Fallback: HTTP → Proxy → Selenium 
- 多账户批处理 (来自 nodeseek_sign.py)
- 环境检测与优化 (GitHub Actions / Qinglong / Local)
- Cookie 自动管理与持久化
- 可选的30天统计追踪
- 无需验证码服务依赖

Author: Based on multiple NodeSeek signin scripts
License: MIT
"""

import os
import time
import json
import base64
import random
import traceback
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass

# 尝试导入HTTP库 (优先使用 curl_cffi)
try:
    from curl_cffi import requests as cf_requests
    USE_CURL_CFFI = True
    print("✅ 使用 curl_cffi 增强 Cloudflare 绕过能力")
except ImportError:
    import requests as cf_requests
    USE_CURL_CFFI = False
    print("⚠️  使用 requests 库 (建议安装 curl_cffi)")

# 尝试导入 Selenium (fallback 使用)
SELENIUM_AVAILABLE = False
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options
    import undetected_chromedriver as uc
    SELENIUM_AVAILABLE = True
    print("✅ Selenium 终极 Fallback 可用")
except ImportError:
    print("⚠️  Selenium 不可用 (GitHub Actions 中会自动安装)")

# 通知模块动态加载
try:
    from notify import send
    NOTIFICATION_AVAILABLE = True
except ImportError:
    NOTIFICATION_AVAILABLE = False
    def send(title, content):
        print(f"📢 {title}: {content}")

# Telegram Bot 推送功能
def send_telegram_message(message: str, parse_mode: str = "HTML"):
    """发送Telegram消息"""
    bot_token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    
    if not bot_token or not chat_id:
        logging.warning("⚠️  TG_BOT_TOKEN 或 TG_CHAT_ID 未配置，跳过TG推送")
        return False
    
    try:
        import requests as py_requests
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": parse_mode
        }
        
        response = py_requests.post(url, json=data, timeout=10)
        if response.status_code == 200:
            logging.info("✅ TG消息发送成功")
            return True
        else:
            logging.error(f"❌ TG消息发送失败: {response.status_code}")
            return False
    except Exception as e:
        logging.error(f"❌ TG推送异常: {str(e)}")
        return False

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

def parse_member_id_from_cookie(cookie: str) -> Optional[int]:
    """从 Cookie 的 pjwt 中解析成员 ID"""
    if not cookie:
        return None
    try:
        for item in cookie.split(";"):
            item = item.strip()
            if item.startswith("pjwt="):
                token = item[len("pjwt="):].strip()
                # pjwt 不是标准三段 JWT: 逐段尝试解码, 取含 id 的 JSON 段
                for part in token.split("."):
                    try:
                        payload_b64 = part + "=" * (-len(part) % 4)
                        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")))
                        if isinstance(payload, dict) and payload.get("id") is not None:
                            return int(payload["id"])
                    except Exception:
                        continue
    except Exception:
        pass
    return None

def normalize_user_info(detail: Dict[str, Any], fallback_id: Optional[int] = None) -> Dict[str, Any]:
    """统一用户信息字段 (来自 /api/account/getInfo 的 detail)"""
    return {
        'member_id': detail.get('member_id', fallback_id),
        'member_name': detail.get('member_name', ''),
        'rank': detail.get('rank', ''),
        'coin': detail.get('coin', ''),
        'nPost': detail.get('nPost', ''),
        'nComment': detail.get('nComment', ''),
    }

@dataclass
class SigninResult:
    """签到结果数据类"""
    success: bool
    message: str
    method: str  # 'http', 'proxy', 'selenium'
    cookie_expired: bool = False  # Cookie是否过期
    statistics: Optional[Dict] = None
    user_info: Optional[Dict] = None  # 用户信息 (用户名/等级/鸡腿/主题/评论)

@dataclass  
class AccountConfig:
    """账户配置数据类"""
    index: int
    display_name: str
    cookie: str
    username: str = ""
    password: str = ""

class EnvironmentDetector:
    """环境检测器"""
    
    @staticmethod
    def detect_environment() -> str:
        """检测当前运行环境"""
        # 检测青龙面板
        ql_markers = ['/ql/data/', '/ql/config/', '/ql/', '/.ql/']
        for path in ql_markers:
            if os.path.exists(path):
                return "qinglong"
        
        # 检测 GitHub Actions
        if os.environ.get("GITHUB_ACTIONS") == "true":
            return "github"
        
        return "local"

    @staticmethod
    def get_env_config() -> Dict[str, Any]:
        """获取环境特定配置"""
        env_type = EnvironmentDetector.detect_environment()
        
        config = {
            'environment': env_type,
            'enable_statistics': os.environ.get("ENABLE_STATISTICS", "true").lower() == "true",
            'enable_selenium': os.environ.get("ENABLE_SELENIUM", "auto"),
            'proxy_url': os.environ.get("PROXY_URL", ""),
            'random_mode': os.environ.get("NS_RANDOM", "true").lower() == "true",
            'headless': os.environ.get("HEADLESS", "true").lower() == "true",
            'timeout': int(os.environ.get("TIMEOUT", "30")),
        }
        
        # GitHub Actions 特定优化
        if env_type == "github":
            config.update({
                'enable_selenium': config['enable_selenium'] if config['enable_selenium'] != "auto" else "true",
                'timeout': min(config['timeout'], 120),  # GitHub Actions 限制
            })
        
        return config

class StatisticsTracker:
    """签到统计追踪器"""
    
    def __init__(self, cookie: str):
        self.cookie = cookie
        
    def get_signin_stats(self, days: int = 30) -> Tuple[Optional[Dict], str]:
        """获取签到统计 (来自 nodeseek_sign.py)"""
        if not self.cookie:
            return None, "无有效Cookie"
        
        try:
            headers = {
                'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                'Cookie': self.cookie
            }
            
            all_records = []
            page = 1
            
            # 最多查询10页 (GitHub Actions 资源限制)
            while page <= 10:
                url = f"https://www.nodeseek.com/api/account/credit/page-{page}"
                
                if USE_CURL_CFFI:
                    try:
                        response = cf_requests.get(url, headers=headers, timeout=10, impersonate="chrome120")
                    except:
                        response = cf_requests.get(url, headers=headers, timeout=10)
                else:
                    response = cf_requests.get(url, headers=headers, timeout=10)
                
                data = response.json()
                if not data.get("success") or not data.get("data"):
                    break
                    
                records = data.get("data", [])
                if not records:
                    break
                    
                all_records.extend(records)
                page += 1
                time.sleep(0.3)  # 降低请求频率
            
            # 简化统计逻辑
            signin_records = []
            for record in all_records:
                if len(record) >= 4 and "签到收益" in str(record[2]):
                    signin_records.append({
                        'amount': record[0],
                        'description': record[2]
                    })
            
            if not signin_records:
                return None, "未找到签到记录"
            
            total_amount = sum(r['amount'] for r in signin_records[:days])
            count = min(len(signin_records), days)
            average = round(total_amount / count, 2) if count > 0 else 0
            
            stats = {
                'total_amount': total_amount,
                'average': average,
                'days_count': count,
                'period': f"近{days}天"
            }
            
            return stats, "查询成功"
            
        except Exception as e:
            return None, f"统计查询异常: {str(e)}"

    def get_user_info(self) -> Optional[Dict]:
        """获取账户用户信息 (用户名/等级/鸡腿总数/主题帖数/评论数)"""
        member_id = parse_member_id_from_cookie(self.cookie)
        if not member_id:
            return None
        try:
            headers = {
                'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                'Cookie': self.cookie
            }
            url = f"https://www.nodeseek.com/api/account/getInfo/{member_id}?readme=1"
            if USE_CURL_CFFI:
                try:
                    response = cf_requests.get(url, headers=headers, timeout=10, impersonate="chrome120")
                except:
                    response = cf_requests.get(url, headers=headers, timeout=10)
            else:
                response = cf_requests.get(url, headers=headers, timeout=10)
            data = response.json()
            detail = data.get("detail") if isinstance(data, dict) else None
            if detail:
                return normalize_user_info(detail, member_id)
        except Exception as e:
            logging.warning(f"⚠️  用户信息查询失败: {str(e)}")
        return None

class HTTPSigner:
    """HTTP 签到器 (轻量级方案)"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.session = None
        
    def create_session(self, use_proxy: bool = False):
        """创建 HTTP 会话"""
        if USE_CURL_CFFI:
            self.session = cf_requests.Session()
        else:
            self.session = cf_requests.Session()
            
        if use_proxy and self.config['proxy_url']:
            proxies = {
                'http': self.config['proxy_url'],
                'https': self.config['proxy_url']
            }
            if hasattr(self.session, 'proxies'):
                self.session.proxies.update(proxies)
                
    def get_headers(self, cookie: str) -> Dict[str, str]:
        """获取请求头"""
        return {
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Content-Type': 'application/json',
            'Cookie': cookie,
            'Host': 'www.nodeseek.com',
            'Origin': 'https://www.nodeseek.com',
            'Referer': 'https://www.nodeseek.com/board',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'X-Requested-With': 'XMLHttpRequest'
        }
    
    def signin(self, cookie: str, use_proxy: bool = False) -> SigninResult:
        """HTTP 签到"""
        try:
            self.create_session(use_proxy)
            headers = self.get_headers(cookie)
            
            # 随机延迟
            delay = random.uniform(1, 3)
            time.sleep(delay)
            
            # 构造签到请求
            random_param = "true" if self.config['random_mode'] else "false"
            url = f"https://www.nodeseek.com/api/attendance?random={random_param}"
            
            # 发送请求
            if USE_CURL_CFFI:
                try:
                    response = self.session.post(
                        url, headers=headers, json={}, 
                        timeout=self.config['timeout'],
                        impersonate="chrome120"
                    )
                except:
                    response = self.session.post(url, headers=headers, json={}, timeout=self.config['timeout'])
            else:
                response = self.session.post(url, headers=headers, json={}, timeout=self.config['timeout'])
            
            # 解析响应
            if response.status_code == 200:
                try:
                    result = response.json()
                    if result.get('success'):
                        gain = result.get('gain', 0)
                        current = result.get('current', 0)
                        message = f"签到成功！今天获得 {gain} 个鸡腿，总计 {current} 个鸡腿"
                        method = "proxy" if use_proxy else "http"
                        return SigninResult(True, message, method)
                    else:
                        return SigninResult(False, result.get('message', '签到失败'), "http")
                except json.JSONDecodeError:
                    return SigninResult(False, f"响应解析失败: {response.text[:100]}", "http")
            
            elif response.status_code == 500:
                try:
                    result = response.json()
                    message = result.get('message', '')
                    if any(keyword in message for keyword in ['已完成签到', '已签到', '重复操作']):
                        return SigninResult(True, f"今日已签到: {message}", "http")
                    else:
                        return SigninResult(False, f"服务器错误: {message}", "http")
                except:
                    return SigninResult(False, f"服务器 500 错误", "http")
            
            elif response.status_code == 401:
                # Cookie过期或无效
                return SigninResult(False, "Cookie已过期，请手动更新", "http", cookie_expired=True)
            
            elif response.status_code == 403:
                return SigninResult(False, "403 Forbidden - 可能被 Cloudflare 拦截", "http")
                
            elif response.status_code == 302:
                # 重定向通常意味着未登录
                return SigninResult(False, "302重定向 - Cookie可能已过期", "http", cookie_expired=True)
            
            else:
                # 检查响应文本是否包含登录页面特征
                response_text = response.text.lower()
                if any(keyword in response_text for keyword in ['login', 'signin', 'sign in', '登录', '请登录']):
                    return SigninResult(False, f"HTTP {response.status_code} - Cookie可能已过期", "http", cookie_expired=True)
                else:
                    return SigninResult(False, f"HTTP {response.status_code} 错误", "http")
                
        except Exception as e:
            return SigninResult(False, f"HTTP 签到异常: {str(e)}", "http")

class SeleniumSigner:
    """Selenium 签到器 (终极方案)"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.driver = None
        
    def create_driver(self):
        """创建 WebDriver"""
        if not SELENIUM_AVAILABLE:
            raise ImportError("Selenium 不可用")
            
        chrome_options = Options()
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        
        if self.config['headless']:
            chrome_options.add_argument("--headless=new")
            
        chrome_options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        
        try:
            self.driver = uc.Chrome(options=chrome_options)
        except:
            # GitHub Actions fallback
            self.driver = webdriver.Chrome(options=chrome_options)
            
        # 隐藏自动化特征
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """
        })
        
    def _fetch_user_info(self, member_id: int) -> Optional[Dict]:
        """在已登录的浏览器会话内获取用户信息 (同源 fetch, 不受 Cloudflare 拦截)"""
        try:
            self.driver.set_script_timeout(15)
            data = self.driver.execute_async_script("""
                var mid = arguments[0];
                var done = arguments[arguments.length - 1];
                fetch('/api/account/getInfo/' + mid + '?readme=1', {credentials: 'include'})
                    .then(function(r) { return r.json(); })
                    .then(function(d) { done(d); })
                    .catch(function(e) { done(null); });
            """, member_id)
            if isinstance(data, dict):
                detail = data.get('detail')
                if detail:
                    return normalize_user_info(detail, member_id)
        except Exception as e:
            logging.warning(f"⚠️  Selenium 获取用户信息失败: {str(e)}")
        return None

    def signin(self, cookie: str) -> SigninResult:
        """Selenium 签到"""
        try:
            self.create_driver()
            
            # 访问网站
            self.driver.get("https://www.nodeseek.com")
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            
            # 添加 Cookie
            for item in cookie.split(";"):
                try:
                    name, value = item.strip().split("=", 1)
                    self.driver.add_cookie({
                        "name": name,
                        "value": value,
                        "domain": ".nodeseek.com",
                        "path": "/",
                    })
                except:
                    continue
                    
            # 刷新页面
            self.driver.refresh()
            time.sleep(3)
            
            # 验证登录状态
            try:
                username_element = WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "a.Username"))
                )
                username = username_element.text.strip()
                logging.info(f"🔐 Selenium 登录成功: {username}")
            except:
                # 检查是否被重定向到登录页面
                current_url = self.driver.current_url
                if "signin" in current_url.lower() or "login" in current_url.lower():
                    return SigninResult(False, "Selenium - Cookie已过期，需要重新登录", "selenium", cookie_expired=True)
                else:
                    return SigninResult(False, "Selenium 登录验证失败", "selenium")
            
            # 访问签到页面
            self.driver.get("https://www.nodeseek.com/board")
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".head-info > div"))
            )
            
            # 检查签到状态（先等页面加载完成，避免把 Loading 占位误判为已签到）
            head_info = None
            buttons = []
            info_text = ""
            for _ in range(20):
                head_info = self.driver.find_element(By.CSS_SELECTOR, ".head-info > div")
                buttons = head_info.find_elements(By.TAG_NAME, "button")
                info_text = head_info.text.strip()
                if buttons or (info_text and "Loading" not in info_text):
                    break
                time.sleep(1)

            if not buttons:
                # 已签到
                result = SigninResult(True, f"今日已签到: {info_text}", "selenium")
                member_id = parse_member_id_from_cookie(cookie)
                if member_id:
                    result.user_info = self._fetch_user_info(member_id)
                return result
            
            # 执行签到
            sign_div = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((
                    By.XPATH, "//div[button[text()='鸡腿 x 5'] and button[text()='试试手气']]"
                ))
            )
            
            if self.config['random_mode']:
                button = sign_div.find_element(By.XPATH, ".//button[text()='试试手气']")
                mode = "试试手气"
            else:
                button = sign_div.find_element(By.XPATH, ".//button[text()='鸡腿 x 5']")
                mode = "鸡腿 x 5"
                
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
            time.sleep(0.5)
            button.click()
            time.sleep(2)

            result = SigninResult(True, f"Selenium 签到成功 ({mode})", "selenium")
            member_id = parse_member_id_from_cookie(cookie)
            if member_id:
                result.user_info = self._fetch_user_info(member_id)
            return result
            
        except Exception as e:
            error_msg = str(e)
            # 检查是否是登录相关错误
            if any(keyword in error_msg.lower() for keyword in ['login', 'signin', 'authentication', '登录']):
                return SigninResult(False, f"Selenium - Cookie可能已过期: {error_msg}", "selenium", cookie_expired=True)
            else:
                return SigninResult(False, f"Selenium 签到异常: {error_msg}", "selenium")
        finally:
            if self.driver:
                self.driver.quit()

class NodeSeekHybridSigner:
    """NodeSeek 混合签到器主类"""
    
    def __init__(self):
        self.config = EnvironmentDetector.get_env_config()
        self.http_signer = HTTPSigner(self.config)
        self.selenium_signer = SeleniumSigner(self.config) if SELENIUM_AVAILABLE else None
        
        logging.info(f"🌍 运行环境: {self.config['environment']}")
        logging.info(f"📊 统计功能: {'开启' if self.config['enable_statistics'] else '关闭'}")
        logging.info(f"🤖 Selenium: {'可用' if self.selenium_signer else '不可用'}")
        
    def load_accounts(self) -> List[AccountConfig]:
        """加载账户配置 (来自 nodeseek_sign.py 逻辑)"""
        accounts = []
        cookies = []
        
        # 解析 Cookie 字符串
        cookie_str = os.environ.get("NS_COOKIE", "")
        if cookie_str:
            cookies = [c.strip() for c in cookie_str.split("&") if c.strip()]
        
        # 收集账户配置
        user = os.environ.get("USER", "")
        password = os.environ.get("PASS", "")
        if user and password:
            accounts.append((user, password))
            
        # 多账户支持
        index = 1
        while True:
            user = os.environ.get(f"USER{index}", "")
            password = os.environ.get(f"PASS{index}", "")
            if user and password:
                accounts.append((user, password))
                index += 1
            else:
                break
        
        # 确保账户和 Cookie 数量匹配
        max_count = max(len(accounts), len(cookies))
        while len(accounts) < max_count:
            accounts.append(("", ""))
        while len(cookies) < max_count:
            cookies.append("")
            
        # 构建账户配置
        account_configs = []
        for i in range(max_count):
            username, password = accounts[i] if i < len(accounts) else ("", "")
            cookie = cookies[i] if i < len(cookies) else ""
            display_name = username if username else f"账号{i+1}"
            
            account_configs.append(AccountConfig(
                index=i+1,
                display_name=display_name,
                cookie=cookie,
                username=username,
                password=password
            ))
            
        return account_configs
    
    def progressive_signin(self, account: AccountConfig) -> SigninResult:
        """渐进式签到策略"""
        logging.info(f"🎯 开始签到: {account.display_name}")
        
        if not account.cookie:
            return SigninResult(False, "无 Cookie", "none")
            
        # 方法 1: HTTP 签到 (优先)
        result = self.http_signer.signin(account.cookie)
        if result.success:
            logging.info(f"✅ HTTP 签到成功: {account.display_name}")
            return result
        else:
            logging.warning(f"⚠️  HTTP 签到失败: {result.message}")
            
        # 方法 2: 代理 HTTP 签到 (如果配置了代理)
        if self.config['proxy_url']:
            result = self.http_signer.signin(account.cookie, use_proxy=True)
            if result.success:
                logging.info(f"✅ 代理签到成功: {account.display_name}")
                return result
            else:
                logging.warning(f"⚠️  代理签到失败: {result.message}")
        
        # 方法 3: Selenium 签到 (终极方案)
        if (self.selenium_signer and 
            self.config['enable_selenium'] in ["true", "auto"]):
            try:
                result = self.selenium_signer.signin(account.cookie)
                if result.success:
                    logging.info(f"✅ Selenium 签到成功: {account.display_name}")
                    return result
                else:
                    logging.error(f"❌ Selenium 签到失败: {result.message}")
            except Exception as e:
                logging.error(f"❌ Selenium 异常: {str(e)}")
        
        # 所有方法都失败
        return SigninResult(False, "所有签到方法都失败，建议手动更新 Cookie", "failed")
    
    def enhance_with_statistics(self, result: SigninResult, cookie: str) -> SigninResult:
        """增强结果 - 添加统计信息"""
        if not self.config['enable_statistics'] or not result.success:
            return result
            
        try:
            tracker = StatisticsTracker(cookie)
            stats, msg = tracker.get_signin_stats(30)
            if stats:
                result.statistics = stats
                result.message += f" | 30天已签到{stats['days_count']}天，平均{stats['average']}个鸡腿/天"
        except Exception as e:
            logging.warning(f"⚠️  统计查询失败: {str(e)}")
            
        return result
    
    def run(self):
        """主执行流程"""
        logging.info("🚀 NodeSeek 混合签到器启动")
        logging.info("=" * 50)
        
        accounts = self.load_accounts()
        if not accounts:
            logging.error("❌ 未找到任何账户配置")
            return
            
        logging.info(f"📋 发现 {len(accounts)} 个账户")
        
        results = []
        cookies_updated = False
        updated_cookies = []
        expired_accounts = []  # 记录Cookie过期的账户
        
        for account in accounts:
            logging.info(f"\n{'='*30} {account.display_name} {'='*30}")
            
            # 执行签到
            result = self.progressive_signin(account)
            
            # 增强统计信息
            if result.success and account.cookie:
                result = self.enhance_with_statistics(result, account.cookie)

            # 附加用户信息 (Selenium 路径已在浏览器内取到的不重复取)
            if result.success and account.cookie and not result.user_info:
                try:
                    result.user_info = StatisticsTracker(account.cookie).get_user_info()
                except Exception as e:
                    logging.warning(f"⚠️  用户信息获取失败: {str(e)}")
            if result.user_info:
                _info = result.user_info
                logging.info(f"👤 用户信息: {_info.get('member_name')} (ID {_info.get('member_id')}) 等级 {_info.get('rank')} 鸡腿 {_info.get('coin')} 主题 {_info.get('nPost')} 评论 {_info.get('nComment')}")
            
            # 记录结果
            results.append((account, result))
            
            if result.success:
                logging.info(f"✅ {account.display_name}: {result.message}")
                updated_cookies.append(account.cookie)
                
                # 发送通知
                if NOTIFICATION_AVAILABLE:
                    try:
                        send(f"NodeSeek 签到成功", f"{account.display_name}: {result.message}")
                    except Exception as e:
                        logging.warning(f"⚠️  通知发送失败: {str(e)}")
                        
            else:
                logging.error(f"❌ {account.display_name}: {result.message}")
                updated_cookies.append(account.cookie)  # 保持原 Cookie
                
                # 检查是否Cookie过期
                if result.cookie_expired:
                    expired_accounts.append(account.display_name)
                    logging.warning(f"🚨 检测到Cookie过期: {account.display_name}")
                
                # 发送失败通知
                if NOTIFICATION_AVAILABLE:
                    try:
                        send(f"NodeSeek 签到失败", f"{account.display_name}: {result.message}")
                    except:
                        pass
        
        # 发送Cookie过期的TG通知
        if expired_accounts:
            expired_msg = f"🚨 <b>NodeSeek Cookie过期提醒</b>\n\n"
            expired_msg += f"以下账户的Cookie已过期，需要手动更新：\n"
            for i, account_name in enumerate(expired_accounts, 1):
                expired_msg += f"{i}. {account_name}\n"
            expired_msg += f"\n请到GitHub仓库的Variables页面更新NS_COOKIE变量"
            
            # 发送TG通知
            if send_telegram_message(expired_msg):
                logging.info(f"✅ 已通过TG通知Cookie过期: {len(expired_accounts)}个账户")
            else:
                logging.warning(f"⚠️  TG通知发送失败，但检测到{len(expired_accounts)}个Cookie过期")
        
        # Cookie检查完毕 - 用户可根据TG通知手动更新过期Cookie
        logging.info("ℹ️  Cookie状态已检查完毕，过期Cookie已通过TG通知")
        
        # 生成摘要报告
        success_count = sum(1 for _, result in results if result.success)
        logging.info(f"\n{'='*50}")
        logging.info(f"📊 签到完成: {success_count}/{len(results)} 成功")
        
        # 构建详细的签到结果消息
        summary_msg = f"🌟 <b>NodeSeek 签到报告</b>\n"
        summary_msg += f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        # 成功账户详情
        success_results = [(acc, res) for acc, res in results if res.success]
        if success_results:
            summary_msg += f"✅ <b>签到成功 ({len(success_results)}个账户)</b>\n"
            for i, (account, result) in enumerate(success_results, 1):
                # 提取鸡腿信息
                if "鸡腿" in result.message:
                    drumstick_info = result.message.split("：")[-1] if "：" in result.message else result.message
                    summary_msg += f"📱 账户{i}：{drumstick_info}\n"
                else:
                    summary_msg += f"📱 账户{i}：{result.message}\n"
                if result.user_info:
                    _info = result.user_info
                    _name = str(_info.get('member_name', '')).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    summary_msg += f"👤 用户：{_name}（ID {_info.get('member_id', '')}）\n"
                    summary_msg += f"🏅 等级：{_info.get('rank', '')}｜🍗 鸡腿总数：{_info.get('coin', '')}\n"
                    summary_msg += f"📝 主题帖数：{_info.get('nPost', '')}｜💬 评论数：{_info.get('nComment', '')}\n"
            summary_msg += "\n"
        
        # 失败账户处理
        failed_results = [(acc, res) for acc, res in results if not res.success]
        if failed_results:
            failed_count = len(failed_results)
            expired_count = len(expired_accounts)
            logging.warning(f"⚠️  {failed_count}个账户签到失败 (其中{expired_count}个Cookie过期)")
            
            summary_msg += f"❌ <b>签到失败 ({failed_count}个账户)</b>\n"
            for i, (account, result) in enumerate(failed_results, 1):
                summary_msg += f"🚫 账户{i}：{result.message}\n"
            summary_msg += "\n"
            
            if expired_count > 0:
                summary_msg += f"🚨 <b>Cookie过期：{expired_count}个账户</b>\n"
                summary_msg += f"💡 请及时更新过期的Cookie以确保正常签到\n\n"
        
        # 添加统计摘要
        summary_msg += f"📊 <b>统计摘要</b>\n"
        summary_msg += f"✅ 成功：{success_count}个\n"
        summary_msg += f"❌ 失败：{len(failed_results)}个\n"
        summary_msg += f"📈 成功率：{(success_count/len(results)*100):.1f}%"
        
        # 发送TG通知（无论成功失败都发送）
        if send_telegram_message(summary_msg):
            logging.info("✅ 签到结果已通过TG推送")
        else:
            logging.warning("⚠️  TG推送失败，但签到任务已完成")
            
        logging.info("🏁 混合签到器执行完毕")

def main():
    """主函数"""
    try:
        signer = NodeSeekHybridSigner()
        signer.run()
    except KeyboardInterrupt:
        logging.info("⏹️  用户中断执行")
    except Exception as e:
        logging.error(f"💥 执行异常: {str(e)}")
        logging.debug(traceback.format_exc())

if __name__ == "__main__":
    main()