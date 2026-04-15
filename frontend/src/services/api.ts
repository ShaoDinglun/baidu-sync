// API 服务层
import { httpClient } from './http'
import type { 
  Task, User, Config,
  CreateTaskRequest, UpdateTaskRequest,
  CreateUserRequest, UpdateUserRequest,
  ApiResponse,
  LocalSyncOverview,
  LocalSyncTaskConfig,
  LocalSyncDirectoryOption
} from '@/types'

type TaskQueryRef = number | {
  taskId?: number
  taskUid?: string
  taskOrder?: number
}

export class ApiService {
  private normalizeTaskQuery(taskRef: TaskQueryRef): { taskId: number; params?: { task_uid?: string; task_order?: number } } {
    if (typeof taskRef === 'number') {
      return { taskId: taskRef }
    }

    const taskId = taskRef.taskId ?? (typeof taskRef.taskOrder === 'number' ? Math.max(taskRef.taskOrder - 1, 0) : 0)

    return {
      taskId,
      params: {
        task_uid: taskRef.taskUid,
        task_order: taskRef.taskOrder,
      }
    }
  }

  // 任务相关API
  async getTasks(): Promise<ApiResponse<{ tasks: Task[] }>> {
    return httpClient.get('/api/tasks')
  }

  async createTask(data: CreateTaskRequest): Promise<ApiResponse<Task>> {
    return httpClient.post('/api/task/add', data)
  }

  async updateTask(taskId: number, data: UpdateTaskRequest): Promise<ApiResponse<Task>> {
    return httpClient.post('/api/task/update', { task_id: taskId, ...data })
  }

  async deleteTask(taskId: number): Promise<ApiResponse<void>> {
    return httpClient.post('/api/task/delete', { task_id: taskId })
  }

  async executeTask(taskId: number): Promise<ApiResponse<any>> {
    return httpClient.post('/api/task/execute', { task_id: taskId })
  }

  async executeBatchTasks(taskIds: number[]): Promise<ApiResponse<any>> {
    return httpClient.post('/api/tasks/execute-all', { task_ids: taskIds })
  }

  async cancelTask(taskOrder: number, taskUid?: string): Promise<ApiResponse<any>> {
    return httpClient.post('/api/task/cancel', { task_order: taskOrder, task_uid: taskUid })
  }

  async deleteBatchTasks(taskIds: number[]): Promise<ApiResponse<void>> {
    return httpClient.post('/api/tasks/batch-delete', { task_ids: taskIds })
  }

  async shareTask(taskId: number, options?: { password?: string, period?: number }): Promise<ApiResponse<any>> {
    return httpClient.post('/api/task/share', { task_id: taskId, ...options })
  }

  async getShareInfo(url: string, pwd?: string): Promise<ApiResponse<any>> {
    return httpClient.post('/api/share/info', { url, pwd })
  }

  // 移除parseShareUrl，使用现有的getShareInfo接口获取文件名

  async moveTask(taskId: number, newIndex: number): Promise<ApiResponse<void>> {
    return httpClient.post('/api/task/move', { task_id: taskId, new_index: newIndex })
  }

  // 用户相关API
  async getUsers(): Promise<ApiResponse<{ users: User[], current_user: string }>> {
    return httpClient.get('/api/users')
  }

  async createUser(data: CreateUserRequest): Promise<ApiResponse<User>> {
    return httpClient.post('/api/user/add', data)
  }

  async updateUser(data: UpdateUserRequest): Promise<ApiResponse<User>> {
    return httpClient.post('/api/user/update', data)
  }

  async switchUser(username: string): Promise<ApiResponse<any>> {
    return httpClient.post('/api/user/switch', { username })
  }

  async deleteUser(username: string): Promise<ApiResponse<void>> {
    return httpClient.post('/api/user/delete', { username })
  }

  async getUserQuota(): Promise<ApiResponse<any>> {
    return httpClient.get('/api/user/quota')
  }

  async getUserCookies(username: string): Promise<ApiResponse<{ cookies: string }>> {
    return httpClient.get(`/api/user/${username}/cookies`)
  }

  // 配置相关API
  async getConfig(): Promise<ApiResponse<{ config: Config }>> {
    return httpClient.get('/api/config')
  }

  async updateConfig(config: any): Promise<ApiResponse<void>> {
    return httpClient.post('/api/config/update', config)
  }

  async testNotify(): Promise<ApiResponse<void>> {
    return httpClient.post('/api/notify/test')
  }

  async addNotifyField(name: string, value: string): Promise<ApiResponse<void>> {
    return httpClient.post('/api/notify/fields', { name, value })
  }

  async deleteNotifyField(name: string): Promise<ApiResponse<void>> {
    return httpClient.delete('/api/notify/fields', { name })
  }

  async updateAuth(data: { username: string, password: string, old_password: string }): Promise<ApiResponse<void>> {
    return httpClient.post('/api/auth/update', data)
  }

  // 其他API
  async checkVersion(source?: string): Promise<ApiResponse<any>> {
    return httpClient.get('/api/version/check', { source })
  }

  async getLocalSyncStatus(): Promise<ApiResponse<LocalSyncOverview>> {
    return httpClient.get('/api/local-sync/status')
  }

  async getLocalSyncTasks(): Promise<ApiResponse<{ tasks: LocalSyncTaskConfig[] }>> {
    return httpClient.get('/api/local-sync/tasks')
  }

  async saveLocalSyncTask(task: Partial<LocalSyncTaskConfig>): Promise<ApiResponse<{ task: LocalSyncTaskConfig; tasks: LocalSyncTaskConfig[] }>> {
    return httpClient.post('/api/local-sync/tasks/save', task)
  }

  async deleteLocalSyncTask(taskId: string): Promise<ApiResponse<{ tasks: LocalSyncTaskConfig[] }>> {
    return httpClient.post('/api/local-sync/tasks/delete', { task_id: taskId })
  }

  async runLocalSyncTask(taskId: string, dryRun = false): Promise<ApiResponse<any>> {
    return httpClient.post('/api/local-sync/tasks/run', {
      task_id: taskId,
      dry_run: dryRun,
    })
  }

  async getLocalSyncTaskLogs(taskId: string, lines = 200): Promise<ApiResponse<{ logs: string; log_file: string; task_name: string; running: boolean; message?: string }>> {
    return httpClient.get('/api/local-sync/tasks/logs', {
      task_id: taskId,
      lines,
    })
  }

  async getLocalSyncDirectories(params: { taskId?: string; remoteRoot?: string }): Promise<ApiResponse<{ directories: LocalSyncDirectoryOption[]; remote_root: string }>> {
    return httpClient.get('/api/local-sync/directories', {
      task_id: params.taskId,
      remote_root: params.remoteRoot,
    })
  }

  async startLocalSync(syncType: 'incremental' | 'full', dryRun = false, tasks: string[] = []): Promise<ApiResponse<any>> {
    return httpClient.post('/api/local-sync/start', {
      sync_type: syncType,
      dry_run: dryRun,
      tasks,
    })
  }

  async stopLocalSync(syncType: 'incremental' | 'full'): Promise<ApiResponse<any>> {
    return httpClient.post('/api/local-sync/stop', { sync_type: syncType })
  }

  async getLocalSyncLogs(syncType: 'incremental' | 'full', lines = 120): Promise<ApiResponse<{ logs: string; log_file: string }>> {
    return httpClient.get('/api/local-sync/logs', {
      sync_type: syncType,
      lines,
    })
  }

  async getTasksStatus(): Promise<ApiResponse<{ tasks: Task[] }>> {
    return httpClient.get('/api/tasks/status')
  }

  async getTaskStatus(taskRef: TaskQueryRef): Promise<ApiResponse<Task>> {
    const { taskId, params } = this.normalizeTaskQuery(taskRef)
    return httpClient.get(`/api/tasks/${taskId}/status`, params)
  }

  async getTaskLog(taskRef: TaskQueryRef): Promise<ApiResponse<any>> {
    const { taskId, params } = this.normalizeTaskQuery(taskRef)
    return httpClient.get(`/api/task/log/${taskId}`, params)
  }

  // 认证相关API
  async login(username: string, password: string): Promise<ApiResponse<any>> {
    return httpClient.post('/api/auth/login', { username, password })
  }

  async logout(): Promise<ApiResponse<any>> {
    return httpClient.post('/api/auth/logout')
  }

  async checkAuth(): Promise<ApiResponse<any>> {
    return httpClient.get('/api/auth/check')
  }
}

// 单例模式导出
export const apiService = new ApiService()
