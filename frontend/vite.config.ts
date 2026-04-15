import { readFileSync } from 'fs'
import type { IncomingMessage } from 'http'
import { defineConfig, type ProxyOptions } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

const packageJson = JSON.parse(
  readFileSync(resolve(__dirname, 'package.json'), 'utf-8'),
) as { version: string }
const appVersion = `v${packageJson.version}`
const buildTime = new Date().toISOString()
const backendTarget = process.env.VITE_BACKEND_TARGET || 'http://localhost:5000'
const forwardedProto = (() => {
  try {
    return new URL(backendTarget).protocol.replace(':', '') || 'http'
  } catch {
    return 'http'
  }
})()

const createProxyConfig = (withBypass = false): ProxyOptions => ({
  target: backendTarget,
  changeOrigin: false,
  secure: false,
  xfwd: true,
  cookieDomainRewrite: false,
  cookiePathRewrite: false,
  configure: (proxy: any) => {
    proxy.on('proxyReq', (proxyReq: any, req: IncomingMessage) => {
      // 使用当前访问地址透传 Host，兼容本机 IP 调试。
      const host = req.headers.host || 'localhost:3001'
      proxyReq.setHeader('X-Forwarded-Host', host)
      proxyReq.setHeader('X-Forwarded-Proto', forwardedProto)
    })
  },
  ...(withBypass
    ? {
        bypass: (req: IncomingMessage) => {
          // 只代理表单提交，页面路由继续交给前端处理。
          if (req.method !== 'POST') {
            return '/index.html'
          }
        },
      }
    : {}),
})

const getElementChunkName = (id: string) => {
  if (id.includes('/node_modules/@element-plus/icons-vue/')) {
    return 'element-icons'
  }

  if (!id.includes('/node_modules/element-plus/')) {
    return null
  }

  if (/(form|input|input-number|select|option|checkbox|radio|switch|cascader|upload|autocomplete)/.test(id)) {
    return 'element-form'
  }

  if (/(table|tag|card|descriptions|empty|skeleton|image|badge|avatar|progress|result)/.test(id)) {
    return 'element-data'
  }

  if (/(dialog|drawer|message|message-box|notification|loading|popover|tooltip|tour|overlay)/.test(id)) {
    return 'element-feedback'
  }

  if (/(menu|tabs|breadcrumb|dropdown|pagination|steps|segmented)/.test(id)) {
    return 'element-navigation'
  }

  return 'element-base'
}

export default defineConfig({
  base: './',
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
    __BUILD_TIME__: JSON.stringify(buildTime),
  },
  plugins: [
    vue(),
    AutoImport({
      resolvers: [ElementPlusResolver()],
      imports: ['vue', 'vue-router', 'pinia'],
      dts: true,
    }),
    Components({
      resolvers: [ElementPlusResolver()],
    }),
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    host: '0.0.0.0', // 允许外部网络访问（包括手机）
    port: 3001,
    open: false,
    proxy: {
      '/api': {
        ...createProxyConfig(),
        ws: true,
      },
      '/login': createProxyConfig(true),
      '/logout': createProxyConfig(true),
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          const normalizedId = id.replace(/\\/g, '/')

          if (normalizedId.includes('/node_modules/vue/') || normalizedId.includes('/node_modules/vue-router/') || normalizedId.includes('/node_modules/pinia/')) {
            return 'vendor-core'
          }

          if (normalizedId.includes('/node_modules/sortablejs/')) {
            return 'vendor-sortable'
          }

          const elementChunkName = getElementChunkName(normalizedId)
          if (elementChunkName) {
            return elementChunkName
          }

          if (normalizedId.includes('/node_modules/')) {
            return 'vendor-misc'
          }
        },
      },
    },
  },
})
