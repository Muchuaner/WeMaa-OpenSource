import { EmbedPageShell } from '@/components/embed-page-shell'

import { PluginMarketplacePage } from './PluginMarketplacePage'

export function PluginMarketplaceEmbedPage() {
  return (
    <EmbedPageShell shellId="embed-plugin-marketplace" title="插件市场 - WeMaa">
      <PluginMarketplacePage embedded />
    </EmbedPageShell>
  )
}
