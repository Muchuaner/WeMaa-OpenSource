import { Evidence, Empty, Stamp } from './shared'
import type { MaaPayload } from './types'

function speaker(payload: MaaPayload) {
  return payload.sender_name || payload.sender_display_name || payload.sender_identity?.display_name
    || (payload.group_sender_verified ? payload.group_sender_name : undefined)
    || (payload.sender && !['unresolved', 'unknown', 'system'].includes(payload.sender) ? payload.sender : undefined)
    || (payload.direction === 'outgoing' ? '当前微信账号' : '发言人待核验')
}

function identity(payload: MaaPayload) {
  const wxid = payload.sender_identity_verified === true && payload.sender_identity_status === 'profile_verified'
    ? payload.sender_wechat_id : payload.sender_identity?.verified === true ? payload.sender_identity.wechat_id : undefined
  if (wxid) return { label: '微信号', value: wxid }
  const key = payload.sender_identity_key || payload.sender_key || payload.sender_identity?.key
  return key ? { label: '视觉身份', value: key } : undefined
}

export function MessageTimeline({ rows = [] }: { rows?: Array<{payload: MaaPayload; seen?: number}> }) {
  const displayed: typeof rows = [], review: MaaPayload[] = []
  for (const row of rows) {
    const { payload } = row
    if (payload.message_kind === 'time') {
      if (!payload.text?.trim()) continue
      const previous = displayed[displayed.length - 1]?.payload
      if (previous?.message_kind === 'time' && previous.text?.trim() === payload.text.trim()) continue
      displayed.push(row)
    } else if (['incoming', 'outgoing'].includes(payload.direction || '')
      && [undefined, 'text', 'media', 'unsupported'].includes(payload.message_kind)) {
      displayed.push(row)
    } else if (payload.message_kind !== 'sender_label') review.push(payload)
  }
  return <><div role="log" aria-label="微信消息" className="space-y-4">
    {displayed.length ? displayed.map(({payload, seen}, index) => {
      if (payload.message_kind === 'time') return <div className="text-center text-xs text-muted-foreground" key={index}><time>{payload.text}</time></div>
      const name = speaker(payload), id = identity(payload), outgoing = payload.direction === 'outgoing'
      return <div key={index} className={`flex gap-2 ${outgoing ? 'flex-row-reverse' : ''}`} data-message-direction={payload.direction}>
        <Evidence sha={payload.sender_avatar_asset} avatar alt={`${name}的头像`} />
        <div className={`max-w-[85%] rounded-lg border px-4 py-3 ${outgoing ? 'bg-primary/10' : 'bg-muted/40'}`}>
          <div className="mb-1 flex flex-wrap gap-2 text-xs text-muted-foreground"><span>{name}</span>{id && <span className="break-all" data-sender-id={id.value} title={`${id.label}：${id.value}`}>{id.label}：{id.label === '视觉身份' ? `${id.value.slice(0,12)}…` : id.value}</span>}</div>
          {payload.message_kind === 'media' ? <><Evidence sha={payload.media_asset} alt={`${name}发送的媒体`} /><p className="mt-2 text-xs text-muted-foreground">图片 / 视频 · 截图核验</p></> : <p className="whitespace-pre-wrap break-words text-sm">{payload.text || '内容待核验'}</p>}
          <details className="mt-2 text-xs text-muted-foreground"><summary className="cursor-pointer">原文与身份证据</summary>{seen && <p className="mt-2">采集于 <Stamp value={seen} /></p>}<pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(payload,null,2)}</pre></details>
        </div>
      </div>
    }) : <Empty>尚无可见消息</Empty>}
  </div>{review.length > 0 && <details className="mt-4 text-xs text-muted-foreground"><summary>{review.length} 条未归属识别记录</summary><pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(review,null,2)}</pre></details>}</>
}
