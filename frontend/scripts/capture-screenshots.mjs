import fs from 'node:fs/promises'
import path from 'node:path'
import { chromium } from 'playwright'

const baseUrl = process.env.SCREENSHOT_BASE_URL || 'http://127.0.0.1:3001'
const username = process.env.SCREENSHOT_USERNAME
const password = process.env.SCREENSHOT_PASSWORD
const outputDir = process.env.SCREENSHOT_OUTPUT_DIR || path.resolve(process.cwd(), '../docs/screenshots')

const pages = [
  {
    name: 'dashboard',
    route: '/dashboard',
    waitFor: { role: 'heading', name: '仪表盘' },
  },
  {
    name: 'tasks',
    route: '/tasks',
    waitFor: { role: 'heading', name: '任务管理' },
  },
  {
    name: 'users',
    route: '/users',
    waitFor: { role: 'heading', name: '用户管理' },
  },
  {
    name: 'settings',
    route: '/settings',
    waitFor: { role: 'heading', name: '系统设置' },
  },
]

async function ensureOutputDir() {
  await fs.mkdir(outputDir, { recursive: true })
}

async function capturePage(page, route, fileName, waitFor) {
  await page.goto(new URL(route, baseUrl).toString(), { waitUntil: 'domcontentloaded' })
  await page.getByRole(waitFor.role, { name: waitFor.name }).waitFor({ timeout: 15000 })
  await page.waitForTimeout(1200)
  await page.screenshot({
    path: path.join(outputDir, `${fileName}.png`),
    fullPage: true,
  })
}

async function main() {
  await ensureOutputDir()

  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1080 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()

  try {
    await page.goto(new URL('/login', baseUrl).toString(), { waitUntil: 'domcontentloaded' })
    await page.getByRole('heading', { name: '百度网盘自动转存工具' }).waitFor({ timeout: 15000 })
    await page.screenshot({
      path: path.join(outputDir, 'login.png'),
      fullPage: true,
    })

    if (!username || !password) {
      throw new Error('Please set SCREENSHOT_USERNAME and SCREENSHOT_PASSWORD before capturing authenticated pages.')
    }

    await page.locator('input[name="username"]').fill(username)
    await page.locator('input[name="current-password"]').fill(password)
    await page.getByRole('button', { name: '登录' }).click()

    await page.waitForURL(/\/dashboard$/, { timeout: 15000 })
    await page.getByRole('heading', { name: '仪表盘' }).waitFor({ timeout: 15000 })
    await page.waitForTimeout(1200)
    await page.screenshot({
      path: path.join(outputDir, 'dashboard.png'),
      fullPage: true,
    })

    for (const item of pages.slice(1)) {
      await capturePage(page, item.route, item.name, item.waitFor)
    }

    console.log(`Screenshots saved to ${outputDir}`)
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error('Failed to capture screenshots:', error)
  process.exitCode = 1
})