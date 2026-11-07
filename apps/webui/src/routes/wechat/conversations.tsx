import { useEffect, useRef, useState } from 'react'
import { useSearch } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Page, Panel, Details, Empty, ErrorNotice, Evidence, Stamp, Status, Choice, api, useNative, useCommand } from './shared'
import { MessageTimeline } from './messages'
import type { MaaCommand, MaaConversation } from './types'

export interface MediaOption { id: number; name: string; type: string; size: number }
export function WechatConversationsPage() {
  const list = useNative<{items:MaaConversation[];stale?:boolean}>('/conversations')
  const routeSearch = useSearch({strict:false})
  const [selected, setSelected] = useState(routeSearch.conversation || '')
  useEffect(() => { if (routeSearch.conversation) setSelected(routeSearch.conversation) }, [routeSearch.conversation])
  const [search, setSearch] = useState('')
  const detail = useNative<MaaConversation>(selected ? `/conversations/${selected}` : null)
  const [drafts, setDrafts] = useState<Record<string,string>>({})
  const [mediaId, setMediaId] = useState('')
  const options = useNative<MediaOption[]>('/media-options',0)
  const [uploadError, setUploadError] = useState('')
  const [uploading, setUploading] = useState(false)
  const command = useCommand(list.refresh)
  const [pending, setPending] = useState<{id:string; target:string; text:string; created:number}>()
  const result = useNative<MaaCommand>(pending ? `/commands/${pending.id}` : null,2000)
  const completed = useRef('')
  const [notice, setNotice] = useState('')
  const last = result.data
  const terminal = !!last && (['succeeded','failed','expired','uncertain','sent'].includes(last.status))
  const timedOut = !!pending && Date.now() - pending.created > 310_000
  const sending = !!pending && !terminal && !timedOut
  useEffect(() => {
    if (!pending || !last || !terminal || completed.current === last.id) return
    completed.current = last.id
    const receipt = last.detail as {status?:string; reason?:string} | undefined
    if (last.status === 'sent' || (last.status === 'succeeded' && receipt?.status === 'sent')) {
      setNotice('已收到微信客户端的成功发送核验。')
      setDrafts(d => d[pending.target] === pending.text ? {...d, [pending.target]:''} : d)
    } else setNotice(`发送未确认：${receipt?.reason || receipt?.status || last.status}。请核对回执。`)
  }, [last, pending, terminal])
  const send = async (media = false) => {
    if (!selected || sending) return
    const text = drafts[selected] || ''
    const r = await command.run(media ? 'send_media' : 'send_message', {conversation_id:selected, ...(media ? {asset_id:Number(mediaId)} : {text})})
    if (r) {setPending({id:r.id,target:selected,text:media ? '' : text,created:Date.now()});setNotice('消息已进入队列，等待现场核验与发送回执。')}
  }
  const upload = async (file?: File) => {
    if (!file) return
    setUploading(true);setUploadError('')
    try { const form = new FormData();form.append('file',file);const asset = await api<MediaOption>('/media',{method:'POST',body:form});setMediaId(String(asset.id));options.refresh() }
    catch(e) {setUploadError(e instanceof Error ? e.message : '上传失败')}
    finally {setUploading(false)}
  }
  return <Page title="微信会话" description="查看已观察的上下文、身份和截图，在同一页面发送文字、图片或视频。" refresh={() => {list.refresh();detail.refresh()}}><ErrorNotice error={list.error || detail.error || result.error || uploadError || (list.data?.stale || detail.data?.stale ? '当前为历史快照，等待微信遥测恢复后再发送。' : '')} />
    <div className="grid items-start gap-5 lg:grid-cols-[280px_minmax(0,1fr)]"><Panel title={`最近聊天 · ${list.data?.items.length || 0}`}><Input value={search} onChange={e => setSearch(e.target.value)} aria-label="搜索聊天" placeholder="搜索聊天" /><div className="mt-3 max-h-[65vh] space-y-1 overflow-auto">{list.data?.items.filter(c => c.title.includes(search)).map(c => <button key={c.id} type="button" onClick={() => setSelected(c.id)} className={`flex w-full gap-3 rounded-md p-3 text-left hover:bg-muted ${selected === c.id ? 'bg-accent text-accent-foreground' : ''}`}><Evidence sha={c.avatar_asset} avatar /><div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{c.title}</p><p className="mt-1 text-xs text-muted-foreground">{c.message_row_count || 0} 条记录 · {c.private_contact?.profile_verified ? '好友已核验' : c.group_profile ? '群聊' : '待关联'}</p>{c.unread_at_observation && <Badge variant="outline" className="mt-1">观察到未读</Badge>}</div></button>)}</div>{!list.data?.items.length && <Empty />}</Panel>
    <Panel title={detail.data?.title || '选择一个会话'}>{!selected ? <Empty>从左侧选择已观察的微信会话</Empty> : <><Tabs defaultValue="visible"><TabsList><TabsTrigger value="visible">可见上下文</TabsTrigger><TabsTrigger value="history">消息记录</TabsTrigger><TabsTrigger value="evidence">截图与身份</TabsTrigger></TabsList><TabsContent value="visible"><div className="mb-4 flex gap-3 text-xs text-muted-foreground"><Status value={detail.data?.latest_snapshot?.status} /><Stamp value={detail.data?.latest_snapshot?.seen} /></div>{detail.data?.latest_snapshot?.status === 'gap' && <ErrorNotice error="本次观察与上一段上下文存在缺口，以下内容仅代表当前可见画面。" />}<div className="mt-4 max-h-[55vh] space-y-4 overflow-auto"><MessageTimeline rows={detail.data?.latest_snapshot?.payloads?.map(payload => ({payload}))} /></div></TabsContent><TabsContent value="history"><div className="max-h-[55vh] space-y-4 overflow-auto"><MessageTimeline rows={detail.data?.messages} /></div></TabsContent><TabsContent value="evidence"><div className="space-y-4"><Evidence sha={detail.data?.latest_snapshot?.frame_asset} /><Details title="会话身份" value={{id:detail.data?.id,private_contact:detail.data?.private_contact,group_profile:detail.data?.group_profile}} /></div></TabsContent></Tabs>
      <div className="mt-6 space-y-3 border-t pt-4"><Textarea aria-label="发送文字" placeholder="输入消息（最多 400 字）" value={drafts[selected] || ''} maxLength={400} rows={3} onChange={e => setDrafts(v => ({...v,[selected]:e.target.value}))} /><div className="flex flex-wrap items-center gap-2"><Button disabled={!drafts[selected]?.trim() || command.busy || sending || !detail.data?.manual_target_observed || list.data?.stale || detail.data?.stale || !!list.error || !!detail.error} onClick={() => void send()}>发送文字</Button><Choice label="选择媒体素材" value={mediaId} onChange={setMediaId} options={[{value:'',label:'选择图片 / 视频'},...(options.data || []).map(a => ({value:String(a.id),label:a.name}))]} /><Button variant="outline" disabled={!mediaId || sending || command.busy || !detail.data?.manual_target_observed || list.data?.stale || detail.data?.stale || !!list.error || !!detail.error} onClick={() => void send(true)}>发送媒体</Button><label className="inline-flex cursor-pointer items-center rounded-md border px-3 py-2 text-sm">{uploading ? '上传中…' : '上传素材'}<input className="sr-only" type="file" accept=".png,.jpg,.jpeg,.webp,.mp4" disabled={uploading} onChange={e => void upload(e.target.files?.[0])} /></label></div>{notice && <p role="status" className="text-sm text-muted-foreground">{notice}</p>}{timedOut && !terminal && <ErrorNotice error="仍未取得终态回执。请核对客户端与执行记录，避免重复发送。" />}{last && <Details title="发送回执" value={last} />}</div></>}</Panel></div>
  </Page>
}
