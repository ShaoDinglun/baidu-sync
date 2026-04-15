# 百度同步 - 前端

基于 Vue 3 + TypeScript + Element Plus 的前端管理中台。

## 🚀 快速开始

### 环境要求

- Node.js >= 16.0.0
- npm >= 8.0.0 (或 yarn >= 1.22.0)

### 安装依赖

```bash
cd frontend
npm install
```

### 开发模式

需要按顺序启动后端和前端。

#### 1. 启动后端

```bash
cd /path/to/baidu-sync
python -m backend.web_app
```

后端默认运行在 `http://localhost:5000`。

#### 2. 启动前端

```bash
cd /path/to/baidu-sync/frontend
npm run dev
```

也可以使用快捷脚本：

```bash
# Windows
start.bat

# Linux/Mac
./start.sh
```

启动后访问 `http://localhost:3001`，API 请求会自动代理到后端 `5000` 端口。

### 生产构建

```bash
npm run build
```

构建文件输出到 `dist` 目录。

## 📁 项目结构

```text
frontend/
├── src/
│   ├── components/
│   │   ├── layout/                  # 布局组件
│   │   └── business/                # 业务组件
│   │       ├── AddTaskDialog.vue    # 订阅任务对话框
│   │       ├── TaskRunnerDialog.vue # 订阅任务执行与日志
│   │       └── LocalSyncTaskManager.vue # 本地同步任务面板
│   ├── views/
│   │   ├── dashboard/
│   │   ├── login/
│   │   ├── settings/
│   │   ├── tasks/                   # 任务管理页面
│   │   └── users/
│   ├── stores/                      # Pinia 状态管理
│   ├── services/                    # API 服务与轮询
│   ├── composables/
│   ├── router/
│   ├── types/
│   ├── utils/
│   ├── App.vue
│   └── main.ts
├── public/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── start.sh
└── start.bat
```

## 🛠️ 技术栈

- Vue 3
- TypeScript
- Vite
- Element Plus
- Pinia
- Vue Router 4

## 📋 当前实现

- 用户认证和登录。
- 订阅任务管理：新增、编辑、删除、执行、批量操作。
- 本地同步任务管理：新增、编辑、删除、单任务执行、日志查看。
- 任务管理页面双面板：订阅同步任务 / 本地同步任务。
- 用户管理、设置管理、版本检查。
- 基于轮询的任务状态更新。

## 当前边界

- 前端当前只消费 HTTP 接口与轮询接口，不依赖 WebSocket。
- “任务管理”页面分为两套独立面板：订阅同步任务和本地同步任务。
- 本地同步面板只负责百度网盘到本地目录的任务配置、执行与日志展示，不直接访问分享链接。

### API兼容性

- ✅ 完全兼容现有后端API
- ✅ 无需修改后端代码
- ✅ 保持数据格式一致
- ✅ 支持所有现有功能

## ⚙️ 配置说明

### 版本管理

版本信息在 `src/config/version.ts` 中管理：

```typescript
export const VERSION_CONFIG = {
  APP_VERSION: 'v1.1.3',
  BUILD_TIME: '2024-09-15T20:00:00Z',
  RELEASE_NOTES: '前端重构版本 - Vue 3 + TypeScript'
} as const
```

### API代理配置

开发环境API代理配置在 `vite.config.ts` 中：

```typescript
server: {
  port: 3001,
  proxy: {
    '/api': 'http://localhost:5000',
    '/login': 'http://localhost:5000'
  }
}
```

### 环境变量

可以在项目根目录创建以下环境变量文件：

- `.env.development` - 开发环境
- `.env.production` - 生产环境

## 🔧 开发指南

### 代码规范

- 使用 TypeScript 严格模式
- 遵循 Vue 3 Composition API 最佳实践
- 使用 ESLint + Prettier 进行代码格式化
- 组件名使用 PascalCase
- 文件名使用 kebab-case

### 状态管理

使用 Pinia 进行状态管理，按功能模块分离：

```typescript
// stores/tasks.ts
export const useTaskStore = defineStore('tasks', () => {
  const tasks = ref<Task[]>([])
  // ...
})
```

### API调用

统一使用服务层进行API调用：

```typescript
// services/api.ts
export class ApiService {
  async getTasks(): Promise<ApiResponse<{ tasks: Task[] }>> {
    return this.http.get('/api/tasks')
  }
}
```

### 组合式函数

将业务逻辑封装为可复用的组合式函数：

```typescript
// composables/useTasks.ts
export function useTasks() {
  const taskStore = useTaskStore()
  // 业务逻辑
  return { /* 导出的状态和方法 */ }
}
```

## 📱 响应式设计

项目采用响应式设计，支持多种设备：

- **移动端**: < 768px
- **平板端**: 768px - 1024px  
- **桌面端**: > 1024px

使用 CSS 媒体查询和组件级响应式样式适配不同设备宽度。

## 🚦 部署说明

### 开发环境部署

1. 启动后端服务（端口5000）
2. 启动前端开发服务器：`npm run dev`
3. 访问 http://localhost:3001

### 生产环境部署

1. 构建前端资源：`npm run build`
2. 构建文件将输出到 `dist` 目录
3. 配置后端服务器指向新的静态文件目录
4. 重启后端服务

## 🔍 调试指南

### Chrome DevTools

- Vue DevTools：查看组件状态和 Pinia store
- Network 面板：检查API请求
- Console 面板：查看日志和错误

### 常见问题

1. **API请求失败**：检查后端服务是否启动
2. **轮询不工作**：检查网络连接和API响应
3. **页面空白**：查看浏览器控制台错误信息

## 📖 更多文档

- [Vue 3 官方文档](https://vuejs.org/)
- [TypeScript 官方文档](https://www.typescriptlang.org/)
- [Element Plus 官方文档](https://element-plus.org/)
- [Pinia 官方文档](https://pinia.vuejs.org/)

## 🤝 贡献指南

1. Fork 项目
2. 创建功能分支：`git checkout -b feature/new-feature`
3. 提交更改：`git commit -m 'Add some feature'`
4. 推送分支：`git push origin feature/new-feature`
5. 提交 Pull Request

## 📄 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](../LICENSE) 文件了解详情。
