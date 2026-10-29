/**
 * `/settings/watchdog` —— 「页面监控」Tab 的路由。
 *
 * 这里**只做占位**：整个设置页的外壳（标题 + 二级导航 + 当前 Tab 的内容）挂在同级
 * `../layout.tsx` 上。内容是 `WatchdogPanel`，由外壳按路径渲染；这个 page 只负责
 * 让地址可收藏、可分享、可后退。
 *
 * `server/tests/test_settings_tabs.py` 钉住这条结构：每个 Tab 都要有真实路由目录、
 * 每个 `page.tsx` 都返回 null、取数只能发生在 `layout.tsx` 里。
 */
export default function SettingsWatchdogRoute() {
  return null;
}
