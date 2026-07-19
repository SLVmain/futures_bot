import hashlib
import uuid
import time

class SignatureGenerator:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
    
    @staticmethod
    def sha256_hex(input_string: str) -> str:
        return hashlib.sha256(input_string.encode('utf-8')).hexdigest()
    
    def generate(self, query_params: str = "", body_str: str = "") -> dict:
        """
        Генерирует заголовки.
        digest = SHA256(nonce + timestamp + api-key + queryParams + body)
        sign = SHA256(digest + secretKey)
        """
        nonce = uuid.uuid4().hex
        timestamp = str(int(time.time() * 1000))
        
        digest_input = nonce + timestamp + self.api_key + query_params + body_str
        digest = self.sha256_hex(digest_input)
        
        sign_input = digest + self.api_secret
        sign = self.sha256_hex(sign_input)
        
        # print(f"🔐 nonce: {nonce}")
        # print(f"🔐 timestamp: {timestamp}")
        # print(f"🔐 api-key: {self.api_key}")
        # print(f"🔐 queryParams: '{query_params}'")
        # print(f"🔐 body: '{body_str}'")
        # print(f"🔐 digest_input: {digest_input}")
        # print(f"🔐 digest: {digest}")
        # print(f"🔐 sign: {sign}")
        
        return {
            'api-key': self.api_key,
            'sign': sign,
            'nonce': nonce,
            'timestamp': timestamp,
            'language': 'en-US',
            'Content-Type': 'application/json'
        }