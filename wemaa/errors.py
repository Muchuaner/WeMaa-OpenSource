"""统一业务异常。"""
from fastapi import HTTPException


class BizError(HTTPException):
    """业务错误：code 对应文档《04-API接口设计》错误码约定。"""

    def __init__(self, code: int, message: str, status_code: int = 400) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message})


def unauthorized(message: str = "未登录或登录已过期") -> BizError:
    return BizError(code=40101, message=message, status_code=401)


def forbidden(message: str = "无权限执行该操作") -> BizError:
    return BizError(code=40301, message=message, status_code=403)


def not_found(message: str = "资源不存在") -> BizError:
    return BizError(code=40401, message=message, status_code=404)


def insufficient_credit(message: str = "积分不足") -> BizError:
    return BizError(code=40901, message=message, status_code=409)


def conflict(message: str, code: int = 40902) -> BizError:
    return BizError(code=code, message=message, status_code=409)
