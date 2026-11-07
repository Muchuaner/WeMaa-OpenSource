"""Private contact summaries, one-shot sends and bounded delivery receipts."""
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
import redis

from wemaa.routes import bounded_json
from wemaa.errors import BizError
from wemaa.services import api as service
from wemaa.routes import ok


def api_auth(request: Request) -> None:
    service.authenticate(request.headers.get('authorization', ''))


router = APIRouter(dependencies=[Depends(api_auth)])


def available_call(fn, *args):
    try:
        return fn(*args)
    except redis.RedisError:
        raise BizError(50334, '微信 API 队列暂不可用', 503) from None


@router.get('/contacts')
def contacts(keyword: str | None = Query(None, max_length=256),
             page: int = Query(1, ge=1, le=10000), page_size: int = Query(30, ge=1, le=100)) -> dict:
    return ok(available_call(service.contacts, keyword, page, page_size))


@router.post('/messages', status_code=202)
async def create_message(request: Request, response: Response) -> dict:
    body = await bounded_json(request, 16 * 1024)
    row, created = await run_in_threadpool(available_call, service.create_message, body)
    response.status_code = 202 if created else 200
    return ok(row)


@router.get('/messages/{message_id}')
def message(message_id: str) -> dict:
    return ok(available_call(service.message, message_id))


@router.get('/received-messages')
def received_messages(wechat_id: str | None = Query(None,min_length=1,max_length=256),
                      contact_id: str | None = Query(None,min_length=1,max_length=256),
                      after: int = Query(0,ge=0,le=9007199254740991),
                      limit: int = Query(100,ge=1,le=200)) -> dict:
    return ok(available_call(service.received_messages,wechat_id,contact_id,after,limit))


@router.get('/messages/{message_id}/replies')
def replies(message_id: str, after: int = Query(0,ge=0,le=9007199254740991),
            limit: int = Query(100,ge=1,le=200)) -> dict:
    return ok(available_call(service.replies,message_id,after,limit))
