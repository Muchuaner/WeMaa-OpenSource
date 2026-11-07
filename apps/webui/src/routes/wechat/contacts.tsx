import { useState } from 'react'
import { Link, useNavigate } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Page, Panel, Details, Empty, ErrorNotice, Evidence, Stamp, Choice, useNative, useCommand, waitForCommand } from './shared'
import type { MaaContact, MaaContactScope, MaaPage } from './types'

export function WechatContactsPage() {
  const [scope, setScope] = useState<MaaContactScope>('personal')
  const [keyword, setKeyword] = useState('')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<MaaContact>()
  const { data, error, refresh, loading } = useNative<MaaPage<MaaContact>>(`/contacts?scope=${scope}&keyword=${encodeURIComponent(keyword)}&page=${page}&page_size=30`, 15000)
  const command = useCommand(refresh)
  const navigate = useNavigate()
  const [opening, setOpening] = useState(false)
  const [openError, setOpenError] = useState('')
  const go = async (row: MaaContact) => {
    setOpening(true); setOpenError('')
    try {
      const queued = await command.run(row.scope === 'group' ? 'open_conversation' : 'open_contact',
        row.scope === 'group' ? {conversation_id:row.id} : {contact_id:row.id})
      if (!queued) return
      const result = await waitForCommand(queued)
      const detail = result.detail as {status?:string; conversation_id?:string} | undefined
      if (detail?.status !== 'opened' || !detail.conversation_id || !/^[a-f0-9]{64}$/.test(detail.conversation_id)) {
        throw new Error('未取得已核验的聊天路径，请查看执行回执。')
      }
      await navigate({to:'/wechat/conversations',search:{conversation:detail.conversation_id}})
    } catch(e) { setOpenError(e instanceof Error ? e.message : '打开聊天失败') }
    finally { setOpening(false) }
  }
  return <Page title="微信好友" description="微信号对应持久人物身份。昵称和备注变化不会创建新的好友档案。" refresh={refresh} actions={<Button variant="outline" asChild><Link to="/resource/person">人物档案</Link></Button>}>
    <ErrorNotice error={openError || error || (data?.stale ? '当前为历史快照，等待 Maa 上报后恢复操作。' : undefined)} />
    {opening && <p role="status" className="text-sm text-muted-foreground">正在等待 Maa 核验并打开聊天…</p>}
    <div className="flex flex-wrap gap-3"><Choice label="联系人分区" value={scope} onChange={v => {setScope(v as MaaContactScope);setPage(1)}} options={[{value:'personal',label:'微信好友'},{value:'group',label:'群聊'},{value:'enterprise',label:'企业联系人'},{value:'public',label:'公众号'}]} /><Input aria-label="搜索好友" className="max-w-sm" value={keyword} onChange={e => {setKeyword(e.target.value);setPage(1)}} placeholder="搜索昵称、备注或微信号" /></div>
    <Panel title={`${scope === 'group' ? '群聊' : '联系人'} · ${data?.total ?? '—'}`}><Table><TableHeader><TableRow><TableHead>联系人</TableHead><TableHead>{scope === 'group' ? '群人数' : '微信号'}</TableHead><TableHead>核心身份</TableHead><TableHead>最后观察</TableHead><TableHead className="text-right">操作</TableHead></TableRow></TableHeader><TableBody>{data?.items.map(r => <TableRow key={`${r.scan_id || ''}:${r.ordinal}:${r.id}`}><TableCell><div className="flex items-center gap-3"><Evidence sha={r.avatar_asset} avatar /><div className="min-w-28"><div className="font-medium">{r.display_name || r.list_name}</div>{r.list_name !== r.display_name && <div className="text-xs text-muted-foreground">{r.list_name}</div>}</div></div></TableCell><TableCell className="font-mono text-xs">{scope === 'group' ? r.member_count ?? '—' : r.wechat_id || '待核验'}</TableCell><TableCell>{r.core_identity?.person_id ? <Badge variant="secondary">已关联人物档案</Badge> : <Badge variant="outline">{scope === 'personal' ? '待关联' : '独立会话'}</Badge>}</TableCell><TableCell className="whitespace-nowrap text-xs text-muted-foreground"><Stamp value={r.last_seen} /></TableCell><TableCell><div className="flex justify-end gap-2"><Button size="sm" variant="ghost" onClick={() => setSelected(r)}>资料</Button><Button size="sm" variant="outline" disabled={opening || command.busy || !data || data.stale || !!error || (r.scope !== 'group' && !r.wechat_id)} onClick={() => void go(r)}>前往聊天</Button></div></TableCell></TableRow>)}</TableBody></Table>{!data?.items.length && <Empty>{loading ? '正在加载好友…' : '没有符合条件的联系人'}</Empty>}<div className="mt-4 flex items-center justify-between"><span className="text-sm text-muted-foreground">第 {page} 页 · 共 {data?.total || 0} 条</span><div className="flex gap-2"><Button size="sm" variant="outline" disabled={page === 1} onClick={() => setPage(p => p-1)}>上一页</Button><Button size="sm" variant="outline" disabled={!data || page*30 >= data.total} onClick={() => setPage(p => p+1)}>下一页</Button></div></div></Panel>
    <Dialog open={!!selected} onOpenChange={v => !v && setSelected(undefined)}><DialogContent className="max-h-[90vh] overflow-auto sm:max-w-2xl"><DialogHeader><DialogTitle>{selected?.display_name || selected?.list_name || '联系人资料'}</DialogTitle><DialogDescription>以资料页微信号关联人物，保留可回查的识别证据。</DialogDescription></DialogHeader><div className="space-y-4"><p className="text-sm">微信号：<span className="font-mono">{selected?.wechat_id || '—'}</span></p><p className="break-all text-xs text-muted-foreground">人物标识：{selected?.core_identity?.person_id || '尚未注册'}</p><Evidence sha={selected?.profile_asset} alt="好友资料页" /><Details value={selected} title="身份与采集记录" /></div></DialogContent></Dialog>
  </Page>
}
