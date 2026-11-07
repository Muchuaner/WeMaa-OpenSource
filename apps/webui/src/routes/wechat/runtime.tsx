import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { Activity, CheckCircle2, Users, MessageSquare, Clock3 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Progress } from '@/components/ui/progress'
import { Page, Panel, Details, Empty, ErrorNotice, Evidence, Stamp, Status, Choice, useNative, useCommand } from './shared'
import type { MaaOverview, MaaScope } from './types'

const scopes = [{ value: 'personal', label: '微信好友' }, { value: 'enterprise', label: '企业联系人' }, { value: 'public', label: '公众号' }]
export function WechatRuntimePage() {
  const { data, error, refresh } = useNative<MaaOverview>('')
  const { run, busy } = useCommand(refresh)
  const [scope, setScope] = useState<MaaScope>('personal')
  const runtime = data?.runtime || {}
  const progress = runtime.progress || {}
  const guiBusy = ['running','starting','stopping'].includes((runtime.worker_state || '').toLowerCase()) || progress.status === 'running'
  const disabled = busy || !data || data.stale || !!error
  const guiDisabled = disabled || guiBusy
  const scans = [...(data?.scans || [])].sort((a,b) => (b.finished || b.started || 0) - (a.finished || a.started || 0))
  const latest = scans.find(s => s.scope === scope)
  const reuse = scans.find(s => s.scope === scope && s.status === 'complete' && s.coverage_valid && s.bottom_verified)
  const identity = data?.wemaa?.identity
  const stats = [
    { icon: Users, label: '已关联好友', value: identity ? `${identity.registered} / ${identity.identified}` : '—', detail: '同一好友沿用同一人物档案' },
    { icon: MessageSquare, label: '已观察会话', value: data?.summary?.conversation_count ?? '—', detail: '由官方客户端持续采集' },
    { icon: CheckCircle2, label: '已核验私聊路径', value: identity?.routable ?? '—', detail: '现场身份核验后可回复' },
    { icon: Clock3, label: '上下文快照', value: data?.summary?.snapshot_count?.toLocaleString() ?? '—', detail: '保留观察时间与截图证据' },
  ]
  return <Page title="微信运行" description="查看微信连接、采集进度与执行结果，管理 WeMaa 的官方客户端适配器。" refresh={refresh}>
    <ErrorNotice error={error} />
    <div className="flex flex-wrap items-center gap-3 rounded-lg border bg-card px-5 py-4"><Activity className="size-5 text-primary" /><span className="font-medium">{!data ? '正在连接微信遥测' : data.stale ? '微信遥测离线' : '微信遥测在线'}</span><Badge variant={data?.wemaa?.ready ? 'secondary' : 'outline'}>{data?.wemaa?.ready ? '核心已连接' : '核心待连接'}</Badge><Badge variant="outline">{runtime.auto_reply_enabled ? '自动回复' : '只读观察'}{runtime.continuous ? ' · 持续运行' : ''}</Badge><span className="ml-auto text-xs text-muted-foreground">最后更新 <Stamp value={data?.collected_at} /></span></div>
    {data?.wemaa?.error && <ErrorNotice error={data.wemaa.error} />}
    {progress.desktop_input && progress.desktop_input.ready === false && <ErrorNotice error={`Windows 输入桌面尚未就绪：${progress.desktop_input.reason || '等待布局核验'}。任务将在桌面就绪后继续。`} />}
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{stats.map(s => <Panel key={s.label} title={s.label}><div className="flex items-center justify-between"><span className="text-3xl font-semibold tabular-nums">{s.value}</span><s.icon className="size-5 text-muted-foreground" /></div><p className="mt-3 text-xs text-muted-foreground">{s.detail}</p></Panel>)}</div>
    <div className="grid gap-6 xl:grid-cols-2"><Panel title="任务控制" description="所有微信操作进入同一个执行队列。">
      <div className="space-y-5"><div className="flex flex-wrap gap-2"><Button disabled={guiDisabled || data?.reply_backend_enabled !== true} onClick={() => void run('start_watch', {cycles:1000, auto_reply:true, continuous:true})}>启动持续自动回复</Button><Button variant="outline" disabled={guiDisabled} onClick={() => void run('start_watch', {cycles:1000, auto_reply:false, continuous:false})}>只读轮询</Button><Button variant="outline" disabled={disabled || !guiBusy} onClick={() => void run('set_polling', {enabled: progress.polling_enabled === false})}>{progress.polling_enabled === false ? '开启轮询' : '暂停轮询'}</Button><Button variant="destructive" disabled={disabled} onClick={() => void run('stop_worker')}>停止任务</Button></div>
      <div className="space-y-3 border-t pt-4"><div className="text-sm font-medium">联系人采集</div><div className="flex flex-wrap gap-2"><Choice label="采集分区" value={scope} onChange={v => setScope(v as MaaScope)} options={scopes} /><Button variant="outline" disabled={guiDisabled} onClick={() => void run('scan_contacts', {scope})}>新扫描</Button><Button variant="outline" disabled={guiDisabled || !reuse} onClick={() => reuse && void run('scan_contacts', {scope, reuse_scan:reuse.id})}>复用资料扫描</Button><Button variant="outline" disabled={guiDisabled || latest?.status !== 'incomplete' || !latest.position_count} onClick={() => latest && void run('scan_contacts', {scope, scan_id:latest.id})}>继续未完成扫描</Button></div></div>
      <div className="flex flex-wrap gap-2 border-t pt-4">{([{ action:'capture', label:'当前截图' },{action:'open_contacts',label:'联系人页'},{action:'open_recent',label:'聊天页'},{action:'friend_count',label:'管理数量核验'}] as const).map(b => <Button key={b.action} variant="outline" disabled={guiDisabled} onClick={() => void run(b.action)}>{b.label}</Button>)}<Button variant="outline" disabled={disabled} onClick={() => void run('checkpoint')}>保存恢复检查点</Button></div>
      <Details title="桌面与任务状态" value={runtime} /></div>
    </Panel><Panel title="联系人采集覆盖" description="每个分区独立核验数量、资料与列表底部。"><div className="space-y-6">{scopes.map(s => {
      const scan = scans.find(r => r.scope === s.value && r.status === 'complete' && r.coverage_valid) || scans.find(r => r.scope === s.value)
      const count = scan?.position_count || scan?.observed || 0
      return <div key={s.value}><div className="mb-2 flex items-center justify-between text-sm"><span className="font-medium">{s.label}</span><span className="tabular-nums">{count} / {scan?.expected || '—'} <Status value={scan?.status} /></span></div><Progress value={scan?.expected ? Math.min(100,count / scan.expected * 100) : 0} /><p className="mt-2 text-xs text-muted-foreground">{scan?.bottom_verified && scan.coverage_valid ? '数量与底部已核验' : '等待完整覆盖核验'}</p></div>
    })}<div className="border-t pt-4"><Button variant="outline" asChild><Link to="/wechat/contacts">查看好友身份档案</Link></Button></div></div></Panel></div>
    <div className="grid gap-6 xl:grid-cols-2"><Panel title="执行回执">{!data?.commands?.length ? <Empty /> : <div className="divide-y">{data.commands.slice(0,10).map(c => <div key={c.id} className="space-y-2 py-3"><div className="flex flex-wrap items-center gap-2"><span className="font-mono text-xs">{c.action}</span><Status value={c.status} /><span className="ml-auto text-xs text-muted-foreground"><Stamp value={c.created_at} /></span></div><Details value={c.detail || c} title="执行详情" /></div>)}</div>}</Panel><Panel title="最新微信画面"><Evidence sha={runtime.capture_asset} alt="微信客户端截图" /><div className="mt-4 space-y-3"><Details title="恢复检查点" value={data?.backups} /><Details title="运行验收记录" value={data?.acceptance} /><Details title="已记录的处理规则" value={data?.lessons} /></div></Panel></div>
  </Page>
}
