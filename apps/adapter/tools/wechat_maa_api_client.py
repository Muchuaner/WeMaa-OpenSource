"""Explicit Maa API calls. No automatic send, retry, or polling."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',default='https://your-service.example.com/api/v1/wechat-maa-api')
    commands=parser.add_subparsers(dest='command',required=True)
    contacts=commands.add_parser('contacts');contacts.add_argument('--keyword',default='');contacts.add_argument('--page',type=int,default=1)
    send=commands.add_parser('send');send.add_argument('--request-id',required=True,type=uuid.UUID)
    target=send.add_mutually_exclusive_group(required=True);target.add_argument('--wechat-id');target.add_argument('--contact-id')
    send.add_argument('--text',required=True)
    status=commands.add_parser('status');status.add_argument('message_id')
    received=commands.add_parser('received')
    sender=received.add_mutually_exclusive_group(required=True);sender.add_argument('--wechat-id');sender.add_argument('--contact-id')
    replies=commands.add_parser('replies');replies.add_argument('message_id')
    for reading in (received,replies):
        reading.add_argument('--after',type=int,default=0);reading.add_argument('--limit',type=int,default=100)
    args=parser.parse_args()
    token=os.environ.get('WECHAT_MAA_API_TOKEN','')
    if not token:parser.error('Set WECHAT_MAA_API_TOKEN in your private environment')
    if urllib.parse.urlsplit(args.base).scheme!='https':parser.error('HTTPS is required')
    body=None
    if args.command=='contacts':path='/contacts?'+urllib.parse.urlencode({'keyword':args.keyword,'page':args.page})
    elif args.command=='status':path='/messages/'+urllib.parse.quote(args.message_id,safe='')
    elif args.command in ('received','replies'):
        query={'after':args.after,'limit':args.limit}
        if args.command=='received':
            query.update({'wechat_id':args.wechat_id} if args.wechat_id else {'contact_id':args.contact_id})
            path='/received-messages'
        else:path='/messages/'+urllib.parse.quote(args.message_id,safe='')+'/replies'
        path+='?'+urllib.parse.urlencode(query)
    else:
        path='/messages'
        recipient={'wechat_id':args.wechat_id} if args.wechat_id else {'contact_id':args.contact_id}
        body=json.dumps({'request_id':str(args.request_id),'recipient':recipient,'text':args.text},ensure_ascii=False).encode('utf-8')
    request=urllib.request.Request(args.base.rstrip('/')+path,data=body,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=30) as response:
            print(json.dumps(json.load(response),ensure_ascii=False,indent=2))
    except urllib.error.HTTPError as exc:
        print('HTTP '+str(exc.code),file=sys.stderr)
        print(exc.read(16384).decode('utf-8','replace'),file=sys.stderr);return 1
    except urllib.error.URLError:
        message=('Network error; recover this task by repeating the same request_id and parameters. Do not create a new request_id.'
                 if args.command=='send' else 'Network error; retry the read-only query when the connection recovers.')
        print(message,file=sys.stderr);return 1
    return 0


if __name__=='__main__':sys.exit(main())
