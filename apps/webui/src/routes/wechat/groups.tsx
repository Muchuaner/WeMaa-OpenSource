import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import { Badge } from '@/components/ui/badge'
import { Page, Panel, Empty, ErrorNotice, Evidence, Stamp, useNative, useCommand, waitForCommand } from './shared'
import type { MaaContact, MaaOverview, MaaPage } from './types'

export function WechatGroupsPage() {
  const overview = useNative<MaaOverview>('')
  const contacts = useNative<MaaPage<MaaContact>>('/contacts?scope=group&page_size=100',15000)
  type Policy = NonNullable<NonNullable<MaaOverview['runtime']>['group_whitelist']>
  const [applied, setApplied] = useState<Policy>()
  const remote = overview.data?.runtime?.group_whitelist
  const policy = applied && (applied.updated_at || 0) >= (remote?.updated_at || 0) ? applied : remote
  const [draft, setDraft] = useState<{ids:string[]; revision:string}>()
  const [search, setSearch] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const ids = draft?.ids || policy?.groups.map(g => g.conversation_id) || []
  const {busy, run} = useCommand(overview.refresh)
  const groups = new Map((contacts.data?.items || []).map(g => [g.id,g]))
  for (const g of policy?.groups || []) if (!groups.has(g.conversation_id)) groups.set(g.conversation_id,{id:g.conversation_id,scope:'group',display_name:g.name,avatar_asset:g.avatar_sha})
  const stale = !!draft && draft.revision !== policy?.revision
  const save = async () => {
    if (!draft) return
    setSaving(true); setError(''); setNotice('正在保存，等待 Windows 确认…')
    try {
      const queued = await run('set_group_whitelist',{conversation_ids:draft.ids,expected_revision:draft.revision})
      if (!queued) { setNotice(''); return }
      const result = await waitForCommand(queued)
      const next = (result.detail as {group_whitelist?:Policy} | undefined)?.group_whitelist
      if (!next?.revision || !Array.isArray(next.groups)) throw new Error('未取得 Windows 生效配置，请核对执行记录。')
      setApplied(next); setDraft(undefined); setNotice(`已在 Windows 生效 · ${next.groups.length} 个群聊`)
      overview.refresh()
    } catch(e) { setError(e instanceof Error ? e.message : '保存失败'); setNotice('') }
    finally { setSaving(false) }
  }
  return <Page title="群聊白名单" description="按已核验的群聊身份管理自动回复、创作与发送权限。" refresh={() => {overview.refresh();contacts.refresh()}}><ErrorNotice error={error || overview.error || contacts.error || (stale ? '配置已发生变化，请重新载入后再编辑。' : '')} />
    <Panel title={`已生效 · ${policy?.groups.length || 0} 个群聊`} description="保存后等待 Windows 同步生效。空名单会停止所有群聊的回复与发送。"><div className="mb-5 flex flex-wrap gap-2">{policy?.groups.map(g => <Badge key={g.conversation_id} variant="secondary">{g.name}</Badge>)}{!policy && <Empty>等待 Windows 白名单配置</Empty>}</div><Input aria-label="搜索群聊" placeholder="搜索已识别的群聊" value={search} onChange={e => setSearch(e.target.value)} /><div className="divide-y">{[...groups.values()].filter(g => (g.display_name || g.list_name || '').toLowerCase().includes(search.toLowerCase())).map(g => <label key={g.id} className="flex cursor-pointer items-center gap-3 py-4"><Checkbox aria-label={`允许${g.display_name || g.list_name}`} checked={ids.includes(g.id)} disabled={!policy || busy || saving || overview.data?.stale || !!overview.error} onCheckedChange={v => {if(policy){setNotice('');setDraft({revision:draft?.revision || policy.revision,ids:v ? [...ids,g.id] : ids.filter(x => x !== g.id)})}}} /><Evidence sha={g.avatar_asset} avatar /><div><p className="text-sm font-medium">{g.display_name || g.list_name}</p><p className="mt-1 text-xs text-muted-foreground">{g.member_count ? `${g.member_count} 人 · ` : ''}已绑定群聊身份</p></div></label>)}</div><div className="mt-5 flex flex-wrap items-center gap-3"><Button disabled={!draft || stale || busy || saving || overview.data?.stale || !!overview.error} onClick={() => void save()}>保存白名单</Button><Button variant="outline" disabled={saving} onClick={() => {setDraft(undefined);setNotice('');setError('');overview.refresh()}}>重新载入</Button><span role="status" className="text-sm text-muted-foreground">{notice || (draft ? '有未保存的修改' : '')}</span><span className="text-xs text-muted-foreground">配置更新于 <Stamp value={policy?.updated_at} /></span></div></Panel>
  </Page>
}
