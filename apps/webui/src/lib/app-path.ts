/** 路由以部署子路径为基准，布局只处理应用内的路径。 */
export function appPath(pathname: string): string {
  const base = import.meta.env.BASE_URL.replace(/\/$/, '')
  if (base && (pathname === base || pathname.startsWith(base + '/'))) return pathname.slice(base.length) || '/'
  return pathname
}
