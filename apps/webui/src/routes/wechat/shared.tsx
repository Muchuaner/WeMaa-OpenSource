import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { RefreshCw, UserRound } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useToast } from '@/hooks/use-toast'
import type { MaaCommand, MaaCommandAction, MaaFacts } from './types'

export const BASE = '/api/wemaa'
class ApiError extends Error {
  readonly status: number
  constructor(message: string, status: number) { super(message); this.status = status }
}
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(BASE + path, { credentials: 'include', ...init,
    headers: { ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...init?.headers } })
  const result = await response.json()
  if (!response.ok || result.code !== 0) throw new ApiError(result.message || result.detail?.message || result.detail || `请求失败 (${response.status})`, response.status)
  return result.data as T
}
export const post = <T,>(path: string, body: unknown) => api<T>(path, { method: 'POST', body: JSON.stringify(body) })

export async function waitForCommand(command: MaaCommand): Promise<MaaCommand> {
  const deadline = Math.min(Date.now() + 120_000, (command.expires_at || Date.now() / 1000 + 120) * 1000 + 5000)
  while (Date.now() < deadline) {
    const result = await api<MaaCommand>(`/commands/${command.id}`)
    if (result.status === 'succeeded') return result
    if (['failed', 'expired', 'uncertain'].includes(result.status)) {
      const detail = result.detail as {reason?: string; error?: string} | undefined
      throw new Error(detail?.reason || detail?.error || 'Windows 未确认操作成功，请查看执行回执。')
    }
    await new Promise(resolve => setTimeout(resolve, 1500))
  }
  throw new Error('尚未取得 Windows 终态回执，请查看执行记录；不会自动重发。')
}

export function useNative<T>(path: string | null, interval = 5000) {
  const [data, setData] = useState<T>()
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  const refresh = useCallback(() => setRevision(v => v + 1), [])
  useEffect(() => {
    if (path === null) { setData(undefined); setLoading(false); return }
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    setLoading(true)
    setData(undefined)
    const load = async () => {
      try {
        const value = await api<T>(path, { signal: controller.signal })
        if (active) { setData(value); setError('') }
      } catch (e) { if (active) setError(e instanceof Error ? e.message : '加载失败') }
      finally { if (active) { setLoading(false); if (interval) timer = setTimeout(load, interval) } }
    }
    void load()
    return () => { active = false; clearTimeout(timer); controller.abort() }
  }, [path, revision, interval])
  return { data, error, loading, refresh }
}

export function Stamp({ value }: { value?: number | null }) {
  return <span>{value ? new Date(value * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'}</span>
}
const states: Record<string, string> = { queued:'排队中', issued:'执行核验中', succeeded:'执行完成', sent:'已核验发送',
  ready:'待发送', failed:'失败', uncertain:'结果不确定', expired:'已过期', pending:'待发送', sending:'发送中',
  dispatched:'已提交', complete:'已完成', incomplete:'未完成', cancelled:'已取消', no_reply:'已处理',
  generating:'处理中', running:'运行中', unchanged:'无新增', stable:'已稳定', initial:'首次观察', append:'新增消息', gap:'上下文有缺口',
  profile_observed:'已建立身份', ocr_wechat_id:'微信号已识别', skipped:'已停止' }
export function Status({ value }: { value?: string }) {
  return <Badge variant={['failed','uncertain','expired','gap'].includes(value || '') ? 'destructive' : ['sent','succeeded','complete'].includes(value || '') ? 'default' : 'secondary'}>{states[value || ''] || value || '—'}</Badge>
}
export function Page({ title, description, children, refresh, actions }: { title: string; description: string; children: ReactNode; refresh?: () => void; actions?: ReactNode }) {
  return <div className="space-y-6 p-4 sm:p-6"><div className="flex flex-wrap items-start justify-between gap-4"><div><h1 className="text-2xl font-semibold tracking-tight">{title}</h1><p className="mt-2 text-sm text-muted-foreground">{description}</p></div><div className="flex flex-wrap gap-2">{actions}{refresh && <Button variant="outline" onClick={refresh}><RefreshCw className="mr-2 size-4" />刷新</Button>}</div></div>{children}</div>
}
export function Panel({ title, description, children, className = '' }: { title: string; description?: string; children: ReactNode; className?: string }) {
  return <Card className={className}><CardHeader><CardTitle>{title}</CardTitle>{description && <CardDescription>{description}</CardDescription>}</CardHeader><CardContent>{children}</CardContent></Card>
}
export function ErrorNotice({ error }: { error?: string }) {
  return error ? <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">{error}</div> : null
}
export function Empty({ children = '暂无记录' }: { children?: ReactNode }) { return <p className="py-10 text-center text-sm text-muted-foreground">{children}</p> }
export function Evidence({ sha, avatar = false, alt = '微信现场证据' }: { sha?: string; avatar?: boolean; alt?: string }) {
  const [open, setOpen] = useState(false)
  if (!sha || !/^[a-f0-9]{64}$/.test(sha)) return avatar ? <div className="grid size-10 shrink-0 place-items-center rounded-lg bg-muted"><UserRound className="size-5 text-muted-foreground" /></div> : <Empty>暂无截图证据</Empty>
  const src = `${BASE}/assets/${sha}`
  if (avatar) return <img src={src} alt={alt} loading="lazy" className="size-10 shrink-0 rounded-lg object-cover" />
  return <><button type="button" className={avatar ? 'shrink-0' : 'block max-w-full'} onClick={() => setOpen(true)} aria-label={`放大${alt}`}><img src={src} alt={alt} loading="lazy" className={avatar ? 'size-10 rounded-lg object-cover' : 'max-h-96 max-w-full rounded-md border object-contain'} /></button><Dialog open={open} onOpenChange={setOpen}><DialogContent className="max-w-5xl"><DialogHeader><DialogTitle>{alt}</DialogTitle><DialogDescription>来自当前微信官方客户端的已记录画面。</DialogDescription></DialogHeader><img src={src} alt={alt} className="max-h-[75vh] w-full object-contain" /></DialogContent></Dialog></>
}
export function Details({ value, title = '查看详情' }: { value: unknown; title?: string }) {
  return <details className="text-sm"><summary className="cursor-pointer text-muted-foreground">{title}</summary><pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 text-xs">{JSON.stringify(value, null, 2)}</pre></details>
}
export function Choice({ value, onChange, options, label, disabled }: { value: string; onChange: (v: string) => void; options: {value: string; label: string}[]; label: string; disabled?: boolean }) {
  return <select aria-label={label} value={value} onChange={e => onChange(e.target.value)} disabled={disabled} className="h-10 min-w-0 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}</select>
}
export function useCommand(refresh: () => void) {
  const { toast } = useToast()
  const [busy, setBusy] = useState(false)
  const [receipt, setReceipt] = useState<MaaCommand>()
  // 网络结果未知时保留相同请求编号；同一操作的重试只读取同一条指令。
  const intent = useRef<{ fingerprint: string; id: string } | null>(null)
  const run = async (action: MaaCommandAction, params: MaaFacts = {}) => {
    if (busy) return
    const fingerprint = JSON.stringify({ action, params })
    if (intent.current && intent.current.fingerprint !== fingerprint) {
      toast({ title: '上一条操作的结果未知', description: '请先刷新并核对指令记录。', variant: 'destructive' }); return
    }
    intent.current ??= { fingerprint, id: crypto.randomUUID() }
    setBusy(true)
    try {
      const result = await post<MaaCommand>('/commands', { action, params, request_id: intent.current.id })
      setReceipt(result)
      intent.current = null
      toast({ title: '指令已提交', description: '执行结果以回执为准。' })
      refresh()
      return result
    } catch (e) {
      const rejected = e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 408
      if (rejected) intent.current = null
      toast({ title: rejected ? '操作未提交' : '未能确认提交结果', description: e instanceof Error ? e.message : '请刷新核对记录', variant: 'destructive' })
    }
    finally { setBusy(false) }
  }
  return { run, busy, receipt }
}
