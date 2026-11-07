import { Link } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Page, Panel, Details, ErrorNotice, useNative } from './shared'

export function WebsitePluginPage() {
  const {data,error,refresh} = useNative<{enabled:boolean;configured:boolean;connected:boolean;name:string;capabilities:string[];status?:string}>('/plugins/website',15000)
  return <Page title="网站 Agent" description="通过插件接入创作平台 AI 的网站会话、账户绑定和图片视频创作。" refresh={refresh} actions={<Button variant="outline" asChild><Link to="/plugin-config">全部插件</Link></Button>}><ErrorNotice error={error} /><Panel title="创作平台 AI"><div className="space-y-5"><Badge variant={data?.configured ? 'secondary' : 'outline'}>{data?.connected ? '消息网关在线' : data?.configured ? '已配置 · 等待连接' : '插件未配置'}</Badge><p className="text-sm text-muted-foreground">网站能力通过插件接入 WeMaa。微信收发、好友档案、定时任务与 API 由 WeMaa 管理，网站插件负责网站会话和创作业务。</p><div className="flex flex-wrap gap-2">{data?.capabilities.map(c => <Badge variant="outline" key={c}>{c}</Badge>)}</div><Details title="插件连接状态" value={data} /></div></Panel></Page>
}
