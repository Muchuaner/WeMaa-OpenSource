import unittest
from endpoint_config import valid_endpoint

class EndpointConfigTests(unittest.TestCase):
    def test_operator_configured_domain(self):
        self.assertTrue(valid_endpoint("https://chat.example.org/api/v1/wechat-maa", "chat.example.org"))
        self.assertFalse(valid_endpoint("https://other.example.org/api/v1/wechat-maa", "chat.example.org"))
    def test_reject_unsafe_or_wrong_endpoint(self):
        for url in ["http://chat.example.org/api/v1/wechat-maa", "https://user:pass@chat.example.org/api/v1/wechat-maa", "https://chat.example.org/api/v1/wechat-maa?token=x", "https://chat.example.org/other", "https://chat.example.org:bad/api/v1/wechat-maa"]:
            self.assertFalse(valid_endpoint(url), url)
