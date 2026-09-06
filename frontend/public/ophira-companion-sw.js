/* Standard Web Push only. No fetch handler, cached project data or API authority. */
const mobilePath = '/app/mobile'
const mobileUrl = new URL(mobilePath, self.location.origin).href

self.addEventListener('install', (event) => {
  event.waitUntil(self.skipWaiting())
})

self.addEventListener('push', (event) => {
  // Intentionally do not parse payloads: neither server data nor notification
  // actions can supply a URL, approval, prompt, secret or business content.
  event.waitUntil(self.registration.showNotification('TradeMind', {
    body: 'There is an update. Open your trusted companion to review it.',
    tag: 'ophira-companion-update',
    icon: '/trademind-mark.svg',
  }))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    // Do not focus an old invitation URL, query command or another application.
    const existing = windows.find((client) => client.url === mobileUrl)
    if (existing) {
      try { await existing.focus(); return } catch { /* A window can close mid-click. */ }
    }
    await self.clients.openWindow(mobileUrl)
  })())
})

self.addEventListener('pushsubscriptionchange', (event) => {
  // Browser support is incomplete; the page also reconciles on focus/poll.
  // Renewal stays behind an explicit click instead of silently subscribing.
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    for (const client of windows) {
      if (client.url === mobileUrl) client.postMessage({ type: 'ophira-companion-subscription-changed' })
    }
  })())
})
