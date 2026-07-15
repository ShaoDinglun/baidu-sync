import { chromium } from 'playwright'

const baseUrl = (process.env.SMOKE_BASE_URL || 'http://127.0.0.1:3001').replace(/\/$/, '')
const browser = await chromium.launch({ headless: true })
const page = await browser.newPage()
const runtimeErrors = []

page.on('console', (message) => {
  if (message.type() === 'error') {
    runtimeErrors.push(`console: ${message.text()}`)
  }
})
page.on('pageerror', (error) => {
  runtimeErrors.push(`pageerror: ${error.message}`)
})
page.on('requestfailed', (request) => {
  runtimeErrors.push(`requestfailed: ${request.url()} (${request.failure()?.errorText || 'unknown'})`)
})

try {
  const response = await page.goto(`${baseUrl}/login`, {
    waitUntil: 'networkidle',
    timeout: 30_000,
  })

  if (!response?.ok()) {
    throw new Error(`登录页响应异常: HTTP ${response?.status() ?? 'unknown'}`)
  }

  await page.waitForFunction(
    () => (document.querySelector('#app')?.children.length || 0) > 0,
    undefined,
    { timeout: 10_000 },
  )

  if (runtimeErrors.length > 0) {
    throw new Error(`浏览器运行时错误:\n${runtimeErrors.join('\n')}`)
  }

  console.log(`登录页 smoke test 通过: ${baseUrl}/login`)
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error))
  if (runtimeErrors.length > 0) {
    console.error(runtimeErrors.join('\n'))
  }
  process.exitCode = 1
} finally {
  await browser.close()
}
