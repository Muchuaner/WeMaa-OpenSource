import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Page, Panel, Details, Empty, ErrorNotice, Stamp, Status, Choice, useNative } from './shared'
import type { MaaActivity, MaaReplyHistory } from './types'

export function WechatActivityPage() {
  const [category, setCategory] = useState('all')
  const [before, setBefore] = useState(0)
  const [paused, setPaused] = useState(false)
  const logs = useNative<{items:MaaActivity[];has_more:boolean;latest_seq:number;stale:boolean}>(`/activity?category=${category}&before=${before}&limit=100`,before || paused ? 0 : 3000)
  const replies = useNative<MaaReplyHistory>('/replies',paused ? 0 : 10000)
  return <Page title="微信运行日志" description="按时间回查收信、联系人采集、轮询、指令与回复处理记录。" refresh={() => {logs.refresh();replies.refresh()}}><ErrorNotice error={logs.error || replies.error || (logs.data?.stale ? 'Windows 遥测过期，当前显示历史日志。' : '')} /><div className="flex gap-3"><Choice label="日志类型" value={category} onChange={v => {setCategory(v);setBefore(0)}} options={[{value:'all',label:'全部事件'},{value:'messages',label:'消息'},{value:'contacts',label:'联系人'},{value:'polling',label:'轮询'},{value:'actions',label:'指令'}]} /><Button variant="outline" onClick={() => setPaused(v => !v)}>{paused ? '恢复更新' : '暂停更新'}</Button><Button variant="outline" onClick={() => {setBefore(0);setPaused(false)}}>返回实时日志</Button></div><Panel title="事件流"><div className="divide-y">{logs.data?.items.map(r => <div key={r.seq} className="space-y-2 py-3"><div className="flex flex-wrap items-center gap-3"><span className="text-xs tabular-nums text-muted-foreground">#{r.seq}</span><span className="text-sm font-medium">{r.kind}</span><span className="ml-auto text-xs text-muted-foreground"><Stamp value={r.seen} /></span></div><Details value={r.detail} /></div>)}</div>{!logs.data?.items.length && <Empty />}<div className="mt-4"><Button variant="outline" disabled={!logs.data?.has_more || !logs.data.items.length} onClick={() => setBefore(Math.min(...(logs.data?.items.map(r => r.seq) || [0])))}>更早记录</Button></div></Panel><Panel title="回复处理记录"><div className="divide-y">{replies.data?.items.map(r => <div key={r.event_id} className="flex flex-wrap items-center gap-3 py-3 text-sm"><Status value={r.status} /><span>{r.reason === 'WEMAA_CORE_ACCEPTED' ? '已交给核心消息流程' : r.reason || r.event_id.slice(0,16)}</span><span className="ml-auto text-xs text-muted-foreground"><Stamp value={r.updated_at} /></span><Details value={r} /></div>)}</div>{!replies.data?.items.length && <Empty />}</Panel></Page>
}
