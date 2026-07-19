import requests
from .signature import SignatureGenerator
import json

class BitunixClient:
    BASE_URL = "https://fapi.bitunix.com"
    
    def __init__(self, api_key: str, api_secret: str):
        self.sig_gen = SignatureGenerator(api_key, api_secret)
    
    def _request(self, method: str, endpoint: str, query_params: str = "", body: dict = None):
        import json
        
        # Для подписи: JSON без пробелов (как в официальном коде)
        body_str = json.dumps(body) if body else ""
        
        # Для POST: query_params пустой
        if method == "POST":
            sign_query = ""
        else:
            sign_query = query_params.replace("=", "").replace("&", "") if query_params else ""
        
        headers = self.sig_gen.generate(sign_query, body_str)
        
        url = f"{self.BASE_URL}{endpoint}"
        if query_params:
            url += f"?{query_params}"
        
        # print(f"🔍 URL: {url}")
        
        if body:
            response = requests.request(method, url, headers=headers, json=body)
        else:
            response = requests.request(method, url, headers=headers)
        
        result = response.json()
        # print(f"📡 Ответ: {result.get('code')} - {result.get('msg')}")
        return result
    
    def get(self, endpoint: str, query_params: str = ""):
        return self._request("GET", endpoint, query_params)
    
    def post(self, endpoint: str, query_params: str = "", body: dict = None):
        return self._request("POST", endpoint, query_params, body)