"""验证操作者配置的 HTTPS 接入地址；不接受账号口令、查询或片段。"""
from urllib.parse import urlsplit


def valid_endpoint(endpoint, trusted_host=None):
    try:
        u = urlsplit(endpoint)
        return bool(u.scheme == "https" and u.hostname and not u.username and not u.password
                    and u.port in (None, 443) and u.path == "/api/v1/wechat-maa"
                    and not u.query and not u.fragment
                    and (trusted_host is None or u.hostname == trusted_host.lower()))
    except (TypeError, ValueError, AttributeError):
        return False
