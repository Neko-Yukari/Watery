import logging
import asyncio
import httpx
import yaml
import os
from typing import List, Dict, Any, Optional
from app.core.config import settings

logger = logging.getLogger(__name__)

class ProxyManager:
    def __init__(self):
        self.clash_api_url = os.getenv("CLASH_API_URL", "http://clash:9090")
        self.sub_url = os.getenv("PROXY_SUB_URL", "")
        self.config_path = os.getenv("CLASH_CONFIG_PATH", "/app/data/clash/config.yaml")
        self.proxy_status = "unknown"
        self.failed_reason = ""

    async def update_proxies(self):
        """核心逻辑：抓取订阅，过滤美国节点，重写配置文件并热重载"""
        logger.info("Starting proxy update task...")
        
        if not self.sub_url:
            logger.warning("No PROXY_SUB_URL provided, skipping update.")
            return

        try:
            # 1. 抓取订阅内容
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(self.sub_url)
                response.raise_for_status()
                sub_data = yaml.safe_load(response.text)

            if not sub_data or "proxies" not in sub_data:
                logger.error("Invalid subscription data: 'proxies' field missing.")
                return

            # 2. 过滤美国节点 
            # (同时匹配 '\U0001F1FA\U0001F1F8' 美国国旗, '美国', 'US')
            us_keywords = ["美国", "US", "\U0001F1FA\U0001F1F8"]
            us_proxies = []
            
            for p in sub_data["proxies"]:
                name = p.get("name", "")
                if any(kw in name for kw in us_keywords):
                    us_proxies.append(p)
            
            if not us_proxies:
                logger.warning("No US proxies found in subscription. Falling back to all proxies if needed.")
                # 若没美国节点，退而求其次选一个可访问 Gemini 的地区或所有节点
                us_proxies = sub_data["proxies"][:10] 

            # 3. 构造新的 config.yaml
            proxy_names = [p["name"] for p in us_proxies]
            
            new_config = {
                "port": 7890,
                "socks-port": 7891,
                "external-controller": "0.0.0.0:9090",
                "secret": "",
                "mode": "rule",
                "log-level": "info",
                "allow-lan": True,
                "proxies": us_proxies,
                "proxy-groups": [
                    {
                        "name": "Gemini-Pool",
                        "type": "url-test",
                        "url": "http://www.gstatic.com/generate_204",
                        "interval": 300,
                        "proxies": proxy_names
                    }
                ],
                "rules": [
                    "DOMAIN-SUFFIX,googleapis.com,Gemini-Pool",
                    "DOMAIN-SUFFIX,google.com,Gemini-Pool",
                    "MATCH,DIRECT"
                ]
            }

            # 4. 写入宿主机挂载的文件
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(new_config, f, allow_unicode=True)
            
            # 5. 通知 Clash 控制台进行热重载 (PUT /configs)
            # 由于运行在容器内，且由于 docker-compose 挂载，文件已同步。
            # 告知控制台只需重新加载。
            await self.reload_clash_config()
            logger.info(f"Successfully updated Clash config with {len(us_proxies)} US proxies.")
            
        except Exception as e:
            logger.error(f"Failed to update proxies: {str(e)}")
            self.proxy_status = "error"
            self.failed_reason = str(e)

    async def reload_clash_config(self):
        """调用 Clash REST API 重载配置"""
        payload = {"path": "", "payload": ""} # 空 path 意味着重载当前配置文件
        try:
            async with httpx.AsyncClient() as client:
                res = await client.put(f"{self.clash_api_url}/configs", json=payload, timeout=5)
                if res.status_code != 204:
                    logger.warning(f"Clash reload returned status: {res.status_code}")
        except Exception as e:
            logger.error(f"Failed to notify Clash reload: {str(e)}")

    async def get_health_status(self) -> Dict[str, Any]:
        """获取当前代理由测速组的状态信息"""
        try:
            async with httpx.AsyncClient() as client:
                # 获取代理状态
                res = await client.get(f"{self.clash_api_url}/proxies/Gemini-Pool", timeout=2)
                if res.status_code == 200:
                    data = res.json()
                    # 检查是否有在线节点 (delay > 0)
                    history = data.get("history", [])
                    if history and history[-1].get("delay", 0) > 0:
                        self.proxy_status = "alive"
                        return {"status": "ok", "latency": history[-1].get("delay")}
                    else:
                        self.proxy_status = "timeout"
                        return {"status": "timeout", "message": "All US nodes failed latency test."}
        except Exception as e:
            self.proxy_status = "error"
            return {"status": "error", "message": str(e)}
        
        return {"status": "unknown"}

    async def start_loop(self):
        """后台轮询任务"""
        logger.info("ProxyManager loop started.")
        # 初始刷新
        await self.update_proxies()
        
        while True:
            # 每 30 分钟同步一次订阅
            await asyncio.sleep(1800)
            await self.update_proxies()

# 全局单例
proxy_manager = ProxyManager()
