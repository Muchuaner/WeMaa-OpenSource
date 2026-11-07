import { EmbedPageShell } from '@/components/embed-page-shell'

import { PluginMirrorsPage } from './plugin-mirrors'

export function PluginMirrorsEmbedPage() {
  return (
    <EmbedPageShell shellId="embed-plugin-mirrors" title="插件商店设置 - WeMaa">
      <PluginMirrorsPage embedded />
    </EmbedPageShell>
  )
}
