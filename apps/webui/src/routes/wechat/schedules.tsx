import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useToast } from '@/hooks/use-toast'
import { Page, Panel, Empty, ErrorNotice, Stamp, Status, Choice, Details, api, post, useNative } from './shared'
import type { MaaConversation, MaaSchedule, MaaScheduledMessage } from './types'
import type { MediaOption } from './conversations'

const weekdays = ['周一','周二','周三','周四','周五','周六','周日']
type Draft = {id?:string; revision?:number; request_id:string; conversation_id:string; messages:MaaScheduledMessage[]; mode:string; time:string; days:number[]; at:string}
function localTime(epoch: number) { return new Date(epoch * 1000 + 8 * 3600000).toISOString().slice(0,16) }
export function WechatSchedulesPage() {
  const {toast} = useToast()
  const [trash, setTrash] = useState(false)
  const list = useNative<{items:MaaSchedule[]}>(`/schedules?deleted=${trash}`)
  const targets = useNative<{items:MaaConversation[]}>('/conversations',15000)
  const media = useNative<MediaOption[]>('/media-options',0)
  const greetings = useNative<{enabled:boolean; next_at?:number; reason?:string; validated?:unknown; items:unknown[]}>('/greetings',15000)
  const [draft, setDraft] = useState<Draft>()
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [confirm, setConfirm] = useState<{row:MaaSchedule;action:'delete'|'cancel'|'restore'}>()
  const open = (row?:MaaSchedule) => {
    setError('');media.refresh();targets.refresh()
    setDraft({id:row?.id,revision:row?.revision,request_id:crypto.randomUUID(),conversation_id:row?.conversation_id || '',
      messages:row ? (row.messages?.length ? row.messages.map(m => ({kind:m.kind,text:m.text,asset_id:m.asset_id,category:m.category})) : [{kind:'text',text:row.text}]) : [{kind:'text',text:''}],
      mode:row?.recurrence?.weekdays ? 'weekly' : 'once',days:row?.recurrence?.weekdays || [0,1,2,3,4,5,6],time:row?.recurrence?.time || '09:00',at:row ? localTime(row.send_at) : ''})
  }
  const updateMessage = (index:number, value:Partial<MaaScheduledMessage>) => setDraft(d => d && ({...d,messages:d.messages.map((m,i) => i === index ? {...m,...value} : m)}))
  const save = async () => {
    if (!draft) return
    setSaving(true);setError('')
    try {
      const body = {conversation_id:draft.conversation_id,messages:draft.messages.map(m => m.kind === 'text' ? {kind:m.kind,text:m.text} : m.kind === 'greeting' ? {kind:m.kind,category:m.category} : {kind:m.kind,asset_id:m.asset_id}),
        ...(draft.mode === 'weekly' ? {recurrence:{weekdays:draft.days,time:draft.time}} : {send_at:Date.parse(draft.at + ':00+08:00')/1000}),
        ...(draft.id ? {revision:draft.revision} : {request_id:draft.request_id})}
      await api(draft.id ? `/schedules/${draft.id}` : '/schedules',{method:draft.id ? 'PUT' : 'POST',body:JSON.stringify(body)})
      setDraft(undefined);list.refresh();toast({title:'定时任务已保存'})
    } catch(e) {setError(e instanceof Error ? e.message : '保存失败')}
    finally {setSaving(false)}
  }
  const upload = async (index:number,file?:File) => {
    if(!file)return
    setSaving(true);setError('')
    try { const form = new FormData();form.append('file',file);const asset=await api<MediaOption>('/media',{method:'POST',body:form});updateMessage(index,{asset_id:asset.id});media.refresh() }
    catch(e) {setError(e instanceof Error ? e.message : '上传失败')}
    finally {setSaving(false)}
  }
  return <Page title="定时发送" description="编排文字、图片、视频和问候语，支持单次发送及按星期循环。所有时间均为北京时间。" refresh={list.refresh} actions={<><Button variant="outline" onClick={() => setTrash(v => !v)}>{trash ? '返回任务' : '回收站'}</Button><Button onClick={() => open()}>新建定时任务</Button></>}>
    <ErrorNotice error={list.error || targets.error || (!draft ? error : '')} />
    <Panel title={trash ? '已删除的任务' : '任务列表'}><Table><TableHeader><TableRow><TableHead>对象</TableHead><TableHead>消息</TableHead><TableHead>发送时间 · 北京</TableHead><TableHead>状态</TableHead><TableHead>操作</TableHead></TableRow></TableHeader><TableBody>{list.data?.items.map(r => <TableRow key={r.id}><TableCell className="font-medium">{r.target.title}</TableCell><TableCell className="min-w-48 max-w-md"><div className="space-y-2">{(r.messages?.length ? r.messages : [{kind:'text',text:r.text} as MaaScheduledMessage]).map((m,i) => <p key={i} className="break-words text-sm">{i+1}. {m.kind === 'text' ? m.text : m.kind === 'greeting' ? m.resolved_text || (m.category === 'daily' ? '每日一言' : '日常问候') : `${m.kind === 'image' ? '图片' : '视频'} · ${m.name || m.asset_id}`}{m.status && <span className="ml-2"><Status value={m.status} /></span>}</p>)}</div></TableCell><TableCell className="min-w-44 text-xs">{r.recurrence?.weekdays && <p className="mb-2">{r.recurrence.weekdays.map(d => weekdays[d]).join('、')} {r.recurrence.time}</p>}<Stamp value={r.send_at} /></TableCell><TableCell><Status value={r.status} />{r.detail && <div className="mt-2"><Details value={r.detail} /></div>}</TableCell><TableCell><div className="flex flex-wrap gap-1">{trash ? <Button size="sm" variant="ghost" onClick={() => setConfirm({row:r,action:'restore'})}>恢复记录</Button> : <>{r.status === 'pending' && <><Button size="sm" variant="ghost" onClick={() => open(r)}>编辑</Button><Button size="sm" variant="ghost" onClick={() => setConfirm({row:r,action:'cancel'})}>取消</Button></>}<Button size="sm" variant="ghost" disabled={['sending','dispatched','issued','queued'].includes(r.status)} onClick={() => setConfirm({row:r,action:'delete'})}>删除</Button></>}</div></TableCell></TableRow>)}</TableBody></Table>{!list.data?.items.length && <Empty>{list.loading ? '正在读取任务…' : trash ? '回收站为空' : '还没有定时任务'}</Empty>}</Panel>
    <Panel title="日常问候" description="保留已有的频率、冷却时间和现场核验规则。"><ErrorNotice error={greetings.error} /><div className="flex flex-wrap items-center gap-4"><Status value={greetings.data?.enabled ? 'running' : 'cancelled'} /><span className="text-sm text-muted-foreground">下次计划：<Stamp value={greetings.data?.next_at} /></span><Button variant="outline" disabled={!greetings.data || saving} onClick={async () => {setSaving(true);try{await post('/greetings/control',{enabled:!greetings.data?.enabled});greetings.refresh()}catch(e){setError(e instanceof Error ? e.message : '设置失败')}finally{setSaving(false)}}}>{greetings.data?.enabled ? '暂停日常问候' : '开启日常问候'}</Button></div><div className="mt-4"><Details title="问候任务与核验记录" value={greetings.data} /></div></Panel>
    <Dialog open={!!draft} onOpenChange={v => !v && !saving && setDraft(undefined)}><DialogContent className="max-h-[90vh] overflow-auto sm:max-w-2xl"><DialogHeader><DialogTitle>{draft?.id ? '编辑定时任务' : '新建定时任务'}</DialogTitle><DialogDescription>逐条执行消息，前一条核验成功后再发送下一条。</DialogDescription></DialogHeader><ErrorNotice error={error} />{draft && <div className="space-y-5"><label className="grid gap-2 text-sm">发送对象<Choice label="发送对象" value={draft.conversation_id} onChange={v => setDraft({...draft,conversation_id:v})} options={[{value:'',label:'选择已核验的会话'},...(targets.data?.items || []).filter(c => c.manual_target_observed && c.avatar_asset).map(c => ({value:c.id,label:`${c.title} · ${c.id.slice(0,8)}`}))]} /></label>
      <div className="space-y-3">{draft.messages.map((m,i) => <div key={i} className="space-y-3 rounded-lg border p-4"><div className="flex flex-wrap items-center justify-between gap-2"><span className="text-sm font-medium">消息 {i+1}</span><div className="flex gap-1"><Button size="sm" variant="ghost" disabled={!i || saving} onClick={() => {const messages=[...draft.messages];[messages[i-1],messages[i]]=[messages[i],messages[i-1]];setDraft({...draft,messages})}}>上移</Button><Button size="sm" variant="ghost" disabled={i === draft.messages.length-1 || saving} onClick={() => {const messages=[...draft.messages];[messages[i+1],messages[i]]=[messages[i],messages[i+1]];setDraft({...draft,messages})}}>下移</Button><Button size="sm" variant="ghost" disabled={saving || draft.messages.length === 1} onClick={() => setDraft({...draft,messages:draft.messages.filter((_,j) => j !== i)})}>移除</Button></div></div>
      <Choice label={`消息 ${i+1} 类型`} value={m.kind} onChange={v => updateMessage(i,{kind:v as MaaScheduledMessage['kind'],text:'',asset_id:undefined,category:'daily'})} options={[{value:'text',label:'文字'},{value:'image',label:'图片'},{value:'video',label:'视频'},{value:'greeting',label:'随机问候语'}]} />{m.kind === 'text' ? <Textarea aria-label={`消息 ${i+1} 内容`} value={m.text || ''} onChange={e => updateMessage(i,{text:e.target.value})} rows={3} maxLength={400} /> : m.kind === 'greeting' ? <Choice label="问候类型" value={m.category || 'daily'} onChange={v => updateMessage(i,{category:v as 'daily'|'greeting'})} options={[{value:'daily',label:'每日一言'},{value:'greeting',label:'日常问候'}]} /> : <div className="flex flex-wrap gap-2"><Choice label={`消息 ${i+1} 素材`} value={String(m.asset_id || '')} onChange={v => updateMessage(i,{asset_id:Number(v)})} options={[{value:'',label:'选择素材'},...(media.data || []).filter(a => a.type === m.kind).map(a => ({value:String(a.id),label:a.name}))]} /><label className="cursor-pointer rounded-md border px-3 py-2 text-sm">上传{m.kind === 'image' ? '图片' : '视频'}<input className="sr-only" type="file" disabled={saving} accept={m.kind === 'image' ? '.png,.jpg,.jpeg,.webp' : '.mp4'} onChange={e => void upload(i,e.target.files?.[0])} /></label></div>}</div>)}<Button variant="outline" disabled={draft.messages.length >= 20 || saving} onClick={() => setDraft({...draft,messages:[...draft.messages,{kind:'text',text:''}]})}>添加消息</Button></div>
      <label className="grid gap-2 text-sm">发送模式<Choice label="发送模式" value={draft.mode} onChange={v => setDraft({...draft,mode:v})} options={[{value:'once',label:'单次定时'},{value:'weekly',label:'每周循环'}]} /></label>{draft.mode === 'weekly' ? <div className="space-y-4"><div className="flex flex-wrap gap-3">{weekdays.map((label,i) => <label key={i} className="flex items-center gap-2 text-sm"><Checkbox checked={draft.days.includes(i)} onCheckedChange={v => setDraft({...draft,days:v ? [...draft.days,i].sort() : draft.days.filter(d => d !== i)})} />{label}</label>)}</div><label className="grid gap-2 text-sm">发送时间（北京）<Input type="time" value={draft.time} onChange={e => setDraft({...draft,time:e.target.value})} /></label></div> : <label className="grid gap-2 text-sm">发送时间（北京）<Input type="datetime-local" value={draft.at} onChange={e => setDraft({...draft,at:e.target.value})} /></label>}</div>}<DialogFooter><Button variant="outline" disabled={saving} onClick={() => setDraft(undefined)}>取消</Button><Button disabled={saving} onClick={() => void save()}>{saving ? '保存中…' : '保存任务'}</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={!!confirm} onOpenChange={v => !v && !saving && setConfirm(undefined)}><DialogContent><DialogHeader><DialogTitle>{confirm?.action === 'restore' ? '恢复任务记录' : confirm?.action === 'delete' ? '移入回收站' : '取消定时任务'}</DialogTitle><DialogDescription>{confirm?.action === 'restore' ? '恢复后保留原执行状态，不会重新发送已取消或已完成的消息。' : `将操作发送给“${confirm?.row.target.title}”的任务，待发送的消息会停止。`}</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={saving} onClick={() => setConfirm(undefined)}>返回</Button><Button disabled={saving} onClick={async () => {if(!confirm)return;setSaving(true);try{await post(`/schedules/${confirm.row.id}/${confirm.action}`,{revision:confirm.row.revision});setConfirm(undefined);list.refresh()}catch(e){setError(e instanceof Error ? e.message : '操作失败');setConfirm(undefined)}finally{setSaving(false)}}}>确认</Button></DialogFooter></DialogContent></Dialog>
  </Page>
}
