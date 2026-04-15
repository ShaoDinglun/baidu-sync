<template>
  <div>
    <div class="stats-grid">
      <div class="stats-card">
        <div class="stats-label">本地同步任务总数</div>
        <div class="stats-value">{{ localSyncStats.total }}</div>
      </div>
      <div class="stats-card">
        <div class="stats-label">已启用</div>
        <div class="stats-value">{{ localSyncStats.enabled }}</div>
      </div>
      <div class="stats-card">
        <div class="stats-label">自动运行</div>
        <div class="stats-value">{{ localSyncStats.autoRun }}</div>
      </div>
      <div class="stats-card">
        <div class="stats-label">网盘根目录数</div>
        <div class="stats-value">{{ localSyncRootOptions.length }}</div>
      </div>
    </div>

    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="localSyncSearchQuery"
          placeholder="搜索本地同步任务..."
          clearable
          style="width: 300px"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>

        <el-select v-model="localSyncEnabledFilter" placeholder="启用状态" style="width: 140px">
          <el-option label="全部状态" value="all" />
          <el-option label="已启用" value="enabled" />
          <el-option label="已停用" value="disabled" />
        </el-select>
      </div>

      <div class="toolbar-right">
        <el-button @click="loadLocalSyncRootOptions(true)" :loading="localSyncRootLoading">
          刷新网盘根目录
        </el-button>
        <el-button @click="loadLocalSyncTasks(true)" :loading="localSyncTaskLoading">
          刷新任务
        </el-button>
      </div>
    </div>

    <div class="task-list-container">
      <el-table
        v-loading="localSyncTaskLoading"
        :data="filteredLocalSyncTasks"
        border
        empty-text="暂无本地同步任务"
        class="desktop-table"
      >
        <el-table-column prop="name" label="任务名称" min-width="180" />
        <el-table-column label="同步文件夹" min-width="220">
          <template #default="{ row }">
            <div class="task-name">{{ getLocalSyncRootName(row.remote_root) }}</div>
            <div class="task-subtitle">{{ row.remote_root }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="local_root" label="本地保存目录" min-width="240">
          <template #default="{ row }">
            <div class="save-dir text-truncate" :title="row.local_root">{{ row.local_root }}</div>
          </template>
        </el-table-column>
        <el-table-column label="增量范围" min-width="260">
          <template #default="{ row }">
            <div v-if="row.sync_mode === 'manual' && row.directory_filters?.length" class="advanced-tags">
              <el-tag v-for="dir in row.directory_filters" :key="`${row.task_id}-${dir}`" size="small" type="info">
                {{ dir }}
              </el-tag>
            </div>
            <span v-else class="text-muted">{{ getLocalSyncScopeSummary(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="覆盖策略" min-width="180">
          <template #default="{ row }">
            <span class="text-muted">{{ getLocalSyncOverwriteSummary(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
              {{ row.enabled ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="自动运行" min-width="250">
          <template #default="{ row }">
            <div class="task-name">{{ formatLocalSyncSchedule(row) }}</div>
            <div class="task-subtitle">{{ formatLocalSyncNextRun(row) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="最近状态" min-width="220">
          <template #default="{ row }">
            <el-tag :type="getLocalSyncRecentStatusType(row)" size="small">
              {{ getLocalSyncRecentStatusText(row) }}
            </el-tag>
            <div class="task-subtitle">{{ formatLocalSyncRecentStatusDetail(row) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" align="center">
          <template #default="{ row }">
            <el-button-group size="small">
              <el-button
                type="primary"
                @click="handleRunLocalSyncTask(row)"
                :loading="isRunningLocalSyncTask(row.task_id)"
              >
                <el-icon><VideoPlay /></el-icon>
              </el-button>
              <el-button @click="openLocalSyncTaskLogDialog(row)">
                <el-icon><Document /></el-icon>
              </el-button>
              <el-button @click="openEditLocalSyncTaskDialog(row)">
                <el-icon><Edit /></el-icon>
              </el-button>
              <el-button type="danger" @click="handleDeleteLocalSyncTask(row)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </el-button-group>
          </template>
        </el-table-column>
      </el-table>

      <div class="mobile-cards" v-loading="localSyncTaskLoading">
        <div v-if="filteredLocalSyncTasks.length === 0" class="empty-state">
          <div class="empty-text">暂无本地同步任务</div>
        </div>
        <div v-for="task in filteredLocalSyncTasks" :key="task.task_id" class="task-card">
          <div class="task-card-body">
            <div class="card-header">
              <div class="task-info">
                <h4 class="task-name">{{ task.name }}</h4>
                <div class="task-order">{{ getLocalSyncRootName(task.remote_root) }}</div>
              </div>
              <el-tag :type="task.enabled ? 'success' : 'info'" size="default">
                {{ task.enabled ? '启用' : '停用' }}
              </el-tag>
            </div>

            <div class="card-content">
              <div class="content-row">
                <span class="label">同步文件夹:</span>
                <span class="value">{{ task.remote_root }}</span>
              </div>
              <div class="content-row">
                <span class="label">本地目录:</span>
                <span class="value">{{ task.local_root }}</span>
              </div>
              <div class="content-row">
                <span class="label">增量范围:</span>
                <div v-if="task.sync_mode === 'manual' && task.directory_filters?.length" class="advanced-tags">
                  <el-tag v-for="dir in task.directory_filters" :key="`${task.task_id}-${dir}`" size="small" type="info">
                    {{ dir }}
                  </el-tag>
                </div>
                <span v-else class="value">{{ getLocalSyncScopeSummary(task) }}</span>
              </div>
              <div class="content-row">
                <span class="label">覆盖策略:</span>
                <span class="value">{{ getLocalSyncOverwriteSummary(task) }}</span>
              </div>
              <div class="content-row">
                <span class="label">自动运行:</span>
                <span class="value">{{ formatLocalSyncSchedule(task) }}</span>
              </div>
              <div class="content-row">
                <span class="label">下次执行:</span>
                <span class="value">{{ formatLocalSyncNextRun(task) }}</span>
              </div>
              <div class="content-row">
                <span class="label">最近状态:</span>
                <div class="value-group">
                  <span class="value">{{ getLocalSyncRecentStatusText(task) }}</span>
                  <span class="task-subtitle">{{ formatLocalSyncRecentStatusDetail(task) }}</span>
                </div>
              </div>
            </div>
          </div>

          <div class="card-actions">
            <button
              class="action-btn action-btn-primary"
              @click="handleRunLocalSyncTask(task)"
              title="立即执行"
              :disabled="isRunningLocalSyncTask(task.task_id)"
            >
              <el-icon><VideoPlay /></el-icon>
            </button>
            <button class="action-btn" @click="openLocalSyncTaskLogDialog(task)" title="查看日志">
              <el-icon><Document /></el-icon>
            </button>
            <button class="action-btn" @click="openEditLocalSyncTaskDialog(task)" title="编辑任务">
              <el-icon><Edit /></el-icon>
            </button>
            <button class="action-btn action-btn-danger" @click="handleDeleteLocalSyncTask(task)" title="删除任务">
              <el-icon><Delete /></el-icon>
            </button>
          </div>
        </div>
      </div>
    </div>

    <el-dialog
      v-model="localSyncTaskDialogVisible"
      :title="editingLocalSyncTaskId ? '编辑本地同步任务' : '新增本地同步任务'"
      width="680px"
    >
      <el-form label-position="top">
        <el-form-item label="任务名称">
          <el-input v-model="localSyncTaskForm.name" placeholder="例如：A股数据本地同步" />
        </el-form-item>

        <el-form-item label="网盘根目录文件夹">
          <div class="dialog-toolbar">
            <el-button @click="loadLocalSyncRootOptions(true)" :loading="localSyncRootLoading">
              获取网盘根目录
            </el-button>
            <span class="task-page-tip">从网盘根目录选择要同步的文件夹。</span>
          </div>
          <el-select
            v-model="localSyncTaskForm.remote_root"
            filterable
            placeholder="选择网盘根目录下的文件夹"
            style="width: 100%;"
            @change="handleLocalSyncRootChange"
          >
            <el-option
              v-for="option in localSyncRootOptions"
              :key="option.path"
              :label="option.label"
              :value="option.path"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="本地保存目录">
          <el-input v-model="localSyncTaskForm.local_root" placeholder="例如：/app/data/example-task（Docker）或 /data/example-task" />
        </el-form-item>

        <el-form-item label="任务启用状态">
          <el-switch v-model="localSyncTaskForm.enabled" />
        </el-form-item>

        <el-form-item label="自动运行">
          <el-switch v-model="localSyncTaskForm.auto_run_enabled" />
          <div class="task-page-tip">启用后按 crontab 表达式自动执行该任务。</div>
        </el-form-item>

        <el-form-item label="crontab 表达式">
          <el-input
            v-model="localSyncTaskForm.cron"
            placeholder="例如：*/30 * * * * 或 0 2 * * *"
            :disabled="!localSyncTaskForm.auto_run_enabled"
          />
          <div class="task-page-tip">标准 5 段 crontab：分 时 日 月 周。</div>
        </el-form-item>

        <el-form-item label="增量范围">
          <el-radio-group v-model="localSyncTaskForm.sync_mode">
            <el-radio label="all">整个文件夹</el-radio>
            <el-radio label="manual">手动选择子目录</el-radio>
            <el-radio label="recent_days">最近 n 天</el-radio>
            <el-radio label="recent_months">最近 n 月</el-radio>
          </el-radio-group>
          <div class="task-page-tip">最近 n 天或 n 月会递归识别常见日期目录名；如果完全识别不到日期目录，会回退为整个文件夹同步。</div>
        </el-form-item>

        <el-form-item v-if="isRecentSyncMode" :label="localSyncTaskForm.sync_mode === 'recent_days' ? '最近天数' : '最近月数'">
          <el-input-number v-model="localSyncTaskForm.recent_value" :min="1" :max="3650" style="width: 220px" />
        </el-form-item>

        <el-form-item label="覆盖策略">
          <el-radio-group v-model="localSyncTaskForm.overwrite_policy">
            <el-radio label="never">不覆盖本地已有文件</el-radio>
            <el-radio label="if_newer">远端较新时覆盖</el-radio>
            <el-radio label="always">始终覆盖</el-radio>
          </el-radio-group>
          <div class="task-page-tip">仅影响增量同步时已存在文件的处理方式。</div>
        </el-form-item>

        <el-form-item v-if="localSyncTaskForm.sync_mode === 'manual'" label="指定更新子目录">
          <div class="dialog-toolbar">
            <el-button @click="loadLocalSyncDirectoryOptions(true)" :loading="localSyncDirectoryLoading">
              获取子目录
            </el-button>
            <span class="task-page-tip">不选时默认同步整个文件夹。</span>
          </div>
          <el-select
            v-model="localSyncTaskForm.directory_filters"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            placeholder="选择需要更新的子目录"
            style="width: 100%;"
          >
            <el-option
              v-for="option in localSyncDirectoryOptions"
              :key="option.path"
              :label="option.label"
              :value="option.path"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="localSyncTaskDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSaveLocalSyncTask" :loading="localSyncTaskSaving">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="localSyncTaskLogDialogVisible"
      :title="localSyncTaskLogDialogTitle"
      width="900px"
    >
      <div class="dialog-toolbar">
        <el-button @click="refreshLocalSyncTaskLogs" :loading="localSyncTaskLogLoading">刷新日志</el-button>
        <span class="task-page-tip">{{ localSyncTaskLogFile || '暂未找到日志文件' }}</span>
      </div>
      <div v-if="localSyncTaskLogMessage" class="task-page-tip">{{ localSyncTaskLogMessage }}</div>
      <pre class="task-log-panel">{{ localSyncTaskLogContent || '暂无日志信息' }}</pre>

      <template #footer>
        <el-button @click="localSyncTaskLogDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Edit, Delete, VideoPlay, Document } from '@element-plus/icons-vue'
import { apiService } from '@/services'
import type { LocalSyncDirectoryOption, LocalSyncTaskConfig } from '@/types'

const localSyncTaskLoading = ref(false)
const localSyncTaskDialogVisible = ref(false)
const localSyncTaskLogDialogVisible = ref(false)
const localSyncTaskSaving = ref(false)
const localSyncTaskLogLoading = ref(false)
const localSyncSearchQuery = ref('')
const localSyncEnabledFilter = ref<'all' | 'enabled' | 'disabled'>('all')
const localSyncRootLoading = ref(false)
const localSyncDirectoryLoading = ref(false)
const localSyncTaskConfigs = ref<LocalSyncTaskConfig[]>([])
const runningLocalSyncTaskIds = ref<string[]>([])
const localSyncRootOptions = ref<LocalSyncDirectoryOption[]>([])
const localSyncDirectoryOptions = ref<LocalSyncDirectoryOption[]>([])
const editingLocalSyncTaskId = ref('')
const activeLocalSyncLogTask = ref<LocalSyncTaskConfig | null>(null)
const localSyncTaskLogContent = ref('')
const localSyncTaskLogFile = ref('')
const localSyncTaskLogMessage = ref('')
const localSyncTaskForm = reactive<LocalSyncTaskConfig>({
  task_id: '',
  name: '',
  enabled: true,
  auto_run_enabled: false,
  cron: '',
  remote_root: '',
  local_root: '',
  directory_filters: [],
  sync_mode: 'all',
  recent_value: 3,
  overwrite_policy: 'if_newer',
})

const localSyncStats = computed(() => ({
  total: localSyncTaskConfigs.value.length,
  enabled: localSyncTaskConfigs.value.filter(task => task.enabled).length,
  autoRun: localSyncTaskConfigs.value.filter(task => task.enabled && task.auto_run_enabled && task.cron.trim()).length,
}))

const isRecentSyncMode = computed(() => {
  return localSyncTaskForm.sync_mode === 'recent_days' || localSyncTaskForm.sync_mode === 'recent_months'
})

const filteredLocalSyncTasks = computed(() => {
  let result = localSyncTaskConfigs.value

  if (localSyncSearchQuery.value) {
    const query = localSyncSearchQuery.value.toLowerCase()
    result = result.filter(task => {
      const values = [
        task.name,
        task.remote_root,
        task.local_root,
        task.cron,
        formatLocalSyncSchedule(task),
        getLocalSyncScopeSummary(task),
        ...(task.directory_filters || []),
      ]
      return values.some(value => value.toLowerCase().includes(query))
    })
  }

  if (localSyncEnabledFilter.value === 'enabled') {
    result = result.filter(task => task.enabled)
  }
  if (localSyncEnabledFilter.value === 'disabled') {
    result = result.filter(task => !task.enabled)
  }

  return result.slice().sort((left, right) => left.name.localeCompare(right.name, 'zh-CN'))
})

const resetLocalSyncTaskForm = () => {
  editingLocalSyncTaskId.value = ''
  localSyncTaskForm.task_id = ''
  localSyncTaskForm.name = ''
  localSyncTaskForm.enabled = true
  localSyncTaskForm.auto_run_enabled = false
  localSyncTaskForm.cron = ''
  localSyncTaskForm.remote_root = ''
  localSyncTaskForm.local_root = ''
  localSyncTaskForm.directory_filters = []
  localSyncTaskForm.sync_mode = 'all'
  localSyncTaskForm.recent_value = 3
  localSyncTaskForm.overwrite_policy = 'if_newer'
  localSyncDirectoryOptions.value = []
}

const getLocalSyncScopeSummary = (task: LocalSyncTaskConfig) => {
  if (task.sync_mode === 'manual') {
    return task.directory_filters?.length ? `手动选择 ${task.directory_filters.length} 个子目录` : '手动选择子目录'
  }
  if (task.sync_mode === 'recent_days') {
    return `最近 ${task.recent_value || 0} 天（自动识别日期目录）`
  }
  if (task.sync_mode === 'recent_months') {
    return `最近 ${task.recent_value || 0} 月（自动识别日期目录）`
  }
  return '整个文件夹'
}

const getLocalSyncOverwriteSummary = (task: LocalSyncTaskConfig) => {
  if (task.overwrite_policy === 'never') {
    return '不覆盖本地已有文件'
  }
  if (task.overwrite_policy === 'always') {
    return '始终覆盖本地已有文件'
  }
  return '远端较新时覆盖'
}

const getLocalSyncRootName = (remoteRoot: string) => {
  const normalized = remoteRoot.replace(/\\/g, '/').replace(/\/+$/, '')
  const segments = normalized.split('/').filter(Boolean)
  return segments[segments.length - 1] || '/'
}

const isRunningLocalSyncTask = (taskId: string) => {
  return runningLocalSyncTaskIds.value.includes(taskId)
}

const formatLocalSyncSchedule = (task: LocalSyncTaskConfig) => {
  if (!task.enabled) {
    return '任务已停用'
  }
  if (!task.auto_run_enabled) {
    return '未启用自动运行'
  }
  if (!task.cron.trim()) {
    return '未配置 crontab'
  }
  return task.cron.trim()
}

const formatLocalSyncNextRun = (task: LocalSyncTaskConfig) => {
  if (!task.enabled) {
    return '任务停用时不会调度'
  }
  if (!task.auto_run_enabled || !task.cron.trim()) {
    return '未安排自动执行'
  }
  if (!task.next_run_at) {
    return '等待调度器加载'
  }

  const date = new Date(task.next_run_at)
  if (Number.isNaN(date.getTime())) {
    return '下次执行时间解析失败'
  }

  return `下次执行：${date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })}`
}

const getLocalSyncRecentStatusText = (task: LocalSyncTaskConfig) => {
  const status = task.recent_run_status
  if (status === 'running') {
    return '执行中'
  }
  if (status === 'success') {
    return '成功'
  }
  if (status === 'failed') {
    return '失败'
  }
  if (status === 'skipped') {
    return '无更新'
  }
  if (status === 'unknown') {
    return '待确认'
  }
  return '未执行'
}

const getLocalSyncRecentStatusType = (task: LocalSyncTaskConfig) => {
  const status = task.recent_run_status
  if (status === 'running') {
    return 'warning'
  }
  if (status === 'success') {
    return 'success'
  }
  if (status === 'failed') {
    return 'danger'
  }
  if (status === 'skipped') {
    return 'info'
  }
  return 'info'
}

const formatLocalSyncRecentStatusDetail = (task: LocalSyncTaskConfig) => {
  const parts = []
  if (task.recent_run_message) {
    parts.push(task.recent_run_message)
  }

  if (task.recent_run_at) {
    const date = new Date(task.recent_run_at)
    if (!Number.isNaN(date.getTime())) {
      parts.push(date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      }))
    }
  }

  return parts.join(' | ') || '暂未记录最近一次执行结果'
}

const localSyncTaskLogDialogTitle = computed(() => {
  return activeLocalSyncLogTask.value ? `任务日志 - ${activeLocalSyncLogTask.value.name}` : '任务日志'
})

const loadLocalSyncTasks = async (showMessage = false) => {
  localSyncTaskLoading.value = true

  try {
    const response = await apiService.getLocalSyncTasks()
    if (!response.success) {
      throw new Error(response.message || '获取本地同步任务失败')
    }

    localSyncTaskConfigs.value = response.tasks || []
    if (showMessage) {
      ElMessage.success('本地同步任务已刷新')
    }
  } catch (error) {
    ElMessage.error(`获取本地同步任务失败：${error}`)
  } finally {
    localSyncTaskLoading.value = false
  }
}

const loadLocalSyncRootOptions = async (showMessage = false) => {
  localSyncRootLoading.value = true

  try {
    const response = await apiService.getLocalSyncDirectories({ remoteRoot: '/' })
    if (!response.success) {
      throw new Error(response.message || '获取网盘根目录失败')
    }

    localSyncRootOptions.value = (response.directories || []).map((option: LocalSyncDirectoryOption) => ({
      path: `/${option.path.replace(/^\/+/, '')}`,
      label: option.label,
    }))

    if (showMessage) {
      ElMessage.success('网盘根目录已刷新')
    }
  } catch (error) {
    ElMessage.error(`获取网盘根目录失败：${error}`)
  } finally {
    localSyncRootLoading.value = false
  }
}

const loadLocalSyncDirectoryOptions = async (showMessage = false) => {
  if (!localSyncTaskForm.remote_root.trim()) {
    ElMessage.warning('请先选择网盘根目录文件夹')
    return
  }

  localSyncDirectoryLoading.value = true

  try {
    const response = await apiService.getLocalSyncDirectories({
      taskId: editingLocalSyncTaskId.value || undefined,
      remoteRoot: localSyncTaskForm.remote_root,
    })
    if (!response.success) {
      throw new Error(response.message || '获取子目录失败')
    }

    localSyncDirectoryOptions.value = response.directories || []
    localSyncTaskForm.directory_filters = localSyncTaskForm.directory_filters.filter(path =>
      localSyncDirectoryOptions.value.some(option => option.path === path)
    )
    if (showMessage) {
      ElMessage.success('子目录已刷新')
    }
  } catch (error) {
    ElMessage.error(`获取子目录失败：${error}`)
  } finally {
    localSyncDirectoryLoading.value = false
  }
}

const handleLocalSyncRootChange = async () => {
  localSyncTaskForm.directory_filters = []
  localSyncDirectoryOptions.value = []
  if (localSyncTaskForm.remote_root && localSyncTaskForm.sync_mode === 'manual') {
    await loadLocalSyncDirectoryOptions()
  }
}

const openCreateDialog = async () => {
  resetLocalSyncTaskForm()
  localSyncTaskDialogVisible.value = true
  if (localSyncRootOptions.value.length === 0) {
    await loadLocalSyncRootOptions()
  }
}

const openEditLocalSyncTaskDialog = async (task: LocalSyncTaskConfig) => {
  editingLocalSyncTaskId.value = task.task_id
  localSyncTaskForm.task_id = task.task_id
  localSyncTaskForm.name = task.name
  localSyncTaskForm.enabled = task.enabled
  localSyncTaskForm.auto_run_enabled = task.auto_run_enabled
  localSyncTaskForm.cron = task.cron || ''
  localSyncTaskForm.remote_root = task.remote_root
  localSyncTaskForm.local_root = task.local_root
  localSyncTaskForm.directory_filters = [...task.directory_filters]
  localSyncTaskForm.sync_mode = task.sync_mode || (task.directory_filters?.length ? 'manual' : 'all')
  localSyncTaskForm.recent_value = task.recent_value || 3
  localSyncTaskForm.overwrite_policy = task.overwrite_policy || 'if_newer'
  localSyncTaskDialogVisible.value = true
  if (localSyncRootOptions.value.length === 0) {
    await loadLocalSyncRootOptions()
  }
  if (localSyncTaskForm.sync_mode === 'manual') {
    await loadLocalSyncDirectoryOptions()
  }
}

const handleSaveLocalSyncTask = async () => {
  if (!localSyncTaskForm.name.trim()) {
    ElMessage.warning('请输入任务名称')
    return
  }
  if (!localSyncTaskForm.remote_root.trim()) {
    ElMessage.warning('请选择网盘根目录文件夹')
    return
  }
  if (!localSyncTaskForm.local_root.trim()) {
    ElMessage.warning('请输入本地保存目录')
    return
  }
  if (localSyncTaskForm.auto_run_enabled && !localSyncTaskForm.cron.trim()) {
    ElMessage.warning('启用自动运行时，请填写 crontab 表达式')
    return
  }
  if (localSyncTaskForm.sync_mode === 'manual' && localSyncTaskForm.directory_filters.length === 0) {
    ElMessage.warning('请至少选择一个子目录')
    return
  }
  if (isRecentSyncMode.value && (!Number.isInteger(localSyncTaskForm.recent_value) || localSyncTaskForm.recent_value <= 0)) {
    ElMessage.warning('最近天数或月数必须大于 0')
    return
  }

  localSyncTaskSaving.value = true

  try {
    const response = await apiService.saveLocalSyncTask({
      task_id: localSyncTaskForm.task_id || undefined,
      name: localSyncTaskForm.name.trim(),
      enabled: localSyncTaskForm.enabled,
      auto_run_enabled: localSyncTaskForm.auto_run_enabled,
      cron: localSyncTaskForm.auto_run_enabled ? localSyncTaskForm.cron.trim() : localSyncTaskForm.cron.trim(),
      remote_root: localSyncTaskForm.remote_root,
      local_root: localSyncTaskForm.local_root.trim(),
      directory_filters: localSyncTaskForm.sync_mode === 'manual' ? [...localSyncTaskForm.directory_filters] : [],
      sync_mode: localSyncTaskForm.sync_mode,
      recent_value: isRecentSyncMode.value ? localSyncTaskForm.recent_value : 0,
      overwrite_policy: localSyncTaskForm.overwrite_policy,
    })
    if (!response.success) {
      throw new Error(response.message || '保存失败')
    }

    ElMessage.success(response.message || '本地同步任务已保存')
    localSyncTaskDialogVisible.value = false
    await loadLocalSyncTasks()
  } catch (error) {
    ElMessage.error(`保存失败：${error}`)
  } finally {
    localSyncTaskSaving.value = false
  }
}

const handleRunLocalSyncTask = async (task: LocalSyncTaskConfig) => {
  if (isRunningLocalSyncTask(task.task_id)) {
    return
  }

  runningLocalSyncTaskIds.value = [...runningLocalSyncTaskIds.value, task.task_id]

  try {
    const response = await apiService.runLocalSyncTask(task.task_id)
    if (!response.success) {
      throw new Error(response.message || '执行失败')
    }

    ElMessage.success(response.message || `已开始执行任务：${task.name}`)
  } catch (error) {
    ElMessage.error(`执行失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    runningLocalSyncTaskIds.value = runningLocalSyncTaskIds.value.filter(item => item !== task.task_id)
  }
}

const loadLocalSyncTaskLogs = async (task: LocalSyncTaskConfig) => {
  localSyncTaskLogLoading.value = true

  try {
    const response = await apiService.getLocalSyncTaskLogs(task.task_id)
    if (!response.success) {
      throw new Error(response.message || '获取日志失败')
    }

    localSyncTaskLogContent.value = response.logs || ''
    localSyncTaskLogFile.value = response.log_file || ''
    localSyncTaskLogMessage.value = response.message || ''
  } catch (error) {
    localSyncTaskLogContent.value = ''
    localSyncTaskLogFile.value = ''
    localSyncTaskLogMessage.value = error instanceof Error ? error.message : String(error)
    ElMessage.error(`获取日志失败：${localSyncTaskLogMessage.value}`)
  } finally {
    localSyncTaskLogLoading.value = false
  }
}

const openLocalSyncTaskLogDialog = async (task: LocalSyncTaskConfig) => {
  activeLocalSyncLogTask.value = task
  localSyncTaskLogDialogVisible.value = true
  await loadLocalSyncTaskLogs(task)
}

const refreshLocalSyncTaskLogs = async () => {
  if (!activeLocalSyncLogTask.value) {
    return
  }
  await loadLocalSyncTaskLogs(activeLocalSyncLogTask.value)
}

const handleDeleteLocalSyncTask = async (task: LocalSyncTaskConfig) => {
  try {
    await ElMessageBox.confirm(`确认删除本地同步任务“${task.name}”吗？`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }

  try {
    const response = await apiService.deleteLocalSyncTask(task.task_id)
    if (!response.success) {
      throw new Error(response.message || '删除失败')
    }

    ElMessage.success(response.message || '本地同步任务已删除')
    await loadLocalSyncTasks()
  } catch (error) {
    ElMessage.error(`删除失败：${error}`)
  }
}

onMounted(async () => {
  await loadLocalSyncTasks()
  await loadLocalSyncRootOptions()
})

watch(
  () => localSyncTaskForm.sync_mode,
  async (mode, previousMode) => {
    if (mode !== 'manual') {
      localSyncTaskForm.directory_filters = []
      localSyncDirectoryOptions.value = []
    }

    if (mode === 'manual' && previousMode !== 'manual' && localSyncTaskForm.remote_root) {
      await loadLocalSyncDirectoryOptions()
    }

    if (mode === 'all') {
      localSyncTaskForm.recent_value = 3
    }
  }
)

defineExpose({
  openCreateDialog,
})
</script>

<style scoped>
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}

.stats-card {
  padding: 18px 20px;
  background: linear-gradient(180deg, #ffffff 0%, #f7f9fc 100%);
  border: 1px solid #e4e7ed;
  border-radius: 10px;
  box-shadow: 0 2px 6px rgba(15, 23, 42, 0.04);
}

.stats-label {
  font-size: 13px;
  color: #909399;
  margin-bottom: 10px;
}

.stats-value {
  font-size: 28px;
  line-height: 1;
  font-weight: 700;
  color: #303133;
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding: 16px;
  background: white;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

.toolbar-left {
  display: flex;
  gap: 16px;
  align-items: center;
}

.toolbar-right {
  display: flex;
  gap: 16px;
  align-items: center;
}

.task-list-container {
  background: white;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  overflow: hidden;
}

.desktop-table {
  width: 100%;
}

.task-name {
  font-weight: 500;
  color: #333;
}

.task-subtitle {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}

.save-dir {
  color: #666;
}

.text-muted {
  color: #909399;
}

.task-page-tip {
  margin-top: 6px;
  font-size: 13px;
  color: #909399;
}

.dialog-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

.advanced-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.mobile-cards {
  display: none;
}

.task-card {
  background: white;
  border-radius: 12px;
  border: 1px solid #e4e7ed;
  padding: 14px;
  margin-bottom: 10px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  border-left: 3px solid transparent;
  transition: all 0.2s ease;
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.task-card:hover {
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  border-left-color: #409eff;
}

.task-card-body {
  flex: 1;
  min-width: 0;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 12px;
}

.task-info {
  flex: 1;
  min-width: 0;
}

.task-order {
  font-size: 12px;
  color: #909399;
  font-weight: normal;
}

.card-content {
  margin-bottom: 16px;
}

.content-row {
  display: flex;
  align-items: flex-start;
  margin-bottom: 8px;
  font-size: 14px;
}

.content-row:last-child {
  margin-bottom: 0;
}

.content-row .label {
  color: #606266;
  font-weight: 500;
  min-width: 80px;
  flex-shrink: 0;
}

.content-row .value {
  color: #303133;
  word-break: break-all;
  flex: 1;
}

.card-actions {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
  justify-content: flex-start;
  min-width: 44px;
  width: 44px;
  flex-shrink: 0;
}

.action-btn {
  width: 36px;
  height: 36px;
  padding: 8px;
  border-radius: 8px;
  font-size: 16px;
  transition: all 0.2s ease;
  touch-action: manipulation;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  color: #606266;
  margin: 0;
  cursor: pointer;
}

.action-btn:hover:not(:disabled) {
  transform: scale(0.95);
  background: #409eff;
  color: white;
  border-color: #409eff;
}

.action-btn-primary {
  color: #409eff;
  border-color: rgba(64, 158, 255, 0.3);
}

.action-btn-danger {
  color: #f56c6c;
  border-color: rgba(245, 108, 108, 0.3);
}

.action-btn-danger:hover:not(:disabled) {
  background: #f56c6c;
  border-color: #f56c6c;
  color: white;
}

.empty-state {
  text-align: center;
  padding: 40px 20px;
  color: #909399;
}

.empty-text {
  font-size: 16px;
}

.task-log-panel {
  min-height: 320px;
  max-height: 60vh;
  overflow: auto;
  margin: 0;
  padding: 16px;
  border-radius: 8px;
  background: #0f172a;
  color: #dbeafe;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

@media (max-width: 992px) {
  .stats-grid {
    grid-template-columns: 1fr 1fr;
  }
}

@media (max-width: 768px) {
  .stats-grid {
    grid-template-columns: 1fr;
  }

  .toolbar,
  .toolbar-left,
  .toolbar-right {
    flex-direction: column;
    align-items: stretch;
  }

  .desktop-table {
    display: none;
  }

  .mobile-cards {
    display: block;
    padding: 12px;
  }
}
</style>