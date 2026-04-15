// API 相关类型定义

export interface ApiResponse<T = any> {
  success: boolean
  message?: string
  data?: T
  [key: string]: any
}

export type SubscriptionSyncMode = 'full' | 'incremental'
export type SubscriptionSyncScopeType = 'all' | 'recent_months' | 'month_range'
export type SubscriptionOverwritePolicy = 'never' | 'window_only' | 'always'
export type SubscriptionDateDirMode = 'auto' | 'custom'

// 任务相关类型
export interface Task {
  order: number
  task_uid?: string
  name?: string
  url: string
  save_dir: string
  pwd?: string
  simple_transfer?: boolean
  monthly_precise_sync?: boolean
  sync_mode?: SubscriptionSyncMode
  sync_scope_type?: SubscriptionSyncScopeType
  recent_months?: number
  scope_start_month?: string
  scope_end_month?: string
  overwrite_policy?: SubscriptionOverwritePolicy
  date_dir_mode?: SubscriptionDateDirMode
  date_dir_patterns?: string[]
  status: 'normal' | 'error' | 'running' | 'success' | 'failed' | 'completed' | 'skipped' | 'cancelled'
  message?: string
  progress?: number
  category?: string
  cron?: string
  regex_pattern?: string
  regex_replace?: string
  share_info?: ShareInfo
  created_at?: string
  updated_at?: string
  last_execute_time?: number
  next_run_at?: string | null
  transferred_files?: string[]
}

export interface ShareInfo {
  url: string
  password?: string
  expires_at?: string
}

export interface CreateTaskRequest {
  url: string
  save_dir: string
  pwd?: string
  name?: string
  sync_mode?: SubscriptionSyncMode
  sync_scope_type?: SubscriptionSyncScopeType
  recent_months?: number
  scope_start_month?: string
  scope_end_month?: string
  overwrite_policy?: SubscriptionOverwritePolicy
  date_dir_mode?: SubscriptionDateDirMode
  date_dir_patterns?: string[]
  category?: string
  cron?: string
  regex_pattern?: string
  regex_replace?: string
}

export interface UpdateTaskRequest {
  url?: string
  save_dir?: string
  pwd?: string
  name?: string
  sync_mode?: SubscriptionSyncMode
  sync_scope_type?: SubscriptionSyncScopeType
  recent_months?: number
  scope_start_month?: string
  scope_end_month?: string
  overwrite_policy?: SubscriptionOverwritePolicy
  date_dir_mode?: SubscriptionDateDirMode
  date_dir_patterns?: string[]
  category?: string
  cron?: string
  regex_pattern?: string
  regex_replace?: string
}

export interface TaskOperation {
  type: 'execute' | 'edit' | 'delete' | 'share'
  taskId: number
}

export interface BatchOperation {
  type: 'execute' | 'delete'
  taskIds: number[]
}

// 用户相关类型
export interface User {
  username: string
  is_current: boolean
  quota?: UserQuota
  cookies_valid?: boolean
  last_active?: string
}

export interface UserQuota {
  used: number
  total: number
  used_formatted: string
  total_formatted: string
  percent: number
}

export interface CreateUserRequest {
  username: string
  cookies: string
}

export interface UpdateUserRequest {
  original_username: string
  username: string
  cookies: string
}

// 配置相关类型
export interface Config {
  notifications: NotificationConfig
  scheduling: SchedulingConfig
  sharing: SharingConfig
  general: GeneralConfig
}

export interface NotificationConfig {
  enabled: boolean
  webhook_url?: string
  custom_fields?: Record<string, string>
}

export interface SchedulingConfig {
  enabled: boolean
  interval: number
  start_time?: string
  end_time?: string
}

export interface SharingConfig {
  enabled: boolean
  default_password: boolean
  default_period: number
}

export interface GeneralConfig {
  max_retries: number
  timeout: number
  concurrent_limit: number
}

// 日志相关类型
export interface LogEntry {
  timestamp: string
  level: 'INFO' | 'WARNING' | 'ERROR' | 'DEBUG'
  message: string
  module?: string
}

// 版本相关类型
export interface VersionInfo {
  current: string
  latest: string
  has_update: boolean
  update_url?: string
  release_notes?: string
}

export interface LocalSyncRuntimeStatus {
  sync_type: 'incremental' | 'full'
  running: boolean
  status: string
  pid?: number | null
  log_file?: string
  started_at?: string | null
  finished_at?: string | null
  message?: string
  dry_run?: boolean
  tasks?: string[]
  summary_text?: string
}

export interface LocalSyncTaskConfig {
  task_id: string
  name: string
  enabled: boolean
  auto_run_enabled: boolean
  cron: string
  remote_root: string
  local_root: string
  directory_filters: string[]
  sync_mode: 'all' | 'manual' | 'recent_days' | 'recent_months'
  recent_value: number
  overwrite_policy: 'never' | 'if_newer' | 'always'
  next_run_at?: string | null
  recent_run_status?: 'running' | 'success' | 'failed' | 'skipped' | 'unknown' | null
  recent_run_message?: string
  recent_run_at?: string | null
}

export interface LocalSyncDirectoryOption {
  path: string
  label: string
}

export interface LocalSyncOverview {
  tasks: string[]
  incremental: LocalSyncRuntimeStatus
  full: LocalSyncRuntimeStatus
}
